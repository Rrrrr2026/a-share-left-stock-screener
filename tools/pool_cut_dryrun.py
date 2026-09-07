#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
裁池空跑 (dry-run) —— **零联网**复现一遍候选池构建 + 按库裁, 打印各桶计数与恒等式。

为什么要有这个脚本: 裁池改的是**对外公布的分母** (n_scanned)。09-07 那一轮的交付里
"absent 拆解 163+7+12 ≠ 177" 就是因为分桶数字是分几次手算出来的, 没有一个东西能一次
把"原池 = 留下 + 各桶裁掉"当场算给你看。这里把它变成一条命令。

零联网怎么做到: 候选池的两个外部输入 (全A快照 / 行业成分股) 只从 `data/cache/*.pkl`
里读**指定那一天**的缓存, 读不到就报错退出 —— 绝不回落网络 (回落一次, "零联网复现"
这句话就作废了)。价格库本来就是本地文件。

    python tools/pool_cut_dryrun.py                    # 用最近一天有缓存的快照
    python tools/pool_cut_dryrun.py --date 2026-09-07
    python tools/pool_cut_dryrun.py --date 2026-09-07 --write-cut   # 顺便产一份留痕 json

`--write-cut` 落的是 data/pool_cut/<date>_dryrun.json (data/ 不进 git), run_date 带
`_dryrun` 后缀, 不会盖掉生产那天的留痕。
"""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import os
import pickle
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd                                             # noqa: E402

from ashare.config import CONFIG, DATA_DIR                      # noqa: E402
from ashare import datasource as ds                             # noqa: E402
import run_pipeline as rp                                       # noqa: E402

CACHE_DIR = CONFIG["source"]["cache_dir"]


def _key(name: str, *args) -> str:
    """与 datasource._cache_key 同一套算法 (故意抄一份: 那边是私有函数, 抄比 import 稳)。"""
    raw = name + "|" + "|".join(str(a) for a in args)
    return f"{name}_{hashlib.md5(raw.encode('utf-8')).hexdigest()[:16]}"


def _load(name: str, *args):
    """直接读 pkl, **绕开 TTL**: 复现历史那天要的就是那天的缓存, 不是"还新鲜的缓存"。"""
    p = os.path.join(CACHE_DIR, _key(name, *args) + ".pkl")
    if not os.path.exists(p):
        return None
    with open(p, "rb") as f:
        return pickle.load(f)


def _latest_spot_date() -> str | None:
    for i in range(0, 20):
        d = (dt.date.today() - dt.timedelta(days=i)).isoformat()
        if os.path.exists(os.path.join(CACHE_DIR, _key("spot", d) + ".pkl")):
            return d
    return None


def _industries_of(date: str) -> pd.DataFrame | None:
    """那一天真实入选/在册的行业 (来自 data/ashare.db 的 industry_score 快照)。"""
    db = os.path.join(DATA_DIR, "ashare.db")
    if not os.path.exists(db):
        return None
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT industry, selected FROM industry_score "
                            "WHERE run_date=?", (date,)).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    return pd.DataFrame({"industry": [r[0] for r in rows],
                         "selected": [bool(r[1]) for r in rows]})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="用哪一天的快照缓存 (默认: 最近一天)")
    ap.add_argument("--write-cut", action="store_true",
                    help="顺便走 trim_universe_by_store 落一份 pool_cut 留痕 json")
    args = ap.parse_args()

    date = args.date or _latest_spot_date()
    if not date:
        print("找不到任何 spot 快照缓存, 无法零联网复现", file=sys.stderr)
        return 2
    spot = _load("spot", date)
    if spot is None or getattr(spot, "empty", True):
        print(f"没有 {date} 的 spot 缓存 (data/cache/{_key('spot', date)}.pkl)", file=sys.stderr)
        return 2
    spot = spot.copy()
    spot["code"] = spot["code"].astype(str).str.zfill(6)
    spot_map = {r["code"]: r.to_dict() for _, r in spot.iterrows()}
    ind_df = _industries_of(date)

    # ---- 把两个联网入口换成"只读那一天的缓存", 读不到就炸 (绝不回落网络)
    misses = []

    def _cons(industry):
        df = _load("ind_cons", industry, date)
        if df is None:
            misses.append(industry)
        return df

    ds.fetch_spot_snapshot = lambda force=False: spot
    ds.fetch_industry_cons = _cons

    print(f"== 裁池空跑 (零联网) · 快照日 {date} · 今天 {dt.date.today().isoformat()} ==")
    print(f"   价格库 {os.path.join(DATA_DIR, 'pricestore.db')} "
          f"({os.path.getsize(os.path.join(DATA_DIR, 'pricestore.db')) / 1e9:.2f} GB)")
    universe, ind_to_codes = rp.build_candidate_universe(
        spot, spot_map, ind_df,
        list(ind_df[ind_df["selected"]]["industry"]) if ind_df is not None else [])
    if misses:
        # ⚠ 这一行是读下面所有 B 股数字的前提: 缺了几十个行业成分表, 池子本身就是个小样本,
        # "其中 B 股 N 只" 只是**这几个行业里的** B 股数, 不是全市场在市 B 股数 (09-08 复检:
        # 首版把 09-07 的 7 当成"在市 B 股只剩 7 只"写进了 config, 是错的)。
        print(f"   ! 有 {len(misses)} 个行业没有当日成分缓存 (已按 None 处理): "
              f"{misses[:5]}{' ...' if len(misses) > 5 else ''}")
        print(f"   ! 因此下面的 B 股/北交所只数是**这 {len(ind_df) - len(misses) if ind_df is not None else '?'}"
              f" 个有缓存的行业**里的数, 不是全市场数, 不要外推")
    raw_n = len(universe)
    n_b = sum(1 for (c, _, _) in universe if ds.is_b_share(c))
    n_bj = sum(1 for (c, _, _) in universe if str(c).startswith(("8", "4", "920")))
    print(f"   候选池 (裁前) {raw_n} 只 | 其中 B 股 {n_b} 只 / 北交所 {n_bj} 只 "
          f"(exclude_b_share={CONFIG['tech'].get('exclude_b_share')}, "
          f"exclude_bj={CONFIG['tech']['exclude_bj']})")

    ruler = ds.store_ruler_freshness()
    print(f"   尺子: 个股末日 {ruler['store_max_d']} / 库自己的交易日历末日 {ruler['idx_max_d']}"
          f" -> 落后 {ruler['lag_trade_days']} 个交易日 (自然日 {ruler['lag_calendar_days']}, "
          f"工作日 {ruler['lag_weekdays']} 仅供诊断) -> stale={ruler['stale']}"
          f"{(' (' + ruler['stale_reason'] + ')') if ruler['stale_reason'] else ''}")

    keep, dropped, degraded, kept_detail = ds.store_universe_filter(
        [c for (c, _, _) in universe])
    if degraded:
        print(f"   ! 降级放行: {degraded} (对外口径应标 raw_spot)")
    n_gap = len(kept_detail.get("gap") or [])
    n_keep = len(keep) - n_gap
    print("\n-- 各桶计数 --")
    print(f"   留下 {len(keep)} = 有K线 keep {n_keep} + 缺K线 gap {n_gap}")
    for k in sorted(dropped, key=lambda k: -len(dropped[k])):
        extra = ""
        if k == "uncovered":
            nb = sum(1 for x in dropped[k] if x.get("note"))
            extra = f" (其中 B 股 {nb} 只, 策略射程外)"
        print(f"   裁 {k:<9} {len(dropped[k]):>5}  {ds.STORE_DROP_REASON_CN.get(k, k)}{extra}")
    n_dropped = sum(len(v) for v in dropped.values())
    ok = (n_keep + n_gap + n_dropped == raw_n)
    print("\n-- 恒等式 --")
    print(f"   keep {n_keep} + gap {n_gap} + 裁 {n_dropped} = {n_keep + n_gap + n_dropped}"
          f"  {'==' if ok else '!='}  原池 {raw_n}   -> {'闭合 ✓' if ok else '不闭合 ✗'}")
    print(f"   对外 n_scanned 将是 {len(keep)} (老口径会是 {raw_n})")

    if args.write_cut:
        out, basis = rp.trim_universe_by_store(universe, f"{date}_dryrun")
        print(f"\n   留痕: data/pool_cut/{date}_dryrun.json | scan_basis={basis} | "
              f"裁后 {len(out)} 只")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
