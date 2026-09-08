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


def fetch_price_series(codes: list, start: str, need_date: str | None = None) -> dict:
    """回测/模拟盘/双周的取价。

    -> {code: {"dates":[...], "ohlc": ndarray[N,4] 前复权(o,h,l,c),
               "raw_close": ndarray[N] 原始收盘 (只有走库时才有)}}

    **主路: 直读 data/pricestore.db** (P2, 2026-09-07)。这是阶段A 之后剩下的最后一条逐股网络
    依赖: 09-07 晚上 2,586 只走腾讯只拿回 657 只 (WAF 配额), 服务器同日 14:00 那轮更是
    "价格进度 543/543 (拿到 2)" 全靠 stock_detail 兜底 —— 这条路已经烂了。读库实测 0.19 秒。
    同时多返回一条 `raw_close`: 核心的 `find_anchor` 要用**原始价**匹配快照价 (快照价是当天的
    成交价; 前复权基准是"库内最新一天", 快照日之后除权就会平移那天的 qfq 价, 打穿 0.25% 容差)。

    回落链 (仅限库里真没有的那几只 / 开关关掉 / 库落后太多): 原来的腾讯前复权日线,
    窗口只有几个月 (<640根) 单请求拿全; 盘中运行时丢弃今天未走完的bar。
    (stock_detail 兜底由核心统一处理, 这里只负责取数。)
    """
    from concurrent.futures import ThreadPoolExecutor
    from . import datasource as ds
    today = dt.date.today().isoformat()
    skip_day = _drop_partial_today()
    res = {}

    if ds.backtest_prices_from_store_on():
        res, codes = ds.price_series_from_store(codes, start, need_date)
        if not codes:
            return res

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

    n_store = len(res)
    with ThreadPoolExecutor(max_workers=6) as exe:
        for i, (code, ser) in enumerate(exe.map(one, codes), 1):
            if ser:
                res[code] = ser
            if i % 300 == 0 or i == len(codes):
                log.info("价格进度(联网) %d/%d (拿到 %d, 另有 %d 只来自价格库)",
                         i, len(codes), len(res) - n_store, n_store)
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


def _bars_source() -> str:
    """A 股日线主源开关: CONFIG.source.bars ∈ {"fuyao"(默认/现状), "tushare"}。

    **每次调用现读**, 不在导入期定死 —— 这样切源不用重启进程 (重建脚本/测试直接改
    CONFIG 即可), 也保证 P1 期间生产默认值不被本模块的导入顺序意外改掉。
    """
    from .config import CONFIG
    return str(CONFIG["source"].get("bars", "fuyao") or "fuyao").lower()


def _tushare_on() -> bool:
    try:
        from . import tushare_client as tsc
        return _bars_source() == "tushare" and tsc.available()
    except Exception:       # noqa: BLE001
        return False


def _stock_ts(code: str) -> str:
    """6 位代码 -> ts_code。**不能用 tushare_client.to_ts_code** —— 那个把 000001 特判成
    沪深300 的 000001.SH (指数用), 个股 000001 是平安银行 (SZ)。"""
    c = str(code).zfill(6)
    if c[0] in ("6", "9"):
        return f"{c}.SH"
    if c[0] in ("4", "8"):
        return f"{c}.BJ"
    return f"{c}.SZ"


def keep_a_code(ts_code: str) -> str | None:
    """A 股主板/创业板/科创板过滤 -> 6 位代码; 不要的返回 None。
    剔除: 北交所 (.BJ 或 8/4/920 前缀) 与 B 股 (900xxx.SH / 200xxx.SZ) —— 与 tech.exclude_bj
    及 universe_codes 的 0/3/6 规则一致 (设计 §4)。"""
    s = str(ts_code or "").strip().upper()
    if not s:
        return None
    code, _, ex = s.partition(".")
    code = code.zfill(6)
    if len(code) != 6 or not code.isdigit():
        return None
    if ex == "BJ" or code[0] in ("4", "8") or code.startswith("920"):
        return None
    if code[0] not in ("0", "3", "6"):       # 900/200 B股、其他前缀一并剔除
        return None
    return code


def _day_ymd(d: str) -> str:
    """'2026-09-04' -> '20260904'。**禁止时区换算** (Tushare trade_date 是北京日历)。"""
    return str(d).replace("-", "")[:8]


def _iso(d8) -> str:
    s = str(d8)[:10].replace("-", "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else ""


def fetch_bars_by_date(trade_date: str):
    """某交易日**全市场原始日线** -> {code: (o,h,l,c,v股,amt元)}。

    返回 None = 本路径未启用 (CONFIG.source.bars 不是 tushare, 或没配 token) —— pricestore
    据此回退旧的逐股增量; 返回 {} = 启用了但当日无数据 (非交易日 / 源尚未入库)。
    **单位** (设计 §P1): Tushare vol 单位手 ×100 = 股; amount 单位千元 ×1000 = 元。
    """
    if not _tushare_on():
        return None
    from . import tushare_client as tsc
    df = tsc.query("daily", trade_date=_day_ymd(trade_date),
                   fields="ts_code,trade_date,open,high,low,close,vol,amount")
    return _rows_from_daily(df)


def _rows_from_daily(df) -> dict:
    out = {}
    if df is None or len(df) == 0:
        return out
    need = {"ts_code", "open", "high", "low", "close", "vol"}
    if not need <= set(df.columns):
        log.warning("daily 返回缺列 %s", sorted(need - set(df.columns)))
        return out
    has_amt = "amount" in df.columns
    for t in df.itertuples(index=False):
        code = keep_a_code(t.ts_code)
        if not code:
            continue
        try:
            o, h, l, c = float(t.open), float(t.high), float(t.low), float(t.close)
            v = float(t.vol) * 100.0                       # 手 -> 股
            amt = float(t.amount) * 1000.0 if has_amt and t.amount is not None else None
        except (TypeError, ValueError):
            continue
        if h < l or min(o, h, l, c) <= 0 or v < 0:
            continue
        out[code] = (o, h, l, c, v, amt)
    return out


def fetch_adj_by_date(trade_date: str):
    """某交易日全市场复权因子 -> {code: factor}; None = 路径未启用。"""
    if not _tushare_on():
        return None
    from . import tushare_client as tsc
    df = tsc.query("adj_factor", trade_date=_day_ymd(trade_date),
                   fields="ts_code,trade_date,adj_factor")
    out = {}
    if df is None or len(df) == 0 or "adj_factor" not in df.columns:
        return out
    for t in df.itertuples(index=False):
        code = keep_a_code(t.ts_code)
        if not code:
            continue
        try:
            f = float(t.adj_factor)
        except (TypeError, ValueError):
            continue
        if f > 0:
            out[code] = f
    return out


def trading_days(start: str, end: str):
    """开市日 ['YYYY-MM-DD', ...]; None = 路径未启用 (让 pricestore 回退旧增量)。

    **取历失败一律往外抛, 不再吞成 `[]`** (2026-09-08 卡 DATA-B 返工): 这里原来是
    `except -> return []`, 而空列表在下游 `leftside_core.pricestore._ready_verdict` 里的
    含义是"区间内没有开市日 (周末/长假) -> 已就绪"。于是 **镜像挂掉 == 周末**:
    `ready` 返回 0 -> run_a.sh v2 的等待循环第一轮就放行 -> 整条流水线拿昨日库跑完 ->
    榜单/买卖点/`day_*.json` 全带昨天的价并落进回测样本 —— 正是收盘后提前跑要防的那件事
    (实测: 库停在 09-07、问 09-08、trade_cal 抛 500, 改前 `ready_for` 给 code=0)。
    同一个空列表还让 `_update_daily_by_date` 打出"库内已到 X, 无新交易日"这句假追平。

    调用方各自接: `ready_for` 接住判 UNKNOWN(2) 不放行, `_update_daily_by_date` 接住
    停下且不推 meta。**别在这里加 try 把它变回 [] 或 None** —— None 的含义是"路径未启用",
    会让 pricestore 回退到逐股回看的旧增量 (v2 库上那条路是被拒写的)。
    """
    if not _tushare_on():
        return None
    from . import tushare_client as tsc
    return tsc.trade_cal(start, end)


def fetch_universe_rows(list_status: str = "L,D,P") -> list:
    """全市场股票池 (含**退市**) -> [(code,name,list_date,delist_date,status), ...]。
    status: L 上市 / D 退市 / P 暂停上市 (Tushare stock_basic 口径)。"""
    if not _tushare_on():
        return []
    from . import tushare_client as tsc
    rows, seen = [], set()
    for st in [s.strip() for s in list_status.split(",") if s.strip()]:
        try:
            df = tsc.query("stock_basic", list_status=st,
                           fields="ts_code,symbol,name,list_date,delist_date,list_status")
        except Exception as e:      # noqa: BLE001
            log.warning("stock_basic %s 失败: %s", st, str(e)[:120])
            continue
        if df is None or len(df) == 0:
            continue
        for t in df.itertuples(index=False):
            code = keep_a_code(t.ts_code)
            if not code or code in seen:
                continue
            seen.add(code)
            rows.append((code, str(getattr(t, "name", "") or ""),
                         _iso(getattr(t, "list_date", "") or ""),
                         _iso(getattr(t, "delist_date", "") or ""),
                         str(getattr(t, "list_status", st) or st)))
    return sorted(rows)


def fetch_bars_bulk_tushare(codes: list, start: str) -> dict:
    """逐股长历史 **前复权** 日线 (daily + adj_factor 各一次) -> {code: [(d,o,h,l,c,v),...]}。

    给 pricestore 的旧 v1 钩子用 (backfill / 逐股补缺); 整库重建走
    research/rebuild_a_pricestore_tushare.py 的按 trade_date 路线 (调用量少两个数量级)。
    qfq 基准 = 该股窗口内最新因子, v 单位 = 股。
    """
    from concurrent.futures import ThreadPoolExecutor
    from . import tushare_client as tsc
    s8, e8 = _day_ymd(start), dt.date.today().strftime("%Y%m%d")
    skip_day = _drop_partial_today()

    def one(code):
        ts = _stock_ts(code)
        try:
            df = tsc.query("daily", ts_code=ts, start_date=s8, end_date=e8,
                           fields="trade_date,open,high,low,close,vol")
            fa = tsc.query("adj_factor", ts_code=ts, start_date=s8, end_date=e8,
                           fields="trade_date,adj_factor")
        except Exception as e:      # noqa: BLE001
            log.debug("tushare 长历史 %s 失败: %s", code, str(e)[:100])
            return code, None
        if df is None or len(df) == 0:
            return code, None
        fac = {}
        if fa is not None and len(fa) and "adj_factor" in fa.columns:
            for t in fa.itertuples(index=False):
                try:
                    fac[_iso(t.trade_date)] = float(t.adj_factor)
                except (TypeError, ValueError):
                    continue
        base = fac[max(fac)] if fac else 1.0
        rows = []
        for t in df.itertuples(index=False):
            d1 = _iso(t.trade_date)
            if not d1 or d1 < start or (skip_day and d1 >= skip_day):
                continue
            try:
                o, h, l, c = float(t.open), float(t.high), float(t.low), float(t.close)
                v = float(t.vol) * 100.0
            except (TypeError, ValueError):
                continue
            if h < l or min(o, h, l, c) <= 0 or v < 0:
                continue
            k = fac.get(d1, base) / base if base else 1.0
            rows.append((d1, o * k, h * k, l * k, c * k, v))
        rows.sort()
        return code, (rows if len(rows) >= 60 else None)

    res = {}
    with ThreadPoolExecutor(max_workers=4) as exe:      # 令牌桶在客户端里, 这里只控并发
        for i, (code, rows) in enumerate(exe.map(one, codes), 1):
            if rows:
                res[code] = rows
            if i % 200 == 0 or i == len(codes):
                log.info("Tushare长历史进度 %d/%d (拿到 %d)", i, len(codes), len(res))
    return res


def fetch_bars_bulk(codes: list, start: str) -> dict:
    """长历史日线(含成交量)。**源由 CONFIG.source.bars 决定** (默认 fuyao 之外的历史行为
    = 腾讯; 设为 "tushare" 才走 Tushare) —— P1 期间生产默认值不动。
    -> {code: [(d,o,h,l,c,v), ...]} 升序; 盘中丢当日未走完bar。

    以下为腾讯实现: 前复权, 按 ~700 自然日分页拼接 (单请求上限640根)。"""
    if _tushare_on():
        return fetch_bars_bulk_tushare(codes, start)
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


def _index_bars_tushare(sym: str, start: str, skip_day: str | None) -> list:
    """Tushare index_daily 一次拉全段 -> [(d,o,h,l,c,v), ...] 升序 (无分页, 单次 8000 行足够 10 年)。

    **单位**: Tushare 指数 vol 就是 "手", 与库内 idx_bars (腾讯/东财口径) 一致 ——
    2026-09-07 实测 000300.SH 2026-09-04: OHLC 与服务器库内逐字相等, vol 比 1.0000。
    所以这里不做任何换算; 若日后换指数发现比值 ≠1, ingest_cache_to_pricestore 的
    10 次幂校准守卫会拦住 (它只放行 10 的整数次幂), 但本函数直写库, 故此处留此实测记录。
    """
    from . import tushare_client as tsc
    df = tsc.index_daily(sym, start, dt.date.today())
    if df is None or len(df) == 0:
        return []
    cols = set(df.columns)
    need = {"trade_date", "open", "high", "low", "close", "vol"}
    if not need <= cols:
        log.warning("基准指数 %s: Tushare 返回缺列 %s, 放弃", sym, sorted(need - cols))
        return []
    rows = []
    for t in df.itertuples(index=False):
        s8 = str(t.trade_date)[:10].replace("-", "")
        if len(s8) != 8:
            continue
        d1 = f"{s8[:4]}-{s8[4:6]}-{s8[6:]}"
        if d1 < start or (skip_day and d1 >= skip_day):
            continue
        try:
            o, h, l, c, v = (float(t.open), float(t.high), float(t.low),
                             float(t.close), float(t.vol))
        except (TypeError, ValueError):
            continue
        if h < l or min(o, h, l, c) <= 0 or v < 0:
            continue
        rows.append((d1, o, h, l, c, v))
    rows.sort()
    return rows


def fetch_index_bars(start: str) -> list:
    """沪深300 (sh000300) 长历史日线 -> [(d,o,h,l,c,v), ...]。
    源顺序: **Tushare (有 token 时的主源)** -> 腾讯 -> 东财直连 -> 新浪
    (每页失败才降级, 失败原因进日志, 每源根数进日志)。
    2026-09-03 教训: 单源静默 `except: kl = []` 让指数表卡在 09-01 四天无人知, lab 连败;
    2026-09-06/07 改造: 腾讯/东财/新浪三个免费源同时不给指数的日子越来越多, 买了 Tushare
    就让它当主源, 三个免费源退为兜底 (口径一致: 成交量均为 "手")。"""
    from . import datasource as ds
    from .config import CONFIG
    sym = CONFIG["source"].get("benchmark_index", "sh000300")
    sources = (("腾讯", ds._tencent_chunk), ("东财", ds._em_index_chunk), ("新浪", ds._sina_index_chunk))
    skip_day = _drop_partial_today()
    try:
        from . import tushare_client as tsc
        if tsc.available():
            rows = _index_bars_tushare(sym, start, skip_day)
            if rows:
                log.info("基准指数 %s: Tushare %d 根 (%s..%s)", sym, len(rows),
                         rows[0][0], rows[-1][0])
                return rows
            log.warning("基准指数 %s: Tushare 主源返回 0 根, 回退 腾讯/东财/新浪", sym)
        else:
            log.info("基准指数 %s: 未配 tushare_token, 走 腾讯/东财/新浪", sym)
    except Exception as e:      # noqa: BLE001  (Tushare 挂了不能拖垮指数表 — 还有三个兜底源)
        log.warning("基准指数 %s: Tushare 主源失败 (%s), 回退 腾讯/东财/新浪",
                    sym, str(e)[:150])
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
    """全A**在市**代码 (主板/创业板/科创板: 0/3/6 前缀; 剔除北交所与B股)。

    源由 CONFIG.source.bars 决定: "tushare" -> stock_basic(list_status=L) (稳定, 不会像
    东财 push2 那样被截断成 1086 行); 否则沿用东财快照。退市股不在这里 —— 点时股票池请用
    pricestore.universe_at(date)。"""
    if _tushare_on():
        rows = fetch_universe_rows("L")
        if rows:
            return sorted({r[0] for r in rows})
        log.warning("universe_codes: Tushare stock_basic 返回空, 回退东财快照")
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
    # schema v2 按日增量钩子 (未启用时各自返回 None -> pricestore 自动回退旧路径)
    fetch_bars_by_date=fetch_bars_by_date, fetch_adj_by_date=fetch_adj_by_date,
    trading_days=trading_days, fetch_universe_rows=fetch_universe_rows,
    log_prefix="ashare",
))
