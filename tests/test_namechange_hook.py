#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
改名表月刷钩子 `ashare.market.fetch_namechange_rows` + 核心守卫在本仓 update 路径上的接线 (2026-10-24 卡 UNIVERSE-RULES-2)
离线自测: 零联网 (tushare_client.query 打桩), 零真等待 (时钟注入), 临时库。

  python -X utf8 -m pytest -c ../stock-core/pytest.ini --rootdir . tests/test_namechange_hook.py -q

覆盖:
  · 钩子约定: 源不指向 Tushare -> None (未启用); 源返回空 -> []; 回包 -> 六元组 (北交所 / B 股由 keep_a_code 剔掉, 日期原样
    交核心 normalize_namechange_rows 规整); 调的是 namechange 端点 + 六个字段; 抛错往外抛 (核心兜)
  · MARKET 里接上了这个钩子 (`-m ashare.pricestore update` 跑的就是它)
  · 跨仓兼容 (2026-10-25 卡 UNIVERSE-RULES-3, 校验员 MEDIUM「跨仓硬耦合」): 钩子不当构造关键字传, _attach_optional_hooks 只在核心
    Market 真有字段时接 —— 旧核心 (无字段) 下 import 不崩、不接、info 一行, 核心 getattr 容错跳过; 已有值不覆盖; 缺了能补回
  · 端到端 (真 ashare Market 克隆到临时库 + 桩钩子): 拉取日 40 天前 -> `pricestore.update_daily()` 一跑就拉, 新行只追加,
    meta namechange_pulled_at = 今天, 不写 namechange_asof; 第二跑不到期不再调
  · 美股形态 (Market 无钩子) 不调不写
  · 旧核心旁 (2026-10-25 卡 UNIVERSE-RULES-4, 校验员 verify:UNIVERSE-RULES-3 LOW 2c): 依赖新核心 (Market 有 fetch_namechange_rows 字段 /
    pricestore 有月刷) 的用例一律按 NEW_CORE 探测 skip 并说明, 只核钩子本身的用例照跑 —— DEPLOY 回滚 ① 只退 stock-core 时 PC 任务会把
    旧核心 robocopy 进 vendor, 干净导出树里这份用例不能红
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import inspect
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import types

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ashare.market as amkt                                    # noqa: E402  (注入 Market)
import leftside_core.market as lm                               # noqa: E402
from leftside_core import pricestore as ps                      # noqa: E402

FIELDS = "ts_code,name,start_date,end_date,ann_date,change_reason"
#: 旁边的核心是不是带改名表月刷的新核心 (2026-10-24 卡 UNIVERSE-RULES-2 起): Market 有 fetch_namechange_rows 字段且 pricestore 有月刷。
#: 旧核心旁 (ashare/config.py 查找顺序 STOCK_CORE_DIR -> ../stock-core -> vendor/; PC 共享树没 ff 就是这形态) 钩子按兼容约定不接,
#: 下面依赖新核心的用例 skip 并说明, 不红 (校验员 verify:UNIVERSE-RULES-3 LOW 2c: 原版在旧核心旁 3 failed / 2 errors)。
NEW_CORE = ("fetch_namechange_rows" in {f.name for f in dataclasses.fields(lm.Market)}
            and hasattr(ps, "_refresh_namechange_if_due") and hasattr(ps, "normalize_namechange_rows"))
needs_new_core = pytest.mark.skipif(
    not NEW_CORE, reason="旁边的核心 (leftside_core) 比本仓老: Market 无 fetch_namechange_rows 字段 / pricestore 无改名表月刷 —— "
                         "兼容约定下钩子不接, 这条用例只在新核心旁有意义 (ashare/config.py: STOCK_CORE_DIR -> ../stock-core -> vendor/)")


def _df(rows):
    return pd.DataFrame(rows, columns=["ts_code", "name", "start_date", "end_date", "ann_date", "change_reason"])


def test_hook_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(amkt, "_tushare_on", lambda: False)
    called = []
    import ashare.tushare_client as tsc
    monkeypatch.setattr(tsc, "query", lambda *a, **k: called.append(a) or _df([]))
    assert amkt.fetch_namechange_rows() is None
    assert called == []


def test_hook_shape_and_filters(monkeypatch):
    monkeypatch.setattr(amkt, "_tushare_on", lambda: True)
    import ashare.tushare_client as tsc
    seen = {}

    def fake_query(api, fields="", **params):
        seen["api"], seen["fields"], seen["params"] = api, fields, params
        return _df([
            ("000929.SZ", "*ST兰黄", "20250430", None, "20250429", "*ST"),
            ("600439.SH", "ST瑞贝卡", "20260901", "", "20260831", "ST"),
            ("430047.BJ", "北交所票", "20240101", "", "", "改名"),          # 北交所 -> 剔
            ("900901.SH", "云赛B", "20200101", "", "", "改名"),            # B 股 -> 剔
        ])
    monkeypatch.setattr(tsc, "query", fake_query)
    rows = amkt.fetch_namechange_rows()
    assert (seen["api"], seen["fields"], seen["params"]) == ("namechange", FIELDS, {})
    assert rows == [("000929", "*ST兰黄", "20250430", None, "20250429", "*ST"),
                    ("600439", "ST瑞贝卡", "20260901", "", "20260831", "ST")]
    # 核心规整后可直接落表 (规整函数是新核心才有的, 旧核心旁只核钩子本身)
    if NEW_CORE:
        assert ps.normalize_namechange_rows(rows) == [("000929", "*ST兰黄", "2025-04-30", "", "2025-04-29", "*ST"),
                                                      ("600439", "ST瑞贝卡", "2026-09-01", "", "2026-08-31", "ST")]
    monkeypatch.setattr(tsc, "query", lambda *a, **k: _df([]))
    assert amkt.fetch_namechange_rows() == []
    monkeypatch.setattr(tsc, "query", lambda *a, **k: None)
    assert amkt.fetch_namechange_rows() == []

    def boom(*a, **k):
        raise RuntimeError("TushareTransport")
    monkeypatch.setattr(tsc, "query", boom)
    with pytest.raises(RuntimeError):
        amkt.fetch_namechange_rows()


@needs_new_core
def test_market_wired():
    assert amkt.MARKET.fetch_namechange_rows is amkt.fetch_namechange_rows
    assert lm.current().fetch_namechange_rows is amkt.fetch_namechange_rows


def test_hook_not_passed_as_constructor_kwarg():
    """兼容的根: 钩子**不**当 Market(...) 的关键字传 (旧核心没这个字段会 TypeError), 走 _OPTIONAL_HOOKS 表。"""
    src = inspect.getsource(amkt)
    ctor = src[src.index("MARKET = "):]
    assert "fetch_namechange_rows=" not in ctor
    assert dict(amkt._OPTIONAL_HOOKS) == {"fetch_namechange_rows": amkt.fetch_namechange_rows}


def test_attach_tolerates_old_core(caplog):
    """旧核心形态: Market dataclass 没有 fetch_namechange_rows 字段 -> 不接、不崩、info 一行; 核心侧 getattr 容错 -> 月刷跳过。"""
    @dataclasses.dataclass
    class OldMarket:                      # 10-24 之前的 leftside_core.market.Market 形态 (只保留相关字段)
        name: str
        fetch_universe_rows: object = None
        log_prefix: str = "leftside_core"

    old = OldMarket(name="ashare")
    with caplog.at_level(logging.INFO, logger="ashare.market"):
        assert amkt._attach_optional_hooks(old) is old
    assert not hasattr(old, "fetch_namechange_rows")
    assert any("无字段 fetch_namechange_rows" in r.getMessage() for r in caplog.records)
    if not NEW_CORE:                          # 旧核心旁: 核心侧没有月刷可核, 到这里已经证明 import / 接钩子不崩
        return
    conn = sqlite3.connect(":memory:")
    ps._ensure_schema(conn)
    assert ps._refresh_namechange_if_due(old, conn, today="2026-10-25") is None       # 核心: 无钩子 -> None, 一行不打
    ps._after_universe_refresh(old, conn, today="2026-10-25")                           # 不抛
    assert ps.NAMECHANGE_PULLED_KEY not in ps.meta_all(conn)
    conn.close()


@needs_new_core
def test_attach_fills_missing_and_keeps_existing():
    m = dataclasses.replace(amkt.MARKET, fetch_namechange_rows=None)
    assert amkt._attach_optional_hooks(m) is m
    assert m.fetch_namechange_rows is amkt.fetch_namechange_rows
    sentinel = lambda: []                                                               # noqa: E731
    m2 = dataclasses.replace(amkt.MARKET, fetch_namechange_rows=sentinel)
    amkt._attach_optional_hooks(m2)
    assert m2.fetch_namechange_rows is sentinel                                         # 已有值不覆盖


@pytest.fixture
def tmp_market(monkeypatch):
    """真 ashare Market 克隆到临时 data_dir, 按日钩子打桩 (无新交易日), 改名表钩子记调用。"""
    tmp = tempfile.mkdtemp(prefix="ur2_hook_")
    calls = []
    m = dataclasses.replace(
        amkt.MARKET, data_dir=tmp, dashboard_dir=tmp, db_path=os.path.join(tmp, "x.db"),
        trading_days=lambda a, b: [],
        fetch_bars_by_date=lambda d: {},
        fetch_adj_by_date=lambda d: {},
        fetch_universe_rows=lambda: [("000001", "平安银行", "1991-04-03", None, "L")],
        fetch_namechange_rows=lambda: calls.append(1) or [("000929", "*ST兰黄", "20250430", "", "", "*ST")])
    saved = lm._CURRENT
    lm.set_market(m)
    conn = ps._conn()
    conn.execute("INSERT INTO bars_raw VALUES(?,?,?,?,?,?,?,?)", ("000001", "2026-10-23", 10, 10.5, 9.5, 10, 1e6, 1e7))
    conn.execute("INSERT INTO universe VALUES(?,?,?,?,?)", ("000001", "平安银行", "1991-04-03", None, "L"))
    conn.execute("INSERT INTO namechange(code,name,start_date,end_date,ann_date,change_reason) VALUES(?,?,?,?,?,?)",
                 ("000929", "ST兰黄", "2018-05-04", "", "", "ST"))
    conn.commit()
    conn.close()
    yield types.SimpleNamespace(market=m, calls=calls, tmp=tmp)
    c = ps._PIT_CONN.get("conn")
    if c is not None and str(ps._PIT_CONN.get("path", "")).startswith(tmp):
        c.close()
        ps._PIT_CONN["path"], ps._PIT_CONN["conn"] = None, None
    lm._CURRENT = saved
    shutil.rmtree(tmp, True)


@needs_new_core
def test_update_pulls_namechange_when_due(tmp_market):
    conn = ps._conn()
    ps.meta_set({ps.NAMECHANGE_PULLED_KEY: (dt.date.today() - dt.timedelta(days=40)).isoformat()}, conn)
    conn.close()
    assert ps.update_daily() == 0                   # 无新交易日那条路也要刷
    assert tmp_market.calls == [1]
    conn = ps._conn()
    meta = ps.meta_all(conn)
    assert meta[ps.NAMECHANGE_PULLED_KEY] == dt.date.today().isoformat()
    assert meta["namechange_pull_added"] == "1"
    assert "namechange_asof" not in meta
    assert conn.execute("SELECT start_date, change_reason FROM namechange WHERE code='000929' ORDER BY start_date").fetchall() \
        == [("2018-05-04", "ST"), ("2025-04-30", "*ST")]
    conn.close()
    assert ps.update_daily() == 0                   # 第二天: 不到期
    assert tmp_market.calls == [1]


@needs_new_core
def test_update_without_hook_does_not_pull(tmp_market):
    m = dataclasses.replace(tmp_market.market, fetch_namechange_rows=None)     # 美股形态
    lm.set_market(m)
    conn = ps._conn()
    ps.meta_set({ps.NAMECHANGE_PULLED_KEY: (dt.date.today() - dt.timedelta(days=40)).isoformat()}, conn)
    conn.close()
    assert ps.update_daily() == 0
    assert tmp_market.calls == []
    conn = ps._conn()
    assert ps.meta_all(conn)[ps.NAMECHANGE_PULLED_KEY] == (dt.date.today() - dt.timedelta(days=40)).isoformat()
    conn.close()
