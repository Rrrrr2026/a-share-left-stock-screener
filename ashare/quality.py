#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块9 — 👑 优质公司推荐 (Quality Compounders)
=============================================
与"左侧候选"不同, 这是对**全市场**做的质量筛选: 找"连续增长 + 行业龙头 +
高ROE + 深护城河 + 估值不贵"的公司, 每天给出打分排名前10 (硬性门槛全过的
标记 👑, 未全过的列出差在哪一条)。

数据: 东财业绩报表按报告期批量 (全市场归母净利/营收/加权ROE, 20期≈5年) +
全A快照 (PE-TTM = 东财 f115 / Tushare daily_basic.pe_ttm, 真 TTM; 总市值) + 行业归属 = **东财全市场行业分类**
(push2 clist f100, ~128 个二级类; 东财不可达时 Tushare stock_basic 经别名表换成东财口径) —— 老板 2026-09-23 拍板 ①②,
卡 IND-PE; 研发强度用同花顺年度利润表, 只对入围短名单逐只取 (py_mini_racer 不允许并发)。

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
from .datasource import YJBB_EMPTY_MAX_AGE_DAYS

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
FIN_INDUSTRIES = ("银行", "保险", "证券", "多元金融")   # 研发强度豁免 (去掉东财级别后缀后精确命中, 见 is_fin_industry)
# 东财 f100 (批量映射 / 东财直连快照的所属行业) 里金融名带二级后缀: 银行Ⅱ / 证券Ⅱ / 保险Ⅱ (多元金融 没有); 东财成分口径与
# Tushare 别名表给的是 银行 / 证券 / 保险。豁免判定先去掉级别后缀再精确比 (2026-09-23 卡 QL-SPOT 回修: 首版精确比,
# 东财日的银行/保险/券商全都不豁免, 白打 THS 研发接口)。"III" 排在 "II" 前面: 先剥长的。
IND_LEVEL_SUFFIX = ("Ⅱ", "Ⅲ", "Ⅳ", "III", "II")


def industry_base_name(name) -> str:
    """行业名去掉东财二/三级后缀 (银行Ⅱ → 银行, 股份制银行Ⅲ → 股份制银行); 非字符串 → ''。"""
    s = name.strip() if isinstance(name, str) else ""
    for suf in IND_LEVEL_SUFFIX:
        if s.endswith(suf):
            return s[: -len(suf)].strip()
    return s


def is_fin_industry(name) -> bool:
    """研发强度豁免的金融行业: 去后缀后**精确**命中 FIN_INDUSTRIES (子串匹配会把 '非银金融' 之类误收)。"""
    return industry_base_name(name) in FIN_INDUSTRIES
CAP_MIN = 300e8          # 蓝筹门槛: 总市值 >= 300亿
UPSIDE_MIN = 20.0        # 盈利空间门槛 (PEG法模型值) >= 20%
JUSTIFIED_PE_LO, JUSTIFIED_PE_HI = 10.0, 35.0
# 最新报告期允许「源站还没有数据」的期限 (期末后天数; 2026-09-13 卡 QL-EMPTY 回修): 与 datasource 判据 ② 同一个数,
# 只在那边定义 (首批披露通常在期末后 8-20 天, 最短法定截止 30 天; 45 天后还答无数据只能是源站/限频出了问题)。
# datasource 记 empty 已经要三条同时成立, 这里再核一遍年龄是第二道防线 (coverage 若来自别处/旧版也拦得住)。
EMPTY_LATEST_MAX_AGE_DAYS = YJBB_EMPTY_MAX_AGE_DAYS
# 沿用旧榜时, 榜比这更旧就用 warning 而不是 info: 正常的上一版榜是 1-3 天前的; 一份几周前的榜多半是 git reset
# 恢复出来的 HEAD 版 (dashboard/quality_data.js 是跟踪文件), 说明 data/quality_last_good.js 与 docs 副本都不在。
CARRY_STALE_WARN_DAYS = 7
# 行业映射 (龙头判定 dom 用的分组) 覆盖低于这么多只就算「部分」: 仍用, 但来源标 (部分) 并 WARNING 龙头判定降级 (全A ~5200 只;
# 与 run_pipeline 的 3000 只裁池阈值同一口径)。
# **口径唯一 (老板 2026-09-23 拍板 ①, 卡 IND-PE): 东财全市场行业分类** = datasource.fetch_industry_map() 的 push2 clist f100 批量
# 映射 (全市场一次, 二级口径 ~128 组, 与 run_pipeline 市场地位/行业 PE 中位分组同一份), 东财不可达时它自己退到 Tushare
# stock_basic (名字经别名表换成东财口径)。**「东财成分口径」(fetch_industry_list × fetch_industry_cons) 已从这里删掉, 永不再用**:
# 它只覆盖 3-13 个一级行业 / 62-1041 只 (服务器 journal 09-01 起每一跑 `候选池: 全行业成分股 62/214/329/…/1041/485/25/0 只`,
# 从没到过 3000), 用它分组等于只给一小撮票判龙头; 卡 QL-SPOT 首版把它排在 ① "成分 >= 3000 才用" —— 那条分支在生产里一次
# 都没走到, 却让 09-21 之前的榜 (成分部分分组) 与之后的榜 (f100 全市场) 是两套口径。现在三态只剩 东财批量 / 东财当日缓存 /
# Tushare, 成分接口在本模块里一次都不调 (用例锁住: 成分 5000 只也不选)。
IND_MAP_MIN_CODES = 3000


def _today() -> dt.date:
    """今天 (单独成函数是为了让用例钉住日期: 季初形态、旧榜年龄)。"""
    return dt.date.today()


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


def _period_end(p: str) -> dt.date | None:
    """'YYYYMMDD' -> 期末日期; 解析不了 -> None。"""
    try:
        return dt.date(int(p[:4]), int(p[4:6]), int(p[6:8]))
    except (TypeError, ValueError):
        return None


def _coverage_problem(cov: dict, n_expected: int = N_PERIODS, today: dt.date | None = None) -> str | None:
    """报告期覆盖够不够出榜 —— 不够就返回一句写明缺了哪几期的理由, 够返回 None。
    三条 (2026-09-09 卡 QL-EMPTY, 离线重放 PC 13:30 那份 20 期全到的报表为证):
      · 最新一期抓取失败且无回退 → 不发: 单季差分会悄悄退到上一季, 出一份"看着正常的旧榜" (只缺
        20260630 时入池 145, 与真榜 154 几乎分不出来);
      · 任一期抓取失败且无回退 → 不发: 20 期窗口里恰好 5 个年报, 少任何一个年报, 每只票的 4 年
        同比都算不出 (只缺 20251231 → 入池 53); 服务器 16:12 那轮缺 10 期 → 11350 只全部跳过, 入池 0;
      · 到齐的期 (今天抓到 + 按策略沿用盘上 + 回退到更早一天) < 应有 - 1 → 不发。
    「源站答空」(empty) 的豁免**只给最新一期, 且只在期末后 EMPTY_LATEST_MAX_AGE_DAYS 天内** (2026-09-13 卡 QL-EMPTY
      回修 HIGH): 期末当天起到首家披露前, 最新一期在源站本来就是空的 (akshare 对它抛 TypeError, 09-13 实测 20260930;
      datasource 三条判据同时成立才记 empty) —— 这是每季头 1-3 周的正常形态, 不是故障, 19/20 照常出榜。其它位置的
      答空 (那些期早已过披露期) 与期末 45 天后的答空都不可能是真的没数据, 视同抓取失败。"""
    expected = list(cov.get("expected") or [])
    covered = set(cov.get("ok") or []) | set(cov.get("fallback") or [])
    empty = set(cov.get("empty") or []) - covered
    if not expected:
        return "报告期列表为空"
    today = today or _today()
    latest = expected[0]
    if latest not in covered and latest not in empty:
        return f"最新报告期 {latest} 缺失 (抓取失败且无回退)"
    tolerated: set = set()
    if latest in empty:
        end = _period_end(latest)
        age = (today - end).days if end else None
        if age is None or age > EMPTY_LATEST_MAX_AGE_DAYS:
            return (f"最新报告期 {latest} 源站答空, 但期末已过 "
                    + (f"{age} 天 (> {EMPTY_LATEST_MAX_AGE_DAYS} 天不可能还没人披露)" if age is not None
                       else "多久无法判断 (期末日期解析不了)")
                    + ", 视同抓取失败")
        tolerated.add(latest)
    bogus = [p for p in expected if p in empty and p not in tolerated]
    if bogus:
        return "报告期源站答空但不是新报告期 (早已过披露期, 视同抓取失败): " + ", ".join(bogus)
    failed = [p for p in expected if p not in covered and p not in tolerated]
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


def _board_age_days(date_s) -> int | None:
    """榜的 meta.date 距今几天; 解析不了 -> None。"""
    try:
        return (_today() - dt.date.fromisoformat(str(date_s)[:10])).days
    except (TypeError, ValueError):
        return None


def _carry_last_good() -> str | None:
    """不发布的日子: 把"上一版真榜"摆在 dashboard/quality_data.js 上 (见 QL_LAST_GOOD 那段注释)。
    候选: 当前看板文件 / data/quality_last_good.js / docs/quality_data.js; 当前那份已经是最新非空榜就一字不写。
    沿用的榜比 CARRY_STALE_WARN_DAYS 还旧 → warning 而不是 info (多半是 reset 恢复出来的 HEAD 版, 另两处副本都不在)。
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
    age = _board_age_days(date)
    stale = age is None or age > CARRY_STALE_WARN_DAYS
    note = ("" if not stale else
            f" — 这份榜已 {age} 天旧 (正常的上一版榜只有 1-3 天): 上次真榜副本 {QL_LAST_GOOD} 与 docs 副本都不在?"
            " 多半是 git reset 恢复出来的 HEAD 版")
    emit = log.warning if stale else log.info
    if label == "dashboard":
        emit("优质榜看板保留 %s 的榜 (当前文件就是最新的非空榜, 不写)%s", date, note)
        return date
    try:
        shutil.copyfile(path, QL_JS)
        emit("优质榜看板沿用 %s 的榜 (来源 %s: %s)%s", date, label, path, note)
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


INDUSTRY_BASIS = "em_f100"      # 行业归属口径标记 (meta.industry_basis): 东财全市场行业分类 (兜底日 Tushare 别名表换成同一套名)


def _industry_map_ex(ds) -> tuple:
    """行业映射 + 来源留痕 -> (ind_of, 来源名, info)。**口径唯一: 东财全市场行业分类** (老板 2026-09-23 拍板 ①):
    ds.fetch_industry_map() —— 东财 push2 clist f100 全市场一次 (二级口径 ~128 组, 按日缓存 ind_map_<日>, run_pipeline 同日已取过
    就零调用; 与它的市场地位分组 / 行业 PE 中位同一份), 东财不可达时它自己退到 Tushare stock_basic (名字经 TS_INDUSTRY_ALIAS 换成
    东财口径)。日志三态 「行业映射: 来源 …」: 东财批量 (本进程刚从 push2 拉的, INFO) | 东财当日缓存 (INFO) | tushare_stock_basic
    (WARNING, 东财 push2 不可达)。覆盖 < IND_MAP_MIN_CODES → 仍用但来源标 (部分) 并 WARNING 龙头判定降级 (沉默降级是本队记过三次的
    失败形态); 一只都没有 → 「缺失」WARNING, dom 门全挂 (榜多半入池 0 → 按 QL-EMPTY 拒发)。
    **不再走「东财成分口径」** (fetch_industry_list × fetch_industry_cons, 见 IND_MAP_MIN_CODES 注释): 成分接口在这里一次都不调,
    成分哪怕能给 5000 只也不选 —— 用例锁住。"""
    try:
        m = ds.fetch_industry_map() or {}
    except Exception as e:      # noqa: BLE001
        log.warning("行业映射: 批量映射抛错 (%s: %s)", type(e).__name__, e)
        m = {}
    src, info = ds.industry_map_source()
    n = len(m)
    if not m or not src:
        log.warning("行业映射: 来源 缺失 (东财 f100 批量 / Tushare stock_basic 都不可用: %s) → 龙头判定降级, dom 门全挂",
                    info.get("error"))
        return {}, "缺失", {"source": None, "n": 0, "error": info.get("error"), "basis": INDUSTRY_BASIS}
    cov_txt = ds._coverage_text(info)      # noqa: SLF001
    if src == "东财":
        how = "东财当日缓存" if info.get("cached") else "东财批量"
        msg = "行业映射: 来源 %s (push2 clist f100 全市场分类%s), 覆盖 %d 只; 与 run_pipeline 市场地位/行业 PE 中位分组同一份" % (
            how, (", 主机 " + str(info["host"])) if info.get("host") else "", n)
    else:
        msg = "行业映射: 来源 %s (东财 push2 不可达), 覆盖 %d/%d 只%s" % (
            src, n, info.get("total", n), (" (" + cov_txt + ")") if cov_txt else "")
    if n < IND_MAP_MIN_CODES:
        log.warning("%s — 低于 %d 只, 龙头判定降级", msg, IND_MAP_MIN_CODES)
        return m, src + "(部分)", {**info, "n": n, "basis": INDUSTRY_BASIS}
    (log.info if src == "东财" else log.warning)(msg)
    return m, src, {**info, "n": n, "basis": INDUSTRY_BASIS}


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
    写明缺了哪几期与入池数, 看板摆回上一版真榜, 返回 None。正常榜的 meta 带 periods_* 供事后核。

    **抛错也先把旧榜摆回看板, 再上抛** (2026-09-13 卡 QL-EMPTY 回修 MEDIUM): run_pipeline 对这里的异常只 warning
    不阻断, 而服务器 14:00 先 `git reset --hard` 把 dashboard/quality_data.js 退成 HEAD 那份 (08-28 的榜, 09-13 只读
    核过 HEAD 仍是它), 之后 rsync 原样发布 —— 09-04/05/07 三天「argument of type 'float'」抛错时, 站上挂的就是这样
    一份月前旧榜; 首版的沿用逻辑只挂在拒发路径上, 抛错路径没有。异常本身仍上抛给 run_pipeline 留 traceback; 沿用
    旧榜自己失败了也不许盖住原异常。"""
    try:
        return _build_quality(top_n)
    except Exception as e:      # noqa: BLE001
        log.error("优质榜构建抛错 (%s: %s) — 产物文件不再动, 看板先摆回上一版真榜, 异常上抛", type(e).__name__, e)
        try:
            _carry_last_good()
        except Exception as e2:     # noqa: BLE001
            log.warning("优质榜抛错后沿用旧榜也失败: %s", e2)
        raise


def _build_quality(top_n: int) -> dict | None:
    from . import datasource as ds
    reports, cov = ds.fetch_profit_reports_ex(N_PERIODS)
    problem = _coverage_problem(cov, N_PERIODS)
    rows, n_pool = [], None
    srcs = {"spot_source": "无", "valuation_source": None}
    ind_src, ind_info = "缺失", {"source": None, "n": 0}
    if reports:
        # 快照: fetch_spot_snapshot 自己做兜底 (2026-09-23 卡 QL-SPOT: 新浪快照无估值列 → Tushare daily_basic 补;
        # 无行业列 → 批量行业映射补), 这里只读来源留痕进 meta; 东财直连快照什么都不触发。
        spot = ds.fetch_spot_snapshot()
        srcs = ds.spot_sources(spot)
        spot_map = {}
        if spot is not None and not spot.empty:
            for _, r in spot.iterrows():
                spot_map[str(r.get("code", "")).zfill(6)] = r.to_dict()
        ind_of, ind_src, ind_info = _industry_map_ex(ds)
        cov_txt = ds._coverage_text(ind_info)      # noqa: SLF001  (别叫 cov: 上面那个是报告期覆盖)
        log.info("优质榜数据来源: 快照 %s | 估值 %s%s | 行业 %s 覆盖 %d 只%s | PE 口径 %s (行业口径 %s)",
                 srcs.get("spot_source"), srcs.get("valuation_source"),
                 (" (%s)" % srcs["valuation_trade_date"]) if srcs.get("valuation_trade_date") else "",
                 ind_src, len(ind_of), (" (" + cov_txt + ")") if cov_txt else "",
                 srcs.get("pe_basis") or ds.PE_BASIS, INDUSTRY_BASIS)
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
        if is_fin_industry(r.get("industry")):     # 东财 f100 的 银行Ⅱ/证券Ⅱ/保险Ⅱ 也豁免 (09-23 回修)
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
        "meta": {"date": _today().isoformat(),
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
                 "periods_failed": list(cov.get("failed") or []),
                 # 数据来源留痕 (2026-09-23 卡 QL-SPOT): 东财 push2 海外不可达时快照/估值/行业各走了哪条兜底。
                 # 东财正常的日子: 东财直连 / 东财 / 东财。industry_map_coverage 在 Tushare 兜底时带
                 # total/mapped/kept/empty/kept_names (映射到东财口径 / 原样沿用 Tushare 名 / 空)。
                 "spot_source": srcs.get("spot_source"),
                 "valuation_source": srcs.get("valuation_source"),
                 "valuation_trade_date": srcs.get("valuation_trade_date"),
                 # 口径标记 (老板 2026-09-23 拍板, 卡 IND-PE): pe_basis = "ttm" (picks.pe 与门槛 0<pe<31 都是 TTM: 东财 f115 /
                 # Tushare pe_ttm); **没有 pe_basis 的历史榜按「动态市盈率」解释** (东财 f9, 09-23 之前的东财日)。
                 # industry_basis = "em_f100": dom 分组 = 东财全市场行业分类 (兜底日 Tushare 别名表换成同一套名);
                 # 没有它的历史榜是 09-21 前的东财成分口径 (3-13 个一级行业的部分分组) 或 09-23 兜底日的 Tushare 别名 74 组。
                 "pe_basis": srcs.get("pe_basis") or ds.PE_BASIS,
                 "industry_basis": INDUSTRY_BASIS,
                 "industry_source": ind_src,
                 "industry_map_coverage": ind_info},
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
            json.dump({"date": result["meta"]["date"], "pe_basis": result["meta"]["pe_basis"],
                       "industry_basis": INDUSTRY_BASIS, "picks": slim}, f, ensure_ascii=False)
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
