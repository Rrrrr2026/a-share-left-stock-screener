#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
看板 meta 的交易日历字段 / 数据源留痕 / 历史清单 data_dates (2026-10-04 卡 A-HOLIDAY) —— 离线, 零联网
=====================================================================================================
看板原来分不清「休市」与「没跑」(国庆 10-01..10-07 流水线每个工作日照跑, 数据日停在 09-30, 红条「定时任务可能失败」
从 10-05 00:00 起误亮)。流水线这一侧补三样, 都经 run_log.extra_json -> export meta:

  ① `ashare.datadate.calendar_fields`: last_closed_day / next_open_day / market_status —— **只写日历精确值, 取不到
     一律 None** (按工作日近似会把假期当开市日, 正好把要消的误报写回去); 'holiday' 要先证明日历覆盖到今天;
     `calendar_meta` 薄封装绝不抛。
  ② `run_pipeline.run_sources`: 这一轮实际用的数据源 (页头「数据源」标签从 meta 读, 不再写死 akshare)。
  ③ `export_data.history_data_dates`: history/index.json 的 data_dates {跑批日: 数据日} (日期选择器显示数据日)。
外加: db 迁移 (老库补 extra_json 列)、老 run_log 没有 extra 时三个日历键照样写出 (None)、run_pipeline 接线契约。

时钟全部注入 (now=...), 不读真实的今天。看板那一侧的判定在 tests/test_fresh_banner.py。
运行: python -X utf8 -m pytest -c ../stock-core/pytest.ini --rootdir . tests/test_calendar_meta.py -q
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sqlite3
import sys
import tempfile

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ashare.market as amkt                                     # noqa: E402,F401 (注入 Market)
from ashare import datadate as dd                                # noqa: E402
from ashare import datasource as ds                              # noqa: E402
from ashare import db                                            # noqa: E402
from ashare import earnings_cal                                  # noqa: E402
from ashare import export_data as ex                             # noqa: E402
from ashare.config import CONFIG                                 # noqa: E402
from leftside_core import backtest as bt                         # noqa: E402
from leftside_core.market import Market, current, set_market     # noqa: E402
import run_pipeline as rp                                        # noqa: E402

BJ = dt.timezone(dt.timedelta(hours=8))
CEST = dt.timezone(dt.timedelta(hours=2))
# 2026 国庆前后的真实开市日 (Tushare trade_cal 10-04 实测): 10-01..10-07 休市, 10-08 周四复市, 10-10/11 周末
CAL = ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29",
       "2026-09-30", "2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13", "2026-10-14"]


def _cal(a, b):
    return [d for d in CAL if a <= d <= b]


# ---------------------------------------------------------------- ① calendar_fields (纯函数)

@pytest.mark.parametrize("now,exp", [
    # 服务器 11:30 CEST 跑批 = 北京 17:30 (收盘后)。长假里的工作日
    (dt.datetime(2026, 10, 5, 11, 30, tzinfo=CEST), ("2026-09-30", "2026-10-08", "holiday")),
    (dt.datetime(2026, 10, 7, 11, 30, tzinfo=CEST), ("2026-09-30", "2026-10-08", "holiday")),
    # 长假里的周末 (PC 手工跑): 周末不靠日历也确定
    (dt.datetime(2026, 10, 3, 16, 0, tzinfo=BJ), ("2026-09-30", "2026-10-08", "weekend")),
    # 复市日收盘后: 当天已收盘, 下一个开市日是次日
    (dt.datetime(2026, 10, 8, 11, 30, tzinfo=CEST), ("2026-10-08", "2026-10-09", "open_day")),
    # 复市日盘中 (北京 10:00): 当天还没收盘 -> 最后收盘日仍是节前, 下一个出新收盘的交易日 = 当天
    (dt.datetime(2026, 10, 8, 10, 0, tzinfo=BJ), ("2026-09-30", "2026-10-08", "open_day")),
    # 普通周五收盘后 -> 下一个开市日跨周末到周一
    (dt.datetime(2026, 10, 9, 17, 30, tzinfo=BJ), ("2026-10-09", "2026-10-12", "open_day")),
    # 普通周六
    (dt.datetime(2026, 10, 10, 12, 0, tzinfo=BJ), ("2026-10-09", "2026-10-12", "weekend")),
    # 节前最后一个交易日收盘后: 下一个开市日隔了整个长假
    (dt.datetime(2026, 9, 30, 17, 30, tzinfo=BJ), ("2026-09-30", "2026-10-08", "open_day")),
])
def test_calendar_fields_with_exact_calendar(now, exp):
    got = dd.calendar_fields(now, _cal)
    assert (got["last_closed_day"], got["next_open_day"], got["market_status"]) == exp, got
    assert set(got) == set(dd.CALENDAR_KEYS)


def test_calendar_fields_one_call_with_window_around_today():
    """一次 trade_cal: [最后收盘候选日 − 30 天, 今天 + CAL_FWD_DAYS 天] —— 往后看是为了拿下一个开市日 + 证明日历覆盖到今天。"""
    calls = []

    def fn(a, b):
        calls.append((a, b))
        return _cal(a, b)
    dd.calendar_fields(dt.datetime(2026, 10, 5, 17, 30, tzinfo=BJ), fn)
    assert calls == [("2026-09-05", "2026-10-25")], calls
    assert dd.CAL_FWD_DAYS == 20
    calls.clear()
    dd.calendar_fields(dt.datetime(2026, 10, 5, 9, 0, tzinfo=BJ), fn)            # 盘前: 候选日是昨天
    assert calls == [("2026-09-04", "2026-10-25")], calls


def test_calendar_fields_never_writes_weekday_approximation():
    """日历取不到 -> 三个字段 None (周末除外)。绝不按工作日近似: 长假里近似会把 10-05 当开市日, 看板就会拿
    `data_date < last_closed_day` 亮红条 —— 正是这张卡要消的误报。"""
    hol_weekday = dt.datetime(2026, 10, 5, 17, 30, tzinfo=BJ)
    none3 = {"last_closed_day": None, "next_open_day": None, "market_status": None}

    def boom(a, b):
        raise RuntimeError("trade_cal 500")
    assert dd.calendar_fields(hol_weekday, boom) == none3
    assert dd.calendar_fields(hol_weekday, None) == none3
    assert dd.calendar_fields(hol_weekday, lambda a, b: None) == none3           # 路径未启用 (源开关不是 tushare)
    assert dd.calendar_fields(hol_weekday, lambda a, b: []) == none3
    # 对照: 同一时刻 last_closed_trading_day 的近似给的是 10-05 (exact=False) —— 这个值绝不能进 meta
    assert dd.last_closed_trading_day(hol_weekday, boom) == ("2026-10-05", False)
    # 周末不靠日历也确定
    sat = dt.datetime(2026, 10, 10, 12, 0, tzinfo=BJ)
    assert dd.calendar_fields(sat, boom) == dict(none3, market_status="weekend")
    assert dd.calendar_fields(sat, None)["market_status"] == "weekend"


def test_calendar_fields_holiday_needs_coverage_proof():
    """镜像日历只填到今天之前 (年末下一年日历没入库 / 表滞后): 今天不在开市日列表里**证明不了**是休市 ->
    market_status None、next_open_day None; last_closed_day 仍是日历给的最后一个开市日 (那是确定的)。"""
    short = [d for d in CAL if d <= "2026-09-30"]
    got = dd.calendar_fields(dt.datetime(2026, 10, 5, 17, 30, tzinfo=BJ), lambda a, b: [d for d in short if a <= d <= b])
    assert got == {"last_closed_day": "2026-09-30", "next_open_day": None, "market_status": None}
    # 日历只给了未来的日子 (不该发生): 当没取到
    fut = ["2026-10-08", "2026-10-09"]
    got = dd.calendar_fields(dt.datetime(2026, 10, 5, 17, 30, tzinfo=BJ), lambda a, b: fut)
    assert got == {"last_closed_day": None, "next_open_day": None, "market_status": None}


def test_calendar_fields_tolerates_noise_in_calendar_answer():
    noisy = ["2026-09-30", "2026-09-30", None, "20261008", "2026-10-08T00:00:00", "2026-10-09"]
    got = dd.calendar_fields(dt.datetime(2026, 10, 5, 17, 30, tzinfo=BJ), lambda a, b: noisy)
    assert got == {"last_closed_day": "2026-09-30", "next_open_day": "2026-10-08", "market_status": "holiday"}


# ---------------------------------------------------------------- calendar_meta (薄封装, 绝不抛)

class _TmpMarket:
    def __init__(self, **kw):
        self.kw = kw

    def __enter__(self):
        try:
            self._saved = current()
        except Exception:                                    # noqa: BLE001
            self._saved = None
        self.d = tempfile.mkdtemp(prefix="calm_")
        base = dict(name="ashare", dashboard_dir=self.d, data_dir=self.d, db_path=os.path.join(self.d, "x.db"))
        base.update(self.kw)
        set_market(Market(**base))
        return self

    def __exit__(self, *exc):
        if self._saved is not None:
            set_market(self._saved)
        return False


def test_calendar_meta_reads_market_hook_and_logs_one_line(caplog):
    now = dt.datetime(2026, 10, 5, 11, 30, tzinfo=CEST)
    with _TmpMarket(trading_days=_cal), caplog.at_level(logging.INFO, logger="ashare.datadate"):
        got = dd.calendar_meta(now)
    assert got == {"last_closed_day": "2026-09-30", "next_open_day": "2026-10-08", "market_status": "holiday"}
    lines = [r.getMessage() for r in caplog.records if "交易日历字段" in r.getMessage()]
    assert lines == ["交易日历字段: last_closed_day=2026-09-30 next_open_day=2026-10-08 market_status=holiday"], lines


def test_calendar_meta_never_raises(monkeypatch, caplog):
    now = dt.datetime(2026, 10, 5, 11, 30, tzinfo=CEST)
    none3 = {"last_closed_day": None, "next_open_day": None, "market_status": None}

    def boom(a, b):
        raise TimeoutError("镜像滴流")
    with _TmpMarket(trading_days=boom), caplog.at_level(logging.WARNING, logger="ashare.datadate"):
        assert dd.calendar_meta(now) == none3
    assert any("开市日历取失败" in r.getMessage() for r in caplog.records)
    with _TmpMarket():                                         # Market 没有 trading_days 钩子
        assert dd.calendar_meta(now) == none3
    # calendar_fields 自己炸了 (不该发生) 也不许把出榜拖垮
    monkeypatch.setattr(dd, "calendar_fields", lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))
    with _TmpMarket(trading_days=_cal):
        assert dd.calendar_meta(now) == none3


# ---------------------------------------------------------------- ② run_sources (数据源留痕)

def test_run_sources_em_direct_snapshot():
    spot = pd.DataFrame({"code": ["600000", "000001"], "industry": ["银行Ⅱ", "银行Ⅱ"], "pe_ttm": [6.0, 5.0],
                         "total_mv": [3e11, 2e11]})
    saved = CONFIG["source"].get("bars")
    CONFIG["source"]["bars"] = "tushare"
    try:
        got = rp.run_sources(spot)
    finally:
        CONFIG["source"]["bars"] = saved
    assert got["bars"] == "tushare" and isinstance(got["bars_from_store"], bool)
    assert (got["spot"], got["valuation"], got["industry"]) == ("东财直连", "东财", "东财")
    assert got["industry_basis"] == "em_f100" and got["pe_basis"] == ds.PE_BASIS == "ttm"


def test_run_sources_sina_with_tushare_fallbacks_and_failures():
    spot = pd.DataFrame({"code": ["600000"], "price": [10.0], "pe_ttm": [6.0], "total_mv": [3e11], "industry": ["银行Ⅱ"]})
    spot.attrs.update(spot_source="新浪", valuation_source="tushare_daily_basic", industry_source="tushare_stock_basic")
    got = rp.run_sources(spot)
    assert (got["spot"], got["valuation"], got["industry"]) == ("新浪", "tushare_daily_basic", "tushare_stock_basic")
    # 快照没拿到 / 传进来的不是表: 不抛, 取不到的键是 None, 口径键照写
    for bad in (None, pd.DataFrame(), object()):
        got = rp.run_sources(bad)
        assert (got["spot"], got["valuation"], got["industry"]) == (None, None, None), bad
        assert got["industry_basis"] == "em_f100" and got["pe_basis"] == "ttm"


def test_run_pipeline_wires_calendar_and_sources_into_run_log():
    """接线契约: resolve_data_date 那一行原样在 (test_data_date 也锁), 其后算日历 + 来源并经 extra 进 run_log。"""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "run_pipeline.py"),
               encoding="utf-8").read()
    i = src.index("resolve_data_date(_bench, run_date)")
    j = src.index('run_extra = {"calendar": _dd.calendar_meta(), "sources": run_sources(spot)}')
    k = src.index("scan_basis=scan_basis, extra=run_extra)")
    m = src.index("ex.write_dashboard_js(run_date)")
    assert i < j < k < m


# ---------------------------------------------------------------- run_log.extra_json -> export meta

class _TmpDb:
    def __enter__(self):
        self.tmp = tempfile.mkdtemp(prefix="calx_")
        self.hist = os.path.join(self.tmp, "history")
        os.makedirs(self.hist)
        self._saved = (db.DB_PATH, ex.HISTORY_DIR, ds.fetch_benchmark_close, bt._paths, CONFIG["source"]["use_cache"],
                       earnings_cal.fetch_appoint_map, ds.fetch_long_hist)
        db.DB_PATH = os.path.join(self.tmp, "ashare.db")
        ex.HISTORY_DIR = self.hist
        ds.fetch_benchmark_close = lambda: None
        bt._paths = lambda: (self.hist, os.path.join(self.tmp, "bt.js"), os.path.join(self.tmp, "bt.json"))
        CONFIG["source"]["use_cache"] = False
        # build_payload 顺带做的三件联网的事 (财报预约日 / 错杀股长历史 / 错杀股新闻) 与本文件无关, 全掐掉:
        # 不掐的话每次 build_payload 要白等 ~16 秒的联网超时 (同目录另两条走这条链的用例就是这么慢的)。
        earnings_cal.fetch_appoint_map = lambda *a, **k: {}
        ds.fetch_long_hist = lambda *a, **k: None
        return self

    def __exit__(self, *exc):
        (db.DB_PATH, ex.HISTORY_DIR, ds.fetch_benchmark_close, bt._paths, CONFIG["source"]["use_cache"],
         earnings_cal.fetch_appoint_map, ds.fetch_long_hist) = self._saved
        return False


EXTRA = {"calendar": {"last_closed_day": "2026-09-30", "next_open_day": "2026-10-08", "market_status": "holiday"},
         "sources": {"bars": "tushare", "bars_from_store": True, "spot": "新浪", "valuation": "tushare_daily_basic",
                     "industry": "tushare_stock_basic", "industry_basis": "em_f100", "pe_basis": "ttm"}}


def _log(run, data, extra=None):
    db.log_run(run, run + " 11:30:05", run + " 12:50:11", 4881, 1, ["银行"], "ok", data_date=data,
               n_pool_raw=4906, scan_basis="store_universe", extra=extra)
    db.save_tech(run, [{"code": "600489", "name": "中金黄金", "industry": "贵金属", "price": 20.0, "tech_score": 80,
                        "tag": "深跌抄底", "dip": 1}])
    db.save_final(run, [{"code": "600489", "name": "中金黄金", "industry": "贵金属", "rank": 1, "tag": "深跌抄底",
                         "total_score": 80}])


def test_export_meta_carries_calendar_and_sources():
    with _TmpDb():
        db.init_db()
        _log("2026-10-05", "2026-09-30", EXTRA)
        meta = ex.build_payload("2026-10-05")["meta"]
        assert (meta["run_date"], meta["data_date"], meta["updated_at"]) == ("2026-10-05", "2026-09-30", "2026-10-05 12:50:11")
        assert (meta["last_closed_day"], meta["next_open_day"], meta["market_status"]) == ("2026-09-30", "2026-10-08", "holiday")
        assert meta["sources"] == EXTRA["sources"]
        # 老口径键原样还在 (看板的回退判断与数据源兜底要用)
        assert meta["scan_basis"] == "store_universe" and meta["pe_basis"] == "ttm" and meta["industry_basis"] == "em_f100"
        # 历史快照 / dashboard_data.js 是同一份 build_payload
        snap = json.load(open(ex.write_history_snapshot("2026-10-05"), encoding="utf-8"))
        assert snap["meta"]["next_open_day"] == "2026-10-08" and snap["meta"]["market_status"] == "holiday"


def test_export_meta_keys_present_but_null_without_extra():
    """老 run_log (没有 extra) / 日历取不到: 三个日历键与 sources 照样写出, 值 None -> 看板回退到跑批新鲜度判断。"""
    with _TmpDb():
        db.init_db()
        _log("2026-10-02", "2026-09-30", None)
        meta = ex.build_payload("2026-10-02")["meta"]
        for k in ("last_closed_day", "next_open_day", "market_status", "sources"):
            assert k in meta and meta[k] is None, (k, meta.get(k))
        _log("2026-10-05", "2026-09-30", {"calendar": {"last_closed_day": None, "next_open_day": None,
                                                       "market_status": None}, "sources": None})
        meta = ex.build_payload("2026-10-05")["meta"]
        assert [meta[k] for k in ("last_closed_day", "next_open_day", "market_status", "sources")] == [None] * 4
        # extra_json 被写坏 (不是 JSON / 不是对象) 也不抛
        with db.get_conn() as conn:
            conn.execute("UPDATE run_log SET extra_json=? WHERE run_date=?", ("[1,2", "2026-10-05"))
        assert ex.build_payload("2026-10-05")["meta"]["last_closed_day"] is None
        with db.get_conn() as conn:
            conn.execute("UPDATE run_log SET extra_json=? WHERE run_date=?", ('["x"]', "2026-10-05"))
        assert ex.build_payload("2026-10-05")["meta"]["sources"] is None


def test_db_migration_adds_extra_json_to_old_run_log():
    """服务器 / PC 上现成的库没有 extra_json 列: init_db 的迁移补上, 老行原样; 没迁移的库上 log_run 也不炸 (那一键被丢弃)。"""
    with _TmpDb():
        conn = sqlite3.connect(db.DB_PATH)
        conn.execute("CREATE TABLE run_log(run_date TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT, n_scanned INTEGER, "
                     "n_hit INTEGER, selected_industries TEXT, status TEXT, message TEXT, data_date TEXT, "
                     "n_pool_raw INTEGER, scan_basis TEXT)")
        conn.execute("INSERT INTO run_log VALUES('2026-10-02','a','2026-10-02 12:09:05',4881,277,'[]','ok','','2026-09-30',4906,'store_universe')")
        conn.commit()
        conn.close()
        db.log_run("2026-10-03", "a", "b", 1, 1, [], "ok", data_date="2026-09-30", extra=EXTRA)      # 迁移前: 不抛
        assert "extra_json" not in (db.fetch_run_log("2026-10-03") or {})
        db.init_db()
        old = db.fetch_run_log("2026-10-02")
        assert old["data_date"] == "2026-09-30" and old["extra_json"] is None
        db.log_run("2026-10-05", "a", "2026-10-05 12:50:11", 1, 1, [], "ok", data_date="2026-09-30", extra=EXTRA)
        assert json.loads(db.fetch_run_log("2026-10-05")["extra_json"]) == EXTRA


# ---------------------------------------------------------------- ③ history/index.json 的 data_dates

def _snap(hist, run, data, head_pad=0):
    meta = {"run_date": run}
    if data is not None:
        meta["data_date"] = data
    meta["updated_at"] = run + " 12:50:00"
    meta["disclaimer"] = "x" * head_pad
    with open(os.path.join(hist, "day_%s.json" % run), "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "industries": [], "candidates": [], "columns": []}, f, ensure_ascii=False)


def test_history_data_dates_from_known_prev_and_file_heads():
    with _TmpMarket() as m:
        hist = m.d
        _snap(hist, "2026-10-01", "2026-09-30")            # 休市日照跑
        _snap(hist, "2026-09-30", "2026-09-30")
        _snap(hist, "2026-09-29", None)                    # 极老的快照: meta 没有 data_date -> 不进表
        _snap(hist, "2026-09-28", "2026-09-29")            # 数据日晚于跑批日 = 文件被动过 -> 不采信
        dates = ["2026-10-02", "2026-10-01", "2026-09-30", "2026-09-29", "2026-09-28", "2026-09-25"]
        got = ex.history_data_dates(hist, dates, prev={"2026-09-25": "2026-09-25", "2026-10-01": "2026-09-29"},
                                    known={"2026-10-02": "2026-09-30"})
    assert got == {"2026-10-02": "2026-09-30",             # known: 本轮刚写的那份
                   "2026-10-01": "2026-09-29",             # prev 优先于读文件 (不重读已知的)
                   "2026-09-30": "2026-09-30",             # 读文件开头
                   "2026-09-25": "2026-09-25"}, got        # 文件不在盘上, prev 里有 -> 沿用


def test_history_data_dates_applies_snapshot_fix_table():
    """错标过的两份快照 (07-01 存成 07-01 实为 06-30 / 08-24 存成 08-21 实为 08-24) 走修正表 —— 与回放 / 模拟盘同一口径。"""
    with _TmpMarket() as m:
        hist = m.d
        _snap(hist, "2026-07-01", "2026-07-01")
        _snap(hist, "2026-08-24", "2026-08-21")
        got = ex.history_data_dates(hist, ["2026-08-24", "2026-07-01"])
    assert got == {"2026-08-24": "2026-08-24", "2026-07-01": "2026-06-30"}, got


def test_write_history_snapshot_writes_data_dates_into_index():
    with _TmpDb() as t:
        db.init_db()
        _snap(t.hist, "2026-10-01", "2026-09-30")
        _snap(t.hist, "2026-09-30", "2026-09-30")
        json.dump({"dates": ["2026-10-01", "2026-09-30"], "hits": {"2026-10-01": 277, "2026-09-30": 280}},
                  open(os.path.join(t.hist, "index.json"), "w"))                   # 老清单: 没有 data_dates
        _log("2026-10-02", "2026-09-30", EXTRA)
        ex.write_history_snapshot("2026-10-02")
        idx = json.load(open(os.path.join(t.hist, "index.json"), encoding="utf-8"))
        assert idx["dates"] == ["2026-10-02", "2026-10-01", "2026-09-30"]
        assert idx["hits"] == {"2026-10-02": 1, "2026-10-01": 277, "2026-09-30": 280}      # 老键原样
        assert idx["data_dates"] == {"2026-10-02": "2026-09-30", "2026-10-01": "2026-09-30", "2026-09-30": "2026-09-30"}
        # data_dates 算崩了也不许拖垮快照 / 清单: 沿用上一版里还在的那几天
        saved = ex.history_data_dates
        ex.history_data_dates = lambda *a, **k: (_ for _ in ()).throw(OSError("disk"))
        try:
            _log("2026-10-05", "2026-09-30", EXTRA)
            assert ex.write_history_snapshot("2026-10-05")
        finally:
            ex.history_data_dates = saved
        idx = json.load(open(os.path.join(t.hist, "index.json"), encoding="utf-8"))
        assert idx["dates"][0] == "2026-10-05" and "2026-10-05" not in idx["data_dates"]
        assert idx["data_dates"]["2026-10-02"] == "2026-09-30"


def test_history_data_dates_reads_only_the_file_head():
    """meta 在我们自己写的快照里排最前, 只读开头 4KB (首次部署补读 ~90 个 1MB 文件不该整份解析)。"""
    with _TmpMarket() as m:
        _snap(m.d, "2026-10-01", "2026-09-30")
        opened = []
        real_open = open

        class _Spy:
            def __init__(self, f):
                self.f = f

            def read(self, n=-1):
                opened.append(n)
                return self.f.read(n)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.f.close()
                return False
        ex_open = lambda p, *a, **k: _Spy(real_open(p, *a, **k))             # noqa: E731
        saved = ex.__dict__.get("open")
        ex.open = ex_open
        try:
            got = ex.history_data_dates(m.d, ["2026-10-01"])
        finally:
            if saved is None:
                del ex.open
            else:
                ex.open = saved
    assert got == {"2026-10-01": "2026-09-30"} and opened == [4096], opened
