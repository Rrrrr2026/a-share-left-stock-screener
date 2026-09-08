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

2026-09-08 返工加的四组 (校验员报的四条缺陷, 每条都先复现再上锁):
  · **数据源挂掉不许给假的"已就绪"**: 日历抛异常 / 日历吞成空列表 两条路都判 UNKNOWN(2)。
    改前生产的 `ashare.market.trading_days` 是 `except -> return []`, 空列表在 `_ready_verdict`
    里等于"周末 -> 已就绪", 于是**镜像挂掉 == 周末**, 等待循环第一轮就放行昨日库。
  · `ashare.market.trading_days` 的失败语义: 往外抛, 不是 [] 也不是 None (None 有另一个含义)
  · update 侧: 日历取不到时不打"无新交易日"的假追平、不推 `meta.max_trade_date`
  · **adj_factor 未就绪守卫**: 日线到了、因子没到就不写这一天 (写了 `MAX(bars_raw.d)` 越过去,
    那天缺的因子永远补不回来)
  · 探针的墙钟预算 (必须小于单元 TimeoutStartSec) 与"两个端点都够才算 ready"的备注列

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


# ---------------------------------------------------------------- 数据源挂掉 (2026-09-08 返工)


def _market(dirpath, **kw):
    """临时目录上的假 Market; kw 覆盖任意钩子。"""
    global _SAVED_MARKET
    from leftside_core.market import current
    if _SAVED_MARKET is None:
        try:
            _SAVED_MARKET = current()
        except Exception:                       # noqa: BLE001
            _SAVED_MARKET = None
    m = Market(name="ashare", dashboard_dir=dirpath, data_dir=dirpath,
               db_path=os.path.join(dirpath, "x.db"), **kw)
    set_market(m)
    return m


def _seed_full(dirpath, day="2026-09-07", n=10):
    """bars_raw/adj/adj_base/universe 都种上 -> 可以直接跑 _update_daily_by_date。"""
    codes = [f"{i:06d}" for i in range(1, n + 1)]
    conn = sqlite3.connect(os.path.join(dirpath, "pricestore.db"))
    ps._ensure_schema(conn)
    conn.executemany("INSERT OR REPLACE INTO bars_raw VALUES(?,?,?,?,?,?,?,?)",
                     [(c, day, 1, 1, 1, 1, 1, 1) for c in codes])
    conn.executemany("INSERT OR REPLACE INTO adj(code,d,factor) VALUES(?,?,?)",
                     [(c, day, 1.0) for c in codes])
    conn.executemany("INSERT OR REPLACE INTO adj_base(code,d,factor) VALUES(?,?,?)",
                     [(c, day, 1.0) for c in codes])
    conn.executemany("INSERT OR REPLACE INTO universe(code,name,list_date,delist_date,status) "
                     "VALUES(?,?,?,?,?)", [(c, c, "2000-01-01", "", "L") for c in codes])
    conn.commit()
    conn.close()
    return codes


def _db(dirpath, sql, args=()):
    conn = sqlite3.connect(os.path.join(dirpath, "pricestore.db"))
    try:
        return conn.execute(sql, args).fetchone()[0]
    finally:
        conn.close()


def test_calendar_down_is_never_ready():
    """**本卡返工的主缺陷**: 交易日历调不通时闸门必须判"判不了(2)", 绝不能判"已就绪(0)"。

    生产的 `ashare.market.trading_days` 2026-09-08 之前是 `except -> return []`, 而空列表在
    `_ready_verdict` 里的含义是"周末/长假 -> 已就绪" —— 于是**镜像挂掉 == 周末**, run_a.sh v2
    的等待循环第一轮就放行, 整条流水线拿昨日库跑完并把昨日行情当今天发布。
    两条路都要锁: ① 日历**抛异常** (改后的生产实现); ② 日历**吞成空列表** (任何还这么写的
    实现) —— 后者靠 `_calendar_days` 反问一句"库末日那天开不开市"自检出来。
    """
    print("\n[ready] 数据源挂掉时不许给假的'已就绪'")
    d = tempfile.mkdtemp(prefix="ready_down_")
    _seed(d, bars_raw_days=["2026-09-07"])

    def boom(a, b):
        raise RuntimeError("模拟镜像 500")

    _market(d, trading_days=boom)
    v = ps.ready_for("20260908")
    check("日历抛异常 -> UNKNOWN(2), 不是 YES", v["code"] == ps.READY_UNKNOWN and not v["ready"])
    check("理由里点名日历取失败", "交易日历取失败" in v["reason"])

    calls = []

    def swallow(a, b):                          # 改前生产实现的样子: 失败吞成 []
        calls.append((a, b))
        return []

    _market(d, trading_days=swallow)
    v = ps.ready_for("20260908")
    check("日历吞成空列表 -> 仍判 UNKNOWN(2) (自检拦下)",
          v["code"] == ps.READY_UNKNOWN and not v["ready"])
    check("自检确实反问了库末日那天", ("2026-09-07", "2026-09-07") in calls)
    check("理由说清是自检没过", "自检" in v["reason"])

    good = ["2026-09-04", "2026-09-07"]         # 日历活着, 区间内真的没有开市日
    _market(d, trading_days=lambda a, b: [x for x in good if a <= x <= b])
    v = ps.ready_for("2026-09-13")              # 09-13 周日
    check("真周末 (日历活着) 仍判 ready, 没有被自检误伤", v["ready"] and v["code"] == ps.READY_YES)

    n_calls = []
    _market(d, trading_days=lambda a, b: n_calls.append((a, b)) or [x for x in good
                                                                   if a <= x <= b])
    v = ps.ready_for("20260907")
    check("已追平时一次日历都不问 (短路在比日期那一步)", v["ready"] and not n_calls)


def test_market_trading_days_raises_on_failure():
    """源头那一半: `ashare.market.trading_days` 不许再把取历失败吞成 []。"""
    print("\n[ready] ashare.market.trading_days 的失败语义")
    from ashare import tushare_client as tsc
    orig_cal, orig_on = tsc.trade_cal, _amkt._tushare_on
    try:
        _amkt._tushare_on = lambda: True

        def boom(*a, **k):
            raise tsc.TushareTransport("trade_cal: 模拟镜像 500", "trade_cal", 500)

        tsc.trade_cal = boom
        raised = None
        try:
            got = _amkt.trading_days("2026-09-08", "2026-09-08")
        except Exception as e:                  # noqa: BLE001
            raised, got = e, "raised"
        check("trade_cal 抛错时 trading_days 往外抛 (不是 [] 也不是 None)",
              raised is not None and got == "raised")
        check("抛的是原异常类型 (调用方能分类)", isinstance(raised, tsc.TushareError))
        _amkt._tushare_on = lambda: False
        check("源开关关着仍返回 None (= 路径未启用, 语义没被改掉)",
              _amkt.trading_days("2026-09-08", "2026-09-08") is None)
    finally:
        tsc.trade_cal, _amkt._tushare_on = orig_cal, orig_on


def test_update_by_date_calendar_failure_is_not_caught_up():
    """update 侧: 日历取不到时不许打"库内已到 X, 无新交易日"这句假追平, 也不许推 meta。"""
    print("\n[update] 交易日历取不到 -> 停下, 不假追平")
    d = tempfile.mkdtemp(prefix="upd_cal_")
    _seed_full(d)

    def boom(a, b):
        raise RuntimeError("模拟镜像 500")

    m = _market(d, trading_days=boom,
                fetch_bars_by_date=lambda dd: {}, fetch_adj_by_date=lambda dd: {})
    n = ps._update_daily_by_date(m)
    check("返回 0 (不是 None —— None 会让调用方回退逐股回看那条禁路)", n == 0)
    check("meta.max_trade_date 一个字都没写",
          _db(d, "SELECT COUNT(*) FROM meta WHERE key='max_trade_date'") == 0)
    check("bars_raw 末日原样", _db(d, "SELECT MAX(d) FROM bars_raw") == "2026-09-07")


def test_update_by_date_adj_gate():
    """adj_factor 没到齐就不许写这一天 —— 写了 MAX(d) 越过去, 那天的因子永远补不回来。"""
    print("\n[update] 复权因子未就绪守卫")
    day, days = "2026-09-08", ["2026-09-07", "2026-09-08"]
    d = tempfile.mkdtemp(prefix="upd_adj_")
    codes = _seed_full(d)
    bars = {c: (1.0, 1.0, 1.0, 1.0, 100.0, 100.0) for c in codes}
    cal = lambda a, b: [x for x in days if a <= x <= b]                 # noqa: E731

    m = _market(d, trading_days=cal, fetch_bars_by_date=lambda dd: bars,
                fetch_adj_by_date=lambda dd: {})                       # 因子端点还是空的
    ps._update_daily_by_date(m)
    check("daily 够了但 adj 是空的 -> 这天一根都不写",
          _db(d, "SELECT COUNT(*) FROM bars_raw WHERE d=?", (day,)) == 0)
    check("库末日仍是昨天 (下次还会回来补这天)",
          _db(d, "SELECT MAX(d) FROM bars_raw") == "2026-09-07")
    v = ps.ready_for(day.replace("-", ""))
    check("闸门因此也判'还没到'(1), 等待循环会继续等", v["code"] == ps.READY_NO)

    d2 = tempfile.mkdtemp(prefix="upd_adj_ok_")
    codes = _seed_full(d2)
    m = _market(d2, trading_days=cal, fetch_bars_by_date=lambda dd: bars,
                fetch_adj_by_date=lambda dd: {c: 1.0 for c in codes})   # 因子也到齐
    ps._update_daily_by_date(m)
    check("两个端点都够 -> 正常写入这一天",
          _db(d2, "SELECT COUNT(*) FROM bars_raw WHERE d=?", (day,)) == len(codes))
    check("因子也落了库", _db(d2, "SELECT COUNT(*) FROM adj WHERE d=?", (day,)) == len(codes))

    d3 = tempfile.mkdtemp(prefix="upd_adj_part_")
    codes = _seed_full(d3)                                  # 10 只在市 -> 守卫线 9 只
    m = _market(d3, trading_days=cal, fetch_bars_by_date=lambda dd: bars,
                fetch_adj_by_date=lambda dd: {c: 1.0 for c in codes[:8]})   # 只到 8 只
    ps._update_daily_by_date(m)
    check("因子只到 8/10 (< 90%) -> 仍然不写这天",
          _db(d3, "SELECT COUNT(*) FROM bars_raw WHERE d=?", (day,)) == 0)


def test_probe_budget_and_adj_verdict():
    """探针: 墙钟预算 (别把单元耗到 systemd 超时) + 备注列两个端点都算。"""
    print("\n[probe] 墙钟预算与 ready 判据")
    import importlib.util
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "tools", "tushare_ready_probe.py")
    spec = importlib.util.spec_from_file_location("_ts_probe_budget", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    check("BUDGET_SEC 必须小于单元的 TimeoutStartSec=5min=300s", mod.BUDGET_SEC < 300)
    check("单次调用期限 × 3 个端点 + 起动余量仍在预算内",
          mod.CALL_DEADLINE_SEC * 3 + 20 <= mod.BUDGET_SEC)
    check("预算用完时 count_rows 不发调用, 记 err=budget",
          mod.count_rows("daily", "20260908", 0.0) == (None, None, "daily:budget"))
    check("预算用完时 is_open_day 也不发调用",
          mod.is_open_day("20260908", os.path.join(tempfile.mkdtemp(), "no.json"), 0.0)
          == ("?", 0))

    check("两端都够 -> ready", mod.verdict(5209, 5558, 5218) == "ready")
    check("因子还没到 -> not-ready(adj) (不是 ready)",
          mod.verdict(5209, 0, 5218) == "not-ready(adj)")
    check("日线还没到 -> not-ready(daily)", mod.verdict(10, 5558, 5218) == "not-ready(daily)")
    check("两个都没到 -> 点名两个", mod.verdict(0, 0, 5218) == "not-ready(daily+adj)")
    check("端点调不通 (None) 不算 ready", mod.verdict(5209, None, 5218) == "not-ready(adj)")
    check("日志表头写明两个端点都要够", "adj >= listed" in mod.HEADER)

    tmp = tempfile.mkdtemp(prefix="probe_fail_")
    cache = os.path.join(tmp, "probe.log.cal.json")
    with open(cache, "w", encoding="utf-8") as f:
        json.dump({"20260908": f"?{mod.CAL_MAX_FAILS}"}, f)
    check("当天日历连失够多次后不再问 (省掉每 5 分钟一次的空跑)",
          mod.is_open_day("20260908", cache) == ("?", 0))
    from ashare import tushare_client as tsc
    orig = tsc.trade_cal

    def boom(*a, **k):
        raise RuntimeError("模拟镜像 500")

    try:
        tsc.trade_cal = boom
        with open(cache, "w", encoding="utf-8") as f:
            json.dump({"20260908": "?1"}, f)
        check("还没连失够次数时仍会再问一次", mod.is_open_day("20260908", cache) == ("?", 1))
        with open(cache, encoding="utf-8") as f:
            check("失败也写缓存, 连失计数 +1 (改前只在成功时写, 于是每 5 分钟重走最慢那条路)",
                  json.load(f).get("20260908") == "?2")
    finally:
        tsc.trade_cal = orig


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
         test_probe_line_and_cal_cache,
         # 2026-09-08 返工 (校验员四条): 数据源挂掉不许给假的"已就绪"
         test_calendar_down_is_never_ready, test_market_trading_days_raises_on_failure,
         test_update_by_date_calendar_failure_is_not_caught_up, test_update_by_date_adj_gate,
         test_probe_budget_and_adj_verdict]


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
