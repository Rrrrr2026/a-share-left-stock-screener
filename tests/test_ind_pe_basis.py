#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PE 切真 TTM + 行业口径统一 的离线自测 (不联网; 2026-09-23 卡 IND-PE, 老板拍板 ① 行业口径 = 东财全市场分类 ② PE = 真 TTM)。

改前: 东财直连快照把 f9 (动态市盈率) 装进 pe_ttm, 看板/优质榜标签却写 PE-TTM; 兜底日 Tushare daily_basic 给的是真 TTM ——
同一列两种口径 (校验员合并 5556 只可比: 门槛 0<pe<31 有 163 只由不过变过 / 332 只由过变不过, 中位 TTM/动态 1.088)。
行业归属: quality._industry_map_ex ① 东财成分口径 (3-13 个一级行业) / run_pipeline 成分股元组带一级名、并池票带 f100 二级名,
一份榜里两套口径混着。

改后 (本文件锁住的):
  (a) 东财直连字段表含 f115 且 f115 → pe_ttm, f9 → pe_dynamic (EM_SPOT_COLUMNS); 日志带 PE 口径自检 (f115/f9 非空数、中位比)。
  (b) akshare 东财 (只有市盈率-动态) / 新浪 (老版「市盈率」) 兜底**不把动态当 TTM**: pe_ttm 缺 → Tushare daily_basic 只补 pe_ttm,
      已有的 pb/市值不碰, pe_dynamic 留痕不动; 东财直连 f115 整列空 (字段号错/源改了) 同样由 Tushare 补, 绝不拿 f9 顶。
  (c) 行业归属 (run_pipeline): 候选池元组的 industry 一律来自快照 industry 列 (spot_industry_of), 成分口径行业名不再写进去;
      行业 PE 中位按快照 industry 列分组 (industry_groups_from_spot); 景气分查表 二级名 → 去后缀一级名 (prosperity_for)。
      quality 那边「成分 5000 只也不选」的用例在 test_quality_spot_fallback.py。
  (d) 留痕: spot_sources / fill_spot_valuation 每条路都带 pe_basis="ttm"; 看板 dashboard_data.js 与历史快照 day_<日>.json 的
      meta 带 pe_basis / industry_basis; 优质榜 meta 与 history/quality_<日>.json 的在 test_quality_spot_fallback.py。
  (e) PE_TTM_COLUMN_NAMES 里没有会子串命中「市盈率-动态」的名 (rename_normalize 是子串匹配)。

变异 (隔离副本树逐个跑): f115 改回 f9 / 动态塞进 pe_ttm (akshare 东财 与 新浪) / f115 空时拿 f9 顶 / 成分口径复活 /
别名表删空 / pe_basis 缺失 / Tushare 兜底关掉 / 候选池归属改回成分名 / 行业 PE 中位改回成分分组。
运行:  python -m pytest tests/test_ind_pe_basis.py -q
"""
from __future__ import annotations
import json
import logging
import os
import sys
import tempfile
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ashare import datasource as ds                    # noqa: E402
from ashare import module3_fundamentals as m3          # noqa: E402
from ashare.config import CONFIG                       # noqa: E402
import run_pipeline as rp                              # noqa: E402
from test_quality_spot_fallback import (               # noqa: E402  (同目录用例模块; 夹具 import 进来即可用)
    CODES, D0, ts_box, _daily_basic, _em_spot, _sina_spot, _msgs,   # noqa: F401
)


# ============================================================ (a) 东财直连: f115 → pe_ttm, f9 → pe_dynamic
def _em_rows():
    """东财 clist 的 diff 行 (真实字段号): 艾力斯 f9 14.9 / f115 18.9 (09-21/23 实测量级), 亏损股 "-", 第三只 TTM 高于动态。"""
    base = {"f2": 60.0, "f3": 1.0, "f5": 1e6, "f6": 6e7, "f8": 2.0, "f10": 1.5, "f15": 61.0, "f16": 59.0,
            "f20": 5.06e10, "f21": 4.0e10, "f23": 5.9}
    return [{**base, "f12": "688578", "f14": "艾力斯", "f9": 14.9, "f115": 18.9, "f100": "化学制药"},
            {**base, "f12": "300001", "f14": "亏损股", "f9": "-", "f115": "-", "f100": "-"},
            {**base, "f12": "600519", "f14": "贵州茅台", "f9": 10.0, "f115": 20.0, "f100": "白酒Ⅱ"}]


def test_em_direct_field_table_and_mapping(monkeypatch, caplog):
    assert ds.EM_SPOT_COLUMNS["f115"] == "pe_ttm" and ds.EM_SPOT_COLUMNS["f9"] == "pe_dynamic"
    assert [k for k, v in ds.EM_SPOT_COLUMNS.items() if v == "pe_ttm"] == ["f115"]       # 只有 f115 能进 pe_ttm
    seen = []

    def fake_em_get(path, params, timeout=8):
        seen.append((path, dict(params)))
        return {"total": 3, "diff": _em_rows()} if params["pn"] == 1 else {"diff": []}
    monkeypatch.setattr(ds, "_em_get", fake_em_get)
    with caplog.at_level(logging.INFO, logger="ashare.datasource"):
        df = ds._spot_from_em_direct()
    assert seen and seen[0][0] == "/api/qt/clist/get"
    fields = seen[0][1]["fields"].split(",")
    assert "f115" in fields and "f9" in fields and "f100" in fields
    d = df.set_index("code")
    assert list(d.loc[["688578", "600519"], "pe_ttm"]) == [18.9, 20.0] and np.isnan(d.loc["300001", "pe_ttm"])
    assert list(d.loc[["688578", "600519"], "pe_dynamic"]) == [14.9, 10.0] and np.isnan(d.loc["300001", "pe_dynamic"])
    assert not (d["pe_ttm"].dropna() == d["pe_dynamic"].dropna()).any()                  # 两列真是两回事
    assert d.loc["688578", "industry"] == "化学制药" and pd.isna(d.loc["300001", "industry"]) and d.loc["600519", "pb"] == 5.9
    assert ds.spot_source_of(df) == "东财直连" and not ds.spot_lacks_valuation(df) and ds.spot_missing_valuation_cols(df) == []
    msg = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("东财快照(直连"))
    assert "PE 口径 ttm (f115 非空 2, f9 动态非空 2, 两者都有 2 只 TTM/动态 中位 1.634)" in msg      # (18.9/14.9=1.268, 20/10=2) 中位 1.634
    # 东财直连齐全 → 出口原样, 零 Tushare
    assert ds.spot_sources(df) == {"spot_source": "东财直连", "valuation_source": "东财", "pe_basis": "ttm"}


def test_em_direct_untouched_by_fallbacks_and_zero_tushare(ts_box, monkeypatch):
    monkeypatch.setattr(ds, "_em_get", lambda path, params, timeout=8:
                        {"total": 3, "diff": _em_rows()} if params["pn"] == 1 else {"diff": []})
    df = ds._spot_from_em_direct()
    out = ds._spot_with_fallbacks(df)
    assert out is df and ts_box.calls == []
    assert list(out.set_index("code").loc[["688578", "600519"], "pe_ttm"]) == [18.9, 20.0]


# ============================================================ (b) 兜底源不把动态当 TTM
def _ak_em_raw(codes, pe_dyn=42.65, pb=7.0, mv=5.0e10):
    """akshare stock_zh_a_spot_em 现在的列: 只有「市盈率-动态」, 没有 TTM。"""
    n = len(codes)
    return pd.DataFrame({"代码": list(codes), "名称": [f"N{c}" for c in codes], "最新价": [50.0] * n, "涨跌幅": [0.5] * n,
                         "成交量": [1e6] * n, "成交额": [5e7] * n, "最高": [51.0] * n, "最低": [49.0] * n, "量比": [1.1] * n,
                         "换手率": [1.2] * n, "市盈率-动态": [pe_dyn] * n, "市净率": [pb] * n, "总市值": [mv] * n,
                         "流通市值": [mv * 0.8] * n})


def test_akshare_em_fallback_dynamic_stays_out_of_pe_ttm_and_tushare_fills_only_pe(ts_box, monkeypatch, caplog):
    monkeypatch.setattr(ds, "_em_realtime_down", False)
    monkeypatch.setattr(ds, "_ak", lambda: SimpleNamespace(stock_zh_a_spot_em=lambda: _ak_em_raw(CODES)))
    df = ds._spot_from_em()
    assert "pe_ttm" not in df.columns and (df["pe_dynamic"] == 42.65).all()
    assert ds.spot_source_of(df) == "东财(akshare)" and ds.spot_missing_valuation_cols(df) == ["pe_ttm"]
    with caplog.at_level(logging.INFO, logger="ashare.datasource"):
        out = ds._spot_with_fallbacks(df)
    o = out.set_index("code")
    assert o.loc["688578", "pe_ttm"] == 14.9 and (o.loc[CODES[1:], "pe_ttm"] == 15.0).all()   # 真 TTM 来自 Tushare
    assert (o["pe_dynamic"] == 42.65).all()                                                    # 动态留痕不动
    assert (o["pb"] == 7.0).all() and (o["total_mv"] == 5.0e10).all() and (o["turnover"] == 1.2).all()   # 已有的列一个数不改
    assert out.attrs["valuation_filled"] == ["pe_ttm"] and out.attrs["valuation_source"] == "tushare_daily_basic"
    assert ds.spot_sources(out) == {"spot_source": "东财(akshare)", "valuation_source": "tushare_daily_basic", "pe_basis": "ttm",
                                    "valuation_trade_date": D0, "valuation_filled": ["pe_ttm"]}
    assert ts_box.n("daily_basic") == 1
    warns = [m for m in _msgs(caplog) if "快照无估值列" in m]
    assert len(warns) == 1 and warns[0].startswith("东财(akshare)快照无估值列 (缺 pe_ttm; 动态市盈率不当 TTM 用), 改用 Tushare daily_basic 补")
    assert any("补列 pe_ttm)" in m for m in _msgs(caplog, logging.INFO))
    assert (o["industry"].dropna() == "化学制药").all()                                        # 行业列由批量映射 (Tushare 兜底) 补


def _sina_raw_old_akshare(codes, pe=30.0, pb=4.0):
    """老版 akshare 的新浪快照形态: 有「市盈率」(非 TTM) 与「市净率」, 代码带 sh/sz 前缀。"""
    n = len(codes)
    return pd.DataFrame({"代码": [("sh" if c[0] in "69" else "sz") + c for c in codes], "名称": [f"N{c}" for c in codes],
                         "最新价": [50.0] * n, "涨跌幅": [0.5] * n, "成交量": [1e6] * n, "成交额": [5e7] * n,
                         "最高": [51.0] * n, "最低": [49.0] * n, "换手率": [1.2] * n, "市盈率": [pe] * n, "市净率": [pb] * n})


def test_sina_fallback_old_pe_column_is_dynamic_not_ttm(ts_box, monkeypatch):
    monkeypatch.setattr(ds, "_ak", lambda: SimpleNamespace(stock_zh_a_spot=lambda: _sina_raw_old_akshare(CODES)))
    df = ds._spot_from_sina()
    assert "pe_ttm" not in df.columns and (df["pe_dynamic"] == 30.0).all() and df.loc[0, "code"] == "688578"
    assert ds.spot_source_of(df) == "新浪" and ds.spot_missing_valuation_cols(df) == ["pe_ttm", "total_mv"]
    out = ds._spot_with_fallbacks(df)
    o = out.set_index("code")
    assert o.loc["688578", "pe_ttm"] == 14.9 and (o["pe_dynamic"] == 30.0).all() and (o["pb"] == 4.0).all()
    assert o.loc["688578", "total_mv"] == pytest.approx(4_590_000.0 * 1e4)
    assert out.attrs["valuation_filled"] == ["pe_ttm", "total_mv", "float_mv", "volume_ratio"]   # 新浪有 换手率/市净率, 不补
    # 现在的新浪 (8 列, 无市盈率) 同样: pe_ttm 只来自 Tushare
    out2 = ds._spot_with_fallbacks(_sina_spot(CODES))
    assert "pe_dynamic" not in out2.columns and out2.set_index("code").loc["688578", "pe_ttm"] == 14.9


def test_em_direct_with_empty_f115_gets_ttm_from_tushare_never_from_f9(ts_box, caplog):
    """f115 整列空 (字段号错/源改了) 的东财直连快照: 缺 pe_ttm → Tushare 补真 TTM; f9 (pe_dynamic) 绝不被拿来顶。"""
    em = _em_spot(CODES)
    em["pe_ttm"] = np.nan
    em["pe_dynamic"] = 99.0
    assert ds.spot_missing_valuation_cols(em) == ["pe_ttm"] and ds.spot_source_of(em) == "东财直连"
    with caplog.at_level(logging.INFO, logger="ashare.datasource"):
        out = ds._spot_with_fallbacks(em)
    o = out.set_index("code")
    assert o.loc["688578", "pe_ttm"] == 14.9 and (o.loc[CODES[1:], "pe_ttm"] == 15.0).all()
    assert (o["pe_dynamic"] == 99.0).all() and (o["total_mv"] == 500e8).all() and (o["pb"] == 3.0).all()
    assert out.attrs["valuation_filled"] == ["pe_ttm"] and ds.spot_sources(out)["valuation_source"] == "tushare_daily_basic"
    assert (em["pe_ttm"].isna()).all()                                                         # 原对象不动
    assert any(m.startswith("东财直连快照无估值列 (缺 pe_ttm; 动态市盈率不当 TTM 用)") for m in _msgs(caplog))


def test_pe_ttm_column_names_never_match_dynamic():
    """rename_normalize 是子串匹配: pe_ttm 的候选名里不许有能命中「市盈率-动态」/「市盈率」的名。"""
    for name in ds.PE_TTM_COLUMN_NAMES:
        assert name not in "市盈率-动态" and name not in "市盈率", name
    assert "市盈率" not in ds.PE_TTM_COLUMN_NAMES
    df = ds.rename_normalize(pd.DataFrame({"市盈率-动态": [1.0], "市盈率": [2.0], "代码": ["1"]}),
                             {"pe_ttm": list(ds.PE_TTM_COLUMN_NAMES), "pe_dynamic": ["市盈率-动态"]})
    assert "pe_ttm" not in df.columns and list(df["pe_dynamic"]) == [1.0]
    df2 = ds.rename_normalize(pd.DataFrame({"市盈率-TTM": [3.0], "市盈率-动态": [1.0]}), {"pe_ttm": list(ds.PE_TTM_COLUMN_NAMES)})
    assert list(df2["pe_ttm"]) == [3.0]


# ============================================================ (d) 留痕: 每条路都带 pe_basis
def test_pe_basis_on_every_valuation_path(ts_box, monkeypatch):
    assert ds.PE_BASIS == "ttm"
    assert ds.spot_sources(None) == {"spot_source": "无", "valuation_source": None, "pe_basis": "ttm"}
    assert ds.fill_spot_valuation(None)[1] == {"spot_source": "无", "valuation_source": None, "pe_basis": "ttm"}
    assert ds.fill_spot_valuation(_em_spot(CODES))[1] == {"spot_source": "东财直连", "valuation_source": "东财", "pe_basis": "ttm"}
    _, info = ds.fill_spot_valuation(_sina_spot(CODES))
    assert info["pe_basis"] == "ttm" and info["valuation_source"] == "tushare_daily_basic"
    monkeypatch.setattr(ds, "fetch_valuation_tushare", lambda *a, **k: (None, {"source": None, "error": "down"}))
    same, info = ds.fill_spot_valuation(_sina_spot(CODES))
    assert info["valuation_source"] == "缺失" and info["pe_basis"] == "ttm" and "pe_ttm" not in same.columns


def test_dashboard_payload_and_history_snapshot_meta_carry_basis():
    """看板 dashboard_data.js 的 meta 与历史快照 day_<日>.json 的 meta 都带 pe_basis="ttm" / industry_basis="em_f100"
    (同一份 build_payload); 没有这两个键的历史快照按老口径解释 (DEPLOY/HANDBOOK 一句)。"""
    from ashare import db, export_data as ex, backtest as bt
    tmp = tempfile.mkdtemp(prefix="pebasis_")
    hist = os.path.join(tmp, "history")
    os.makedirs(hist)
    saved = (db.DB_PATH, ex.HISTORY_DIR, ds.fetch_benchmark_close, bt._paths, CONFIG["source"]["use_cache"])
    db.DB_PATH = os.path.join(tmp, "ashare.db")
    ex.HISTORY_DIR = hist
    ds.fetch_benchmark_close = lambda: None
    bt._paths = lambda: (hist, os.path.join(tmp, "bt.js"), os.path.join(tmp, "bt.json"))
    CONFIG["source"]["use_cache"] = False
    try:
        db.init_db()
        db.log_run("2026-09-24", "2026-09-24 10:01:25", "2026-09-24 11:14:19", 4915, 1,
                   ["银行"], "ok", data_date="2026-09-24", n_pool_raw=5158, scan_basis="store_universe")
        db.save_tech("2026-09-24", [{"code": "688578", "name": "艾力斯", "industry": "化学制药", "price": 60.0,
                                     "tech_score": 80, "tag": "深跌抄底", "dip": 1}])
        db.save_fundamental("2026-09-24", "688578", {"pe_ttm": 18.9, "pb": 5.9, "roe": 30.0})
        db.save_final("2026-09-24", [{"code": "688578", "name": "艾力斯", "industry": "化学制药", "rank": 1,
                                      "tag": "深跌抄底", "total_score": 80}])
        payload = ex.build_payload("2026-09-24")
        meta = payload["meta"]
        assert meta["pe_basis"] == "ttm" and meta["industry_basis"] == "em_f100" and meta["data_date"] == "2026-09-24"
        assert payload["candidates"][0]["pe_ttm"] == 18.9 and payload["candidates"][0]["pe_disp"] == "18.9"
        path = ex.write_history_snapshot("2026-09-24")
        snap = json.load(open(path, encoding="utf-8"))
        assert snap["meta"]["pe_basis"] == "ttm" and snap["meta"]["industry_basis"] == "em_f100"
    finally:
        db.DB_PATH, ex.HISTORY_DIR, ds.fetch_benchmark_close, bt._paths, CONFIG["source"]["use_cache"] = saved


# ============================================================ (c) run_pipeline: 归属只认快照 industry 列
def test_candidate_universe_attributes_industry_from_spot_not_from_cons(monkeypatch, caplog):
    """成分口径说 600000/000001 都是「银行」(一级); 快照 f100 说 银行Ⅱ / 股份制银行Ⅲ → 元组里必须是快照的; 成分只用来选票
    (ind_to_codes 仍是成分 {行业: [码]})。凑够 3000 只免得走并全市场池 (那条路要读快照)。"""
    monkeypatch.setitem(CONFIG["industry"], "use_full_market", False)
    filler = [f"{600100 + i:06d}" for i in range(3100)]
    cons = pd.DataFrame({"code": ["600000", "000001", "300750"] + filler,
                         "name": ["浦发银行", "平安银行", "宁德时代"] + [f"填{i}" for i in range(3100)]})
    monkeypatch.setattr(ds, "fetch_industry_cons", lambda ind: cons)
    spot_map = {"600000": {"industry": "银行Ⅱ", "price": 10.0}, "000001": {"industry": "股份制银行Ⅲ"},
                "300750": {"industry": float("nan")}, filler[0]: {"industry": "-"}, filler[1]: {"industry": " 电池 "}}
    ind_df = pd.DataFrame({"industry": ["银行"], "selected": [True]})
    with caplog.at_level(logging.INFO, logger="ashare.run"):
        uni, ind_to_codes = rp.build_candidate_universe(None, spot_map, ind_df, ["银行"])
    got = {c: i for (c, _, i) in uni}
    assert got["600000"] == "银行Ⅱ" and got["000001"] == "股份制银行Ⅲ"
    assert got["300750"] is None and got[filler[0]] is None and got[filler[1]] == "电池" and got[filler[2]] is None
    assert "银行" not in got.values()                                                       # 成分名一个都没写进归属
    assert list(ind_to_codes) == ["银行"] and len(ind_to_codes["银行"]) == 3103 and len(uni) == 3103
    assert any("只用来选票, 行业归属一律按快照 industry 列" in r.getMessage() for r in caplog.records)


def test_spot_industry_of_and_groups_and_prosperity_lookup():
    sm = {"1": {"industry": "银行Ⅱ"}, "2": {"industry": " "}, "3": {"industry": None}, "4": {"industry": "nan"}, "5": {}}
    assert rp.spot_industry_of(sm, "1") == "银行Ⅱ"
    assert all(rp.spot_industry_of(sm, k) is None for k in ("2", "3", "4", "5", "9")) and rp.spot_industry_of({}, "1") is None
    spot = pd.DataFrame({"code": ["600000", "601398", "688578", "300001", "300002"],
                         "industry": ["银行Ⅱ", "银行Ⅱ", "化学制药", None, "-"],
                         "pe_ttm": [5.0, 7.0, 18.9, 30.0, 40.0]})
    groups = rp.industry_groups_from_spot(spot)
    assert groups == {"银行Ⅱ": ["600000", "601398"], "化学制药": ["688578"]}
    assert rp.industry_groups_from_spot(None) == {} and rp.industry_groups_from_spot(spot.drop(columns=["industry"])) == {}
    med = m3.compute_industry_pe_median(spot, groups)                                      # 行业 PE 中位按同一口径分组
    assert med == {"银行Ⅱ": 6.0, "化学制药": 18.9}
    pm = {"银行": 77.0, "半导体": 60.0}
    assert rp.prosperity_for(pm, "银行Ⅱ") == 77.0 and rp.prosperity_for(pm, "银行") == 77.0
    assert rp.prosperity_for(pm, "半导体") == 60.0 and rp.prosperity_for(pm, "股份制银行Ⅲ") is None
    assert rp.prosperity_for(pm, None) is None and rp.prosperity_for({}, "银行") is None
