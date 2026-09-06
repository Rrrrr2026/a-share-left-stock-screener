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


def main():
    if not os.path.exists(DB):
        print("pricestore.db 不存在, 跳过")
        return
    conn = sqlite3.connect(DB)
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
        ingest_index(conn, load_bench_cache(), cutoff)
    except Exception as e:  # noqa: BLE001  指数补缺失败不影响个股灌库结果
        print(f"指数缓存灌库异常 (非致命): {e!r}")
    idx_max = conn.execute("SELECT MAX(d) FROM idx_bars").fetchone()[0]
    print(f"idx_bars 末日 {idx_max}")
    conn.close()


if __name__ == "__main__":
    main()
