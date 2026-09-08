#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tushare 当日行情**到达时间**探针  ——  只读, 每次 1-2 次调用
============================================================
老板决定⑤: "A 股更新改到 A 股收盘的时候拉当天的, 如果 Tushare 支持的话"。
把 stock-a.timer 从 14:00 CEST (北京 20:00, 收盘后 5 小时) 往前挪, 唯一的风险是
**提前跑 = 拿不到当天数据**: `leftside_core.pricestore._update_daily_by_date` 的未就绪守卫
(当日行数 < 在市股 90%) 会就地停下并沿用昨日库, 而 run_a.sh 那一行末尾是
`|| echo "... non-fatal"`, 于是流水线照跑, **把昨日行情当今天发布**。

所以先量, 再挪。本探针每 5 分钟记一行"此刻源里有多少行", 跑几个交易日, 用真实到达时间
反推新的定时点与等待上限 (run_a.sh v2 的 sleep 循环次数 N)。

铁律遵循:
  · token 只经 `ashare/tushare_client.py` 从 `data/secrets.json` 读, **永不打印/入库/进 git**
    (本脚本连读都不读 secrets, 只引用那个模块)
  · **只读**: 不写任何数据库; 价格库只以 `mode=ro` 打开取一个 COUNT (不参与写锁)
  · **轻**: daily / adj_factor 各一次调用, 且只要 `ts_code` 一列 (行数才是我们要的东西,
    价格一律不取) —— 全字段一次约 5,400 行 × 11 列, 只取一列后 payload 小一个数量级,
    这也是它敢每 5 分钟跑一次、MemoryMax 只给 300M 的原因
  · 交易日历一天只问一次 (答案缓存在 `<日志>.cal.json`), 所以稳态是每次 **2 次调用**,
    每个交易日的第一次是 3 次
  · **退出码恒为 0** (除非命令行参数写错): 探针失败不该让 `systemctl --failed` 变红,
    也不该吵醒值班 —— 失败写进日志行的 `err=` 字段, 我们本来就是靠读日志用它的

行数口径 (两个数, 别混):
  rows   = daily 端点返回的**原始行数** (含北交所 / B 股)
  a_rows = 过 `ashare.market.keep_a_code` 之后的 A 股行数 —— **守卫比的就是这个数**
           (`_update_daily_by_date` 里 `len(raw) < listed_n * 0.9`)。
           唯一的差是本探针没跑 `_rows_from_daily` 的价格合法性过滤 (h<l / 价≤0 / 量<0),
           那几条在真实数据上是个位数, 判"到没到"绰绰有余。
  listed = 价格库 `universe` 里 status='L' 的只数 (守卫的分母, 零联网)

用法:
    python tools/tushare_ready_probe.py                      # 探今天, 追加到默认日志
    python tools/tushare_ready_probe.py --log /srv/stock/data/tushare_ready_probe.log
    python tools/tushare_ready_probe.py --date 20260908 --no-log   # 只看不写 (本机手工探)
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _utf8_stdio() -> None:
    """把 stdout/stderr 换成 UTF-8 (systemd 的 journal 环境是 POSIX locale, 中文会炸)。

    **只能在 main() 里调, 不能放模块顶层**: 顶层重绑 `sys.stdout` 会在本模块被 import 时
    (单测按路径 exec_module; 硬期限的 spawn 子进程按路径重导入) 把外面那层 stdout 的 buffer
    包进一个新 TextIOWrapper, 它被回收时会关掉底层 buffer —— 实测让 pytest 的捕获整批报
    "ValueError: I/O operation on closed file" (31 个 error)。
    """
    for name in ("stdout", "stderr"):
        try:
            setattr(sys, name, io.TextIOWrapper(getattr(sys, name).buffer,
                                                encoding="utf-8", errors="replace"))
        except Exception:                           # noqa: BLE001  (被重定向到管道/已被替换)
            pass

BJ = dt.timezone(dt.timedelta(hours=8))
DEFAULT_LOG = os.path.join(ROOT, "data", "tushare_ready_probe.log")
HEADER = ("# tushare_ready_probe · Tushare 当日行情到达时间探针 (只读)\n"
          "# 字段: 北京时间 | 本地时间 | trade_date | open=开市? | rows=daily原始行 | "
          "a_rows=过A股过滤后(守卫比的就是它) | adj=adj_factor行 | listed=在市股(守卫分母) | "
          "a/listed | adj/listed | 耗时 | 备注\n"
          "# 守卫阈值: a_rows >= listed × 90% 才算就绪 "
          "(leftside_core.pricestore._update_daily_by_date)\n")


def beijing_date() -> str:
    return dt.datetime.now(BJ).date().strftime("%Y%m%d")


def listed_count() -> tuple:
    """价格库在市股数 (status='L') -> (n, err)。**只读打开**, 不碰写锁, 拿不到就返回 (None, 原因)。"""
    import sqlite3
    try:
        from ashare import pricestore as ps          # 真模块入口: 顺带把 market 注册好
        path = ps._db_path()
    except Exception as e:                           # noqa: BLE001
        return None, f"db_path:{type(e).__name__}"
    if not os.path.exists(path):
        return None, "db_missing"
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM universe WHERE status='L'").fetchone()[0], ""
        finally:
            conn.close()
    except Exception as e:                           # noqa: BLE001
        return None, f"db:{type(e).__name__}"


def is_open_day(date: str, cache_path: str) -> tuple:
    """今天开不开市 -> ('Y'/'N'/'?', 用了几次调用)。答案按 trade_date 缓存, 一天只问一次。

    为什么值得多花这一次调用: 日志里一整天的 `rows=0` 有两种截然不同的含义 —— 休市 (正常)
    与镜像挂了 (要查)。事后翻日志的人分不出来, 就等于这三天白跑。
    """
    cache = {}
    try:
        with open(cache_path, encoding="utf-8") as f:
            cache = json.load(f) or {}
    except Exception:                                # noqa: BLE001  (没有/损坏都当没有)
        cache = {}
    if date in cache:
        return str(cache[date]), 0
    try:
        from ashare import tushare_client as tsc
        days = tsc.trade_cal(date, date, is_open="1")
        ans = "Y" if days else "N"
    except Exception:                                # noqa: BLE001
        return "?", 1
    cache[date] = ans
    cache = {k: v for k, v in sorted(cache.items())[-60:]}      # 只留最近 60 天
    try:
        tmp = cache_path + ".new"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp, cache_path)
    except Exception:                                # noqa: BLE001  (缓存写不下去不算失败)
        pass
    return ans, 1


def count_rows(api: str, date: str) -> tuple:
    """某端点当日行数 -> (rows, a_rows, err)。只取 ts_code 一列。

    a_rows 只对有 ts_code 列的返回有意义; 端点报错时两个数都是 None (别把失败记成 0 行 ——
    "0 行"是"源还没入库"的证据, "调不通"不是)。
    """
    from ashare import market as mk
    from ashare import tushare_client as tsc
    try:
        df = tsc.query(api, fields="ts_code", retries=1, deadline_sec=90.0, trade_date=date)
    except Exception as e:                           # noqa: BLE001
        return None, None, f"{api}:{type(e).__name__}"
    n = len(df)
    if n and "ts_code" in df.columns:
        a = sum(1 for v in df["ts_code"] if mk.keep_a_code(v))
    else:
        a = 0
    return n, a, ""


def pct(num, den) -> str:
    if num is None or not den:
        return "-"
    return f"{100.0 * num / den:.1f}%"


def main() -> int:
    _utf8_stdio()
    ap = argparse.ArgumentParser(description="Tushare 当日行情到达时间探针 (只读)")
    ap.add_argument("--date", default="", help="trade_date YYYYMMDD (默认: 今天, 北京日历)")
    ap.add_argument("--log", default=os.environ.get("TS_PROBE_LOG", DEFAULT_LOG),
                    help=f"日志文件 (默认 {DEFAULT_LOG})")
    ap.add_argument("--no-log", action="store_true", help="只打印不落盘 (手工探)")
    args = ap.parse_args()

    date = (args.date or beijing_date()).replace("-", "")[:8]
    if len(date) != 8 or not date.isdigit():
        print(f"--date 要 YYYYMMDD, 收到 {args.date!r}", file=sys.stderr)
        return 2

    t0 = time.time()
    now_bj = dt.datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S")
    now_loc = dt.datetime.now().astimezone()
    now_local = now_loc.strftime("%Y-%m-%d %H:%M:%S ") + (now_loc.tzname() or "")

    errs = []
    listed, e = listed_count()
    if e:
        errs.append(e)
    opened, _ = is_open_day(date, args.log + ".cal.json")
    rows, a_rows, e1 = count_rows("daily", date)
    adj, _adj_a, e2 = count_rows("adj_factor", date)
    errs += [x for x in (e1, e2) if x]

    line = (f"{now_bj} CST | {now_local} | {date} | open={opened} | "
            f"rows={'-' if rows is None else rows} | "
            f"a_rows={'-' if a_rows is None else a_rows} | "
            f"adj={'-' if adj is None else adj} | "
            f"listed={'-' if listed is None else listed} | "
            f"a/listed={pct(a_rows, listed)} | adj/listed={pct(adj, listed)} | "
            f"{time.time() - t0:.1f}s | "
            + ("err=" + ";".join(errs) if errs else
               ("ready" if (a_rows is not None and listed and a_rows >= listed * 0.9)
                else "not-ready")))
    print(line)

    if not args.no_log:
        try:
            new = not os.path.exists(args.log) or os.path.getsize(args.log) == 0
            os.makedirs(os.path.dirname(os.path.abspath(args.log)) or ".", exist_ok=True)
            with open(args.log, "a", encoding="utf-8") as f:
                if new:
                    f.write(HEADER)
                f.write(line + "\n")
        except Exception as ex:                      # noqa: BLE001
            print(f"日志写入失败 ({type(ex).__name__}) —— 探针不因此报错", file=sys.stderr)
    return 0                                          # 恒 0: 探针失败不该让单元变红/告警


if __name__ == "__main__":
    raise SystemExit(main())
