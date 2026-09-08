#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
就绪闸门 离线自测 (不联网): `pricestore.ready_for` / `_ready_verdict` + 到达时间探针的纯逻辑
=============================================================================================
背景 (老板 2026-09-08 决定⑤): A 股流水线要从 14:00 CEST (北京 20:00) 挪到收盘后不久,
于是"Tushare 当天还没入库"从理论风险变成每天都要面对的事。run_a.sh v2 的处置是
**调 update -> 问一句 ready -> 没到就睡 10 分钟再来 -> 超时 exit 1 告警而不是发布昨日行情**。
这一份锁住那句"问一句"的全部判定, 以及探针那条日志行的骨架。

覆盖:
  · `_ready_verdict` 三态: 已到 (0) / 还没到 (1) / **判不了 (2)** —— 判不了必须与"还没到"
    分开, 否则交易日历一抖动就空等两小时再告警
  · **日期归一化回归**: 库里是 'YYYY-MM-DD', 命令行传的是 '20260908', 字符串直接比大小
    ('-' 0x2D < '0' 0x30) 会让 `have >= target` 恒假, 再把逆序区间喂给 trade_cal 得 0 个
    开市日, 最后判成 "周末/长假, 无需等待" —— 一个"已就绪"的假答案。首版真的这么错过。
  · 周末/长假 (区间内无开市日) 判已就绪, 不空等
  · `ready_for` 端到端 (临时库 + 假 Market, 零联网), 含 v1 老库回落 bars 取末日
  · 探针 `tools/tushare_ready_probe.py`: 日志行字段齐全; 交易日历缓存命中时**一次调用都不发**

运行:  python tests/test_ready_gate.py    或    python -m pytest tests/test_ready_gate.py -q
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ashare.market as _amkt                                   # noqa: E402,F401  (注入 Market)
from leftside_core import pricestore as ps                      # noqa: E402
from leftside_core.market import Market, set_market             # noqa: E402

PASS, FAIL = 0, 0
_SAVED_MARKET = None


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ FAIL: {name}")


def _tmp_market(days, enabled=True) -> str:
    """临时数据目录 + 只有交易日历的假 Market (ready_for 只用到 trading_days)。"""
    global _SAVED_MARKET
    from leftside_core.market import current
    if _SAVED_MARKET is None:
        try:
            _SAVED_MARKET = current()
        except Exception:                       # noqa: BLE001
            _SAVED_MARKET = None
    d = tempfile.mkdtemp(prefix="ready_")
    set_market(Market(
        name="ashare", dashboard_dir=d, data_dir=d, db_path=os.path.join(d, "x.db"),
        trading_days=(lambda a, b: [x for x in days if a <= x <= b]) if enabled
        else (lambda a, b: None)))
    return d


def _seed(dirpath, bars_raw_days=(), bars_days=()):
    conn = sqlite3.connect(os.path.join(dirpath, "pricestore.db"))
    ps._ensure_schema(conn)
    conn.executemany("INSERT OR REPLACE INTO bars_raw VALUES(?,?,?,?,?,?,?,?)",
                     [("000001", d, 1, 1, 1, 1, 1, 1) for d in bars_raw_days])
    conn.executemany("INSERT OR REPLACE INTO bars VALUES(?,?,?,?,?,?,?,?)",
                     [("000001", d, 1, 1, 1, 1, 1, 1) for d in bars_days])
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- 纯判定


def test_ready_verdict_three_states():
    print("\n[ready] 三态判定")
    v = ps._ready_verdict("2026-09-08", "2026-09-08", ["2026-09-08"])
    check("库内已到当天 -> ready, code 0", v["ready"] and v["code"] == ps.READY_YES)

    v = ps._ready_verdict("2026-09-07", "2026-09-08", ["2026-09-08"])
    check("差一个交易日 -> not ready, code 1",
          (not v["ready"]) and v["code"] == ps.READY_NO and v["missing"] == ["2026-09-08"])

    v = ps._ready_verdict("2026-09-04", "2026-09-08", ["2026-09-07", "2026-09-08"])
    check("差两个交易日 -> missing 两条且升序",
          v["missing"] == ["2026-09-07", "2026-09-08"] and v["code"] == ps.READY_NO)

    v = ps._ready_verdict("2026-09-07", "2026-09-08", None)
    check("交易日历不可用 -> UNKNOWN(2), 不是 NO",
          (not v["ready"]) and v["code"] == ps.READY_UNKNOWN)

    v = ps._ready_verdict(None, "2026-09-08", ["2026-09-08"])
    check("空库 -> UNKNOWN(2) 且理由指向重建",
          v["code"] == ps.READY_UNKNOWN and "重建" in v["reason"])

    v = ps._ready_verdict("2026-09-04", "2026-09-06", [])
    check("周末 (区间内无开市日) -> ready, 不空等",
          v["ready"] and v["code"] == ps.READY_YES and not v["missing"])

    v = ps._ready_verdict("2026-09-30", "2026-10-06", ["2026-09-30"])
    check("长假: 日历只给假期前那天 (<= have) -> ready",
          v["ready"] and not v["missing"])


def test_iso_day_normalization():
    """回归: 'YYYYMMDD' 与 'YYYY-MM-DD' 混比是首版的真实缺陷。"""
    print("\n[ready] 日期归一化 (首版缺陷回归)")
    check("_iso_day('20260908') -> 2026-09-08", ps._iso_day("20260908") == "2026-09-08")
    check("_iso_day('2026-09-08') 原样", ps._iso_day("2026-09-08") == "2026-09-08")
    check("裸串比大小确实是反的 (缺陷成因)", "2026-09-08" < "20260908")

    v = ps._ready_verdict("2026-09-07", "20260907", ["2026-09-07"])
    check("target 传 YYYYMMDD 且已追平 -> ready 且理由里是归一化后的日期",
          v["ready"] and v["code"] == ps.READY_YES and "2026-09-07" in v["reason"])

    v = ps._ready_verdict("2026-09-07", "20260908", ["2026-09-08"])
    check("target 传 YYYYMMDD 且还差一天 -> 仍判 NO (不会被假的'无开市日'蒙混)",
          (not v["ready"]) and v["code"] == ps.READY_NO and v["missing"] == ["2026-09-08"])

    v = ps._ready_verdict("20260908", "2026-09-08", ["2026-09-08"])
    check("have 传 YYYYMMDD 也归一化", v["ready"])


# ---------------------------------------------------------------- 端到端 (零联网)


def test_ready_for_end_to_end():
    print("\n[ready] ready_for 端到端 (临时库 + 假日历, 零联网)")
    days = ["2026-09-04", "2026-09-07", "2026-09-08"]
    d = _tmp_market(days)
    _seed(d, bars_raw_days=["2026-09-04", "2026-09-07"])

    v = ps.ready_for("20260908")
    check("库停在 09-07, 目标 09-08 -> not ready",
          (not v["ready"]) and v["code"] == ps.READY_NO and v["missing"] == ["2026-09-08"])
    check("返回 have/target 两个字段供日志",
          v["have"] == "2026-09-07" and v["target"] == "2026-09-08")

    v = ps.ready_for("20260907")
    check("目标 09-07 (已在库) -> ready", v["ready"] and v["code"] == ps.READY_YES)

    v = ps.ready_for("2026-09-06")
    check("目标 09-06 (周日) -> ready", v["ready"])

    d2 = _tmp_market(days, enabled=False)      # trading_days 返回 None = 路径未启用
    _seed(d2, bars_raw_days=["2026-09-07"])
    v = ps.ready_for("20260908")
    check("日历返回 None -> UNKNOWN(2), 让调用方按老规矩往下走",
          v["code"] == ps.READY_UNKNOWN)

    d3 = _tmp_market(days)                     # v1 老库: 只有 bars, 没有 bars_raw
    _seed(d3, bars_days=["2026-09-07"])
    v = ps.ready_for("20260908")
    check("v1 老库 (bars_raw 空) 也能取到末日, 不误判成空库",
          v["have"] == "2026-09-07" and v["code"] == ps.READY_NO)

    d4 = _tmp_market(days)
    _seed(d4)                                   # 一根 bar 都没有
    v = ps.ready_for("20260908")
    check("真空库 -> UNKNOWN(2)", v["code"] == ps.READY_UNKNOWN)


def test_exit_codes_are_the_contract():
    """run_a.sh v2 的等待循环直接读退出码, 数值本身就是接口, 不许改。"""
    print("\n[ready] 退出码常量")
    check("READY_YES/NO/UNKNOWN == 0/1/2",
          (ps.READY_YES, ps.READY_NO, ps.READY_UNKNOWN) == (0, 1, 2))
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "ashare", "pricestore.py"), encoding="utf-8").read()
    check("ashare.pricestore 的 __main__ 认 ready 子命令", '"ready"' in src)
    check("ready 分支用 v['code'] 当退出码", "SystemExit(v[\"code\"])" in src)


# ---------------------------------------------------------------- 探针


def test_probe_line_and_cal_cache():
    print("\n[probe] 到达时间探针的纯逻辑")
    import importlib.util
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "tools", "tushare_ready_probe.py")
    spec = importlib.util.spec_from_file_location("_ts_ready_probe", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    check("beijing_date() 是 8 位数字",
          len(mod.beijing_date()) == 8 and mod.beijing_date().isdigit())
    check("pct(5207, 5218) = 99.8%", mod.pct(5207, 5218) == "99.8%")
    check("pct(None, n) / pct(n, 0) 都给 '-' 而不是崩",
          mod.pct(None, 5218) == "-" and mod.pct(5207, 0) == "-")

    tmp = tempfile.mkdtemp(prefix="probe_")
    cache = os.path.join(tmp, "probe.log.cal.json")
    with open(cache, "w", encoding="utf-8") as f:
        json.dump({"20260908": "Y"}, f)
    ans, calls = mod.is_open_day("20260908", cache)
    check("交易日历缓存命中 -> 答案对且**零次调用**", ans == "Y" and calls == 0)

    src = open(path, encoding="utf-8").read()
    for field in ("open=", "rows=", "a_rows=", "adj=", "listed=", "a/listed=", "adj/listed="):
        check(f"日志行含字段 {field}", field in src)
    check("日志表头写明守卫阈值 90%", "90%" in mod.HEADER)
    check("探针只读: 不出现任何 INSERT/UPDATE/DELETE",
          not any(k in src.upper() for k in ("INSERT ", "UPDATE ", "DELETE ")))
    check("价格库以 mode=ro 打开 (不参与写锁)", "mode=ro" in src)
    check("退出码恒 0 (探针失败不该让单元变红)", "return 0" in src and "OnFailure" not in src)


def test_zz_no_check_failures():
    """pytest 只看有没有抛异常, 而 check() 是打印不是断言 —— 没有这一条, 上面任何一条
    ✗ FAIL 在 `pytest -q` 里都会被算成绿。放在最后一个 (pytest 按文件顺序跑)。"""
    global _SAVED_MARKET
    if _SAVED_MARKET is not None:
        set_market(_SAVED_MARKET)               # 别把假 Market 留给同批次的其它用例
        _SAVED_MARKET = None
    assert FAIL == 0, f"{FAIL} 条 check 未通过 (逐条见上面的 ✗ FAIL 行)"


TESTS = [test_ready_verdict_three_states, test_iso_day_normalization,
         test_ready_for_end_to_end, test_exit_codes_are_the_contract,
         test_probe_line_and_cal_cache]


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:                       # noqa: BLE001
            pass
    for t in TESTS:
        t()
    test_zz_no_check_failures()
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)
