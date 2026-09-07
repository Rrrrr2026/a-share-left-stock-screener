#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回测取价改读价格库 + 锚定改用原始价 (卡 D, P2) 离线自测 —— 不联网, 临时库 + 造好的序列。

覆盖 (设计 design/backtest_price_from_store.md):
  · raw 锚定成功: 快照日之后除过权时, qfq 序列被整体平移会锚到**错误的那根 bar**,
    raw 序列不会 (锚定是在找"哪一天", 用的必须是不会被未来事件改写的价)
  · 除权日前后的收益仍算在 qfq 上: 原始价一分没动、只发了红利的票, 收益必须是正的
    (跨除权只有 qfq 的涨跌幅是真涨跌幅), 不能退化成 raw 的 0%
  · 没有 raw_close 的序列 (美股 / stock_detail 兜底 / v1 老库) 逐字退回旧行为
  · 库里没有该代码 -> 只有"库说它在市"的才回落联网, 退市/不在池的直接判无数据
  · 库末日落后 need_date 太多 -> 整批回落联网 (而不是拿陈旧价重放)
  · 开关 backtest_prices_from_store=False -> 一行关掉整条库路径

运行:  python tests/test_backtest_anchor.py   或   python -m pytest tests/test_backtest_anchor.py -q
"""
from __future__ import annotations
import datetime as dt
import os
import sqlite3
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ashare.market as amkt                                     # noqa: E402,F401 (注入 Market)
from ashare import datasource as ds                              # noqa: E402
from ashare.config import CONFIG                                 # noqa: E402
from leftside_core import backtest as bt                         # noqa: E402


# ---------------------------------------------------------------- 造序列

def _mk_series(n: int = 40, div_at: int = 15, ratio: float = 0.98,
               bump_idx: int | None = 8) -> dict:
    """原始价恒为 10.00 (股价一分没动), 第 div_at 根除权 —— 于是 qfq 在除权日之前
    整体乘 `ratio`。`bump_idx` 那根把原始价抬到 10/ratio, 好让它的 **qfq 恰好等于
    10.00** —— 这就是"qfq 锚定会锚到错误 bar"的最小复现。
    """
    dates = [(dt.date(2026, 1, 5) + dt.timedelta(days=i)).isoformat() for i in range(n)]
    raw = np.full(n, 10.0)
    if bump_idx is not None:
        raw[bump_idx] = round(10.0 / ratio, 4)
    k = np.where(np.arange(n) < div_at, ratio, 1.0)
    qfq_c = raw * k
    ohlc = np.column_stack([qfq_c, qfq_c, qfq_c, qfq_c])         # o,h,l,c 同值 (平盘)
    return {"dates": dates, "ohlc": ohlc, "raw_close": raw}


def test_anchor_uses_raw_not_qfq():
    """快照价 10.00 落在第 10 根 (除权前)。raw 锚 -> 第 10 根; qfq 锚 -> 被平移骗到第 8 根。"""
    ser = _mk_series()
    idx0, snap_px = 10, 10.0
    i_raw = bt.find_anchor(bt.anchor_closes(ser), idx0, snap_px)
    i_qfq = bt.find_anchor(ser["ohlc"][:, 3], idx0, snap_px)
    assert i_raw == 10, f"raw 锚定应命中信号当天(第10根), 实得 {i_raw}"
    assert i_qfq == 8, f"qfq 锚定应被平移骗到第8根 (说明旧口径确实会错), 实得 {i_qfq}"
    # 没有 raw_close 的序列 (美股/兜底/老库) 必须逐字退回旧行为
    us_like = {"dates": ser["dates"], "ohlc": ser["ohlc"]}
    assert bt.find_anchor(bt.anchor_closes(us_like), idx0, snap_px) == i_qfq
    # ohlcv 五列 (双周内部形状) 也要能取到收盘列
    five = {"dates": ser["dates"],
            "ohlcv": np.column_stack([ser["ohlc"], np.zeros(len(ser["dates"]))]),
            "raw_close": ser["raw_close"]}
    assert bt.find_anchor(bt.anchor_closes(five), idx0, snap_px) == 10


def test_anchor_closes_tolerates_nan():
    """bars 有、bars_raw 缺的那根 bar 会是 NaN —— 不许被当成 0 或参与比较。"""
    ser = _mk_series()
    ser["raw_close"] = ser["raw_close"].copy()
    ser["raw_close"][10] = np.nan
    i = bt.find_anchor(bt.anchor_closes(ser), 10, 10.0)
    assert i in (9, 11) or i == 10, "NaN 不该让搜索崩掉"
    assert np.isnan(bt.anchor_closes(ser)[10]), "NaN 应原样保留, 由 find_anchor 跳过"
    # 整条 raw 全是 NaN -> 视为拿不到, 退回 qfq
    ser["raw_close"] = np.full(len(ser["dates"]), np.nan)
    assert bt.anchor_closes(ser)[0] == ser["ohlc"][0, 3]


def test_return_across_ex_div_uses_qfq():
    """原始价一分没动、只发了红利: 收益必须是 qfq 的 +2%(扣成本), 不是 raw 的 0%。"""
    ser = _mk_series()
    snaps = [{"as_of": ser["dates"][10],
              "cands": [{"code": "000001", "name": "T", "price": 10.0, "atr_pct": 3.0}]}]
    eps = bt.build_and_run(snaps, {"000001": ser})
    assert len(eps) == 1, f"应产出 1 笔事件, 实得 {len(eps)}"
    e = eps[0]
    assert e["fill_date"] == ser["dates"][11], f"应在锚定bar的次日成交, 实得 {e['fill_date']}"
    cost = bt.current().cost_rt
    exp = 1.0 / 0.98 - 1.0 - cost                                # qfq 口径的真实收益
    assert abs(e["ret"] - exp) < 1e-6, f"收益应按 qfq 算 ({exp:.4f}), 实得 {e['ret']}"
    assert e["ret"] > 0, "raw 口径会算成 0-成本 (负), 说明收益算错了序列"


# ---------------------------------------------------------------- 临时价格库

def _mk_store(path: str, last_day: str = "2026-09-07", n: int = 80) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE bars(code TEXT,d TEXT,o REAL,h REAL,l REAL,c REAL,v REAL,"
                 "amt REAL,PRIMARY KEY(code,d)) WITHOUT ROWID")
    conn.execute("CREATE TABLE bars_raw(code TEXT,d TEXT,o REAL,h REAL,l REAL,c REAL,v REAL,"
                 "amt REAL,PRIMARY KEY(code,d)) WITHOUT ROWID")
    conn.execute("CREATE TABLE universe(code TEXT PRIMARY KEY,name TEXT,list_date TEXT,"
                 "delist_date TEXT,status TEXT) WITHOUT ROWID")
    d0 = dt.date.fromisoformat(last_day) - dt.timedelta(days=n - 1)
    rows_q, rows_r = [], []
    for i in range(n):
        d = (d0 + dt.timedelta(days=i)).isoformat()
        rows_q.append(("000001", d, 9.8, 9.8, 9.8, 9.8, 100.0, 980.0))
        rows_r.append(("000001", d, 10.0, 10.0, 10.0, 10.0, 100.0, 1000.0))
    conn.executemany("INSERT INTO bars VALUES(?,?,?,?,?,?,?,?)", rows_q)
    conn.executemany("INSERT INTO bars_raw VALUES(?,?,?,?,?,?,?,?)", rows_r)
    conn.executemany("INSERT INTO universe VALUES(?,?,?,?,?)", [
        ("000001", "有数据", "2015-01-01", None, "L"),
        ("999998", "在市但库里没有", "2015-01-01", None, "L"),
        ("999999", "早退市", "1996-01-01", "2005-06-30", "D"),
    ])
    conn.commit()
    conn.close()


class _use_store:
    """把 datasource 指到临时库, **退出时逐项还原** —— 打桩不还原就是给同一次 pytest 里
    后面的用例喂脏状态 (09-07 刚被这种假绿咬过一次)。"""

    def __init__(self, path: str):
        self.path = path

    def __enter__(self):
        self.saved = (ds._store_path, ds._store_status)          # noqa: SLF001
        ds._store_path = lambda: self.path                       # noqa: SLF001
        ds._STORE_TLS.conn = None                                # noqa: SLF001
        ds._store_status = None                                  # noqa: SLF001
        return self

    def __exit__(self, *exc):
        conn = getattr(ds._STORE_TLS, "conn", None)              # noqa: SLF001
        if conn is not None:
            try:
                conn.close()
            except Exception:                                    # noqa: BLE001
                pass
        ds._store_path, ds._store_status = self.saved            # noqa: SLF001
        ds._STORE_TLS.conn = None                                # noqa: SLF001
        return False


def test_store_hit_and_per_code_fallback():
    """库里有的直接用 (并带上 raw_close); 库说在市却没数据的才回落联网; 退市的不联网。"""
    path = os.path.join(tempfile.mkdtemp(prefix="cardD_test_"), "pricestore.db")
    _mk_store(path)
    with _use_store(path):
        out, fb = ds.price_series_from_store(["000001", "999998", "999999"], "2026-06-01",
                                             need_date="2026-09-07")
    assert set(out) == {"000001"}, f"只有 000001 该命中, 实得 {sorted(out)}"
    assert fb == ["999998"], f"只有'在市但库里没有'的该回落联网, 实得 {fb}"
    ser = out["000001"]
    assert "raw_close" in ser and abs(float(ser["raw_close"][0]) - 10.0) < 1e-9
    assert abs(float(ser["ohlc"][0][3]) - 9.8) < 1e-9, "ohlc 必须是前复权"
    # 锚定用 raw (10.0) 能命中, 用 qfq (9.8) 命不中 -> 库路径确实把两套都带回来了
    assert bt.find_anchor(bt.anchor_closes(ser), 30, 10.0) == 30


def test_store_stale_falls_back_whole_batch():
    """库末日落后 need_date 超过阈值 -> 整批回落联网, 绝不拿陈旧价重放。"""
    path = os.path.join(tempfile.mkdtemp(prefix="cardD_test_"), "pricestore.db")
    _mk_store(path, last_day="2026-08-01")
    codes = ["000001", "999998"]
    with _use_store(path):
        out, fb = ds.price_series_from_store(codes, "2026-06-01", need_date="2026-09-07")
        # 刚好在阈值内 -> 照常用库
        near = (dt.date(2026, 8, 1) + dt.timedelta(days=ds.STORE_STALE_MAX_DAYS)).isoformat()
        out2, _ = ds.price_series_from_store(["000001"], "2026-06-01", need_date=near)
    assert out == {} and fb == codes, f"落后 37 天该整批回落, 实得 {sorted(out)} / {fb}"
    assert set(out2) == {"000001"}, "阈值内不该回落"


def test_switch_off_disables_store_path():
    """一个开关关掉整条链 (回滚不必把 bars 源整个退回 fuyao)。"""
    saved = CONFIG["source"].get("backtest_prices_from_store")
    try:
        CONFIG["source"]["backtest_prices_from_store"] = False
        assert ds.backtest_prices_from_store_on() is False
        CONFIG["source"]["backtest_prices_from_store"] = True
        # bars 源不是 tushare 时也必须关 (库可能还是 v1, 没有 bars_raw 可锚)
        sb = CONFIG["source"].get("bars")
        try:
            CONFIG["source"]["bars"] = "fuyao"
            assert ds.backtest_prices_from_store_on() is False
        finally:
            CONFIG["source"]["bars"] = sb
    finally:
        CONFIG["source"]["backtest_prices_from_store"] = saved


def test_default_is_off_until_boss_signs_off():
    """默认必须是**关**: 重放 win10 +0.9pp 超过卡上 0.5pp 验收线, 差异已归因为"旧口径锚错bar"
    的修正, 但改动对外公布的胜率要老板拍板。谁把默认翻成开, 就要连这条断言一起改 ——
    这份摩擦是故意的 (与 test_pricestore_v2.test_source_switch_default 同一套路)。"""
    if os.environ.get("ASHARE_BACKTEST_PRICES_FROM_STORE"):
        return                                                   # 环境显式指定时不判
    assert CONFIG["source"]["backtest_prices_from_store"] is False


def test_market_hook_signature_is_backward_compatible():
    """核心按老签名 (codes, start) 调用时不能炸 —— 美股钩子至今就是两个参数。"""
    def old_hook(codes, start):
        return {c: {"dates": ["2026-01-01"], "ohlc": np.ones((1, 4))} for c in codes}
    got = bt._call_market_series(old_hook, ["X"], "2026-01-01", need_date="2026-09-07")
    assert set(got) == {"X"}

    def new_hook(codes, start, need_date=None):
        return {"NEED": need_date}
    got2 = bt._call_market_series(new_hook, ["X"], "2026-01-01", need_date="2026-09-07")
    assert got2 == {"NEED": "2026-09-07"}


TESTS = [test_anchor_uses_raw_not_qfq, test_anchor_closes_tolerates_nan,
         test_default_is_off_until_boss_signs_off,
         test_return_across_ex_div_uses_qfq, test_store_hit_and_per_code_fallback,
         test_store_stale_falls_back_whole_batch, test_switch_off_disables_store_path,
         test_market_hook_signature_is_backward_compatible]


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:                                        # noqa: BLE001
            pass
    bad = 0
    for t in TESTS:
        try:
            t()
            print(f"  ✓ {t.__name__}")
        except AssertionError as e:
            bad += 1
            print(f"  ✗ FAIL {t.__name__}: {e}")
    print(f"\n结果: {len(TESTS) - bad} 通过, {bad} 失败")
    sys.exit(1 if bad else 0)
