#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
导出层 (Export)
===============
把某个 run_date 的库表汇成仪表盘数据对象, 写成:
  dashboard/dashboard_data.js  ->  window.__ASHARE__ = {...};
  data/candidates_<date>.csv   ->  主表中文表头, 一键导出 (utf-8-sig, Excel可读)
仪表盘 index.html 用 <script src="dashboard_data.js"> 直接读取, 双击即可打开。
"""
from __future__ import annotations
import os
import csv
import json
import datetime as dt
import logging

from . import db
from .config import DASHBOARD_DATA_JS, DATA_DIR, CONFIG

log = logging.getLogger("ashare.export")

DISCLAIMER = ("本系统仅做技术/基本面数据的自动化整理与形态筛选, 不构成任何投资建议。"
              "“左侧买入”是在下跌中、支撑确认前进场, 风险天然更高(可能继续下跌或破位)。"
              "买卖点建议与胜率为历史回测统计, 不构成对未来的保证。"
              "所有标的需人工复核, 使用者自负盈亏与风控。")

# 主表中文表头 (A股: 换手率/量比; 第二增速列为 营收同比 — 东财批量口径的净利即归母)
# 市场地位 = 东财行业内市值排名/份额 (垄断力代理); 增速为单季同比×4 + 头条(TTM/累计)
MAIN_COLUMNS = [
    ("code", "代码"), ("name", "名称"), ("industry", "所属行业"),
    ("dominance_disp", "市场地位"), ("ni_ttm_yoy", "近四季归母同比%"),
    ("rev_ttm_yoy", "近四季营收同比%"), ("growth_quality", "增长持续性"),
    ("pe_disp", "市盈率TTM(分位)"),
    ("tag", "结论标签"), ("streak", "连续上榜"), ("final_score", "综合分"), ("tech_score", "技术分"),
    ("fund_score", "基本面分"), ("price", "现价"), ("spark", "近期走势"), ("dist_support_pct", "距支撑%"),
    ("support_disp", "关键支撑位"), ("breakdown_price", "破位位"),
    ("pos_52w_pct", "52周位置%"), ("ret_1m_pct", "近一月涨%"), ("ret_half_year_pct", "近半年涨跌%"),
    ("turnover", "换手率"), ("volume_ratio", "量比"), ("kdj_tag", "KDJ"),
    ("pb", "市净率"), ("eps", "EPS"), ("roe", "ROE"),
    ("cuosha_score", "错杀分"), ("cuosha_upside", "修复空间%"), ("cuosha_p20", "30日涨20%概率"),
]


def _index_by_code(rows):
    return {r["code"]: r for r in rows}


def build_payload(run_date: str | None = None) -> dict:
    if run_date is None:
        run_date = db.latest_run_date()
    if run_date is None:
        return {"meta": {"run_date": None, "candidates": []}, "industries": [],
                "candidates": [], "details": {}}

    runlog = db.fetch_run_log(run_date) or {}
    industries = db.fetch_table("industry_score", run_date)
    tech = _index_by_code(db.fetch_table("tech_scan", run_date))
    fund = _index_by_code(db.fetch_table("fundamental", run_date))
    finals = db.fetch_table("final_rank", run_date)
    details_rows = db.fetch_table("stock_detail", run_date)
    plans = {r["code"]: _loads(r.get("plan_json"), default=None)
             for r in db.fetch_table("trade_plan", run_date)}

    # 行业榜 (按景气分降序)
    industries_sorted = sorted(industries, key=lambda r: (r.get("prosperity_score") or -1),
                               reverse=True)
    selected_inds = [r["industry"] for r in industries_sorted if r.get("selected")]

    appear = db.recent_appearance_counts(db.recent_run_dates(5))   # 连续上榜次数
    candidates = []
    for fr in finals:
        code = fr["code"]
        t = tech.get(code, {})
        f = fund.get(code, {})
        support_disp = None
        if t.get("support_price") is not None:
            support_disp = f"{t.get('support_label') or '支撑'} {round(float(t['support_price']), 2)}"
        pe_disp = None
        if f.get("pe_ttm") is not None:
            pe_disp = f"{round(f['pe_ttm'],1)}"
            if f.get("pe_pct") is not None:
                pe_disp += f" ({round(f['pe_pct'])}%分位)"
        row = {
            **fr,
            # 技术/行情字段
            "price": t.get("price"),
            "dist_support_pct": t.get("dist_support_pct"),
            "support_label": t.get("support_label"),
            "support_price": t.get("support_price"),
            "support_disp": support_disp,
            "breakdown_price": t.get("breakdown_price"),
            "pos_52w_pct": t.get("pos_52w_pct"),
            "high_52w": t.get("high_52w"), "low_52w": t.get("low_52w"),
            "ret_half_year_pct": t.get("ret_half_year_pct"),
            "ret_1m_pct": t.get("ret_1m_pct"),
            "turnover": t.get("turnover"), "volume_ratio": t.get("volume_ratio"),
            "amount_today": t.get("amount_today"), "avg_amt20_yi": t.get("avg_amt20_yi"),
            "kdj_tag": t.get("kdj_tag"),
            "kdj_k": t.get("kdj_k"), "kdj_d": t.get("kdj_d"), "kdj_j": t.get("kdj_j"),
            "rsi": t.get("rsi"),
            "sig_channel": t.get("sig_channel"), "sig_pivot": t.get("sig_pivot"),
            "sig_ma": t.get("sig_ma"), "sig_osc": t.get("sig_osc"),
            "n_hit": t.get("n_hit"),
            # 基本面字段
            "pe_ttm": f.get("pe_ttm"), "pe_pct": f.get("pe_pct"),
            "pe_industry_median": f.get("pe_industry_median"),
            "pe_vs_industry": f.get("pe_vs_industry"), "pe_disp": pe_disp,
            "pb": f.get("pb"), "pb_pct": f.get("pb_pct"),
            "dividend_yield": f.get("dividend_yield"),
            "eps": f.get("eps"), "eps_yoy": f.get("eps_yoy"), "roe": f.get("roe"),
            "revenue_yoy": f.get("revenue_yoy"), "netprofit_yoy": f.get("netprofit_yoy"),
            "gross_margin": f.get("gross_margin"), "debt_ratio": f.get("debt_ratio"),
            "roe_trend": _loads(f.get("roe_trend_json"), default=[]),
            "roe_trend_q": _loads(f.get("roe_trend_q_json"), default=[]),
            "fund_flags": _loads(f.get("fund_flags_json"), default=[]),
            # 新增: sparkline / 风控 / 量能 / 斐波那契 / 分析师 / 连续上榜
            "spark": _loads(t.get("spark_json"), default=[]),
            "atr_pct": t.get("atr_pct"), "max_dd_pct": t.get("max_dd_pct"),
            "beta": t.get("beta"), "vol_ratio_calc": t.get("vol_ratio_calc"),
            "sig_vol": t.get("sig_vol"), "boll_low": t.get("boll_low"),
            "supp_touches": t.get("supp_touches"), "trend_ok": t.get("trend_ok"),
            "rs_60": t.get("rs_60"), "fcf_yield": f.get("fcf_yield"),
            "box_hi": t.get("box_hi"), "box_lo": t.get("box_lo"),
            # 市场地位 / 近四季增速 / 增长持续性
            "dominance_disp": f.get("dominance_disp"), "dom_rank": f.get("dom_rank"),
            "dom_n": f.get("dom_n"), "dom_share": f.get("dom_share"),
            "ni_ttm_yoy": f.get("ni_ttm_yoy"), "ni_basis": f.get("ni_basis"),
            "rev_ttm_yoy": f.get("rev_ttm_yoy"), "rev_basis": f.get("rev_basis"),
            "growth_quality": f.get("growth_quality"),
            "growth_quality_score": f.get("growth_quality_score"),
            "growth_quality_note": f.get("growth_quality_note"),
            "ni_qoq": _loads(f.get("ni_qoq_json"), default=[]),
            "rev_qoq": _loads(f.get("rev_qoq_json"), default=[]),
            "ni_q_labels": _loads(f.get("ni_q_labels_json"), default=[]),
            "fib_382": t.get("fib_382"), "fib_500": t.get("fib_500"), "fib_618": t.get("fib_618"),
            "target_price": f.get("target_price"), "analyst_rating": f.get("analyst_rating"),
            "analyst_count": f.get("analyst_count"), "upside_pct": f.get("upside_pct"),
            "streak": appear.get(code, 1),
            # 买卖点建议 (入场区/止损/目标梯子+胜率), 详情弹窗渲染
            "plan": plans.get(code),
        }
        candidates.append(row)

    candidates.sort(key=lambda r: (-(r["final_score"] if r.get("final_score") is not None else -1),
                                   r.get("code") or ""))
    top_n = CONFIG["output"]["final_top_n"]
    head = candidates[:top_n]                                   # 支撑型主榜(展示上限)
    # 深跌抄底桶: 支撑分低会被 final_top_n 截掉, 这里把落榜的 dip 候选按 dip_score 补回来
    # (上限 dip_top_n), 保证 BABA 这类作为独立标签组浮现, 不挤占支撑型名额。
    seen = {r.get("code") for r in head}
    dip_extra = sorted((r for r in candidates[top_n:] if r.get("dip")),
                       key=lambda r: -(r.get("dip_score") or 0.0))[:CONFIG["output"].get("dip_top_n", 40)]
    coil_extra = sorted((r for r in candidates[top_n:] if r.get("coil") and not r.get("dip")),
                        key=lambda r: -(r.get("coil_score") or 0.0))[:CONFIG["output"].get("coil_top_n", 40)]
    extras = [r for r in dip_extra + coil_extra if r.get("code") not in seen]
    candidates = head + extras

    details = {}
    for dr in details_rows:
        details[dr["code"]] = _loads(dr["detail_json"], default={})

    profiles = {}
    for pr in db.fetch_table("profile", run_date):
        profiles[pr["code"]] = _loads(pr["profile_json"], default={})

    # 错杀检测: 高质量+情绪性下跌 打分 (字段随快照沉淀, 回测可分段验证)
    try:
        from . import cuosha
        n_cs = cuosha.annotate(candidates)
        log.info("错杀候选: %d 只", n_cs)
        # 30日内涨20%的历史概率 (条件: 该股历史上同样深跌的日子); 长历史不可得时退回存档K线
        from . import prob20
        _cs_items = [c for c in candidates if c.get("cuosha_score")]
        _details = locals().get("details") or {}

        def _hist(code):
            try:
                from . import datasource as _ds2
                df = _ds2.fetch_long_hist(code, years=5)
                if df is not None and len(df) >= 120 and "high" in df.columns and "close" in df.columns:
                    return (df["high"].to_numpy(float), df["close"].to_numpy(float))
            except Exception:
                pass
            d = _details.get(code) or {}
            oh = d.get("ohlc") or []
            if len(oh) >= 120:
                return ([r[3] for r in oh], [r[1] for r in oh])      # echarts [o,c,l,h]
            return None
        n_p = prob20.annotate(_cs_items, _hist, conditional=True, key="cuosha_p20")
        log.info("错杀候选 30日涨20%%概率: %d/%d 只有数", n_p, len(_cs_items))
        # 财报预约披露日: 7天内亮 📅, 错杀/买卖点提示"财报前不建仓"
        from . import earnings_cal
        n_e = earnings_cal.annotate(candidates, as_of=str(runlog.get("data_date") or run_date)[:10])
        log.info("财报预约日标注: %d 只", n_e)
        # "为什么跌"线索: 错杀候选的近期新闻标题关键词 🚩 (只拉错杀股, 数量小)
        from . import newsflag
        n_f = newsflag.annotate(candidates, as_of=str(runlog.get("data_date") or run_date)[:10])
        log.info("错杀候选新闻标记: %d 只有🚩", n_f)
    except Exception as e:
        log.warning("错杀检测失败: %s", e)

    # 机会温度计: 当日榜单质量 vs 自身历史的分位 (指导"今天该不该重仓")
    opp_result = None
    try:
        from . import opportunity as opp
        from . import datasource as _ds
        _bench = _ds.fetch_benchmark_close()
        if _bench is not None and not _bench.empty and run_date and "date" in _bench.columns:
            # as-of 截断: 为历史日期重算快照时, 指数回撤必须只用该日之前的数据
            _bench = _bench[_bench["date"].astype(str).str[:10] <= run_date]
        _bc = _bench["close"] if (_bench is not None and not _bench.empty) else None
        comps = opp.compute_components(candidates, _bc)
        hist = opp.load_history_components(HISTORY_DIR, exclude_date=run_date)
        opp_result = opp.temperature(comps, hist)
    except Exception as e:
        log.warning("机会温度计计算失败: %s", e)

    payload = {
        "meta": {
            "run_date": run_date,
            "data_date": runlog.get("data_date") or run_date,   # 真实行情数据日期(最新收盘)
            "updated_at": runlog.get("finished_at") or run_date,
            # 2026-09-08 起 n_scanned = **裁后**的数 (按价格库的点时股票池, 约 4,950), 不再是
            # 东财快照的 5,180 (那里面混着 196 只早已退市的老代码)。scan_basis/n_pool_raw 让
            # 前端和历史快照能分辨口径: 'store_universe' = 新口径, 'raw_spot'/缺失 = 老口径,
            # 'store_universe_stale' = 新口径但**尺子旧了** (个股末日落后库自己的交易日历
            # >3 个交易日, 或整库 >20 自然日不动 —— 判据不看挂钟, 长假不会误标)。
            "n_scanned": runlog.get("n_scanned"),
            "n_pool_raw": runlog.get("n_pool_raw"),
            "scan_basis": runlog.get("scan_basis") or "raw_spot",
            "n_hit": len(candidates),   # 与主表展示条数一致
            "selected_industries": selected_inds,
            "disclaimer": DISCLAIMER,
            "opp": opp_result,
        },
        "industries": industries_sorted,
        "candidates": candidates,
        "details": details,
        "profiles": profiles,
        "columns": [{"key": k, "label": lab} for k, lab in MAIN_COLUMNS],
    }
    return payload


def write_dashboard_js(run_date: str | None = None) -> str:
    payload = build_payload(run_date)
    os.makedirs(os.path.dirname(DASHBOARD_DATA_JS), exist_ok=True)
    js = "window.__ASHARE__ = " + json.dumps(payload, ensure_ascii=False) + ";\n"
    with open(DASHBOARD_DATA_JS, "w", encoding="utf-8") as f:
        f.write(js)
    log.info("仪表盘数据已写出: %s (%d 候选)", DASHBOARD_DATA_JS, len(payload["candidates"]))
    return DASHBOARD_DATA_JS


HISTORY_DIR = os.path.join(os.path.dirname(DASHBOARD_DATA_JS), "history")


#: 东财在除权/除息当天给名字加的前缀 (XD 除息 / XR 除权 / DR 除权除息)。**不联网就能判**,
#: 是价格库读不到时唯一的 XD 线索; 库在的时候只当作第二条独立证据。
XD_NAME_PREFIXES = ("XD", "XR", "DR")

#: 快照价与库里原始收盘的容差 —— 与 `leftside_core.backtest.ANCHOR_TOL_EXACT` 同一个 0.25%,
#: 免得"生成侧认为对上了"而"锚定侧认为没对上"。
_XD_TOL = 0.0025


def _xd_probe_store(codes: list, data_date: str, run_date: str) -> tuple[dict, set]:
    """价格库 -> ({code: data_date 那天的原始收盘}, {在 (data_date, run_date] 里除过权的 code})。

    只读、失败即放弃 (返回空): 这是快照的**锦上添花**, 绝不能因为库出问题就写不出快照。
    """
    from . import datasource as ds
    raw_at: dict = {}
    xd: set = set()
    if not codes or not data_date:
        return raw_at, xd
    conn = ds._store_conn()
    hi = run_date or data_date
    for i in range(0, len(codes), 400):
        chunk = codes[i:i + 400]
        ph = ",".join("?" * len(chunk))
        for code, c in conn.execute(
                f"SELECT code, c FROM bars_raw WHERE d=? AND code IN ({ph})",
                [data_date, *chunk]):
            if c and float(c) > 0:
                raw_at[code] = float(c)
        # 因子在 [data_date, run_date] 这个窗口里变过 = 快照那天导出的前复权基准已经被除权
        # 平移过, 于是榜单里的 price (取自序列最后一根) 是一个**除权后的昨收**。
        for code, fmin, fmax in conn.execute(
                f"SELECT code, MIN(factor), MAX(factor) FROM adj "
                f"WHERE d>=? AND d<=? AND code IN ({ph}) GROUP BY code",
                [data_date, hi, *chunk]):
            if fmin is not None and fmax is not None and abs(float(fmax) - float(fmin)) > 1e-9:
                xd.add(code)
    return raw_at, xd


def xd_fix_snapshot_prices(slim: dict) -> dict:
    """**除权日快照的生成侧修法** (2026-09-08 卡 R3-4, GM 决定①)。

    病灶: 榜单里的 `price` 来自阶段A 取回的**前复权**序列的最后一根。前复权的基准是"拉取
    那一天", 所以只要某只票**在拉取那天除权**、而序列最后一根是前一天, 存进快照的就是一个
    **除权后的昨收** —— 它既不等于那天的原始收盘, 也不是任何一天的成交价。实证
    (2026-07-01 那份快照, 服务器与 PC 两份副本都一样):
        600061 XD国投资  快照 6.40 = 6.55 × 9.8854/10.1171 (raw 收盘 6.55)
        603201 XD常润股  快照 13.67 = 13.96 × 2.5229/2.5764 (raw 收盘 13.96)
    这两笔是生产同款样本上 raw 锚定仅有的 2 条非 exact, 其中 600061 就是重放里**唯一**
    那笔"新口径更差" (raw 锚定退到 06-26, near 1.72%, 反而错一格)。也就是说 0.9pp 里
    唯一一条反向证据的根因在**快照生成**, 不在取价/锚定口径。

    修法 (只改生成侧, **历史文件一个字节都不动**):
      · 判 XD 两条独立证据 —— ① 名字以 XD/XR/DR 开头 (东财除权日给的前缀, 不联网就能判);
        ② 价格库里该票的复权因子在 [data_date, run_date] 里变过。任一条命中即视为 XD。
      · 命中且库里有 data_date 那根的原始收盘 -> `price` 改记**原始价**, 打
        `price_basis="raw_close"`; 价真的被换掉时另存 `price_qfq_rebased` = 原值 (可追溯,
        将来要对账"当时导出的是什么"不用去翻库)。
      · 命中但拿不到原始价 (库没这只/没这天/库比快照旧) -> 只打 `xd: true`,
        让锚定侧改用 `leftside_core.backtest.xd_rebased_closes` 的 "raw × 因子比" 序列比,
        而不是拿一个不同基准的价去撞 0.25% 的容差。
      · 没命中的候选一个字段都不加 (快照体积敏感; 也让"带标记"本身就是信息)。

    meta 里留一行 `xd_fix` 汇总 (n_xd / n_repriced / n_flagged / codes), 事后能一眼看出
    某一天到底动了谁 —— 静默改价是本队明令禁止的。
    -> 汇总 dict (也写进 slim["meta"]["xd_fix"])。
    """
    meta = slim.get("meta") or {}
    cands = slim.get("candidates") or []
    data_date = str(meta.get("data_date") or meta.get("run_date") or "")[:10]
    run_date = str(meta.get("run_date") or data_date)[:10]
    out = {"data_date": data_date, "n_xd": 0, "n_repriced": 0, "n_flagged": 0,
           "by_name": 0, "by_factor": 0, "codes": [], "store_ok": False}
    if not cands or not data_date:
        return out
    codes = [c["code"] for c in cands if c.get("code")]
    raw_at, xd_store = {}, set()
    try:
        if CONFIG["source"].get("bars") == "tushare":
            raw_at, xd_store = _xd_probe_store(codes, data_date, run_date)
            out["store_ok"] = True
    except Exception as e:                                     # noqa: BLE001
        log.warning("除权日快照修正: 读价格库失败, 只按名称前缀判 (%s)", e)
    for c in cands:
        code, px = c.get("code"), c.get("price")
        if not code or not px or float(px) <= 0:
            continue
        by_name = str(c.get("name") or "").strip().upper().startswith(XD_NAME_PREFIXES)
        by_factor = code in xd_store
        if not (by_name or by_factor):
            continue
        out["n_xd"] += 1
        out["by_name"] += int(by_name)
        out["by_factor"] += int(by_factor)
        rec = {"code": code, "name": c.get("name"), "snap_price": float(px),
               "by": ("name" if by_name else "") + ("+factor" if by_factor else "")}
        raw = raw_at.get(code)
        if raw is None:
            c["xd"] = True                       # 拿不到原始价 -> 交给锚定侧的 raw×因子比
            out["n_flagged"] += 1
            rec["action"] = "flag_xd"
        else:
            c["price_basis"] = "raw_close"
            if abs(float(px) / raw - 1.0) > _XD_TOL:
                c["price_qfq_rebased"] = float(px)
                c["price"] = round(raw, 4)
                out["n_repriced"] += 1
                rec["action"] = "reprice"
                rec["raw_close"] = raw
            else:
                rec["action"] = "already_raw"
        out["codes"].append(rec)
    meta["xd_fix"] = out
    if out["n_xd"]:
        log.info("除权日快照修正: %d 只 XD/XR/DR (名称判 %d, 因子判 %d) -> 改记原始价 %d, "
                 "只打 xd 标记 %d", out["n_xd"], out["by_name"], out["by_factor"],
                 out["n_repriced"], out["n_flagged"])
    return out


def write_history_snapshot(run_date: str | None = None) -> str | None:
    """把某个 run_date 的候选榜写成"瘦身版"历史快照 (无K线明细/深度档案,
    体积 ~1MB), 供前端的日期切换器回看历史扫描结果:
      dashboard/history/day_<date>.json  +  dashboard/history/index.json (可用日期清单)
    auto_update.bat 会把 history/ 整目录同步到 docs/ 发布。"""
    payload = build_payload(run_date)
    rd = payload["meta"].get("run_date")
    if not rd:
        return None
    slim = {"meta": payload["meta"], "industries": payload["industries"],
            "candidates": payload["candidates"], "columns": payload["columns"]}
    # 除权日的候选: price 记原始价 (或打 xd 标记), 否则回放锚定会撞上一个不同基准的价。
    # 只动**这一份将要落盘的快照**, dashboard_data.js 那份是另一次 build_payload, 不受影响。
    try:
        xd_fix_snapshot_prices(slim)
    except Exception as e:                                     # noqa: BLE001
        log.warning("除权日快照修正失败(快照照常写出, 该日 XD 票的锚定可能偏一格): %s", e)
    os.makedirs(HISTORY_DIR, exist_ok=True)
    path = os.path.join(HISTORY_DIR, f"day_{rd}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(slim, f, ensure_ascii=False)
    # 更新清单 (按日期倒序, 只保留 history_days 天)
    keep = CONFIG["output"].get("history_days", 90)
    dates = sorted({fn[4:14] for fn in os.listdir(HISTORY_DIR)
                    if fn.startswith("day_") and fn.endswith(".json")}, reverse=True)
    for stale in dates[keep:]:
        try:
            os.remove(os.path.join(HISTORY_DIR, f"day_{stale}.json"))
        except OSError:
            pass
    dates = dates[:keep]
    idx_path = os.path.join(HISTORY_DIR, "index.json")
    hits: dict = {}
    try:
        with open(idx_path, encoding="utf-8") as f:
            hits = json.load(f).get("hits") or {}
    except Exception:
        hits = {}
    hits[rd] = len(payload["candidates"])
    hits = {d: hits[d] for d in dates if d in hits}
    with open(idx_path, "w", encoding="utf-8") as f:
        json.dump({"dates": dates, "hits": hits}, f)
    log.info("历史快照已写出: %s (%d 天可回看)", path, len(dates))
    return path


def write_csv(run_date: str | None = None) -> str:
    payload = build_payload(run_date)
    rd = payload["meta"]["run_date"] or dt.date.today().isoformat()
    path = os.path.join(DATA_DIR, f"candidates_{rd}.csv")
    headers = [lab for _, lab in MAIN_COLUMNS]
    keys = [k for k, _ in MAIN_COLUMNS]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        wtr = csv.writer(f)
        wtr.writerow(headers)
        for r in payload["candidates"]:
            wtr.writerow(["" if k == "spark" else ("—" if r.get(k) in (None, "") else r.get(k)) for k in keys])
    log.info("CSV 已导出: %s", path)
    return path


def _loads(s, default=None):
    """default 就是 default —— 传 None 必须真的返回 None
    (前端用 if(!p) 判断 plan 缺失, [] 在 JS 里是 truthy, 会渲染出一张空卡)。"""
    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


def write_watch_js() -> None:
    """全市场迷你行情表 -> dashboard/watch_data.js (自建模拟盘/自选股可引用任意股票)。"""
    from . import datasource as ds
    from .config import DASHBOARD_DIR
    try:
        spot = ds.fetch_spot_snapshot()
        if spot is None or spot.empty:
            return
        out = {}
        for _, r in spot.iterrows():
            code = str(r.get("code", "")).zfill(6)
            try:
                px = float(r.get("price"))
                chg = float(r.get("pct_chg")) if r.get("pct_chg") == r.get("pct_chg") else None
            except (TypeError, ValueError):
                continue
            if px > 0:
                def _f(k, nd=2):
                    try:
                        v = float(r.get(k))
                        return round(v, nd) if v == v else None
                    except (TypeError, ValueError):
                        return None
                mv = _f("total_mv", 4)
                out[code] = [str(r.get("name") or ""), round(px, 2),
                             round(chg, 2) if chg is not None else None,
                             str(r.get("industry") or ""),
                             _f("pe_ttm", 1), _f("pb", 2),
                             round(mv / 1e8, 1) if mv else None]
        path = os.path.join(DASHBOARD_DIR, "watch_data.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write("window.__ALL__ = " + json.dumps(out, ensure_ascii=False) + ";\n")
        log.info("全市场迷你行情: %d 只 -> watch_data.js", len(out))
    except Exception as e:
        log.warning("全市场迷你行情导出失败: %s", e)


def write_starmap_js(top_n: int = 1200) -> None:
    """大盘星图数据 -> dashboard/starmap_data.js: 按行业分组的 [名称, 涨跌%, 市值亿]。
    取市值前 top_n 只 (覆盖 ~85% 总市值, 视觉上与全量无异), 控制文件体积。"""
    from . import datasource as ds
    from .config import DASHBOARD_DIR
    try:
        spot = ds.fetch_spot_snapshot()
        if spot is None or spot.empty or "industry" not in spot.columns:
            return
        df = spot.dropna(subset=["total_mv"]).copy()
        df = df[df["total_mv"] > 0].sort_values("total_mv", ascending=False).head(top_n)
        out = {}
        for _, r in df.iterrows():
            ind = str(r.get("industry") or "其他")
            try:
                chg = float(r.get("pct_chg"))
            except (TypeError, ValueError):
                chg = None
            out.setdefault(ind, []).append([
                str(r.get("name") or r.get("code")),
                round(chg, 2) if chg is not None and chg == chg else None,
                round(float(r["total_mv"]) / 1e8, 1),
            ])
        path = os.path.join(DASHBOARD_DIR, "starmap_data.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write("window.__STAR__ = " + json.dumps(out, ensure_ascii=False) + ";\n")
        log.info("大盘星图: %d 行业 / %d 只 -> starmap_data.js", len(out), int(len(df)))
    except Exception as e:
        log.warning("大盘星图导出失败: %s", e)
