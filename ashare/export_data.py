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
#: 免得"生成侧认为对上了"而"锚定侧认为没对上"。09-08 回修后它**只用来给 meta 里的诊断分桶**,
#: 不再参与任何改价 (改价那条路已经整条删掉, 理由见 `xd_fix_snapshot_prices` 的 docstring)。
_XD_TOL = 0.0025


def _xd_data_date(meta: dict) -> tuple[str, str, str | None]:
    """-> (**过了修正表的** data_date, run_date, 修正说明或 None)。

    凡是拿 `meta["data_date"]` 去查价格库的地方都必须先过
    `leftside_core.backtest.SNAPSHOT_DATA_DATE_FIX` —— 那张表存在的**唯一**理由就是这个
    字段在本仓错过两次 (day_2026-07-01.json 存成 07-01 而价来自 06-30;
    day_2026-08-24.json 存成 08-21 而价来自 08-24)。首版直接读 meta 不过表, 于是在服务器
    那两份仍是错值的副本上, 因子窗口与"库里那天的收盘"全都查到了错的一天。
    修正表按**文件名**索引, 而这份快照将要落盘成 `day_<run_date>.json`, 所以拿它去查。
    正常生产上这是 no-op (今天的文件不在表里), 只有重生成那两天的快照时才会命中。
    """
    run_date = str(meta.get("run_date") or "")[:10]
    data_date = str(meta.get("data_date") or run_date)[:10]
    if not run_date:
        return data_date, data_date, None
    try:
        from leftside_core.backtest import corrected_data_date   # noqa: PLC0415
        fixed, note, changed = corrected_data_date("day_%s.json" % run_date, data_date)
    except Exception as e:                                       # noqa: BLE001
        log.warning("除权日快照标记: 标注日修正表读不到, 按 meta 原值走 (%s)", e)
        return data_date, run_date, None
    if changed:
        log.info("除权日快照标记: %s", note)
        return str(fixed)[:10], run_date, note
    if note:                        # 既不是已知错值也不是修正值 -> 文件被动过, 不修正但要喊
        log.warning("除权日快照标记: %s", note)
    return data_date, run_date, note


def _xd_probe_store(codes: list, data_date: str, run_date: str) -> tuple[dict, set]:
    """价格库 -> ({code: data_date 那天的原始收盘}, {在 [data_date, run_date] 里除过权的 code})。

    第一项现在**只做诊断** (写进 meta 的 `store_raw_close` / `basis` 分桶), 不再拿去改价;
    第二项是判 XD 的第二条证据。`data_date` 必须是 `_xd_data_date` 过完修正表的那个。
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
        # 窗口的左端点就是 data_date 本身, 所以这条问的正是"data_date **之后**有没有除权"。
        for code, fmin, fmax in conn.execute(
                f"SELECT code, MIN(factor), MAX(factor) FROM adj "
                f"WHERE d>=? AND d<=? AND code IN ({ph}) GROUP BY code",
                [data_date, hi, *chunk]):
            if fmin is not None and fmax is not None and abs(float(fmax) - float(fmin)) > 1e-9:
                xd.add(code)
    return raw_at, xd


def xd_fix_snapshot_prices(slim: dict) -> dict:
    """**除权日快照的生成侧标记** (2026-09-08 卡 R3-4; 09-08 回修后**只打标记, 一个价都不改**)。

    病灶: 榜单里的 `price` 来自阶段A 取回的**前复权**序列的最后一根。前复权的基准是"拉取
    那一天", 所以只要某只票**在拉取那天除权**、而序列最后一根是前一天, 存进快照的就是一个
    **除权后的昨收** —— 它既不等于那天的原始收盘, 也不是任何一天的成交价。实证
    (2026-07-01 那份快照, 服务器与 PC 两份副本都一样):
        600061 XD国投资  快照 6.40 = 6.55 × 9.8854/10.1171 (raw 收盘 6.55)
        603201 XD常润股  快照 13.67 = 13.96 × 2.5229/2.5764 (raw 收盘 13.96)
    这两笔是生产同款样本上 raw 锚定仅有的 2 条非 exact, 其中 600061 就是重放里**唯一**
    那笔"新口径更差" (raw 锚定退到 06-26, near 1.72%, 反而错一格)。也就是说 0.9pp 里
    唯一一条反向证据的根因在**快照生成**, 不在取价/锚定口径。

    修法 (只加一个布尔标记, **价一个都不改, 历史文件一个字节都不动**):
      · 判 XD 两条证据 —— ① 名字以 XD/XR/DR 开头 (东财除权日给的前缀, 不联网就能判);
        ② 价格库里该票的复权因子在 [data_date, run_date] 里变过。任一条命中即视为 XD。
      · 命中 -> `c["xd"] = True`; 锚定侧 (`leftside_core.backtest.anchor_closes`) 据此换用
        `xd_rebased_closes` 的 "raw × 因子比" 序列去比, 而不是拿一个不同基准的价去撞
        0.25% 的容差。600061 那笔实测: 该序列在 06-30 上恰好给出 6.40, 精确命中。
      · 没命中的候选一个字段都不加 (快照体积敏感; 也让"带标记"本身就是信息)。

    **为什么不把 price 改记原始价** —— 首版 (提交 a3160ef) 这么做, 09-08 校验被打回, 整条
    路已删除。快照里的 `price` 不是孤立的一个数: `support_price` / `breakdown_price` /
    `box_hi` / `box_lo` / 存档 `plan` 的 entry_ref·entry_low·entry_high·stop_price /
    `dist_support_pct` / `high_52w` / `low_52w` / `spark` 全部出自**同一条前复权序列**
    (`ashare/module2_tech.py` 的 `px = float(close.iloc[-1])`), 与 price 同基准。而下游
    `leftside_core.backtest` 是拿 `scale = qfq[anchor] / snap_px` 把这些计划价位整体搬进
    当下的 qfq 空间的 —— **只换 price 的基准而计划价位不换, 等于把入场带与止损位整体平移
    一个除权因子比**。实测 600061 偏 -2.29%、603201 偏 -2.08%, 且 603201 的 `mode` 被推过
    `reconstruct_plan` 里 `px >= support*0.985` 那道阈值, 由 market 翻成 support (入场剧本
    整个换了)。10送10 那种因子比为 2 的拆股会偏 50%, 而 `0.2 < scale < 5.0` 那道护栏拦不住
    —— scale 的量级根本不变。三条消费链 (回测 build_and_run / 模拟盘 paper / 双周 biweekly)
    吃的是同一份快照, 其中**模拟盘账本注册一次之后再也不回看快照**, 写坏了补不回来 (这正是
    本卡自己要写 tools/migrate_paper_sigdate.py 去补的那类损伤)。
    标记那条路没有这个病: price 原样不动 -> scale 与计划价位仍是同一个基准, 变的只有"锚到
    哪根 bar", 而那正是本卡唯一要改的东西。所以现在只剩这一条路, 不再有"首选/兜底"之分。

    **关于两条证据的诚实说明**: 因子那条只在 `data_date < run_date` 时有判别力 (窗口
    `[data_date, run_date]` 才张得开)。生产上绝大多数天 `data_date == run_date` (36 份
    生产快照里 33 份如此), 窗口塌成一天、MIN==MAX 恒不变 -> 因子判恒 0。这**不是漏判**:
    `data_date == run_date` 时序列最后一根就是 run_date、前复权基准与它同日, `price` 本来
    就是当天的原始收盘, 根本没有"除权后的昨收"这个病。也就是说因子证据恰好在需要它的那种天
    (data_date < run_date) 才起作用, 在不需要它的天上沉默。名称前缀那条两种天都在判 —— 而
    多打出来的标记是**无害**的: `xd_rebased_closes` 在非除权 bar 上逐值退化成 raw, 锚定结果
    与不打标记一模一样 (用例 `test_xd_flag_is_harmless_when_price_is_plain_raw` 锁死)。

    meta 里留一行 `xd_fix` 汇总 (n_xd / n_flagged / by_name / by_factor / codes, 每条附
    `store_raw_close` 与 `basis` 分桶: raw = 快照价本来就是原始价, rebased = 是除权后的昨收,
    unknown = 库里读不到), 事后一眼看得出某一天到底标了谁、那天的价到底是哪种 —— 留证据
    **不需要**动价。
    -> 汇总 dict (也写进 slim["meta"]["xd_fix"])。
    """
    meta = slim.get("meta") or {}
    cands = slim.get("candidates") or []
    data_date, run_date, dd_note = _xd_data_date(meta)
    out = {"data_date": data_date, "run_date": run_date, "data_date_note": dd_note,
           "n_xd": 0, "n_flagged": 0, "by_name": 0, "by_factor": 0,
           "codes": [], "store_ok": False}
    if not cands or not data_date:
        return out
    codes = [c["code"] for c in cands if c.get("code")]
    raw_at, xd_store = {}, set()
    try:
        if CONFIG["source"].get("bars") == "tushare":
            raw_at, xd_store = _xd_probe_store(codes, data_date, run_date)
            out["store_ok"] = True
    except Exception as e:                                     # noqa: BLE001
        log.warning("除权日快照标记: 读价格库失败, 只按名称前缀判 (%s)", e)
    for c in cands:
        code, px = c.get("code"), c.get("price")
        if not code or not px or float(px) <= 0:
            continue
        by_name = str(c.get("name") or "").strip().upper().startswith(XD_NAME_PREFIXES)
        by_factor = code in xd_store
        if not (by_name or by_factor):
            continue
        c["xd"] = True          # **唯一的写操作**: 价与其余价位字段一个字都不动
        raw = raw_at.get(code)
        out["n_xd"] += 1
        out["n_flagged"] += 1
        out["by_name"] += int(by_name)
        out["by_factor"] += int(by_factor)
        out["codes"].append({
            "code": code, "name": c.get("name"), "snap_price": float(px),
            "by": "+".join(s for s, ok in (("name", by_name), ("factor", by_factor)) if ok),
            "store_raw_close": raw,
            "basis": ("unknown" if raw is None else
                      ("raw" if abs(float(px) / raw - 1.0) <= _XD_TOL else "rebased")),
        })
    meta["xd_fix"] = out
    if out["n_xd"]:
        log.info("除权日快照: %d 只 XD/XR/DR 打上 xd 标记 (名称判 %d, 因子判 %d; 标注日 %s) "
                 "—— 只改锚定口径, 快照里的价一个都没动",
                 out["n_xd"], out["by_name"], out["by_factor"], data_date)
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
    # 除权日的候选: 打一个 `xd: true` 标记, 否则回放锚定会拿一个"除权后的昨收"去撞容差。
    # **只加标记, 不改任何价** —— 改价会把 price 与 support/box/plan 的基准拆成两套, 让
    # 下游的 scale 整体偏一个除权因子比 (理由写在 xd_fix_snapshot_prices 的 docstring 里)。
    # 只动**这一份将要落盘的快照**, dashboard_data.js 那份是另一次 build_payload, 不受影响。
    try:
        xd_fix_snapshot_prices(slim)
    except Exception as e:                                     # noqa: BLE001
        log.warning("除权日快照标记失败(快照照常写出, 该日 XD 票的锚定可能偏一格): %s", e)
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
