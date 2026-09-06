#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股 Market 适配器 — leftside_core 共用核心的全部市场差异都在这里
==================================================================
回测交易规则 (T+1 / 一字涨跌停 / 0.3% 往返成本)、成长质量标签、价格序列
(腾讯前复权, 盘中丢弃未收盘bar)、基准指数 (沪深300)、个股新闻标题与风险关键词。
"""
from __future__ import annotations
import datetime as dt
import logging

import numpy as np

from .config import DASHBOARD_DIR, DATA_DIR, DB_PATH
from leftside_core.market import Market, set_market

log = logging.getLogger("ashare.market")

GROWTH_TIER = {"🟢 可持续": "G", "🟡 待观察": "M", "🔴 一次性": "W"}
TIER_LABEL = {"G": "🟢 可持续", "M": "🟡 待观察", "W": "🔴 一次性", "NA": "⚪ 无数据"}

NEWS_KEYWORDS = [
    ("减持", "减持"), ("立案", "立案/调查"), ("调查", "立案/调查"), ("处罚", "处罚"), ("被罚", "处罚"),
    ("警示函", "监管措施"), ("问询", "问询函"), ("关注函", "问询函"), ("商誉", "商誉减值"),
    ("减值", "减值"), ("预亏", "预亏"), ("亏损", "亏损"), ("下修", "下修"), ("业绩下滑", "业绩下滑"),
    ("诉讼", "诉讼"), ("仲裁", "诉讼"), ("质押", "质押"), ("违规", "违规"), ("退市", "退市风险"),
    ("辞职", "高管变动"), ("离职", "高管变动"), ("停牌", "停牌"), ("终止", "终止事项"),
    ("解禁", "解禁"), ("定增", "再融资"), ("配股", "再融资"), ("可转债", "再融资"),
]


def _drop_partial_today() -> str | None:
    """若北京时间尚未收盘(15:05前), 返回今天的日期串 -> 丢弃当日未走完的bar。"""
    bj = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
    if bj.hour < 15 or (bj.hour == 15 and bj.minute < 5):
        return bj.date().isoformat()
    return None


def fetch_price_series(codes: list, start: str) -> dict:
    """code -> {"dates":[...], "ohlc": ndarray[N,4] (o,h,l,c)}; 腾讯前复权日线。
    窗口只有几个月 (<640根), 单请求即可拿全; 盘中运行时丢弃今天未走完的bar。
    (stock_detail 兜底由核心统一处理, 这里只负责网络取数。)"""
    from concurrent.futures import ThreadPoolExecutor
    from . import datasource as ds
    today = dt.date.today().isoformat()
    skip_day = _drop_partial_today()
    res = {}

    def one(code):
        try:
            kl = ds.call_with_retry(ds._tencent_chunk, ds._tencent_symbol(code), start, today)
        except Exception:
            return code, None
        rows = []
        for k in (kl or []):
            if not k or len(k) < 5:
                continue
            try:
                o, c, h, l = float(k[1]), float(k[2]), float(k[3]), float(k[4])
            except (TypeError, ValueError):
                continue
            if h < l or min(o, c, h, l) <= 0:
                continue
            d0 = str(k[0])
            if skip_day and d0 >= skip_day:
                continue
            rows.append((d0, o, h, l, c))
        if len(rows) < 5:
            return code, None
        return code, {"dates": [r[0] for r in rows],
                      "ohlc": np.array([r[1:] for r in rows], dtype=float)}

    with ThreadPoolExecutor(max_workers=6) as exe:
        for i, (code, ser) in enumerate(exe.map(one, codes), 1):
            if ser:
                res[code] = ser
            if i % 300 == 0 or i == len(codes):
                log.info("价格进度 %d/%d (拿到 %d)", i, len(codes), len(res))
    return res


def fetch_benchmark():
    from . import datasource as ds
    return ds.fetch_benchmark_close()


def limit_up_oneline(o, h, l, c, prev_c):
    """一字/准一字涨停买不进: 全天几乎无振幅且涨幅接近主板涨停。"""
    if prev_c is None or prev_c <= 0 or c <= 0:
        return False
    return (h - l) < 0.002 * c and c >= prev_c * 1.085


def limit_down_oneline(o, h, l, c, prev_c):
    if prev_c is None or prev_c <= 0 or c <= 0:
        return False
    return (h - l) < 0.002 * c and c <= prev_c * 0.915


def news_titles(code: str) -> list:
    """[(date 'YYYY-MM-DD', title, url)] 东财个股新闻; 失败返回 []。"""
    try:
        from . import datasource as ds
        import akshare as ak
        df = ds.call_with_retry(ak.stock_news_em, symbol=code)
        if df is None or len(df) == 0:
            return []
        out = []
        for _, r in df.iterrows():
            t = str(r.get("新闻标题") or "").strip()
            d = str(r.get("发布时间") or "")[:10]
            u = str(r.get("新闻链接") or "")
            if t and d:
                out.append((d, t, u))
        return out
    except Exception as e:
        log.debug("news %s 失败: %s", code, e)
        return []


def fetch_bars_bulk(codes: list, start: str) -> dict:
    """长历史日线(含成交量), 腾讯前复权, 按 ~700 自然日分页拼接 (单请求上限640根)。
    -> {code: [(d,o,h,l,c,v), ...]} 升序; 盘中丢当日未走完bar。"""
    from concurrent.futures import ThreadPoolExecutor
    from . import datasource as ds
    skip_day = _drop_partial_today()
    today = dt.date.today()
    d0 = dt.date.fromisoformat(start)
    pages = []
    cur = d0
    while cur < today:
        # 880自然日 ≈ 600交易日 < 单请求上限640 -> 5年只需2页, 省1/3请求量 (腾讯有IP配额)
        nxt = min(cur + dt.timedelta(days=880), today)
        pages.append((cur.isoformat(), nxt.isoformat()))
        cur = nxt + dt.timedelta(days=1)
    res = {}

    def one(code):
        rows, seen = [], set()
        for a, b in pages:
            try:
                kl = ds.call_with_retry(ds._tencent_chunk, ds._tencent_symbol(code), a, b)
            except Exception:
                return code, None
            for k in (kl or []):
                if not k or len(k) < 6:
                    continue
                d1 = str(k[0])[:10]
                if d1 in seen or (skip_day and d1 >= skip_day):
                    continue
                try:
                    o, c, h, l, v = (float(k[1]), float(k[2]), float(k[3]),
                                     float(k[4]), float(k[5]))
                except (TypeError, ValueError):
                    continue
                if h < l or min(o, c, h, l) <= 0 or v < 0:
                    continue
                seen.add(d1)
                rows.append((d1, o, h, l, c, v))
        rows.sort()
        return code, (rows if len(rows) >= 60 else None)

    with ThreadPoolExecutor(max_workers=3) as exe:      # 温和并发, 少触发配额
        for i, (code, rows) in enumerate(exe.map(one, codes), 1):
            if rows:
                res[code] = rows
            if i % 300 == 0 or i == len(codes):
                log.info("长历史进度 %d/%d (拿到 %d)", i, len(codes), len(res))
    return res


def fetch_index_bars(start: str) -> list:
    """沪深300 (sh000300) 长历史日线 -> [(d,o,h,l,c,v), ...]。
    三源顺序: 腾讯 -> 东财直连 -> 新浪 (每页失败才降级, 失败原因进日志, 每源根数进日志)。
    2026-09-03 教训: 单源静默 `except: kl = []` 让指数表卡在 09-01 四天无人知, lab 连败。
    三源口径已对齐 (成交量 "手", 见 datasource._em_index_chunk/_sina_index_chunk)。"""
    from . import datasource as ds
    from .config import CONFIG
    sym = CONFIG["source"].get("benchmark_index", "sh000300")
    sources = (("腾讯", ds._tencent_chunk), ("东财", ds._em_index_chunk), ("新浪", ds._sina_index_chunk))
    skip_day = _drop_partial_today()
    today = dt.date.today()
    d0 = dt.date.fromisoformat(start)
    rows, seen = [], set()
    got = {name: 0 for name, _ in sources}
    cur = d0
    while cur < today:
        nxt = min(cur + dt.timedelta(days=700), today)
        kl, used = [], None
        for name, fn in sources:
            try:
                kl = ds.call_with_retry(fn, sym, cur.isoformat(), nxt.isoformat()) or []
            except Exception as e:  # noqa: BLE001
                log.warning("基准指数 %s [%s..%s] %s 失败: %s", sym, cur, nxt, name, str(e)[:120])
                kl = []
            if kl:
                used = name
                break
            log.warning("基准指数 %s [%s..%s] %s 返回空, 尝试下一源", sym, cur, nxt, name)
        if not kl:
            log.error("基准指数 %s [%s..%s] 三源全部失败, 本页无数据", sym, cur, nxt)
        n_page = 0
        for k in (kl or []):
            if not k or len(k) < 6:
                continue
            d1 = str(k[0])[:10]
            if d1 in seen or (skip_day and d1 >= skip_day):
                continue
            try:
                o, c, h, l, v = (float(k[1]), float(k[2]), float(k[3]),
                                 float(k[4]), float(k[5]))
            except (TypeError, ValueError):
                continue
            seen.add(d1)
            rows.append((d1, o, h, l, c, v))
            n_page += 1
        if used:
            got[used] += n_page
        cur = nxt + dt.timedelta(days=1)
    rows.sort()
    log.info("基准指数 %s: %d 根 (%s), 末日 %s", sym, len(rows),
             " / ".join(f"{k} {v}" for k, v in got.items()), rows[-1][0] if rows else "无")
    return rows


def fetch_bars_bulk_em(codes: list, start: str) -> dict:
    """东财备源长历史日线 (腾讯WAF封禁时用): 一只票一请求, 前复权含成交量。
    与腾讯混用无碍 —— 前复权因子只需同一只票内部一致, 跨票无所谓。"""
    from concurrent.futures import ThreadPoolExecutor
    from . import datasource as ds
    import akshare as ak
    skip_day = _drop_partial_today()
    s8 = start.replace("-", "")
    e8 = dt.date.today().strftime("%Y%m%d")

    def one(code):
        try:
            df = ds.call_with_retry(ak.stock_zh_a_hist, symbol=code, period="daily",
                                    start_date=s8, end_date=e8, adjust="qfq")
        except Exception:
            return code, None
        if df is None or len(df) < 60:
            return code, None
        rows = []
        for _, r in df.iterrows():
            d1 = str(r["日期"])[:10]
            if skip_day and d1 >= skip_day:
                continue
            try:
                o, h, l, c, v = (float(r["开盘"]), float(r["最高"]), float(r["最低"]),
                                 float(r["收盘"]), float(r["成交量"]))
            except (TypeError, ValueError, KeyError):
                continue
            if h < l or min(o, h, l, c) <= 0 or v < 0:
                continue
            rows.append((d1, o, h, l, c, v))
        rows.sort()
        return code, (rows if len(rows) >= 60 else None)

    res = {}
    with ThreadPoolExecutor(max_workers=2) as exe:      # 东财白天限流, 更温和
        for i, (code, rows) in enumerate(exe.map(one, codes), 1):
            if rows:
                res[code] = rows
            if i % 200 == 0 or i == len(codes):
                log.info("EM长历史进度 %d/%d (拿到 %d)", i, len(codes), len(res))
    return res


def fetch_index_bars_em(start: str) -> list:
    """沪深300 长历史: 东财优先, 挂了走新浪 (列名英文)。"""
    from . import datasource as ds
    import akshare as ak
    skip_day = _drop_partial_today()
    rows = []
    try:
        df = ds.call_with_retry(ak.index_zh_a_hist, symbol="000300", period="daily",
                                start_date=start.replace("-", ""),
                                end_date=dt.date.today().strftime("%Y%m%d"))
        for _, r in df.iterrows():
            d1 = str(r["日期"])[:10]
            if not (skip_day and d1 >= skip_day):
                rows.append((d1, float(r["开盘"]), float(r["最高"]), float(r["最低"]),
                             float(r["收盘"]), float(r["成交量"])))
    except Exception as e:
        log.warning("EM指数长历史失败(改用新浪): %s", e)
        try:
            df = ds.call_with_retry(ak.stock_zh_index_daily, symbol="sh000300")
            for _, r in df.iterrows():
                d1 = str(r["date"])[:10]
                if d1 < start or (skip_day and d1 >= skip_day):
                    continue
                rows.append((d1, float(r["open"]), float(r["high"]), float(r["low"]),
                             float(r["close"]), float(r["volume"])))
        except Exception as e2:
            log.warning("新浪指数长历史也失败: %s", e2)
            return []
    rows.sort()
    return rows


def fetch_bars_bulk_fuyao(codes: list, start: str) -> dict:
    """同花顺金融数据API (fuyao) 长历史日线: 一次请求整段前复权, 无拼接断裂。
    注意: 同一只票的序列必须整段来自同一数据源 (复权基准不同, 混拼会有跳变)。"""
    from concurrent.futures import ThreadPoolExecutor
    from . import fuyao
    if not fuyao.available():
        return {}
    years = min(9.8, max(1.0, (dt.date.today() - dt.date.fromisoformat(start)).days / 365.0))
    skip_day = _drop_partial_today()

    def one(code):
        try:
            df = fuyao.hist(code, years=years)
        except Exception:
            return code, None
        if df is None or "volume" not in df.columns:
            return code, None
        rows = []
        for _, r in df.iterrows():
            d1 = str(r["date"])[:10]
            if d1 < start or (skip_day and d1 >= skip_day):
                continue
            try:
                o, h, l, c, v = (float(r["open"]), float(r["high"]), float(r["low"]),
                                 float(r["close"]), float(r["volume"]))
            except (TypeError, ValueError):
                continue
            if h < l or min(o, h, l, c) <= 0 or v < 0:
                continue
            rows.append((d1, o, h, l, c, v))
        rows.sort()
        return code, (rows if len(rows) >= 60 else None)

    res = {}
    with ThreadPoolExecutor(max_workers=4) as exe:
        for i, (code, rows) in enumerate(exe.map(one, codes), 1):
            if rows:
                res[code] = rows
            if i % 200 == 0 or i == len(codes):
                log.info("fuyao长历史进度 %d/%d (拿到 %d)", i, len(codes), len(res))
    return res


def universe_codes() -> list:
    """全A代码 (主板/创业板/科创板: 0/3/6 前缀; 剔除北交所)。"""
    from . import datasource as ds
    spot = ds.fetch_spot_snapshot()
    if spot is None or spot.empty:
        return []
    out = []
    for c in spot["code"]:
        c = str(c).zfill(6)
        if c[0] in ("0", "3", "6"):
            out.append(c)
    return sorted(set(out))


MARKET = set_market(Market(
    name="ashare",
    dashboard_dir=DASHBOARD_DIR, data_dir=DATA_DIR, db_path=DB_PATH,
    t_plus_one=True, limit_boards=True, cost_rt=0.003,
    growth_tier=GROWTH_TIER, tier_label=TIER_LABEL,
    fetch_price_series=fetch_price_series, fetch_benchmark=fetch_benchmark,
    limit_up_oneline=limit_up_oneline, limit_down_oneline=limit_down_oneline,
    news_titles=news_titles, news_keywords=NEWS_KEYWORDS,
    fetch_bars_bulk=fetch_bars_bulk, fetch_index_bars=fetch_index_bars,
    universe_codes=universe_codes,
    log_prefix="ashare",
))
