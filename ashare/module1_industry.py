#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块1 — 行业景气度评分 (Industry Prosperity Score)
==================================================
对每个 (东财) 一级行业, 用五大支柱算景气总分, 选 Top N 作为模块2的候选池。

五大支柱 (各自先横截面归一, 再加权):
  A 趋势   weight 0.25  —— 指数 vs MA60/MA120 + 60日斜率
  B 动量   weight 0.25  —— 20日/60日涨幅 + 对沪深300的60日超额
  C 广度   weight 0.20  —— 成分股在MA60上方比例 / 20日正收益比例 / 近5日涨跌家数差
  D 资金   weight 0.15  —— 行业主力净流入(近5日); 拿不到则权重并入 A、B
  E 基本面 weight 0.15  —— 行业聚合净利/营收同比; 拿不到则权重并入其它

归一: 每个支柱的原始值先做横截面 zscore 合成, 再转成 0-100 横截面百分位。
总分 = 100 * Σ(归一权重_i * 百分位_i/100)。
趋势硬门槛: 行业指数需在 MA120 上方(或容差内) 才有资格入选。

**行业指数末日 = data_date 守卫 (2026-10-04 卡 IND-T1)**: 趋势/动量两根支柱吃的是行业指数日线的**最后一根**。同花顺年度
日线文件里当日那根北京 ~21 点后才落地, 跑批 (北京 17:30) 拿到的末日是 T-1, 而看板/快照/模拟盘登记按 data_date = T 标
—— 09-28 那份快照 90 个行业的 idx_close 与 09-25 (休市重跑) 那份逐个相同。现在每个行业算特征之前先过
`align_industry_tails`: 末日 == data_date 不动; 落后 -> ① Tushare ths_daily 当日那根 (带日期, 与同花顺日线逐位相同)
② 同花顺行业一览的当日涨跌幅补一根 (close = 昨收 × (1 + 涨跌幅%)); 都补不上 -> 照旧用旧一日的算, 但每个行业带
`idx_date` (实际末日) / `idx_fill` (哪条兜底补的), 导出层据此写 meta.industry_asof, 看板景气榜标题旁标「行业数据截至 X」。
"""
from __future__ import annotations
import collections
import datetime as dt
import logging
import numpy as np
import pandas as pd

from .config import CONFIG
from . import datadate as _dd
from . import datasource as ds
from . import indicators as ind
from .statutil import zscore, cross_sectional_percentile, nanmean, safe_div

log = logging.getLogger("ashare.module1")

#: ths_daily 最多往回补几个交易日 (正常只差当日一根; 差得更多说明同花顺那份年度文件连着几天没更新, 不硬补)。
TAIL_MAX_GAP_DAYS = 3
#: ths_daily 的 pre_close 与日线末根收盘「接得上」的容差 (指数点; 两头都是 3 位小数, 09-30 90 个行业实测差 0)。
PRECLOSE_TOL = 0.011
#: 行业一览涨跌幅 (2 位小数, 同花顺是截断) 与日线末根自己的涨跌幅算「相同」的容差 (百分点; 09-30 实测最大差 0.0052)。
SUMMARY_SAME_TOL = 0.011
#: 落后的行业里, 一览涨跌幅与日线末根涨跌幅相同的占比 > 这个数 -> 判不了一览是另一个交易日的, 不补。
#: 相邻两个交易日 90 个行业 2 位小数涨跌幅相同的只有 0-1 个 (09-21..09-30 六对实测), 同一个交易日则是 90/90。
SUMMARY_DIFF_SHARE = 0.2
SUMMARY_SAME_SHARE = 0.8        # ≥ 这个数 = 明确是同一个交易日 (只影响日志话术)
SUMMARY_PCT_SANE = 21.0         # 行业指数单日涨跌幅绝对值超过它 = 脏数, 不拿来补
FILL_TS, FILL_SUMMARY = "ts_ths_daily", "ths_summary"
_BASIS_CN = {"store": "价格库个股末日", "bench": "基准指数末日", "none": "无"}


def _ret(close: pd.Series, bars: int) -> float:
    s = close.dropna()
    if len(s) <= bars:
        return np.nan
    return float(s.iloc[-1] / s.iloc[-1 - bars] - 1.0)


def _industry_index_features(hist: pd.DataFrame, bench_ret60: float) -> dict:
    """从行业指数日线算 趋势/动量 的原始分量。"""
    close = hist["close"].astype(float)
    px = close.iloc[-1]
    ma60 = close.rolling(60).mean().iloc[-1]
    ma120 = close.rolling(120).mean().iloc[-1]
    t1 = safe_div(px - ma60, ma60)
    t2 = safe_div(px - ma120, ma120)
    t3 = ind.reg_slope_norm(close, 60)
    m1 = _ret(close, 20)
    m2 = _ret(close, 60)
    m3 = (m2 - bench_ret60) if (not np.isnan(m2) and not np.isnan(bench_ret60)) else np.nan
    return {
        "idx_close": float(px),
        "ma120": float(ma120) if not np.isnan(ma120) else np.nan,
        "above_ma120": (not np.isnan(ma120)) and px >= ma120,
        "t1": t1, "t2": t2, "t3": t3,
        "m1": m1, "m2": m2, "m3": m3,
    }


def _breadth(cons: pd.DataFrame, sample: int) -> dict:
    """成分股广度: 抽样成分股, 算 MA60上方比例 / 20日正收益比例 / 近5日涨跌家数差。"""
    if cons is None or cons.empty:
        return {"b1": np.nan, "b2": np.nan, "b3": np.nan, "n": 0}
    codes = list(cons["code"])[:sample]
    above_ma60, pos20, adv, dec, n = 0, 0, 0, 0, 0
    for code in codes:
        h = ds.fetch_hist(code)
        if h is None or len(h) < 65:
            continue
        c = h["close"].astype(float)
        px = c.iloc[-1]
        ma60 = c.rolling(60).mean().iloc[-1]
        if not np.isnan(ma60):
            above_ma60 += 1 if px >= ma60 else 0
        r20 = _ret(c, 20)
        if not np.isnan(r20):
            pos20 += 1 if r20 > 0 else 0
        r5 = _ret(c, 5)
        if not np.isnan(r5):
            if r5 > 0:
                adv += 1
            elif r5 < 0:
                dec += 1
        n += 1
    if n == 0:
        return {"b1": np.nan, "b2": np.nan, "b3": np.nan, "n": 0}
    return {
        "b1": above_ma60 / n,
        "b2": pos20 / n,
        "b3": safe_div(adv - dec, n, 0.0),
        "n": n,
    }


# ---------------------------------------------------------------------------
#  行业指数末日 = data_date 守卫 (2026-10-04 卡 IND-T1)
# ---------------------------------------------------------------------------
def _last_date(hist: pd.DataFrame) -> str:
    return str(hist["date"].iloc[-1])[:10]


def synth_close(prev_close: float, pct_chg: float) -> float:
    """行业一览涨跌幅补根的算术: close_T = close_{T-1} × (1 + 涨跌幅% / 100), 留 3 位小数 (同花顺指数就是 3 位)。"""
    return round(float(prev_close) * (1.0 + float(pct_chg) / 100.0), 3)


def _append_bar(hist: pd.DataFrame, date: str, close: float, open_=np.nan, high=np.nan, low=np.nan) -> pd.DataFrame:
    """日线末尾接一根 -> **新** DataFrame (原表不动: 它可能就是当日缓存里那份)。没给的列 (成交额等) 留 NaN。"""
    out = hist.reset_index(drop=True).copy()
    row = {c: np.nan for c in out.columns}
    row.update({"date": str(date)[:10], "close": float(close)})
    for k, v in (("open", open_), ("high", high), ("low", low)):
        if k in row:
            row[k] = v
    out.loc[len(out)] = [row[c] for c in out.columns]
    return out


def _cal_between(cal, after: str, upto: str):
    """交易日历 cal (ISO 日期) 里 (after, upto] 的交易日, 升序; cal 为 None (没有日历) -> None。"""
    if cal is None:
        return None
    return sorted({str(d)[:10] for d in cal if after < str(d)[:10] <= upto})


def _counts_text(dates) -> str:
    c = collections.Counter(dates)
    return " / ".join("%s×%d" % (d, c[d]) for d in sorted(c)) if len(c) > 1 else (next(iter(c)) if c else "—")


def _board_codes(ind_list) -> dict:
    """{行业名: 同花顺板块代码 (6 位数字, 881xxx)}。行业列表是东财口径 (代码 BKxxxx) 或没有代码列 -> {}。"""
    if ind_list is None or "board_code" not in getattr(ind_list, "columns", ()):
        return {}
    out = {}
    for name, code in zip(ind_list["industry"], ind_list["board_code"]):
        code = str(code).strip()
        if len(code) == 6 and code.isdigit():
            out[str(name)] = code
    return out


def _market_calendar():
    try:
        from leftside_core.market import current
        return getattr(current(), "trading_days", None)
    except Exception:                                          # noqa: BLE001
        return None


def _bench_calendar(bench, expect, trading_days=None):
    """「差几根」用的交易日历 (ISO 日期升序, 近 60 个交易日) -> list | None (= 不知道, 调用方按不知道处理)。

    基准指数序列每个交易日一根, 到它自己的末日为止是完整的日历 (阶段A 读库时它就是库内 idx_bars, 零联网)。
    **基准末日 < data_date 时, (基准末日, data_date] 这一段不能靠猜**: 只补一个 data_date 进去的话, 中间若还隔着交易日
    (指数腿落后两天以上), 「只差一根」那道闸会把差两根的行业放过去, 行业一览的单日涨跌幅就乘到了隔天的收盘上。
    这一段去问 Market 日历 (A 股 = Tushare trade_cal, 一次调用, 带硬期限); 问不到 / 结果里没有 data_date -> None。"""
    try:
        if bench is None or len(bench) == 0:
            return None
        days = {str(d)[:10] for d in bench["date"].tail(60)}
    except Exception:                                          # noqa: BLE001
        return None
    days.discard("")
    if not days:
        return None
    exp = str(expect)[:10] if expect else None
    b_last = max(days)
    if exp and b_last < exp:
        fn = trading_days if trading_days is not None else _market_calendar()
        extra = None
        if fn is not None:
            try:
                extra = [str(d)[:10] for d in (fn(b_last, exp) or [])]
            except Exception as e:                             # noqa: BLE001
                log.debug("行业指数守卫: 开市日历取失败 (%s), 交易日历按不知道处理", str(e)[:120])
                extra = None
        if not extra or exp not in extra:
            log.info("行业指数守卫: 基准指数末日 %s 落后 data_date %s 且开市日历问不到, 这一轮数不清「差几根」"
                     " (ths_daily 仍靠 pre_close 核接续, 行业一览涨跌幅不补)", b_last, exp)
            return None
        days.update(extra)
    return sorted(d for d in days if not exp or d <= exp)


def _fill_from_ths_daily(out: dict, last: dict, lag: list, codes: dict, expect: str, cal, info: dict) -> int:
    """兜底 ①: Tushare ths_daily 把落后的行业一根根接到 data_date -> 接到 data_date 的行业数。就地改 out / last。

    只接「接得上」的: 每一根的 pre_close 必须等于日线当前末根的收盘 (容差 PRECLOSE_TOL) —— 接不上说明中间还缺别的根,
    或两头不是同一个指数, 那就停在原地 (宁可留痕落后, 不拼一条断了的序列)。每个缺的交易日一次调用, 最多
    TAIL_MAX_GAP_DAYS 次 (正常就是 data_date 那一次)。"""
    need = {}
    for k in lag:
        if not codes.get(k):
            continue
        days = _cal_between(cal, last[k], expect)
        if days is None:
            days = [expect]                       # 没有日历: 只问 data_date 那一根, 连续性全靠 pre_close 核
        if days and days[-1] == expect and len(days) <= TAIL_MAX_GAP_DAYS:
            need[k] = days
    if not need:
        log.info("行业指数兜底 ① Tushare ths_daily: 落后的 %d 个行业没有同花顺板块代码 (行业列表不是同花顺口径) 或落后超过 "
                 "%d 个交易日, 跳过", len(lag), TAIL_MAX_GAP_DAYS)
        return 0
    bars = {d: ds.fetch_industry_bars_tushare(d) for d in sorted({d for v in need.values() for d in v})}
    done = absent = broken = 0
    for k, days in need.items():
        h, code, added = out[k], codes[k], 0
        for d in days:
            bar = (bars.get(d) or {}).get(code)
            if not bar:
                absent += 1
                break
            pre, prev = bar.get("pre_close"), float(h["close"].iloc[-1])
            has_pre = pre is not None and pre == pre
            if (has_pre and abs(float(pre) - prev) > PRECLOSE_TOL) or (not has_pre and cal is None):
                broken += 1
                log.debug("行业指数兜底 ①: %s (%s) %s 的 pre_close %s 接不上日线末根 %s 的收盘 %s", k, code, d, pre, last[k], prev)
                break
            h = _append_bar(h, d, bar["close"], bar.get("open", np.nan), bar.get("high", np.nan), bar.get("low", np.nan))
            added += 1
        if added:
            out[k], last[k] = h, _last_date(h)
            if last[k] == expect:
                info["fill_by"][k] = FILL_TS
                done += 1
    n_rows = len(bars.get(expect) or {})
    log.info("行业指数兜底 ① Tushare ths_daily: 补齐 %d/%d 个行业 (%s 当日 %d 行%s%s)", done, len(need), expect, n_rows,
             "" if n_rows else " —— 当日还没入库或调用失败",
             (", 表里没有该行业 %d 个 / pre_close 接不上 %d 个" % (absent, broken)) if (n_rows and (absent or broken)) else "")
    return done


def _fill_from_summary(out: dict, last: dict, lag: list, expect: str, cal, now, trading_days, info: dict) -> int:
    """兜底 ②: 同花顺行业一览的当日涨跌幅补 data_date 那一根 -> 补上的行业数。就地改 out / last。

    行业一览是**不带日期的实时快照**, 三道闸都过才补:
      ⓐ 只差一根: 该行业日线末日到 data_date 之间, 按交易日历恰好只缺 data_date (差两根以上单日涨跌幅补不了; 没有日历不补);
      ⓑ 时钟: `datadate.session_settled` —— data_date 已收盘且之后没有交易日开过市 (盘中 / 次日开盘后的一览不是它的收盘值);
      ⓒ 不是同一天: 一览涨跌幅与日线末根**自己的**涨跌幅逐行业比, 相同的占比 > SUMMARY_DIFF_SHARE 就不补 —— 相同说明
         一览还是日线末根那个交易日的 (没翻到 data_date), 再乘一遍就是把同一天的涨跌幅算了两次。"""
    cands = [k for k in lag if _cal_between(cal, last[k], expect) == [expect]]
    if not cands:
        log.info("行业指数兜底 ② 行业一览涨跌幅: 落后的 %d 个行业不是「恰好只差 data_date 一根」(或没有交易日历可核), 不补", len(lag))
        return 0
    summ = ds.fetch_industry_summary_ths()
    if summ is None or "pct_chg" not in summ.columns or "industry" not in summ.columns:
        log.warning("行业指数兜底 ② 行业一览涨跌幅: 同花顺行业一览取不到 (或没有涨跌幅列), 不补")
        return 0
    at = now
    if at is None:
        ts = (getattr(summ, "attrs", None) or {}).get("fetched_at")
        at = dt.datetime.fromtimestamp(float(ts), tz=_dd.BJ_TZ) if ts else dt.datetime.now(_dd.BJ_TZ)
    ok, why = _dd.session_settled(expect, now=at, trading_days=trading_days)
    if not ok:
        log.warning("行业指数兜底 ② 行业一览涨跌幅: 一览此刻不是 data_date %s 的收盘值 (%s), 不补", expect, why)
        return 0
    pct = {}
    for name, p in zip(summ["industry"], summ["pct_chg"]):
        try:
            p = float(p)
        except (TypeError, ValueError):
            continue
        if p == p and abs(p) <= SUMMARY_PCT_SANE:
            pct[str(name).strip()] = p
    same = seen = 0
    for k in cands:
        c = out[k]["close"].astype(float)
        if k not in pct or len(c) < 2 or not c.iloc[-2]:
            continue
        seen += 1
        same += int(abs(pct[k] - (c.iloc[-1] / c.iloc[-2] - 1.0) * 100.0) <= SUMMARY_SAME_TOL)
    if seen == 0:
        log.warning("行业指数兜底 ② 行业一览涨跌幅: 一览里找不到落后的这 %d 个行业 (名字对不上?), 不补", len(cands))
        return 0
    if same / seen > SUMMARY_DIFF_SHARE:
        log.warning("行业指数兜底 ② 行业一览涨跌幅: 一览涨跌幅与日线末根自己的涨跌幅有 %d/%d 个行业相同 —— %s, 不补 "
                    "(再乘一遍会把同一天的涨跌幅算两次)", same, seen,
                    "一览还是日线末根那个交易日的, 没翻到 data_date %s" % expect if same / seen >= SUMMARY_SAME_SHARE
                    else "判不了一览是不是 data_date %s 的" % expect)
        return 0
    done = 0
    for k in cands:
        if k not in pct:
            continue
        h = out[k]
        out[k] = _append_bar(h, expect, synth_close(float(h["close"].iloc[-1]), pct[k]))
        last[k] = expect
        info["fill_by"][k] = FILL_SUMMARY
        done += 1
    log.info("行业指数兜底 ② 行业一览涨跌幅补根: 补齐 %d/%d 个行业 (close = 昨收 × (1 + 当日涨跌幅%%), 涨跌幅只有 2 位小数, "
             "与官方收盘差 ≤ ~0.5 bp; %s)", done, len(cands), why)
    return done


def align_industry_tails(hists: dict, codes: dict | None, expect: str | None, basis: str = "store",
                         cal=None, now=None, trading_days=None) -> tuple[dict, dict]:
    """行业指数日线的末日对齐 data_date -> (对齐后的 {行业: 日线}, info)。**不改传进来的表** (接/截都出新表)。

    hists  = {行业: 日线 DataFrame(date, close, …) 升序};  codes = {行业: 同花顺板块代码} (ths_daily 用, 可空)
    expect = 这批产物的行情日 (`datadate.peek_data_date`); None = 判不了 (没读库也没基准) -> 不守卫
    cal    = 交易日历 (ISO 日期列表, 或返回它的零参函数 —— 真落后了才求值; 模块1 传 _bench_calendar), 用来数「差几根」;
             None = 不知道 (ths_daily 只问 data_date 那一根并靠 pre_close 核接续; 行业一览涨跌幅不补)
    now / trading_days = 只给兜底 ② 的时钟闸用 (用例注入; 生产 now 取行业一览的取数时刻, trading_days 取 Market 日历)

    四种结局, 每种恰好一行汇总日志 (journal 里 grep「行业指数」):
      · 末日 == expect                 INFO   「行业指数末日 X = data_date (N 个行业, 无需兜底)」
      · 落后, 兜底补齐                 WARNING「…落后 data_date…」 + INFO「行业指数末日 X = data_date (兜底补齐 …)」
      · 落后, 兜底后仍落后             WARNING ×2 (第二条点名 meta.industry_asof 与看板标注)
      · 末日比 expect 还新 (库旧了)    WARNING, 截到 expect (快照标的是 expect 的行情, 行业数据不许带它之后的)
    info = {expect, basis, n, raw_last {末日: 行业数} (动手之前), truncated, filled {来源: 行业数}, fill_by {行业: 来源},
            asof (对齐后最旧的末日), n_lag (对齐后仍落后的行业数)}。"""
    n = len(hists)
    last0 = {k: _last_date(h) for k, h in hists.items()}
    info = {"expect": expect, "basis": basis, "n": n, "raw_last": dict(collections.Counter(last0.values())),
            "truncated": 0, "filled": {}, "fill_by": {}, "asof": (min(last0.values()) if last0 else None), "n_lag": 0}
    if n == 0:
        return hists, info
    if not expect:
        log.info("行业指数末日 %s: 这一轮既没读价格库也没拿到基准指数, 判不了它该是哪一天 (不守卫, 不兜底)",
                 _counts_text(last0.values()))
        return hists, info
    out, last = dict(hists), dict(last0)
    ahead = sorted(k for k, d in last.items() if d > expect)
    for k in ahead:
        h = out[k]
        cut = h[h["date"].astype(str).str[:10] <= expect].reset_index(drop=True)
        if len(cut):
            out[k], last[k] = cut, _last_date(cut)
    if ahead:
        info["truncated"] = len(ahead)
        log.warning("行业指数末日晚于 data_date %s: %d/%d 个行业 (末日 %s) 截到 data_date —— 这批产物标的是 %s (%s) 的行情, "
                    "行业数据不许带它之后的", expect, len(ahead), n, _counts_text(last0[k] for k in ahead), expect,
                    _BASIS_CN.get(basis, basis))
    lag = sorted(k for k, d in last.items() if d < expect)
    if not lag:
        info["asof"] = min(last.values())
        log.info("行业指数末日 %s = data_date (%d 个行业, 无需兜底)", expect, n)
        return out, info
    log.warning("行业指数末日落后 data_date %s: %d/%d 个行业停在 %s (同花顺年度日线文件的当日那根北京 ~21 点后才落地) "
                "—— 兜底 ① Tushare ths_daily ② 同花顺行业一览当日涨跌幅补根", expect, len(lag), n,
                _counts_text(last[k] for k in lag))
    if callable(cal):                             # 日历惰性求值: 只有真落后了才去数「差几根」(可能要问一次 trade_cal)
        cal = cal()
    n_ts = _fill_from_ths_daily(out, last, lag, codes or {}, expect, cal, info)
    lag = [k for k in lag if last[k] < expect]
    n_sum = 0
    if lag:
        n_sum = _fill_from_summary(out, last, lag, expect, cal, now,
                                   trading_days if trading_days is not None else _market_calendar(), info)
        lag = [k for k in lag if last[k] < expect]
    info["filled"] = {k: v for k, v in ((FILL_TS, n_ts), (FILL_SUMMARY, n_sum)) if v}
    info["asof"], info["n_lag"] = min(last.values()), len(lag)
    if lag:
        log.warning("行业指数兜底后仍落后 data_date %s: %d/%d 个行业停在 %s (Tushare ths_daily 补 %d 个 / 行业一览涨跌幅补 %d 个) "
                    "—— 这些行业的景气分按旧一日的指数算; meta.industry_asof = %s, 看板景气榜标题旁显示「行业数据截至 %s」",
                    expect, len(lag), n, _counts_text(last[k] for k in lag), n_ts, n_sum, info["asof"], info["asof"])
    else:
        log.info("行业指数末日 %s = data_date (兜底补齐 %d 个行业: Tushare ths_daily %d 个 / 行业一览涨跌幅 %d 个)",
                 expect, n_ts + n_sum, n_ts, n_sum)
    return out, info


def compute_industry_scores(progress_cb=None) -> pd.DataFrame:
    """
    返回 DataFrame, 一行一个行业, 列:
      industry, prosperity_score, trend, momentum, breadth, capital, fundamental,
      idx_close, ma120, above_ma120, eligible, selected, breadth_n,
      idx_date (该行业指数日线的实际末日), idx_fill (末根由哪条兜底补的: ts_ths_daily / ths_summary / None)
    sub-score 列 (trend/momentum/breadth/capital/fundamental) 为 0-100 横截面百分位;
    不可用的支柱该列为 NaN。`df.attrs["industry_asof"]` = align_industry_tails 的 info (去掉逐行业明细)。
    """
    cfg = CONFIG["industry"]
    ind_list = ds.fetch_industry_list()
    if ind_list is None or ind_list.empty:
        log.warning("行业列表拉取失败, 模块1 返回空")
        return pd.DataFrame()

    industries = list(ind_list["industry"].dropna().unique())
    bench = ds.fetch_benchmark_close()
    bench_ret60 = np.nan
    if bench is not None and len(bench) > 61:
        bc = bench["close"].astype(float)
        bench_ret60 = float(bc.iloc[-1] / bc.iloc[-61] - 1.0)

    # 资金流 (可选)
    flow = ds.fetch_industry_fund_flow()
    flow_map = {}
    if flow is not None and not flow.empty:
        flow_map = dict(zip(flow["industry"], flow["net_inflow"]))

    def _one_industry(name):
        hist = ds.fetch_industry_hist(name)
        if hist is None or len(hist) < 130:
            log.debug("行业 %s 指数数据不足, 跳过", name)
            return None
        cons = ds.fetch_industry_cons(name)
        br = _breadth(cons, cfg["breadth_sample"])
        # 趋势/动量特征**不在这里算**: 日线末日要先在主线程里统一对齐 data_date (align_industry_tails), 再算
        return {
            "industry": name,
            "_hist": hist,
            "b1": br["b1"], "b2": br["b2"], "b3": br["b3"], "breadth_n": br["n"],
            "c1": flow_map.get(name, np.nan),
        }

    # 各行业相互独立, 并发拉取指数/成分以加速
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import os as _os
    workers = CONFIG["fetch"].get("max_workers") or min(16, (_os.cpu_count() or 4) * 2)
    rows = []
    total = len(industries)
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_one_industry, n): n for n in industries}
        for fut in as_completed(futs):
            done += 1
            if progress_cb:
                progress_cb(done, total, futs[fut])
            try:
                r = fut.result()
            except Exception as e:
                log.debug("行业 %s 计算失败: %s", futs[fut], e)
                r = None
            if r is not None:
                rows.append(r)

    if not rows:
        return pd.DataFrame()

    # ---- 行业指数末日 = data_date 守卫 (卡 IND-T1): 先对齐, 再算趋势/动量 ----
    # 主线程里做 (线程池已收尾): 兜底 ② 走同花顺 (V8), 兜底 ① 走 Tushare, 都只在「末日 < data_date」时才联网。
    raw_hists = {r["industry"]: r.pop("_hist") for r in rows}
    expect, basis = _dd.peek_data_date(bench)
    try:
        hists, asof = align_industry_tails(raw_hists, _board_codes(ind_list), expect, basis,
                                           cal=lambda: _bench_calendar(bench, expect))
    except Exception as e:                                     # noqa: BLE001  守卫自己出错不许拖垮整轮
        log.warning("行业指数末日守卫出错 (%s: %s), 本轮按原始日线算 (末日见各行业 idx_date)", type(e).__name__, str(e)[:160],
                    exc_info=True)
        hists, asof = raw_hists, {"expect": expect, "basis": basis, "n": len(raw_hists), "fill_by": {}, "error": str(e)[:160]}
    for r in rows:
        h = hists[r["industry"]]
        r.update(_industry_index_features(h, bench_ret60))
        r["idx_date"] = _last_date(h)
        r["idx_fill"] = asof["fill_by"].get(r["industry"])

    df = pd.DataFrame(rows)

    # ---- 支柱A 趋势: zscore(t1,t2,t3) 横截面 -> 行均值 ----
    df["A_raw"] = pd.concat([zscore(df["t1"]), zscore(df["t2"]), zscore(df["t3"])],
                            axis=1).mean(axis=1, skipna=True)
    # ---- 支柱B 动量 ----
    df["B_raw"] = pd.concat([zscore(df["m1"]), zscore(df["m2"]), zscore(df["m3"])],
                            axis=1).mean(axis=1, skipna=True)
    # ---- 支柱C 广度: b1,b2 ∈[0,1]; b3=(涨-跌)/n ∈[-1,1], 先线性映射到[0,1]再取均值 ----
    b3_scaled = (df["b3"] + 1.0) / 2.0
    df["C_raw"] = pd.concat([df["b1"], df["b2"], b3_scaled], axis=1).mean(axis=1, skipna=True)
    # ---- 支柱D 资金 ----
    has_capital = df["c1"].notna().any()
    df["D_raw"] = zscore(df["c1"]) if has_capital else np.nan
    # ---- 支柱E 基本面: 默认不计算 (聚合财务成本高), 留接口, 权重按行并入其它 ----
    has_fundamental = False
    df["E_raw"] = np.nan

    # 每个支柱 -> 横截面百分位 0-100。fill=None 保留 NaN: 某行该支柱无数据时,
    # 既不会被给中位分, 其权重也会在 _score_row 里按行重新分配 (而非全局判断)。
    df["trend"] = cross_sectional_percentile(df["A_raw"], fill=None)
    df["momentum"] = cross_sectional_percentile(df["B_raw"], fill=None)
    df["breadth"] = cross_sectional_percentile(df["C_raw"], fill=None)
    df["capital"] = cross_sectional_percentile(df["D_raw"], fill=None) if has_capital else np.nan
    df["fundamental"] = cross_sectional_percentile(df["E_raw"], fill=None) if has_fundamental else np.nan

    log.info("景气支柱可用情况: 资金=%s 基本面=%s (缺数据的支柱按行重新分配权重)",
             has_capital, has_fundamental)

    weights = cfg["weights"]

    def _score_row(r):
        # 仅对"该行有数据"的支柱加权, 并在这些支柱上重新归一权重
        num, den = 0.0, 0.0
        for pillar, wt in weights.items():
            pct = r.get(pillar)
            if pct is not None and not (isinstance(pct, float) and np.isnan(pct)):
                num += wt * (pct / 100.0)
                den += wt
        return round(100.0 * num / den, 2) if den > 0 else np.nan

    df["prosperity_score"] = df.apply(_score_row, axis=1)

    # 趋势硬门槛
    if cfg["trend_gate_enabled"]:
        tol = cfg["trend_gate_tolerance_pct"] / 100.0
        df["eligible"] = df.apply(
            lambda r: (not np.isnan(r["ma120"])) and r["idx_close"] >= r["ma120"] * (1 - tol),
            axis=1)
    else:
        df["eligible"] = True

    df = df.sort_values("prosperity_score", ascending=False).reset_index(drop=True)

    # Top N 入选 (仅在合格者中取)
    if cfg["use_full_market"]:
        df["selected"] = True
    else:
        elig = df[df["eligible"]].head(cfg["top_n"])
        sel_names = set(elig["industry"])
        df["selected"] = df["industry"].isin(sel_names)

    cols = ["industry", "prosperity_score", "trend", "momentum", "breadth",
            "capital", "fundamental", "idx_close", "ma120", "above_ma120",
            "eligible", "selected", "breadth_n", "idx_date", "idx_fill"]
    res = df[[c for c in cols if c in df.columns]]
    try:
        res.attrs["industry_asof"] = {k: v for k, v in asof.items() if k != "fill_by"}
    except Exception:                                          # noqa: BLE001
        pass
    return res
