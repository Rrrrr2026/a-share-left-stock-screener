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
  · **整个进程有墙钟预算 `BUDGET_SEC`, 而且它必须小于单元的 TimeoutStartSec** (2026-09-08
    返工补): "退出码恒 0" 只挡得住告警钩子, 挡不住 systemd 把超时的单元记成 failed ——
    值班每天那条"失败单元=0"照样过不了。而 `tushare_client` 的默认值是 3 次重试 × 120s
    硬期限 (最坏 3×120+退避 2+4 ≈ 366s), **光交易日历一个端点就超过 5min**。所以这里
    每一次调用都显式传 `retries=1` 与一个从剩余预算里算出来的 `deadline_sec`, 预算用完
    就不再发调用、直接把 `err=budget` 写进日志行。算账见 `BUDGET_SEC` 那几行。

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

#: 一次运行的墙钟预算 (秒)。**必须明显小于 stock-tsprobe.service 的 TimeoutStartSec=5min**,
#: 否则镜像滴流那天单元会被 systemd 判 timeout -> failed (见模块文档最后一条)。
#: 算账: 3 个端点 (trade_cal 只在当天第一次问) × 每次最多 CALL_DEADLINE_SEC=60s + 解释器
#: 起动与三次 spawn 硬期限子进程 ≈ 10s -> 最坏 190s < 200s 预算 < 300s 单元超时。
#: 常态实测 6.4-8.1s (服务器 09-08 三行日志), 预算是给"源滴流"那天兜底的, 不是常态。
BUDGET_SEC = 200.0
CALL_DEADLINE_SEC = 60.0        # 单次调用的硬期限上限 (还要与剩余预算取小)
CAL_MAX_FAILS = 3               # 交易日历当天连失这么多次就不再问 (省下每 5 分钟一次的空跑)
HEADER = ("# tushare_ready_probe · Tushare 当日行情到达时间探针 (只读)\n"
          "# 字段: 北京时间 | 本地时间 | trade_date | open=开市? | rows=daily原始行 | "
          "a_rows=过A股过滤后(守卫比的就是它) | adj=adj_factor行 | listed=在市股(守卫分母) | "
          "a/listed | adj/listed | 耗时 | 备注\n"
          "# 守卫阈值: a_rows >= listed × 90% **且** adj >= listed × 90% 才算 ready ——\n"
          "#   两个端点不同源、到达时间不同步, 而 leftside_core.pricestore."
          "_update_daily_by_date 两个都要够才肯写这一天\n"
          "#   (日线到了、因子没到就写下去的话, 那天缺的因子永远补不回来)\n")


def _left(t0: float) -> float:
    """还剩多少墙钟预算 (秒)。"""
    return BUDGET_SEC - (time.time() - t0)


def _deadline(t0: float) -> float:
    """这一次调用给多少秒: 剩余预算与 `CALL_DEADLINE_SEC` 取小。<=0 表示预算已经用完。"""
    return min(CALL_DEADLINE_SEC, _left(t0))


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


def _save_cal_cache(cache_path: str, cache: dict, date: str, value: str) -> None:
    cache[date] = value
    cache = {k: v for k, v in sorted(cache.items())[-60:]}      # 只留最近 60 天
    try:
        tmp = cache_path + ".new"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp, cache_path)
    except Exception:                                # noqa: BLE001  (缓存写不下去不算失败)
        pass


def is_open_day(date: str, cache_path: str, deadline_sec: float = CALL_DEADLINE_SEC) -> tuple:
    """今天开不开市 -> ('Y'/'N'/'?', 用了几次调用)。答案按 trade_date 缓存, 一天只问一次。

    为什么值得多花这一次调用: 日志里一整天的 `rows=0` 有两种截然不同的含义 —— 休市 (正常)
    与镜像挂了 (要查)。事后翻日志的人分不出来, 就等于这三天白跑。

    **失败也要缓存** (2026-09-08 返工补): 原来只在成功时写缓存, 于是镜像滴流的那一天,
    每 5 分钟一轮都要重走一遍最坏耗时的日历调用 —— 那正是最不该再往源上加压、也最容易把
    单元耗到超时的时候。现在失败记成 `"?N"` (N = 当天连失次数), 连失 `CAL_MAX_FAILS` 次
    就当天不再问 (返回 `?` 且 **0 次调用**); 中途成功一次就被 'Y'/'N' 覆盖, 计数自然清零。
    `deadline_sec` 由调用方按剩余墙钟预算给, 且恒定 `retries=1` —— 默认的 3×120s 会单独
    超过单元的 TimeoutStartSec。
    """
    cache = {}
    try:
        with open(cache_path, encoding="utf-8") as f:
            cache = json.load(f) or {}
    except Exception:                                # noqa: BLE001  (没有/损坏都当没有)
        cache = {}
    cached = str(cache.get(date, ""))
    if cached in ("Y", "N"):
        return cached, 0
    fails = int(cached[1:]) if cached.startswith("?") and cached[1:].isdigit() else 0
    if fails >= CAL_MAX_FAILS or deadline_sec <= 0:
        return "?", 0                                # 当天已经连失够多次 / 预算用完: 不再问
    try:
        from ashare import tushare_client as tsc
        days = tsc.trade_cal(date, date, is_open="1", retries=1, deadline_sec=deadline_sec)
        ans = "Y" if days else "N"
    except Exception:                                # noqa: BLE001
        _save_cal_cache(cache_path, cache, date, f"?{fails + 1}")
        return "?", 1
    _save_cal_cache(cache_path, cache, date, ans)
    return ans, 1


def count_rows(api: str, date: str, deadline_sec: float = CALL_DEADLINE_SEC) -> tuple:
    """某端点当日行数 -> (rows, a_rows, err)。只取 ts_code 一列。

    a_rows 只对有 ts_code 列的返回有意义; 端点报错时两个数都是 None (别把失败记成 0 行 ——
    "0 行"是"源还没入库"的证据, "调不通"不是)。

    `retries=1` + 调用方给的 `deadline_sec` (从剩余墙钟预算里算): 见模块文档最后一条,
    默认常量会让单次调用最坏 366s, 单元 5min 就超时了。
    """
    if deadline_sec <= 0:
        return None, None, f"{api}:budget"           # 预算用完: 不发这次调用
    from ashare import market as mk
    from ashare import tushare_client as tsc
    try:
        df = tsc.query(api, fields="ts_code", retries=1, deadline_sec=deadline_sec,
                       trade_date=date)
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


def verdict(a_rows, adj, listed, ratio: float = 0.9) -> str:
    """备注列: `ready` / `not-ready(daily)` / `not-ready(adj)` / `not-ready(daily+adj)`。

    **两个端点都要够** (2026-09-08 返工补, 与 `_update_daily_by_date` 的两道守卫对齐):
    daily 与 adj_factor 不同源、到达时间不同步 (09-08 17:34 实测 5551 / 5558 行), 而
    "提前跑" 撞上的恰恰是 daily 先到、因子后到那个窗口。备注列只算 daily 的话, GM 三天后
    照日志挑新时点, 挑到的会是"日线到了"的时刻, 而闸门等的是两个都到 —— 差多少全在日志
    里有、在结论里没有。括号里标出是谁没到, 免得事后还要自己拿两列去减。
    """
    if not listed:
        return "not-ready(no-listed)"
    miss = [name for name, n in (("daily", a_rows), ("adj", adj))
            if n is None or n < listed * ratio]
    return "ready" if not miss else "not-ready(" + "+".join(miss) + ")"


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
    # 三个端点各自从**剩余**墙钟预算里领期限, 领不到就不发 (err=budget)。顺序即优先级:
    # 日历最便宜且一天只问一次, 行数才是我们真正要的东西。
    opened, _ = is_open_day(date, args.log + ".cal.json", _deadline(t0))
    rows, a_rows, e1 = count_rows("daily", date, _deadline(t0))
    adj, _adj_a, e2 = count_rows("adj_factor", date, _deadline(t0))
    errs += [x for x in (e1, e2) if x]

    line = (f"{now_bj} CST | {now_local} | {date} | open={opened} | "
            f"rows={'-' if rows is None else rows} | "
            f"a_rows={'-' if a_rows is None else a_rows} | "
            f"adj={'-' if adj is None else adj} | "
            f"listed={'-' if listed is None else listed} | "
            f"a/listed={pct(a_rows, listed)} | adj/listed={pct(adj, listed)} | "
            f"{time.time() - t0:.1f}s | "
            + ("err=" + ";".join(errs) if errs else verdict(a_rows, adj, listed)))
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
