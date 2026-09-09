#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块9 — 👑 优质公司推荐 (Quality Compounders)
=============================================
与"左侧候选"不同, 这是对**全市场**做的质量筛选: 找"连续增长 + 行业龙头 +
高ROE + 深护城河 + 估值不贵"的公司, 每天给出打分排名前10 (硬性门槛全过的
标记 👑, 未全过的列出差在哪一条)。

数据: 东财业绩报表按报告期批量 (全市场归母净利/营收/加权ROE, 20期≈5年) +
全A快照 (PE-TTM/总市值) + 行业成分映射; 研发强度用同花顺年度利润表,
只对入围短名单逐只取 (py_mini_racer 不允许并发)。

硬性门槛 (全过 = 👑):
  Q4  近四个单季: 营收与归母净利的单季同比全部 > 0 (官方累计口径相邻期差分)
  Y4  近四个完整年度: 营收与净利同比全部 > 0
  ROE 最近年度加权ROE >= 15%
  PE  0 < PE-TTM < 31
  DOM 行业营收排名 <= 3 (同行业内按最近年度营收)
加分项 (进评分不进门槛):
  研发强度 (研发费用/营收, 金融行业豁免) / 增长加速(最近单季同比>=TTM同比) /
  ROE持续 (近4年每年>=12%) / 估值更低 / 龙头份额更大
诚实声明: A股没有干净的"一致预期"数据源, "超市场预期"用"增长仍在加速"近似,
美股版用财报日历的实际EPS超预期率。筛选只看已公布财报 —— 财报有滞后,
榜单是"客观条件筛选"而非投资建议, 买前仍需人工研究。
"""
from __future__ import annotations
import datetime as dt
import json
import logging
import os

import numpy as np

from .config import DASHBOARD_DIR, DATA_DIR, ROOT_DIR

log = logging.getLogger("ashare.quality")

QL_JS = os.path.join(DASHBOARD_DIR, "quality_data.js")
QL_JSON = os.path.join(DATA_DIR, "quality_result.json")
# 上一版真榜的逐字节副本 (data/ 不进 git, 服务器上 run_a.sh 的 `git reset --hard` 动不到它)。
# 为什么要它 (2026-09-09 卡 QL-EMPTY): dashboard/quality_data.js 是 git 跟踪文件, 服务器每天 14:00
# 先 reset 到 origin/main —— HEAD 里那份是 08-28 的榜 —— 再跑流水线覆盖。所以"不写文件让旧榜留着"
# 在服务器上留下的不是昨天的榜, 是 08-28 的榜 (09-09 只读核过: git ls-files -v 为 H, HEAD meta.date
# 2026-08-28)。不发布的日子要把真正的上一版榜摆回去, 候选有三处, 取 meta.date 最新且 picks 非空的。
QL_LAST_GOOD = os.path.join(DATA_DIR, "quality_last_good.js")
QL_DOCS_JS = os.path.join(ROOT_DIR, "docs", "quality_data.js")     # PC 每日 auto_update 提交的发布副本

N_PERIODS = 20           # 报告期深度 (~5年: 4个完整年度同比要第5个年报做基数)
PE_MAX = 31.0            # 用户指定: PE-TTM 上限
ROE_MIN = 15.0           # 最近年度加权ROE下限 (%)
DOM_RANK_MAX = 3         # 行业营收排名门槛
TOP_N = 10               # 每日榜单条数
SHORTLIST = 30           # 研发强度只对前 N 名逐只补数据 (THS 单线程)
RD_GOOD, RD_OK = 5.0, 3.0
FIN_INDUSTRIES = ("银行", "保险", "证券", "多元金融")   # 研发强度豁免
CAP_MIN = 300e8          # 蓝筹门槛: 总市值 >= 300亿
UPSIDE_MIN = 20.0        # 盈利空间门槛 (PEG法模型值) >= 20%
JUSTIFIED_PE_LO, JUSTIFIED_PE_HI = 10.0, 35.0


CYCLICAL_KEYS = ("有色", "煤炭", "钢铁", "化工", "化学原料", "化学制品", "化学纤维",
                 "石油", "航运", "航空",
                 "养殖", "农牧", "船舶", "贵金属", "小金属", "能源金属", "工业金属",
                 "水泥", "玻璃", "猪")
FIN_PB_KEYS = ("银行", "保险")


def _fair_upside(industry, pe, pb, roe_a, g_latest, mv, ni_annual):
    """按行业性质选估值模型 -> (空间%, 模型名)。全部是模型值, 非券商目标价。
    银行/保险: PB-ROE (合理PB=ROE/10, 夹0.5-2.5) —— 盈利受拨备调节, PE失真;
    周期/重资产: 三年正常化盈利PE (合理PE=15) —— 单年盈利在周期顶/底都会骗人;
    高增长(>=25%): PEG (合理PE=增速, 夹10-35);
    稳定增长: PE回归 (合理PE=增速夹10-22)。"""
    # 行业字段可能是 pandas 缺失值 float-NaN (非 None): `NaN or ""` 仍是 NaN, `k in NaN` 抛
    # TypeError → 整个优质榜崩溃 (2026-09-01..05 三次丢快照)。非字符串一律当"未知行业"。
    ind = industry if isinstance(industry, str) else ""
    if any(k in ind for k in FIN_PB_KEYS):
        if pb and pb > 0 and roe_a:
            fair_pb = max(0.5, min(2.5, roe_a / 10.0))
            return (fair_pb / pb - 1.0) * 100.0, "PB-ROE"
        return None, None
    if any(k in ind for k in CYCLICAL_KEYS):
        if mv and ni_annual and len(ni_annual) >= 3:
            ni_norm = sum(ni_annual[-3:]) / 3.0
            if ni_norm > 0:
                return (15.0 / (mv / ni_norm) - 1.0) * 100.0, "周期正常化"
        return None, None
    if pe and pe > 0 and g_latest is not None:
        if g_latest >= 25.0:
            justified = min(JUSTIFIED_PE_HI, max(JUSTIFIED_PE_LO, g_latest))
            return (justified / pe - 1.0) * 100.0, "PEG"
        justified = min(22.0, max(10.0, g_latest))
        return (justified / pe - 1.0) * 100.0, "PE回归"
    return None, None


_PREV_Q = {"03": None, "06": "03", "09": "06", "12": "09"}


def _single_quarters(periods: list, cum: list) -> dict:
    """累计口径 -> 单季 {period: value}; 跨期缺失不硬拆 (相邻期差分)。"""
    out = {}
    by = dict(zip(periods, cum))
    for p, v in by.items():
        if v is None:
            continue
        m = p[5:7]
        prev_m = _PREV_Q.get(m)
        if prev_m is None:                       # Q1 即单季
            out[p] = v
            continue
        prev_p = f"{p[:5]}{prev_m}-{30 if prev_m in ('06', '09') else 31}"
        pv = by.get(prev_p)
        if pv is not None:
            out[p] = v - pv
    return out


def _yoy(cur: float | None, prev: float | None) -> float | None:
    if cur is None or prev is None or prev == 0:
        return None
    if prev < 0:
        return None                              # 基数为负, 同比无意义
    return (cur - prev) / abs(prev) * 100.0


def _last_q4_yoy(periods: list, cum: list) -> list | None:
    """最近4个单季的同比(%), 新→旧; 数据不足返回 None。"""
    sq = _single_quarters(periods, cum)
    ps = sorted(sq, reverse=True)
    out = []
    for p in ps[:6]:
        prev = f"{int(p[:4]) - 1}{p[4:]}"
        y = _yoy(sq.get(p), sq.get(prev))
        if y is not None:
            out.append((p, y))
        if len(out) == 4:
            break
    return out if len(out) == 4 else None


def _annual_yoy(periods: list, cum: list, n: int = 4) -> list | None:
    """最近 n 个完整年度的同比(%), 新→旧。"""
    ann = {p[:4]: v for p, v in zip(periods, cum) if p.endswith("12-31") and v is not None}
    ys = sorted(ann, reverse=True)
    out = []
    for y in ys:
        prev = str(int(y) - 1)
        g = _yoy(ann.get(y), ann.get(prev))
        if g is not None:
            out.append((y, g))
        if len(out) == n:
            break
    return out if len(out) == n else None


def _latest_annual(periods: list, vals: list):
    for p, v in sorted(zip(periods, vals), reverse=True):
        if p.endswith("12-31") and v is not None:
            return p[:4], v
    return None, None


def _rd_intensity(code: str) -> float | None:
    """年度研发费用/营业总收入 (%), 同花顺年度利润表; 拿不到返回 None。"""
    try:
        from . import datasource as ds
        import akshare as ak
        df = ds.call_with_retry(ak.stock_financial_benefit_ths, symbol=code,
                                indicator="按年度")
        if df is None or len(df) == 0:
            return None
        row = df.iloc[0]
        rd = ds._parse_cn_amount(row.get("研发费用"))
        rev = ds._parse_cn_amount(row.get("*营业总收入") or row.get("营业总收入"))
        if rd is None or not rev:
            return None
        return rd / rev * 100.0
    except Exception:
        return None

def _long_hist(code: str):
    from . import datasource as ds
    df = ds.fetch_long_hist(code, years=5)
    if df is None or len(df) < 120 or "high" not in df.columns:
        return None
    return (df["high"].to_numpy(float), df["close"].to_numpy(float))


def _coverage_problem(cov: dict, n_expected: int = N_PERIODS) -> str | None:
    """报告期覆盖够不够出榜 —— 不够就返回一句写明缺了哪几期的理由, 够返回 None。
    三条 (2026-09-09 卡 QL-EMPTY, 离线重放 PC 13:30 那份 20 期全到的报表为证):
      · 最新一期抓取失败且无回退 → 不发: 单季差分会悄悄退到上一季, 出一份"看着正常的旧榜" (只缺
        20260630 时入池 145, 与真榜 154 几乎分不出来);
      · 任一期抓取失败且无回退 → 不发: 20 期窗口里恰好 5 个年报, 少任何一个年报, 每只票的 4 年
        同比都算不出 (只缺 20251231 → 入池 53); 服务器 16:12 那轮缺 10 期 → 11350 只全部跳过, 入池 0;
      · 到齐的期 (今天抓到 + 按策略沿用盘上 + 回退到更早一天) < 应有 - 1 → 不发。允许少的那一期只能
        是"源站说还没有数据"的新报告期 (季末后头两周 stock_yjbb_em 对新的期返回空表, 不是故障)。"""
    expected = list(cov.get("expected") or [])
    covered = set(cov.get("ok") or []) | set(cov.get("fallback") or [])
    empty = set(cov.get("empty") or [])
    if not expected:
        return "报告期列表为空"
    if expected[0] not in covered and expected[0] not in empty:
        return f"最新报告期 {expected[0]} 缺失 (抓取失败且无回退)"
    failed = [p for p in expected if p not in covered and p not in empty]
    if failed:
        return "报告期抓取失败且无回退: " + ", ".join(failed)
    if len(covered) < n_expected - 1:
        missing = [p for p in expected if p not in covered]
        return (f"报告期只到齐 {len(covered)}/{n_expected}"
                + (" (缺: " + ", ".join(missing) + ")" if missing else ""))
    return None


def _parse_ql_js(path: str) -> dict | None:
    """读 quality_data.js (window.__QL__ = {...};) -> dict; 没有/解析不了 -> None。"""
    try:
        with open(path, encoding="utf-8") as f:
            txt = f.read()
    except OSError:
        return None
    txt = txt.strip()
    if txt.startswith("window.__QL__"):
        txt = txt[txt.index("=") + 1:]
    txt = txt.strip().rstrip(";").strip()
    try:
        obj = json.loads(txt)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _pick_carry(cands: list) -> tuple | None:
    """cands = [(标签, 路径, 解析后的 dict 或 None), ...] 按优先级排列 -> 取 meta.date 最新且 picks 非空的那份;
    同日取排在前面的。一份非空的都没有 -> None。"""
    best = None
    for label, path, obj in cands:
        if not obj or not obj.get("picks"):
            continue
        d = str((obj.get("meta") or {}).get("date") or "")
        if best is None or d > best[0]:
            best = (d, label, path)
    return best


def _carry_last_good() -> str | None:
    """不发布的日子: 把"上一版真榜"摆在 dashboard/quality_data.js 上 (见 QL_LAST_GOOD 那段注释)。
    候选: 当前看板文件 / data/quality_last_good.js / docs/quality_data.js; 当前那份已经是最新非空榜就一字不写。
    返回沿用的榜的日期; 三处都没有非空榜返回 None (只告警, 不造榜)。"""
    import shutil
    cands = [("dashboard", QL_JS, _parse_ql_js(QL_JS)),
             ("last_good", QL_LAST_GOOD, _parse_ql_js(QL_LAST_GOOD)),
             ("docs", QL_DOCS_JS, _parse_ql_js(QL_DOCS_JS))]
    best = _pick_carry(cands)
    if best is None:
        log.warning("优质榜: 没有可沿用的旧榜 (看板文件/上次真榜副本/docs 副本都没有非空榜), 看板文件原样不动")
        return None
    date, label, path = best
    if label == "dashboard":
        log.info("优质榜看板保留 %s 的榜 (当前文件就是最新的非空榜, 不写)", date)
        return date
    try:
        shutil.copyfile(path, QL_JS)
        log.info("优质榜看板沿用 %s 的榜 (来源 %s: %s)", date, label, path)
    except OSError as e:
        log.warning("优质榜沿用旧榜失败 (%s -> %s): %s", path, QL_JS, e)
        return None
    return date


def _refuse(reason: str, n_screened: int, n_pool, n_picks) -> None:
    """空榜/缺期不发布: 一行 error 写明缘由与三个数, 三个产物文件 (quality_data.js / quality_result.json /
    history/quality_<date>.json) 一个都不写, 看板摆回上一版真榜。返回 None 给 run_pipeline (它只 warning 不阻断)。"""
    log.error("优质榜不发布 (保留上一版榜, 三个产物文件都不写): %s | 全市场 %d, 入池 %s, 榜单 %s",
              reason, n_screened,
              "未评" if n_pool is None else n_pool, "未评" if n_picks is None else n_picks)
    _carry_last_good()
    return None


def _industry_map(ds) -> dict:
    """行业映射 {code: 行业名}; 接口挂了返回能拿到的部分 (龙头判定降级), 不抛。"""
    ind_of = {}
    try:
        inds = ds.fetch_industry_list()
        names = list(inds["industry"]) if inds is not None and "industry" in inds.columns \
            else (list(inds.iloc[:, 0]) if inds is not None else [])
        for ind in names:
            cons = ds.fetch_industry_cons(ind)
            if cons is None:
                continue
            for _, r in cons.iterrows():
                ind_of[str(r["code"]).zfill(6)] = ind
    except Exception as e:
        log.warning("行业映射构建失败(龙头判定降级): %s", e)
    return ind_of


def _score_rows(reports: dict, spot_map: dict, ind_of: dict) -> list:
    """七道门槛 + 评分, 纯函数 (不联网): 过 4 条门槛的票 -> rows (未排序)。逻辑与 2026-09-09 之前的
    build_quality 内联段逐字相同, 拆出来是为了能离线复现"缺哪几期 → 入池几只"。"""
    # 行业内最近年度营收排名
    ind_rev = {}
    for code, rep in reports.items():
        _, rev_a = _latest_annual(rep["periods"], rep["rev_cum"])
        ind = ind_of.get(code)
        if ind and rev_a:
            ind_rev.setdefault(ind, []).append((code, rev_a))
    dom_rank, dom_share = {}, {}
    for ind, arr in ind_rev.items():
        arr.sort(key=lambda x: -x[1])
        tot = sum(v for _, v in arr) or 1.0
        for i, (code, v) in enumerate(arr, 1):
            dom_rank[code] = i
            dom_share[code] = v / tot * 100.0

    rows = []
    for code, rep in reports.items():
        sp = spot_map.get(code, {})
        name = sp.get("name")
        pe = sp.get("pe_ttm")
        pe = float(pe) if isinstance(pe, (int, float)) and pe == pe else None
        ni_q4 = _last_q4_yoy(rep["periods"], rep["ni_cum"])
        rev_q4 = _last_q4_yoy(rep["periods"], rep["rev_cum"])
        ni_y4 = _annual_yoy(rep["periods"], rep["ni_cum"])
        rev_y4 = _annual_yoy(rep["periods"], rep["rev_cum"])
        roe_y, roe_a = _latest_annual(rep["periods"], rep.get("roe_cum") or [])
        if ni_q4 is None and ni_y4 is None:
            continue

        g_q4 = bool(ni_q4 and rev_q4
                    and all(v > 0 for _, v in ni_q4) and all(v > 0 for _, v in rev_q4))
        g_y4 = bool(ni_y4 and rev_y4
                    and all(v > 0 for _, v in ni_y4) and all(v > 0 for _, v in rev_y4))
        g_roe = bool(roe_a is not None and roe_a >= ROE_MIN)
        g_pe = bool(pe is not None and 0 < pe < PE_MAX)
        dr = dom_rank.get(code)
        g_dom = bool(dr is not None and dr <= DOM_RANK_MAX)
        mv = sp.get("total_mv")
        mv = float(mv) if isinstance(mv, (int, float)) and mv == mv else None
        g_cap = bool(mv is not None and mv >= CAP_MIN)
        # 盈利空间 (PEG法, 模型值): 合理PE = 最近年度增速夹在 [10,35], 空间 = 合理PE/当前PE - 1。
        # A股没有干净的一致预期目标价, 这是"如果估值向增速回归"的保守模型, 前端明确标注。
        pb_v = sp.get("pb")
        pb_v = float(pb_v) if isinstance(pb_v, (int, float)) and pb_v == pb_v else None
        ni_annual = [v for pp, v in sorted(zip(rep["periods"], rep["ni_cum"]))
                     if pp.endswith("12-31") and v is not None]
        upside, val_model = _fair_upside(ind_of.get(code) or sp.get("industry"),
                                         pe, pb_v, roe_a,
                                         ni_y4[0][1] if ni_y4 else None, mv, ni_annual)
        if upside is not None:
            upside = min(upside, 100.0)               # 模型值封顶100%
        g_up = bool(upside is not None and upside >= UPSIDE_MIN)
        gates = {"q4": g_q4, "y4": g_y4, "roe": g_roe, "pe": g_pe, "dom": g_dom,
                 "cap": g_cap, "up": g_up}
        n_pass = sum(gates.values())
        if n_pass < 4:                            # 7 个门槛更严: 先过 4 条才进排名池
            continue

        # 评分 (硬门槛之外的排序依据)
        score = 0.0
        if roe_a is not None:
            score += min(30.0, max(0.0, roe_a))
        if ni_y4:
            score += min(20.0, sum(1 for _, v in ni_y4 if v > 0) * 5.0)
        if dr is not None:
            score += 15.0 if dr == 1 else (10.0 if dr <= 3 else 0.0)
        if upside is not None:
            score += min(15.0, max(0.0, upside) / 4.0)
        if g_cap:
            score += 5.0
        if pe is not None and pe > 0:
            score += 10.0 if pe < 20 else (5.0 if pe < PE_MAX else 0.0)
        accel = None
        if ni_q4:
            ttm_yoy = ni_y4[0][1] if ni_y4 else None
            accel = bool(ttm_yoy is not None and ni_q4[0][1] >= ttm_yoy)
            if accel:
                score += 8.0
        rows.append({
            "code": code, "name": name,
            "industry": ind_of.get(code) or (sp.get("industry") if isinstance(sp.get("industry"), str) else None),
            "val_model": val_model,
            "pe": round(pe, 1) if pe is not None else None,
            "roe": round(roe_a, 1) if roe_a is not None else None,
            "roe_year": roe_y,
            "dom_rank": dr,
            "mcap_b": round(mv / 1e8, 0) if mv else None,
            "upside": round(upside, 1) if upside is not None else None,
            "dom_share": round(dom_share.get(code), 1) if code in dom_share else None,
            "ni_q4": [round(v, 1) for _, v in ni_q4][::-1] if ni_q4 else None,   # 旧→新
            "rev_q4": [round(v, 1) for _, v in rev_q4][::-1] if rev_q4 else None,
            "ni_y4": [round(v, 1) for _, v in ni_y4][::-1] if ni_y4 else None,
            "accel": accel,
            "gates": gates, "n_pass": n_pass, "score": round(score, 1),
        })
    return rows


def build_quality(top_n: int = TOP_N) -> dict | None:
    """出榜入口。**空榜与缺期一律不发布** (2026-09-09 卡 QL-EMPTY): 报告期覆盖不全 / 入池 0 / 榜单 0 时
    不写 quality_data.js、quality_result.json、history/quality_<日期>.json 三个文件, 打一行 error
    写明缺了哪几期与入池数, 看板摆回上一版真榜, 返回 None。正常榜的 meta 带 periods_* 供事后核。"""
    from . import datasource as ds
    reports, cov = ds.fetch_profit_reports_ex(N_PERIODS)
    problem = _coverage_problem(cov, N_PERIODS)
    rows, n_pool = [], None
    if reports:
        spot = ds.fetch_spot_snapshot()
        spot_map = {}
        if spot is not None and not spot.empty:
            for _, r in spot.iterrows():
                spot_map[str(r.get("code", "")).zfill(6)] = r.to_dict()
        ind_of = _industry_map(ds)
        rows = _score_rows(reports, spot_map, ind_of)
        n_pool = len(rows)
    if problem:
        return _refuse("报告期覆盖不全 — " + problem, len(reports), n_pool, None)
    if not rows:
        return _refuse("入池 0 (没有一只票过 4 条门槛)", len(reports), 0, 0)

    rows.sort(key=lambda r: (-r["n_pass"], -(r["score"] or 0)))
    short = rows[:SHORTLIST]
    # 研发强度: 只对短名单逐只取 (THS 单线程, 金融行业豁免)
    for r in short:
        if r.get("industry") in FIN_INDUSTRIES:
            r["rd"] = None
            r["rd_exempt"] = 1
            continue
        rd = _rd_intensity(r["code"])
        r["rd"] = round(rd, 1) if rd is not None else None
        if rd is not None:
            r["score"] = round(r["score"] + (10.0 if rd >= RD_GOOD
                                             else (6.0 if rd >= RD_OK else 0.0)), 1)
    short.sort(key=lambda r: (-r["n_pass"], -(r["score"] or 0)))
    picks = short[:top_n]
    if not picks:
        return _refuse("榜单 0", len(reports), len(rows), 0)
    try:
        from . import prob20
        prob20.annotate(picks, _long_hist, conditional=False, key="p20")
    except Exception as e:
        log.warning("30日涨20%%概率计算失败: %s", e)
    n_crown = sum(1 for r in picks if r["gates"] and all(r["gates"].values()))
    result = {
        "meta": {"date": dt.date.today().isoformat(),
                 "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                 "n_screened": len(reports), "n_pool": len(rows),
                 "n_crown": n_crown, "pe_max": PE_MAX, "roe_min": ROE_MIN,
                 # 报告期覆盖留痕 (事后核 "这榜是用哪几期算的"): ok = 今天抓到或按策略沿用盘上的期,
                 # fallback = 今天抓取失败、用了更早一天同期数据的期, empty = 源站说还没数据的新期,
                 # failed 在发布出来的榜里必为空 (非空就不会走到这里)。
                 "periods_expected": len(cov.get("expected") or []),
                 "periods_ok": list(cov.get("ok") or []),
                 "periods_fallback": list(cov.get("fallback") or []),
                 "periods_empty": list(cov.get("empty") or []),
                 "periods_failed": list(cov.get("failed") or [])},
        "picks": picks,
    }
    json.dump(result, open(QL_JSON, "w", encoding="utf-8"), ensure_ascii=False)
    # 每日榜单落盘到 history/, 供"榜单战绩"回测 (优质榜不在候选快照里, 需自己留痕)。只在真榜时写:
    # 留痕是"当天榜单当天什么样", 空榜不是榜 (数据总览把 picks 为空的留痕与缺失同级判陈旧)。
    try:
        hdir = os.path.join(DASHBOARD_DIR, "history")
        os.makedirs(hdir, exist_ok=True)
        slim = [{k: p.get(k) for k in ("code", "name", "industry", "score", "n_pass", "pe", "roe", "gates")}
                for p in picks]
        with open(os.path.join(hdir, f"quality_{result['meta']['date']}.json"), "w", encoding="utf-8") as f:
            json.dump({"date": result["meta"]["date"], "picks": slim}, f, ensure_ascii=False)
    except Exception as e:
        log.warning("优质榜历史落盘失败: %s", e)
    try:
        result["deep_profiles"] = _deep_profiles(picks)
    except Exception as e:
        log.warning("优质深度档案失败(不影响榜单): %s", e)
    try:
        result["profiles"] = _drawer_profiles(picks, reports)
        _merge_fundamental_fields(result["profiles"])
    except Exception as e:
        log.warning("优质榜弹窗档案失败(不影响榜单): %s", e)
    with open(QL_JS, "w", encoding="utf-8") as f:
        f.write("window.__QL__ = ")
        json.dump(result, f, ensure_ascii=False)
        f.write(";\n")
    try:
        import shutil
        os.makedirs(os.path.dirname(QL_LAST_GOOD), exist_ok=True)
        shutil.copyfile(QL_JS, QL_LAST_GOOD)     # 真榜副本, 不发布的日子从这里摆回看板
    except OSError as e:
        log.warning("优质榜真榜副本落盘失败: %s", e)
    log.info("优质榜: 全市场 %d, 入池 %d, 榜单 %d (👑全过 %d); 报告期 到齐 %d/%d (回退 %d, 空 %d)",
             len(reports), len(rows), len(picks), n_crown,
             len(result["meta"]["periods_ok"]) + len(result["meta"]["periods_fallback"]),
             result["meta"]["periods_expected"], len(result["meta"]["periods_fallback"]),
             len(result["meta"]["periods_empty"]))
    return result


def _drawer_profiles(picks: list, reports: dict | None = None) -> dict:
    """优质榜前10只生成候选股同构档案 -> 主表同款弹窗 (尽量填满总览页字段)。"""
    import glob
    import pandas as pd
    from . import datasource as ds
    from leftside_core import indicators as ind
    template = {}
    days = sorted(glob.glob(os.path.join(DASHBOARD_DIR, "history", "day_*.json")))
    if days:
        try:
            cands = (json.load(open(days[-1], encoding="utf-8")).get("candidates") or [])
            if cands:
                template = {k: None for k in cands[0]}
        except Exception:
            pass
    try:
        spot = ds.fetch_spot_snapshot()
        spot_map = {str(r.get("code", "")).zfill(6): r for _, r in spot.iterrows()} \
            if spot is not None and not spot.empty else {}
    except Exception:
        spot_map = {}

    def _f(v):
        try:
            v = float(v)
            return v if v == v else None
        except (TypeError, ValueError):
            return None

    out = {}
    for i, p in enumerate(picks, 1):
        code = p.get("code")
        try:
            df = ds.fetch_long_hist(code, years=2)
            if df is None or len(df) < 60:
                continue
            close = pd.Series(pd.to_numeric(df["close"], errors="coerce")).dropna()
            high = pd.Series(pd.to_numeric(df["high"], errors="coerce")).dropna()
            low = pd.Series(pd.to_numeric(df["low"], errors="coerce")).dropna()
            vol = pd.Series(pd.to_numeric(df.get("volume"), errors="coerce")).dropna() \
                if "volume" in df.columns else None
            k, d, jv = ind.kdj(high, low, close)
            k, d, jv = (float(k.iloc[-1]), float(d.iloc[-1]), float(jv.iloc[-1]))
            sp = spot_map.get(code)
            price = _f(sp.get("price")) if sp is not None else None
            if not price or price <= 0:
                price = float(close.iloc[-1])
            h52 = float(high.iloc[-250:].max())
            l52 = float(low.iloc[-250:].min())
            vr = None
            sig_vol = None
            if vol is not None and len(vol) >= 20 and float(vol.iloc[-20:].mean()) > 0:
                vr = round(float(vol.iloc[-5:].mean()) / float(vol.iloc[-20:].mean()), 2)
                sig_vol = "缩量" if vr < 0.7 else ("放量" if vr > 1.5 else "平量")
            ni_q4 = p.get("ni_q4") or []
            rev_q4 = p.get("rev_q4") or []
            ni_y4 = p.get("ni_y4") or []
            roe_trend = None
            if reports and code in reports:
                rep = reports[code]
                pts = [{"date": pp, "value": round(float(v), 2)}
                       for pp, v in sorted(zip(rep["periods"], rep.get("roe_cum") or []))
                       if pp.endswith("12-31") and v is not None]
                roe_trend = pts[-5:] or None
            prof = dict(template)
            prof.update({
                "code": code, "name": p.get("name"), "industry": p.get("industry"),
                "tag": "🔎 观察", "price": round(price, 2),
                "spark": [round(float(v), 2) for v in close.iloc[-40:]],
                "high_52w": round(h52, 2), "low_52w": round(l52, 2),
                "pos_52w_pct": round((price - l52) / (h52 - l52) * 100, 1) if h52 > l52 else None,
                "max_dd_pct": round(float(ind.max_drawdown(close)), 1),
                "atr_pct": round(float(ind.atr_pct(high, low, close)), 2),
                "boll_low": round(float(ind.bollinger_lower(close).iloc[-1]), 2),
                "vol_ratio_calc": vr, "sig_vol": sig_vol,
                "volume_ratio": _f(sp.get("volume_ratio")) if sp is not None else None,
                "turnover": _f(sp.get("turnover")) if sp is not None else None,
                "kdj_k": round(k, 1), "kdj_d": round(d, 1), "kdj_j": round(jv, 1),
                "kdj_tag": ind.kdj_tag(k, d, jv),
                "rsi": round(float(ind.rsi(close).iloc[-1]), 1),
                "pe_ttm": p.get("pe"), "pe_disp": (str(p.get("pe")) if p.get("pe") is not None else None),
                "pb": _f(sp.get("pb")) if sp is not None else None,
                "roe": p.get("roe"),
                "netprofit_yoy": (ni_q4[0] if ni_q4 else None),
                "revenue_yoy": (rev_q4[0] if rev_q4 else None),
                "ni_ttm_yoy": (ni_y4[0] if ni_y4 else None), "ni_basis": "年度" if ni_y4 else None,
                "ni_qoq": ni_q4, "rev_qoq": rev_q4,
                "roe_trend": roe_trend,
                "fund_score": round(float(p.get("score") or 0)),
                "dom_rank": p.get("dom_rank"), "dom_share": p.get("dom_share"),
                "conclusion": f"👑 优质榜第{i}名 · 硬门槛 {p.get('n_pass')}/7 · 长线研究池标的, 非左侧信号; 买卖点/胜率仅候选股提供。",
                "conclusion_en": f"Quality #{i} · gates {p.get('n_pass')}/7 · research-pool name, not a left-side signal.",
            })
            out[code] = prof
        except Exception as e:
            log.debug("优质档案 %s 失败: %s", code, e)
    log.info("优质榜弹窗档案: %d/%d", len(out), len(picks))
    return out


def _deep_profiles(picks: list, budget_sec: int = 480) -> dict:
    """优质榜标的深度档案 (公司简介/主营/现金流/风险/消息): 库里最近的直接复用,
    没有或超过7天的在预算内现场补拉并入库 -> 弹窗四个页签不再空白。"""
    import sqlite3
    import time
    from .config import DB_PATH
    from . import module6_profile as m6
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    out = {}
    t0 = time.time()
    today = dt.date.today()
    for p in picks:
        code = p.get("code")
        prof, age = None, 999
        try:
            row = conn.execute(
                "SELECT run_date, profile_json FROM profile WHERE code=? "
                "ORDER BY run_date DESC LIMIT 1", (code,)).fetchone()
            if row and row["profile_json"]:
                prof = json.loads(row["profile_json"])
                age = (today - dt.date.fromisoformat(str(row["run_date"])[:10])).days
        except Exception:
            prof = None
        if (prof is None or age > 7) and time.time() - t0 < budget_sec:
            try:
                fresh = m6.pull_profile(code, sector=p.get("industry"))
                if fresh and (fresh.get("summary") or fresh.get("revenue") or fresh.get("cashflow")):
                    prof = fresh
                    conn.execute("INSERT OR REPLACE INTO profile(run_date,code,profile_json) "
                                 "VALUES(?,?,?)",
                                 (today.isoformat(), code, json.dumps(fresh, ensure_ascii=False)))
                    conn.commit()
            except Exception as e:
                log.debug("优质深度档案 %s: %s", code, e)
        if prof:
            out[code] = prof
    conn.close()
    log.info("优质榜深度档案: %d/%d", len(out), len(picks))
    return out


def _merge_fundamental_fields(profiles: dict) -> int:
    """曾进过候选池的股票, fundamental 表已有逐股财务字段 -> 补进弹窗档案空位。"""
    import sqlite3
    from .config import DB_PATH
    FIELDS = ("eps", "gross_margin", "debt_ratio", "dividend_yield", "pe_pct",
              "pb_pct", "pe_industry_median", "pe_vs_industry", "eps_yoy", "fcf_yield")
    JSONF = (("roe_trend_json", "roe_trend"), ("roe_trend_q_json", "roe_trend_q"),
             ("fund_flags_json", "fund_flags"))
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    n = 0
    for code, prof in profiles.items():
        try:
            row = conn.execute("SELECT * FROM fundamental WHERE code=? "
                               "ORDER BY run_date DESC LIMIT 1", (code,)).fetchone()
        except Exception:
            row = None
        if not row:
            continue
        hit = False
        for f in FIELDS:
            if prof.get(f) is None and row[f] is not None:
                prof[f] = row[f]
                hit = True
        for jf, key in JSONF:
            if prof.get(key) is None and row[jf]:
                try:
                    prof[key] = json.loads(row[jf])
                    hit = True
                except Exception:
                    pass
        if hit:
            n += 1
    conn.close()
    log.info("优质档案合并库内财务字段: %d 只", n)
    return n


if __name__ == "__main__":   # 必须放在文件末尾: 直接跑 `-m ashare.quality` 时 build_quality 要能看到下面的档案函数
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    build_quality()
