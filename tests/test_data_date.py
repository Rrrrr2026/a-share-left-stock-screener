#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
data_date 定义与自检 (2026-09-14 卡 DATA-DATE) —— 离线, 零联网
==============================================================
病灶: 跑批挪到 10:00 CEST (北京 16:00) 的首日, `pricestore update` 10:01 把个股写到 09-14,
指数序列 (东财/新浪 + 库内 idx_bars) 却要到流水线跑完 (11:19 的 ingest) 才补, 而 run_pipeline
拿**基准指数末日**当 data_date -> 快照/看板/模拟盘登记整批标成 09-11, 价却是 09-14 收盘。

这里锁四件事:
  ① `ashare.datadate.resolve`: 库末日优先, 基准末日只交叉核对 (不一致 warning / 一致无 warning);
  ② `guard`: data_date < 库末日 必 error 并改正; < 最后收盘日 必 error (日历近似时降 warning);
  ③ `leftside_core.pricestore._update_daily_by_date`: 当日指数随个股同一步写进 idx_bars (尽力而为);
  ④ `datasource.fetch_benchmark_close`: 阶段A 直读库时指数也优先读库, 库内落后才联网。
外加: run_pipeline 必须用 ① 的定义 (源码契约); 快照 meta / 模拟盘 sig_date 吃的是 run_log 里的
data_date; 修正表登记 day_2026-09-14.json; 迁移脚本吃 --from-date/--to-date。

变异实测 (改回取指数末日 / 摘掉 guard / 摘掉指数入库 / 摘掉库优先 / 删修正表条目) 各自必红,
见 CHRONICLE 09-14 段。
"""
from __future__ import annotations
import datetime as dt
import json
import logging
import os
import sqlite3
import sys
import tempfile
import threading

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ashare.market as amkt                                     # noqa: E402,F401 (注入 Market)
from ashare import datadate as dd                                # noqa: E402
from ashare import datasource as ds                              # noqa: E402
from ashare.config import CONFIG                                 # noqa: E402
from leftside_core import backtest as bt                         # noqa: E402
from leftside_core import pricestore as ps                       # noqa: E402
from leftside_core.market import Market, current, set_market     # noqa: E402

BJ = dt.timezone(dt.timedelta(hours=8))
CEST = dt.timezone(dt.timedelta(hours=2))
CAL = ["2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11",
       "2026-09-14", "2026-09-15"]


def _cal(a, b):
    return [d for d in CAL if a <= d <= b]


class _Cap(logging.Handler):
    def __init__(self, *names):
        super().__init__(level=logging.DEBUG)
        self.recs, self._names = [], names

    def emit(self, rec):
        self.recs.append(rec)

    def __enter__(self):
        self._saved = []
        for n in self._names:
            lg = logging.getLogger(n)
            self._saved.append((lg, lg.level))
            lg.setLevel(logging.DEBUG)
            lg.addHandler(self)
        return self

    def __exit__(self, *exc):
        for lg, lvl in self._saved:
            lg.removeHandler(self)
            lg.setLevel(lvl)
        return False

    def msgs(self, level=None):
        return [r.getMessage() for r in self.recs if level is None or r.levelno == level]


# ---------------------------------------------------------------- ① resolve

def test_resolve_store_wins_over_bench():
    """09-14 事故形态: 库末日 09-14 / 指数末日 09-11 -> data_date 09-14, 且有一条 warning 点名。"""
    d, notes = dd.resolve("2026-09-14", "2026-09-11", "2026-09-14")
    assert d == "2026-09-14"
    warns = [n for n in notes if n.startswith("WARN ")]
    assert len(warns) == 1 and "2026-09-11" in warns[0] and "2026-09-14" in warns[0], notes
    assert "以库为准" in warns[0]
    assert not [n for n in notes if n.startswith("ERROR ")]


def test_resolve_consistent_no_warning():
    d, notes = dd.resolve("2026-09-14", "2026-09-14", "2026-09-14")
    assert d == "2026-09-14"
    assert not [n for n in notes if n.startswith(("WARN ", "ERROR "))], notes


def test_resolve_store_behind_bench_still_store():
    """反向: 指数比库新 (个股库落后) -> 仍以库为准 (阶段A 用的就是库里的价), warning 指向 update。"""
    d, notes = dd.resolve("2026-09-11", "2026-09-14", "2026-09-14")
    assert d == "2026-09-11"
    assert any(n.startswith("WARN ") and "个股库落后" in n for n in notes), notes


def test_resolve_legacy_when_no_store():
    """联网模式 (没读库): 老口径 = 基准末日; 连基准都没有 -> run_date。"""
    assert dd.resolve(None, "2026-09-11", "2026-09-14")[0] == "2026-09-11"
    assert dd.resolve(None, None, "2026-09-14")[0] == "2026-09-14"
    assert dd.resolve("", "2026-09-11T00:00", "2026-09-14")[0] == "2026-09-11"


# ---------------------------------------------------------------- ② guard

def test_guard_corrects_when_before_store_max():
    """防再犯: 谁把定义改回指数末日, 这一道会 error 并把 data_date 拨回库末日。"""
    d, notes = dd.guard("2026-09-11", "2026-09-14", None)
    assert d == "2026-09-14"
    errs = [n for n in notes if n.startswith("ERROR ")]
    assert len(errs) == 1 and "早于价格库个股末日" in errs[0], notes


def test_guard_before_last_closed_is_error_only_when_exact():
    d, notes = dd.guard("2026-09-11", "2026-09-11", "2026-09-14", exact=True)
    assert d == "2026-09-11"                       # 库本身旧了, 改不了
    assert any(n.startswith("ERROR ") and "最后一个已收盘交易日 2026-09-14" in n for n in notes), notes
    d, notes = dd.guard("2026-09-11", "2026-09-11", "2026-09-14", exact=False)
    assert not [n for n in notes if n.startswith("ERROR ")], notes
    assert any(n.startswith("WARN ") and "近似" in n for n in notes), notes


def test_guard_silent_when_fine():
    assert dd.guard("2026-09-14", "2026-09-14", "2026-09-14", True) == ("2026-09-14", [])
    assert dd.guard("2026-09-14", None, None) == ("2026-09-14", [])


# ---------------------------------------------------------------- 最后收盘日

def test_last_closed_trading_day_clock_and_calendar():
    # 10:00 CEST = 北京 16:00, 收盘后 -> 当天
    now = dt.datetime(2026, 9, 14, 10, 0, tzinfo=CEST)
    assert dd.last_closed_trading_day(now, _cal) == ("2026-09-14", True)
    # 北京 14:00 (盘中) -> 上一个开市日 09-11 (跳过周末)
    now = dt.datetime(2026, 9, 14, 14, 0, tzinfo=BJ)
    assert dd.last_closed_trading_day(now, _cal) == ("2026-09-11", True)
    # 长假: 日历里 09-30 之后没有开市日, 10-06 问 -> 09-30 (exact), 不会拿工作日当开市日
    hol = ["2026-09-29", "2026-09-30"]
    now = dt.datetime(2026, 10, 6, 16, 0, tzinfo=BJ)
    assert dd.last_closed_trading_day(now, lambda a, b: [x for x in hol if a <= x <= b]) == (
        "2026-09-30", True)


def test_last_closed_trading_day_falls_back_to_weekday_approx():
    def boom(a, b):
        raise RuntimeError("trade_cal 500")
    now = dt.datetime(2026, 9, 14, 16, 0, tzinfo=BJ)
    assert dd.last_closed_trading_day(now, boom) == ("2026-09-14", False)
    assert dd.last_closed_trading_day(now, None) == ("2026-09-14", False)
    # 周日 -> 上周五 (近似)
    now = dt.datetime(2026, 9, 13, 16, 0, tzinfo=BJ)
    assert dd.last_closed_trading_day(now, lambda a, b: []) == ("2026-09-11", False)


# ---------------------------------------------------------------- 夹具: 临时价格库

class _TmpStore:
    """一个真 SQLite 价格库 (bars / idx_bars), 挂到 ds.DATA_DIR 上, 源开关切 tushare。"""

    def __init__(self, bars_max="2026-09-14", idx_max="2026-09-11"):
        self.bars_max, self.idx_max = bars_max, idx_max

    def __enter__(self):
        self.d = tempfile.mkdtemp(prefix="dd_")
        conn = sqlite3.connect(os.path.join(self.d, "pricestore.db"))
        ps._ensure_schema(conn)
        days = [x for x in CAL if x <= self.bars_max]
        conn.executemany("INSERT OR REPLACE INTO bars VALUES(?,?,?,?,?,?,?,?)",
                         [("000001", x, 10, 11, 9, 10.5, 100, 1000) for x in days])
        idays = [x for x in CAL if x <= self.idx_max]
        conn.executemany("INSERT OR REPLACE INTO idx_bars VALUES(?,?,?,?,?,?)",
                         [(x, 4500, 4520, 4480, 4500 + i, 1.5e8) for i, x in enumerate(idays)])
        conn.commit()
        conn.close()
        self._saved = (ds.DATA_DIR, CONFIG["source"].get("bars"), CONFIG["source"]["use_cache"],
                       ds._STORE_TLS)
        ds.DATA_DIR, CONFIG["source"]["bars"], CONFIG["source"]["use_cache"] = self.d, "tushare", False
        ds._STORE_TLS = threading.local()
        return self

    def __exit__(self, *exc):
        ds.DATA_DIR, CONFIG["source"]["bars"], CONFIG["source"]["use_cache"], ds._STORE_TLS = self._saved
        return False


class _TmpMarket:
    def __init__(self, **kw):
        self.kw = kw

    def __enter__(self):
        try:
            self._saved = current()
        except Exception:                                    # noqa: BLE001
            self._saved = None
        self.d = tempfile.mkdtemp(prefix="ddm_")
        base = dict(name="ashare", dashboard_dir=self.d, data_dir=self.d,
                    db_path=os.path.join(self.d, "x.db"))
        base.update(self.kw)
        set_market(Market(**base))
        return self

    def __exit__(self, *exc):
        if self._saved is not None:
            set_market(self._saved)
        return False


# ---------------------------------------------------------------- 端到端: resolve_data_date

def _bench(last):
    days = [x for x in CAL if x <= last]
    return pd.DataFrame({"date": days, "close": [4500.0 + i for i in range(len(days))]})


def test_resolve_data_date_end_to_end_0914_shape():
    """服务器 09-14 10:01 的形态: 库个股到 09-14, idx_bars 与东财指数都停在 09-11 -> 09-14 + 一条
    warning, **零 error** (error 是给"定义被改回去"那天准备的)。"""
    now = dt.datetime(2026, 9, 14, 10, 1, tzinfo=CEST)
    with _TmpStore("2026-09-14", "2026-09-11"), _TmpMarket(trading_days=_cal):
        assert ds.bars_from_store_on()
        with _Cap("ashare.datadate") as cap:
            got = dd.resolve_data_date(_bench("2026-09-11"), "2026-09-14", now=now)
    assert got == "2026-09-14"
    assert not cap.msgs(logging.ERROR), cap.msgs()
    warns = cap.msgs(logging.WARNING)
    assert len(warns) == 1 and "以库为准" in warns[0], warns
    assert any("data_date = 2026-09-14" in m for m in cap.msgs(logging.INFO)), cap.msgs()


def test_resolve_data_date_consistent_is_quiet():
    now = dt.datetime(2026, 9, 14, 10, 1, tzinfo=CEST)
    with _TmpStore("2026-09-14", "2026-09-14"), _TmpMarket(trading_days=_cal):
        with _Cap("ashare.datadate") as cap:
            got = dd.resolve_data_date(_bench("2026-09-14"), "2026-09-14", now=now)
    assert got == "2026-09-14"
    assert not cap.msgs(logging.WARNING) and not cap.msgs(logging.ERROR), cap.msgs()


def test_resolve_data_date_stale_store_is_error():
    """库整个停在 09-11 而今天 09-14 已收盘 (PC 那种没跑 update 的形态) -> error 点名, 值改不了。"""
    now = dt.datetime(2026, 9, 14, 13, 30, tzinfo=CEST)
    with _TmpStore("2026-09-11", "2026-09-11"), _TmpMarket(trading_days=_cal):
        with _Cap("ashare.datadate") as cap:
            got = dd.resolve_data_date(_bench("2026-09-11"), "2026-09-14", now=now)
    assert got == "2026-09-11"
    errs = cap.msgs(logging.ERROR)
    assert len(errs) == 1 and "最后一个已收盘交易日 2026-09-14" in errs[0], cap.msgs()


def test_run_pipeline_uses_datadate_contract():
    """run_pipeline 里 data_date 那一行必须走 datadate, 旧写法 (指数末日) 不许回来。"""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "run_pipeline.py"), encoding="utf-8").read()
    assert "resolve_data_date(_bench, run_date)" in src
    assert 'data_date = str(_bench["date"].iloc[-1])' not in src


# ---------------------------------------------------------------- ③ 当日指数随个股同一步入库

def _seed_store_two_days(conn, days, bars, facs):
    for x in days[:2]:
        conn.executemany("INSERT OR REPLACE INTO bars_raw VALUES(?,?,?,?,?,?,?,?)",
                         [(c, x, *v) for c, v in bars[x].items()])
        conn.executemany("INSERT OR REPLACE INTO adj VALUES(?,?,?)",
                         [(c, x, f) for c, f in facs[x].items()])
    conn.executemany("INSERT OR REPLACE INTO idx_bars VALUES(?,?,?,?,?,?)",
                     [(x, 4500, 4520, 4480, 4500, 1.5e8) for x in days[:2]])
    conn.executemany("INSERT OR REPLACE INTO universe VALUES(?,?,?,?,?)",
                     [("000001", "甲", "2020-01-01", None, "L")])
    conn.commit()
    ps.materialize(None, conn)


def _run_update_with_index_hook(hook):
    days = ["2026-09-10", "2026-09-11", "2026-09-14"]
    bars = {x: {"000001": (10.0 + i, 11.0 + i, 9.0 + i, 10.5 + i, 100.0, 1000.0)}
            for i, x in enumerate(days)}
    facs = {x: {"000001": 3.0} for x in days}
    calls = []

    def _idx(d):
        calls.append(d)
        return hook(d)
    with _TmpMarket(fetch_bars_by_date=lambda d: bars.get(d, {}),
                    fetch_adj_by_date=lambda d: facs.get(d, {}),
                    trading_days=lambda a, b: [x for x in days if a <= x <= b],
                    fetch_universe_rows=lambda: [("000001", "甲", "2020-01-01", "", "L")],
                    fetch_index_bars=lambda s: [], fetch_bars_bulk=lambda c, s: {},
                    fetch_index_by_date=_idx) as m:
        conn = ps._conn()
        _seed_store_two_days(conn, days, bars, facs)
        conn.close()
        with _Cap("leftside_core.pricestore") as cap:
            n = ps.update_daily()
        conn = sqlite3.connect(os.path.join(m.d, "pricestore.db"))
        idx = conn.execute("SELECT d, c, v FROM idx_bars ORDER BY d").fetchall()
        raw_max = conn.execute("SELECT MAX(d) FROM bars_raw").fetchone()[0]
        conn.close()
    return n, calls, idx, raw_max, cap


def test_update_writes_index_same_step():
    n, calls, idx, raw_max, cap = _run_update_with_index_hook(
        lambda d: (4474.9474, 4490.0, 4460.0, 4480.0821, 149488418.0))
    assert n == 1 and raw_max == "2026-09-14"
    assert calls == ["2026-09-14"], calls                     # 只问新入库的那一天
    assert idx[-1][0] == "2026-09-14" and abs(idx[-1][1] - 4480.0821) < 1e-9 and idx[-1][2] == 149488418.0
    assert any("基准指数 2026-09-14 入库" in m for m in cap.msgs(logging.INFO)), cap.msgs()
    assert any("基准指数 +1 根 (idx_bars 末日 2026-09-14)" in m for m in cap.msgs(logging.INFO)), cap.msgs()
    assert not cap.msgs(logging.WARNING), cap.msgs(logging.WARNING)


def test_update_index_not_arrived_is_best_effort():
    """指数当日没到 (None) / 拉挂 (抛错): 个股照常入库, idx_bars 不动, 各一条 warning 点名。"""
    n, calls, idx, raw_max, cap = _run_update_with_index_hook(lambda d: None)
    assert n == 1 and raw_max == "2026-09-14" and idx[-1][0] == "2026-09-11"
    warns = cap.msgs(logging.WARNING)
    assert any("源尚无当日 bar" in w and "2026-09-14" in w for w in warns), warns
    assert any("落后个股末日 2026-09-14" in m for m in cap.msgs(logging.INFO)), cap.msgs()

    def boom(d):
        raise RuntimeError("index_daily 500")
    n, calls, idx, raw_max, cap = _run_update_with_index_hook(boom)
    assert n == 1 and raw_max == "2026-09-14" and idx[-1][0] == "2026-09-11"
    assert any("拉取失败" in w and "index_daily 500" in w for w in cap.msgs(logging.WARNING)), cap.msgs()


def test_update_without_hook_untouched():
    """老 Market (没有 fetch_index_by_date, 美股库形态): 一行指数日志都不打, 行为与从前一致。"""
    days = ["2026-09-10", "2026-09-11", "2026-09-14"]
    bars = {x: {"000001": (10.0, 11.0, 9.0, 10.5, 100.0, 1000.0)} for x in days}
    facs = {x: {"000001": 3.0} for x in days}
    with _TmpMarket(fetch_bars_by_date=lambda d: bars.get(d, {}),
                    fetch_adj_by_date=lambda d: facs.get(d, {}),
                    trading_days=lambda a, b: [x for x in days if a <= x <= b],
                    fetch_universe_rows=lambda: [], fetch_index_bars=lambda s: [],
                    fetch_bars_bulk=lambda c, s: {}) as m:
        conn = ps._conn()
        _seed_store_two_days(conn, days, bars, facs)
        conn.close()
        with _Cap("leftside_core.pricestore") as cap:
            n = ps.update_daily()
        conn = sqlite3.connect(os.path.join(m.d, "pricestore.db"))
        idx_max = conn.execute("SELECT MAX(d) FROM idx_bars").fetchone()[0]
        conn.close()
    assert n == 1 and idx_max == "2026-09-11"
    assert not [x for x in cap.msgs() if "基准指数" in x and "入库" in x and "+0" not in x], cap.msgs()


def test_ashare_market_wires_index_hook():
    m = current()
    assert m.name == "ashare" and m.fetch_index_by_date is amkt.fetch_index_by_date
    saved = CONFIG["source"].get("bars")
    try:
        CONFIG["source"]["bars"] = "fuyao"
        assert amkt.fetch_index_by_date("2026-09-14") is None      # 未启用 -> None, 不联网
    finally:
        CONFIG["source"]["bars"] = saved


# ---------------------------------------------------------------- ④ fetch_benchmark_close 库优先

class _FakeAk:
    def __init__(self, last, fail=False):
        self.last, self.fail, self.calls = last, fail, []

    def stock_zh_index_daily_em(self, symbol):
        self.calls.append("em")
        if self.fail:
            raise RuntimeError("em down")
        return _bench(self.last).rename(columns={"date": "日期", "close": "收盘"})

    def stock_zh_index_daily(self, symbol):
        self.calls.append("sina")
        if self.fail:
            raise RuntimeError("sina down")
        return _bench(self.last)


def _with_fake_net(last, fail=False):
    ak = _FakeAk(last, fail)
    saved = (ds._ak, ds.call_with_retry)
    ds._ak = lambda: ak
    ds.call_with_retry = lambda fn, *a, **k: fn(*a, **k)
    return ak, saved


def test_benchmark_reads_store_when_index_is_current():
    with _TmpStore("2026-09-14", "2026-09-14"):
        ak, saved = _with_fake_net("2026-09-14")
        try:
            with _Cap("ashare.datasource") as cap:
                df = ds.fetch_benchmark_close()
        finally:
            ds._ak, ds.call_with_retry = saved
    assert ak.calls == [], "库内指数已追平个股末日, 不该联网"
    assert list(df.columns)[:2] == ["date", "close"] and str(df["date"].iloc[-1]) == "2026-09-14"
    assert any("直读库" in m for m in cap.msgs(logging.INFO)), cap.msgs()


def test_benchmark_goes_online_when_store_index_behind():
    """09-14 10:01 形态 (idx_bars 09-11 < 个股 09-14): 联网; 网上有 09-14 就用网的。"""
    with _TmpStore("2026-09-14", "2026-09-11"):
        ak, saved = _with_fake_net("2026-09-14")
        try:
            with _Cap("ashare.datasource") as cap:
                df = ds.fetch_benchmark_close()
        finally:
            ds._ak, ds.call_with_retry = saved
    assert ak.calls == ["em"]
    assert str(df["date"].iloc[-1]) == "2026-09-14"
    assert any("落后个股末日" in w for w in cap.msgs(logging.WARNING)), cap.msgs()


def test_benchmark_prefers_newer_side_and_survives_net_failure():
    # 网上比库还旧 -> 用库
    with _TmpStore("2026-09-14", "2026-09-11"):
        ak, saved = _with_fake_net("2026-09-10")
        try:
            df = ds.fetch_benchmark_close()
        finally:
            ds._ak, ds.call_with_retry = saved
        assert str(df["date"].iloc[-1]) == "2026-09-11"
        # 网全挂 -> 退回库内 (旧但有), 不返回 None
        ak, saved = _with_fake_net("2026-09-14", fail=True)
        try:
            df = ds.fetch_benchmark_close()
        finally:
            ds._ak, ds.call_with_retry = saved
        assert df is not None and str(df["date"].iloc[-1]) == "2026-09-11"


def test_benchmark_legacy_path_when_store_off():
    saved_bars = CONFIG["source"].get("bars")
    saved_cache = CONFIG["source"]["use_cache"]
    CONFIG["source"]["bars"], CONFIG["source"]["use_cache"] = "fuyao", False
    ak, saved = _with_fake_net("2026-09-14")
    try:
        df = ds.fetch_benchmark_close()
    finally:
        ds._ak, ds.call_with_retry = saved
        CONFIG["source"]["bars"], CONFIG["source"]["use_cache"] = saved_bars, saved_cache
    assert ak.calls == ["em"] and str(df["date"].iloc[-1]) == "2026-09-14"


# ---------------------------------------------------------------- 快照/模拟盘吃的是 run_log 的 data_date

def test_snapshot_and_paper_consume_runlog_data_date():
    """db.log_run(data_date=X) -> 看板 meta / 快照 meta / paper 的 sig_date 都是 X (③ 的链)。"""
    from ashare import db, export_data as ex, paper as _paper   # noqa: F401
    from leftside_core import paper as lp
    tmp = tempfile.mkdtemp(prefix="ddx_")
    hist = os.path.join(tmp, "history")
    os.makedirs(hist)
    saved = (db.DB_PATH, ex.HISTORY_DIR, ds.fetch_benchmark_close, bt._paths, lp._paths,
             CONFIG["source"]["use_cache"])
    db.DB_PATH = os.path.join(tmp, "ashare.db")
    ex.HISTORY_DIR = hist
    ds.fetch_benchmark_close = lambda: None
    bt._paths = lambda: (hist, os.path.join(tmp, "bt.js"), os.path.join(tmp, "bt.json"))
    # paper 自己的 _paths 也指到临时目录: 否则它会去真 dashboard/history 捞最新的 quality_*.json,
    # 优质榜信号按那份文件自己的日期登记 (与本链无关), 会混进断言
    lp._paths = lambda: (hist, os.path.join(tmp, "paper.json"), os.path.join(tmp, "paper.js"))
    CONFIG["source"]["use_cache"] = False
    try:
        db.init_db()
        db.log_run("2026-09-14", "2026-09-14 10:01:25", "2026-09-14 11:14:19", 4915, 1,
                   ["银行"], "ok", data_date="2026-09-14", n_pool_raw=5158,
                   scan_basis="store_universe")
        db.save_tech("2026-09-14", [{"code": "600489", "name": "中金黄金", "industry": "贵金属",
                                     "price": 20.0, "tech_score": 80, "tag": "深跌抄底",
                                     "dip": 1}])
        db.save_final("2026-09-14", [{"code": "600489", "name": "中金黄金", "industry": "贵金属",
                                      "rank": 1, "tag": "深跌抄底", "total_score": 80}])
        meta = ex.build_payload("2026-09-14")["meta"]
        assert meta["run_date"] == "2026-09-14" and meta["data_date"] == "2026-09-14"
        path = ex.write_history_snapshot("2026-09-14")
        snap = json.load(open(path, encoding="utf-8"))
        assert snap["meta"]["data_date"] == "2026-09-14"
        assert snap["candidates"] and snap["candidates"][0]["code"] == "600489", snap["meta"]
        # 模拟盘登记的 sig_date 就是快照 meta.data_date —— 09-14 那 36 条错标信号就是从这条链来的
        as_of, sigs = lp._latest_signals()
        assert as_of == "2026-09-14"
        assert sigs and all(s[1] == "2026-09-14" for s in sigs), sigs[:3]
    finally:
        db.DB_PATH, ex.HISTORY_DIR, ds.fetch_benchmark_close, bt._paths, lp._paths, \
            CONFIG["source"]["use_cache"] = saved


# ---------------------------------------------------------------- 修正表 + 迁移脚本

def test_fix_table_registers_0914():
    got, note, applied = bt.corrected_data_date("day_2026-09-14.json", "2026-09-11")
    assert (got, applied) == ("2026-09-14", True) and "2026-09-11 -> 2026-09-14" in note
    assert bt.corrected_data_date("day_2026-09-14.json", "2026-09-14") == ("2026-09-14", None, False)
    got, note, applied = bt.corrected_data_date("day_2026-09-14.json", "2026-09-10")
    assert got == "2026-09-10" and not applied and "文件被动过" in note


def test_migrate_tool_takes_from_to_dates():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "tools"))
    import migrate_paper_sigdate as mg                           # noqa: PLC0415
    state = {"signals": [
        {"id": "cuosha:A:2026-09-11", "cat": "cuosha", "code": "A", "sig_date": "2026-09-11",
         "cand": {"code": "A", "price": 2.0}},                    # 只等 09-14 价 -> 迁
        {"id": "dip:B:2026-09-11", "cat": "dip", "code": "B", "sig_date": "2026-09-11",
         "cand": {"code": "B", "price": 5.0}},                    # 两天同价 -> 不动
        {"id": "dip:C:2026-08-21", "cat": "dip", "code": "C", "sig_date": "2026-08-21",
         "cand": {"code": "C", "price": 2.0}},                    # 老默认那天 -> 本次不看
    ]}
    r = mg.classify(state, {"B": 5.0}, {"A": 2.0, "B": 5.0, "C": 2.0}, "2026-09-11", "2026-09-14")
    assert [x["code"] for x in r["move"]] == ["A"] and r["move"][0]["id_new"] == "cuosha:A:2026-09-14"
    assert [x["code"] for x in r["keep"]] == ["B"]
    # 默认参数不变: 08-21 -> 08-24 (老单测钉着), 09-11 那两条这次不在视野里
    r0 = mg.classify(state, {}, {"C": 2.0})
    assert [x["code"] for x in r0["move"]] == ["C"] and r0["move"][0]["id_new"] == "dip:C:2026-08-24"
    # CLI: --from-date/--to-date 真跑 --apply (一次性临时账本)
    repo = tempfile.mkdtemp(prefix="ddmg_")
    hist = os.path.join(repo, "dashboard", "history")
    os.makedirs(hist)
    os.makedirs(os.path.join(repo, "data"))
    for day, cands in (("2026-09-11", [{"code": "B", "price": 5.0}]),
                       ("2026-09-14", [{"code": "A", "price": 2.0}, {"code": "B", "price": 5.0}])):
        with open(os.path.join(hist, "day_%s.json" % day), "w", encoding="utf-8") as f:
            json.dump({"candidates": cands}, f)
    ledger = os.path.join(repo, "data", "paper_portfolio.json")
    with open(ledger, "w", encoding="utf-8") as f:
        json.dump(state, f)
    argv = sys.argv
    try:
        sys.argv = ["migrate_paper_sigdate.py", "--repo", repo, "--from-date", "2026-09-11",
                    "--to-date", "2026-09-14"]
        assert mg.main() == 0                                    # dry-run 不写
        after = json.load(open(ledger, encoding="utf-8"))
        assert after["signals"][0]["sig_date"] == "2026-09-11"
        sys.argv += ["--apply"]
        assert mg.main() == 0
    finally:
        sys.argv = argv
    after = {s["code"]: s for s in json.load(open(ledger, encoding="utf-8"))["signals"]}
    assert after["A"]["sig_date"] == "2026-09-14" and after["A"]["id"] == "cuosha:A:2026-09-14"
    assert after["A"]["sig_date_migrated_from"] == "2026-09-11"
    assert after["B"]["sig_date"] == "2026-09-11" and after["C"]["sig_date"] == "2026-08-21"
    assert os.listdir(os.path.join(repo, "data", "backups")), "--apply 必须先备份"
