#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把当日流水线 hist 缓存灌进共享价格库 (零网络请求)。

背景 (2026-09-01): 价格库唯一的日更来源是 lab 18:30 的 update_daily — 每天 ~5177 个
fuyao 请求, 与 14:00 流水线的 ~5200 次共享同一配额, 晚间必然中途被限流 (实测止步
~2000 只), lab 的 50% 新鲜度门槛永远过不去。而流水线缓存里已有全部当日数据, 复用即可。
防单位混库守卫: 逐码与库内重叠日成交量比对, 比例偏离 [0.5, 2] 的代码跳过
(腾讯"手" vs fuyao"股"事故教训 — 价格库必须单源口径, 见 CHRONICLE 数据源之战)。
用法: 流水线跑完后执行 (run_a.sh 已接线); 幂等, 可重复跑。
2026-09-06 追加: 基准指数也从流水线的 bench 缓存补进 idx_bars (只填库里没有的日期; 成交量按
与库内重叠日的比值校准到库内 "手" 口径 — 东财 = 腾讯, 新浪 = 100 倍股; 收盘对不上则拒绝)。
2026-09-07 追加: bench 缓存补不上时再走 fill_index_gaps —— 直接问 market.fetch_index_bars
(主源已改 Tushare)。lab 改 --no-update 之后, 服务器上再没有第二处会更新指数表, 而三个免费
指数源同时哑火已成常态; 这一步是指数表日更的最后一道 (同款收盘/口径守卫, 失败非致命)。
2026-09-07 换库 (Tushare schema v2) 起: **个股灌库自动空转** —— 见 store_is_v2_tushare()。
个股日更改由 run_a.sh 开头的 pricestore update (按 trade_date 拉全市场) 负责; 本脚本只剩
指数补缺一件事, 保留到 P4 一并删。
"""
import datetime as dt
import math
import os
import sqlite3
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ashare.config import CONFIG                 # noqa: E402
from ashare import datasource as ds              # noqa: E402

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "pricestore.db")
KEEP_DAYS = 10
IDX_CLOSE_TOL = 0.003        # 指数缓存 vs 库内重叠日收盘容差 (三源小数位不同, 实测 <1e-4)


def _partial_today() -> str | None:
    """北京时间 15:05 前, 今天的 bar 未走完 -> 返回今天日期串以便丢弃 (同 market._drop_partial_today)。"""
    bj = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
    if bj.hour < 15 or (bj.hour == 15 and bj.minute < 5):
        return bj.date().isoformat()
    return None


def load_bench_cache():
    key = ds._cache_key("bench", CONFIG["source"]["benchmark_index"], dt.date.today().isoformat())
    return ds._cache_load(key)


def ingest_index(conn, df, cutoff: str) -> int:
    """bench 缓存 DataFrame(date/open/high/low/close/volume) -> idx_bars 补缺。返回写入根数。"""
    need = ("date", "open", "high", "low", "close", "volume")
    if df is None or len(df) == 0:
        print("指数缓存: 今日无 bench 缓存, 跳过")
        return 0
    missing = [c for c in need if c not in df.columns]
    if missing:
        print(f"指数缓存: 缺列 {missing} (旧格式只有收盘), 跳过")
        return 0
    ref = {r[0]: r for r in conn.execute("SELECT d,o,h,l,c,v FROM idx_bars ORDER BY d DESC LIMIT 30")}
    if not ref:
        print("指数缓存: 库内 idx_bars 为空, 无参照不灌 (先跑 pricestore backfill)")
        return 0
    have = {r[0] for r in conn.execute("SELECT d FROM idx_bars WHERE d>=?", (cutoff,))}
    skip_day = _partial_today()
    recs = []
    for _, r in df.iterrows():
        try:
            recs.append((str(r["date"])[:10], float(r["open"]), float(r["high"]), float(r["low"]),
                         float(r["close"]), float(r["volume"])))
        except (TypeError, ValueError):
            continue
    ratios = []
    for d, o, h, l, c, v in recs:
        if d not in ref:
            continue
        c0, v0 = ref[d][4], ref[d][5]
        if not (c0 and c0 > 0 and abs(c / c0 - 1) <= IDX_CLOSE_TOL):
            print(f"指数缓存: {d} 收盘 {c} vs 库内 {c0} 对不上, 拒绝灌库")
            return 0
        if v0 and v0 > 0 and v > 0:
            ratios.append(v / v0)
    if len(ratios) < 3:
        print(f"指数缓存: 与库内重叠且可比的日期仅 {len(ratios)} 天 (<3), 无法校准单位, 跳过")
        return 0
    med = statistics.median(ratios)
    scale = 10 ** round(math.log10(med))           # 单位差只允许是 10 的整数次幂 (股/手)
    if abs(med / scale - 1) > 0.1 or max(abs(x / med - 1) for x in ratios) > 0.1:
        print(f"指数缓存: 成交量比值不稳 (中位 {med:.4g}, 离散 {max(ratios)/min(ratios):.3f}), 拒绝灌库")
        return 0
    rows = [(d, o, h, l, c, v / scale) for d, o, h, l, c, v in recs
            if d >= cutoff and d not in have and not (skip_day and d >= skip_day)
            and h >= l and min(o, h, l, c) > 0]
    if not rows:
        print(f"指数缓存: 库内 idx_bars 已到 {max(ref)}, 无缺日")
        return 0
    conn.executemany("INSERT OR REPLACE INTO idx_bars(d,o,h,l,c,v) VALUES(?,?,?,?,?,?)", rows)
    conn.commit()
    print(f"指数缓存灌库: +{len(rows)} 根 ({rows[0][0]}..{rows[-1][0]}), 成交量÷{scale:g} 折成库内口径")
    return len(rows)


def fill_index_gaps(conn) -> int:
    """bench 缓存补不上时的第二道: 直接问 market.fetch_index_bars (2026-09-07 起主源 = Tushare)。

    为什么需要它: lab 已改 --no-update, 服务器上再没有别的地方调 fetch_index_bars —— 指数表
    的日更全靠流水线的 bench 缓存, 而 bench 走的是 腾讯/东财/新浪 (09-01 起屡屡同时哑火,
    指数卡 09-01 直接让 lab 连败 5 天)。守卫与 ingest_index 同款: 重叠日收盘 0.3% 容差 +
    成交量比必须 ≈1 (Tushare 指数 vol 就是"手", 与库内口径一致, 09-04 实测比值 1.0000)。
    """
    # 只拿最近 12 根做参照: 库内 idx_bars 的成交量口径在 2026-08-24 有个历史断点 (之前是"股",
    # 之后是"手" — 09-07 数据线实测, 无任何消费者读指数量, 故未回改历史)。窗口太长会把断点
    # 两侧混进比值; 12 根足够 (>=3 个可比日即可判口径), 且中位数对残留的异常值免疫。
    ref = {r[0]: r for r in conn.execute("SELECT d,o,h,l,c,v FROM idx_bars ORDER BY d DESC LIMIT 12")}
    if not ref:
        print("指数补缺: 库内 idx_bars 为空, 无参照不灌")
        return 0
    from ashare import market
    start = min(ref)
    rows = market.fetch_index_bars(start)
    if not rows:
        print("指数补缺: 所有源都没给出指数长历史, 放弃 (指数表将停在库内末日)")
        return 0
    ratios = []
    for d, o, h, l, c, v in rows:
        r = ref.get(d)
        if not r:
            continue
        if r[4] and r[4] > 0 and abs(c / r[4] - 1) > IDX_CLOSE_TOL:
            print(f"指数补缺: {d} 收盘 {c} vs 库内 {r[4]} 对不上, 拒绝灌库")
            return 0
        if r[5] and r[5] > 0 and v > 0:
            ratios.append(v / r[5])
    if len(ratios) < 3:
        print(f"指数补缺: 可比重叠日仅 {len(ratios)} 天 (<3), 无法确认成交量口径, 跳过")
        return 0
    med = statistics.median(ratios)
    if abs(med - 1) > 0.1:
        print(f"指数补缺: 成交量口径不符 (中位比值 {med:.4g}, 应 ≈1), 拒绝灌库")
        return 0
    if max(ratios) / min(ratios) > 10:
        print(f"指数补缺: 注意 — 库内参照窗口内成交量口径不一致 (比值 {min(ratios):.4g}..{max(ratios):.4g}), "
              f"按中位 {med:.4f} 判定为一致并继续")
    have = {r[0] for r in conn.execute("SELECT d FROM idx_bars")}
    skip_day = _partial_today()
    new = [(d, o, h, l, c, v) for d, o, h, l, c, v in rows
           if d not in have and d >= start and not (skip_day and d >= skip_day)
           and h >= l and min(o, h, l, c) > 0]
    if not new:
        print(f"指数补缺: 库内 idx_bars 已到 {max(ref)}, 无缺日")
        return 0
    conn.executemany("INSERT OR REPLACE INTO idx_bars(d,o,h,l,c,v) VALUES(?,?,?,?,?,?)", new)
    conn.commit()
    print(f"指数补缺灌库: +{len(new)} 根 ({new[0][0]}..{new[-1][0]}), 重叠日收盘对齐, 量比 {med:.4f}")
    return len(new)


def store_is_v2_tushare(conn) -> bool:
    """库是不是 Tushare 单源 schema v2 (原始价 + 因子, bars 为物化前复权)。

    是的话**必须跳过个股灌库**: 这里灌的是流水线缓存里的前复权价, 复权基准是"抓取那天"的,
    与库内 rebase_date 不同; 一旦 INSERT OR REPLACE 进 bars, 就会
      ① 把刚验收通过的 qfq 口径改花 (P1 验收门4 自证的乘法复权关系当场作废),
      ② amt 写成 NULL -> module2 的 `近20日均成交额 >= 0.5亿` 流动性门失真,
      ③ bars 与 bars_raw/adj 脱钩 -> 之后每次 update_daily 的整段重物化都会把它冲掉。
    v2 的日更走 run_a.sh 开头的 pricestore update (按 trade_date 拉全市场), 这里无事可做。
    """
    try:
        src = dict(conn.execute("SELECT key,value FROM meta")).get("source", "")
        has_raw = conn.execute("SELECT 1 FROM bars_raw LIMIT 1").fetchone() is not None
    except sqlite3.OperationalError:
        return False
    return bool(has_raw) or str(src).lower() == "tushare"


def main():
    if not os.path.exists(DB):
        print("pricestore.db 不存在, 跳过")
        return
    conn = sqlite3.connect(DB)
    if store_is_v2_tushare(conn):
        print("价格库已是 Tushare schema v2 -> 个股缓存灌库跳过 (日更由 pricestore update 负责); "
              "只补指数表")
        try:
            if ingest_index(conn, load_bench_cache(), (dt.date.today()
                                                       - dt.timedelta(days=KEEP_DAYS)).isoformat()) == 0:
                fill_index_gaps(conn)
        except Exception as e:  # noqa: BLE001
            print(f"指数缓存灌库异常 (非致命): {e!r}")
        print(f"idx_bars 末日 {conn.execute('SELECT MAX(d) FROM idx_bars').fetchone()[0]}")
        conn.close()
        return
    codes = [r[0] for r in conn.execute("SELECT DISTINCT code FROM bars")]
    f = CONFIG["fetch"]
    today = dt.date.today().isoformat()
    cutoff = (dt.date.today() - dt.timedelta(days=KEEP_DAYS)).isoformat()
    n_ok = n_miss = n_unit = 0
    for code in codes:
        key = ds._cache_key("hist", code, f["adjust"], f["lookback_days"], today)
        df = ds._cache_load(key)
        if df is None or len(df) == 0 or "volume" not in df.columns:
            n_miss += 1
            continue
        sub = df[df["date"].astype(str) >= cutoff]
        if len(sub) == 0:
            n_miss += 1
            continue
        have = dict(conn.execute(
            "SELECT d, v FROM bars WHERE code=? AND d>=?", (code, cutoff)))
        ratio_ok = True
        for _, r in sub.iterrows():
            v0 = have.get(str(r["date"]))
            if v0 and v0 > 0 and r.get("volume") and float(r["volume"]) > 0:
                q = float(r["volume"]) / float(v0)
                ratio_ok = 0.5 <= q <= 2.0
                break
        if not ratio_ok:
            n_unit += 1
            continue
        rows = [(code, str(r["date"]), float(r["open"]), float(r["high"]),
                 float(r["low"]), float(r["close"]), float(r.get("volume") or 0))
                for _, r in sub.iterrows()]
        conn.executemany(
            "INSERT OR REPLACE INTO bars(code,d,o,h,l,c,v) VALUES(?,?,?,?,?,?,?)", rows)
        n_ok += 1
    conn.commit()
    n_today = conn.execute(
        "SELECT COUNT(DISTINCT code) FROM bars WHERE d=?", (today,)).fetchone()[0]
    print(f"缓存灌库: 成功 {n_ok} / 无缓存 {n_miss} / 单位存疑跳过 {n_unit}; "
          f"今日({today})bar覆盖 {n_today}/{len(codes)}")
    try:
        if ingest_index(conn, load_bench_cache(), cutoff) == 0:
            fill_index_gaps(conn)               # 缓存补不上 -> 直接问 Tushare (主源)
    except Exception as e:  # noqa: BLE001  指数补缺失败不影响个股灌库结果
        print(f"指数缓存灌库异常 (非致命): {e!r}")
    idx_max = conn.execute("SELECT MAX(d) FROM idx_bars").fetchone()[0]
    print(f"idx_bars 末日 {idx_max}")
    conn.close()


if __name__ == "__main__":
    main()
