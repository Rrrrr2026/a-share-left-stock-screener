#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
离线自测 (offline tests) —— 不联网, 用合成数据驱动真实逻辑。
覆盖: 指标 / 模块1景气(打桩datasource) / 模块2技术 / 模块4交叉。
运行:  python tests/test_offline.py     (无需 pytest)
"""
from __future__ import annotations
import os
import sys
import datetime as dt
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ashare import indicators as ind
from ashare import datasource as ds
from ashare import module1_industry as m1
from ashare import module2_tech as m2
from ashare import module3_fundamentals as m3
from ashare import module4_crossscore as m4

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ FAIL: {name}")


def gen_series(seed, base, drift, n=300, pull=True):
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    close = base + base * drift * t + base * 0.08 * np.sin(2 * np.pi * t / 55.0) \
        + rng.normal(0, base * 0.01, n).cumsum() * 0.2
    close = np.maximum(close, base * 0.3)
    if pull:
        close[-10:] = np.linspace(close[-11], close[-11] * 0.9, 10)
    dates = pd.bdate_range(end=dt.date.today(), periods=n).strftime("%Y-%m-%d")
    high = close * 1.01
    low = close * 0.99
    return pd.DataFrame({"date": dates, "open": close, "high": high, "low": low,
                         "close": close, "volume": close * 1e5,
                         "amount": np.full(n, 3e8)})


# ---------------------------------------------------------------------------
def test_indicators():
    print("[指标]")
    s = gen_series(1, 20, 0.0015)["close"]
    dif, dea, hist = ind.macd(s)
    check("MACD 长度一致", len(dif) == len(s) and len(hist) == len(s))
    r = ind.rsi(s)
    check("RSI 在 0-100", float(np.nanmax(r)) <= 100 and float(np.nanmin(r)) >= 0)
    h = gen_series(1, 20, 0.0015)
    k, d, j = ind.kdj(h["high"], h["low"], h["close"])
    check("KDJ 三线非空", k.notna().any() and d.notna().any() and j.notna().any())
    ch = ind.linreg_channel(s, 120, 2.0)
    check("通道下轨返回结构", ch is not None and "lower_band" in ch)
    piv = ind.find_pivot_lows(h["low"], 10)
    check("摆动低点可识别", len(piv) >= 1)
    check("百分位边界", ind.cumulative_return(s, 120) is not None)


def test_sina_symbol():
    print("[新浪代码前缀映射]")
    cases = {"920819": "bj920819", "830799": "bj830799", "430047": "bj430047",
             "600519": "sh600519", "688981": "sh688981", "900901": "sh900901",
             "000001": "sz000001", "300750": "sz300750", "200011": "sz200011"}
    ok = all(ds._sina_symbol(k) == v for k, v in cases.items())
    check("_sina_symbol 各板块前缀正确 (含北交所920不被误判为沪市)", ok)
    if not ok:
        for k, v in cases.items():
            got = ds._sina_symbol(k)
            if got != v:
                print(f"     {k}: got {got}, want {v}")


def test_module2():
    print("[模块2 技术扫描]")
    h = gen_series(7, 30, 0.0016)
    rec, det = m2.scan_one("000001", "测试股", h, {"volume_ratio": 1.1, "turnover": 2.0})
    check("scan_one 返回记录", rec is not None)
    check("技术分 >= 0", rec["tech_score"] >= 0)
    check("详情含 ohlc 与日期", det and len(det["ohlc"]) == len(det["dates"]))
    check("详情 MA/通道/MACD 字段齐全",
          all(kk in det for kk in ["ma60", "ma120", "ma250", "lower_band",
                                   "macd_hist", "kdj_k", "rsi", "pivot_lows"]))
    check("关键位字段存在", "support_price" in rec and "breakdown_price" in rec)
    # 数据不足应返回 None
    short = h.head(50)
    r2, _ = m2.scan_one("000002", "短数据", short, None)
    check("数据不足安全返回None", r2 is None)


def test_module4():
    print("[模块4 交叉打分]")
    # 强左侧技术分门槛 = CONFIG["cross"]["strong_left_tech"] = 2.5 (ashare/config.py:164, 2026-08-10
    # e9eba41 美股对齐重建时由 2.0 提高; 本夹具原写 2.4 是 2.0 时代的旧值, 自那天起就落在
    # "技术弱" 桶 —— 08-30 兜底桶拆分只是把它从 "🔎 观察" 改名成 "🔎 观察·技术弱", 不是回归)
    tech_rec = {"code": "000003", "name": "X", "tech_score": 3.0, "sig_channel": "✓",
                "sig_pivot": "✓", "sig_ma": "MA60", "sig_osc": "超卖",
                "support_price": 10.0, "support_label": "前低",
                "dist_support_pct": 1.2, "breakdown_price": 9.5}
    strong = m4.cross_score(tech_rec, {"roe": 20, "pe_pct": 15, "netprofit_yoy": 30,
                                       "debt_ratio": 30, "fund_flags": ["高ROE"]}, 80)
    check("强左侧标签", strong["tag"] == "✅ 强左侧")
    weak = m4.cross_score(tech_rec, {"roe": -5, "pe_ttm": -1, "pe_pct": 95,
                                     "netprofit_yoy": -40, "debt_ratio": 85,
                                     "fund_flags": []}, 80)
    check("技术好但基本面弱标签", weak["tag"] == "⚠️ 技术好但基本面弱")
    check("综合分在 0-100", 0 <= strong["final_score"] <= 100)
    check("基本面分高低有别", strong["fund_score"] > weak["fund_score"])
    # 景气未知(全市场回退): 展示为 None(前端"—"), 不伪造 50; 仍可按技术+基本面判强左侧
    unknown = m4.cross_score(tech_rec, {"roe": 20, "pe_pct": 15, "netprofit_yoy": 30,
                                        "debt_ratio": 30, "fund_flags": []}, None)
    check("景气未知 -> prosperity_score=None(不伪造50)", unknown["prosperity_score"] is None)
    check("景气未知综合分仍在 0-100", 0 <= unknown["final_score"] <= 100)
    # 08-30 兜底桶拆分 (module4_crossscore._tag): 技术分 < 门槛 → 观察·技术弱; 核心字段全缺 → 观察·缺数据 置顶
    weak_tech = m4.cross_score(dict(tech_rec, tech_score=2.0), {"roe": 20, "pe_pct": 15, "netprofit_yoy": 30,
                                                                "debt_ratio": 30, "fund_flags": []}, 80)
    check("技术分低于门槛 -> 观察·技术弱", weak_tech["tag"] == "🔎 观察·技术弱")
    nodata = m4.cross_score(tech_rec, {"fund_flags": []}, 80)
    check("核心字段全缺 -> 观察·缺数据 (置顶, 不落 ⚠️ 桶)", nodata["tag"] == "🔎 观察·缺数据")


def test_module3_valuation():
    print("[模块3 估值 (stock_value_em 新通道)]")
    import types
    n = 300
    dates = pd.bdate_range(end=dt.date.today(), periods=n)
    # 构造与 akshare stock_value_em 真实输出同列名的合成数据
    val_df = pd.DataFrame({
        "数据日期": dates,
        "当日收盘价": np.linspace(20, 40, n),
        "总市值": np.linspace(1e10, 2e10, n),
        "PE(TTM)": np.linspace(40, 22, n),     # 当前处于历史低位 -> 低分位
        "PE(静)": np.linspace(45, 25, n),
        "市净率": np.linspace(5, 2.2, n),
        "市销率": np.linspace(8, 4, n),
    })
    fin_df = pd.DataFrame({
        "日期": [f"{y}-12-31" for y in range(2021, 2026)],
        "净资产收益率(%)": [15, 17, 19, 20, 21.5],
        "摊薄每股收益(元)": [0.8, 0.9, 1.0, 1.1, 1.2],
        "主营业务收入增长率(%)": [10, 12, 15, 16, 18],
        "净利润增长率(%)": [12, 14, 20, 22, 30],
        "销售毛利率(%)": [40, 41, 43, 45, 49],
        "资产负债率(%)": [35, 34, 33, 32, 31],
    })
    fake = types.SimpleNamespace(
        stock_value_em=lambda symbol=None: val_df,
        stock_financial_analysis_indicator=lambda **kw: fin_df,
        stock_zh_valuation_baidu=lambda **kw: None,
    )
    ds._ak = lambda: fake
    ds.CONFIG["source"]["use_cache"] = False

    raw = ds.fetch_valuation_hist("000001")
    check("估值历史: 含 date/pe_ttm/pb 列",
          raw is not None and all(c in raw.columns for c in ["date", "pe_ttm", "pb"]))

    f = m3.pull_fundamentals("000001", industry=None, industry_pe_median=25.0,
                             spot_row={"pe_ttm": 22, "pb": 2.2})
    check("PE-TTM 取到值", f["pe_ttm"] is not None)
    check("PE 历史分位在 0-100", f["pe_pct"] is not None and 0 <= f["pe_pct"] <= 100)
    check("当前PE处历史低位 -> 低分位(<50)", f["pe_pct"] is not None and f["pe_pct"] < 50)
    check("PB 取到值且有分位", f["pb"] is not None and f["pb_pct"] is not None)
    check("行业PE中位对比有值", f["pe_industry_median"] == 25.0 and f["pe_vs_industry"] is not None)
    check("ROE/EPS/负债率 取到", f["roe"] is not None and f["eps"] is not None and f["debt_ratio"] is not None)
    check("ROE多年趋势非空", len(f["roe_trend"]) >= 3)


def test_quality_nan_industry():
    print("[优质榜 行业字段 float-NaN 守卫]")
    from ashare import quality as q
    args = (12.0, 1.5, 15.0, 20.0, 1e10, [1e8, 1.1e8, 1.2e8])
    try:
        up, model = q._fair_upside(float("nan"), *args)
        check("float-NaN 行业不崩, 走通用 PE 模型", model == "PE回归" and up is not None)
    except TypeError as e:      # 回归形态: argument of type 'float' is not iterable
        check(f"float-NaN 行业不崩 ({e})", False)
    check("None 行业不崩", q._fair_upside(None, *args)[1] == "PE回归")
    check("银行仍走 PB-ROE", q._fair_upside("银行", 5.0, 0.6, 12.0, 5.0, 1e10, [])[1] == "PB-ROE")
    check("周期行业仍走正常化", q._fair_upside("煤炭开采", 8.0, 1.0, 10.0, 5.0, 1.5e10, [1e9, 1e9, 1e9])[1] == "周期正常化")


def test_module1_with_stubs():
    print("[模块1 景气 (打桩datasource)]")
    # 12 个行业, 趋势强弱不同
    inds = [f"行业{i:02d}" for i in range(12)]
    idx_map, cons_map, hist_map = {}, {}, {}
    for i, name in enumerate(inds):
        drift = 0.0030 - i * 0.0004           # 前面的行业趋势更强
        idx_map[name] = gen_series(100 + i, 1000, drift, pull=False)
        codes = [f"{i:02d}{j:04d}" for j in range(8)]
        cons_map[name] = pd.DataFrame({"code": codes, "name": codes})
        for j, code in enumerate(codes):
            hist_map[code] = gen_series(500 + i * 10 + j, 20 + j, drift, pull=False)

    # 打桩
    ds.fetch_industry_list = lambda: pd.DataFrame({"industry": inds})
    ds.fetch_industry_hist = lambda name: idx_map.get(name)
    ds.fetch_industry_cons = lambda name: cons_map.get(name)
    ds.fetch_hist = lambda code: hist_map.get(code)
    ds.fetch_benchmark_close = lambda: gen_series(9, 4000, 0.0010, pull=False)[["date", "close"]]
    # 只给前 6 个行业资金流数据 (模拟资金流接口只返回部分行业)
    flow_inds = inds[:6]
    ds.fetch_industry_fund_flow = lambda: pd.DataFrame(
        {"industry": flow_inds, "net_inflow": np.linspace(5e8, -5e8, len(flow_inds))})

    df = m1.compute_industry_scores()
    check("模块1 返回非空", df is not None and not df.empty)
    check("含景气总分列", "prosperity_score" in df.columns)
    check("分数在 0-100", df["prosperity_score"].between(0, 100).all())
    check("入选数 <= top_n", int(df["selected"].sum()) <= m1.CONFIG["industry"]["top_n"])
    check("五维分项列齐全",
          all(c in df.columns for c in ["trend", "momentum", "breadth", "capital", "fundamental"]))
    # 趋势更强的前几个行业应当景气分更高(排序后)
    top_names = list(df.head(3)["industry"])
    check("强趋势行业靠前", any(n in ("行业00", "行业01", "行业02", "行业03") for n in top_names))
    # Fix1: 无资金流数据的行业, capital 必须是 NaN (而不是被填中位50)
    no_flow = df[~df["industry"].isin(flow_inds)]
    check("无资金流行业 capital=NaN(未被填中位)", no_flow["capital"].isna().all())
    check("有资金流行业 capital 有值", df[df["industry"].isin(flow_inds)]["capital"].notna().all())
    # Fix1: 基本面支柱永远不计算 -> fundamental 全 NaN, 权重按行重分配
    check("基本面支柱全 NaN(权重已并入其它)", df["fundamental"].isna().all())


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    test_indicators()
    test_sina_symbol()
    test_module2()
    test_module4()
    test_module3_valuation()
    test_quality_nan_industry()
    test_module1_with_stubs()
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)
