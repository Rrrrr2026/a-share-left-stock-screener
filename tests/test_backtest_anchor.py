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
import copy
import datetime as dt
import json
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
    # 200028 (一致Ｂ) 故意**不写进 universe**: 真库的 universe 来自 Tushare stock_basic,
    # 对 B 股 / 北交所 0 行, 这一路必须与 "D" 分开处理 (见 test_unknown_code_...)。
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


def test_unknown_code_falls_back_not_treated_as_delisted():
    """**库内查无此码 ≠ 库说它退市**: 必须回落联网, 不能静默判无数据。

    09-08 首版写的是 `status.get(code,"") != "L" -> 不联网`, 于是任何 universe 里没有的码
    都被当成退市。真库的 universe 来自 Tushare stock_basic —— 北交所 (8xx/43x/92x) 与
    B 股 (200x/900x) **各 0 行**, 开关一开这些板块在回测链上就是永久静默无数据, 日志还统一
    写成"判退市跳过"。历史快照里已有 4 只在市 B 股候选 (200019/200028/200468/200553)。
    """
    path = os.path.join(tempfile.mkdtemp(prefix="cardD_test_"), "pricestore.db")
    _mk_store(path)
    with _use_store(path):
        out, fb = ds.price_series_from_store(["000001", "200028", "999999"], "2026-06-01",
                                             need_date="2026-09-07")
    assert set(out) == {"000001"}
    assert fb == ["200028"], (
        f"库内查无此码的 200028 该回落联网、退市的 999999 不该联网, 实得 {fb}")


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


#: **翻开关的那一行的期望值。** 2026-09-09 GM 把 `config.DEFAULT_BACKTEST_STORE` 从 "0"
#: 改成 "1" 时, 连同这一行一起改 —— 两行, 别的都不用动。这份摩擦是故意的 (与
#: test_pricestore_v2.test_source_switch_default 同一套路): 改动对外公布的胜率的那一头,
#: 不许执行者顺手翻过去而没有任何一条用例红给他看。
EXPECTED_DEFAULT = "0"


def test_default_is_off_until_boss_signs_off():
    """代码默认值必须**等于本文件声明的那个期望值**, 且 CONFIG 里的值真的由它算出来。

    2026-09-08 生产同款样本 (只读 scp 下来的服务器 35 份快照) 上按 GM 预登记的三条裁决
    规则复算: (a) anchor 门 pass —— raw exact 99.98% (8,619/8,621), 干净样本 100%;
    (b) "新口径更差" 1 笔 (600061 除权日快照存的是除权后的昨收, 根因在生成侧);
    (c) pool 级最差 0.9pp (win10 52.3%→53.2%), 参与判定的分段最差 1.6pp。
    95 笔成交日变化的归因 = 修复 64 / 两边 exact 但不同 bar 30 / 同一根 bar 0 /
    **新口径更差 1** / 两边都没命中 0 / 来自错标快照 0 —— **94/95 是把入场日拨回快照价
    真正来自的那根, 按"修正"读, 不是"回归"**。开关没有被这三个数否掉;
    09-08 保持关的唯一理由是**排期** (14:00 那轮已经叠了换库+裁池+日更分块三个变量)。
    """
    import ashare.config as cfgmod                               # noqa: PLC0415
    assert cfgmod.DEFAULT_BACKTEST_STORE == EXPECTED_DEFAULT, (
        "翻开关请**同时**改 config.DEFAULT_BACKTEST_STORE 与本文件的 EXPECTED_DEFAULT "
        f"(现在: 常量={cfgmod.DEFAULT_BACKTEST_STORE!r}, 期望={EXPECTED_DEFAULT!r})")
    want = EXPECTED_DEFAULT.strip().lower() in ("1", "true", "on")
    # CONFIG 里那个值必须**真的由这个常量算出来**, 不是两处各写各的 (09-08 之前默认值埋在
    # os.environ.get 的第二个参数里, 谁都能改一处忘一处而单测不响)。
    assert cfgmod._backtest_store_switch(cfgmod.DEFAULT_BACKTEST_STORE)[0] is want
    if os.environ.get("ASHARE_BACKTEST_PRICES_FROM_STORE"):
        return                                                   # 环境显式指定时不判
    if os.path.exists(os.path.join(cfgmod.DATA_DIR, "backtest_store.off")):
        return                                                   # 本机按了停机文件时不判
    assert CONFIG["source"]["backtest_prices_from_store"] is want


def test_backtest_store_switch_three_layers():
    """三层开关: 环境变量 > 停机文件 > 代码默认值; 写错值必须**响**而不是静默按默认走。

    与裁池开关 `_pool_by_store_switch` 逐字同一套路 —— 值班的人只需要记住一种口径。
    这条用例存在的理由是 09-03「静默跑旧码」/ 09-07「优质榜静默失败」同一个失败形态:
    **按下去没反应, 却没有任何一行字说它没反应**。
    """
    import ashare.config as cfgmod                               # noqa: PLC0415
    key = "ASHARE_BACKTEST_PRICES_FROM_STORE"
    saved_env = os.environ.get(key)
    off_file = os.path.join(cfgmod.DATA_DIR, "backtest_store.off")
    made_off = False
    try:
        # ① 环境变量: 6 个值, 大小写不敏感, 两个方向都要能定 (不只是"关")
        for v, want in (("0", False), ("false", False), ("OFF", False),
                        ("1", True), ("true", True), ("On", True)):
            os.environ[key] = v
            on, by, warn = cfgmod._backtest_store_switch("0")
            assert on is want and warn == "" and key in by, (v, on, by, warn)
        # ② 写了个不认识的值 -> 不当数 (按默认走), 但 warn 非空且写明该怎么写
        os.environ[key] = "yes"
        on, by, warn = cfgmod._backtest_store_switch("0")
        assert on is False and by == "" and "yes" in warn and "0/1/true/false/on/off" in warn
        on2, _by2, warn2 = cfgmod._backtest_store_switch("1")
        assert on2 is True and warn2, "写错值时应按默认(这里是开)走, 且照样告警"
        # ③ 停机文件 (data/ 不在 git 里, 服务器 reset --hard 抹不掉) —— 优先级低于环境变量
        del os.environ[key]
        if not os.path.exists(off_file):
            open(off_file, "w").close()
            made_off = True
        on, by, _warn = cfgmod._backtest_store_switch("1")
        assert on is False and "backtest_store.off" in by, (on, by)
        os.environ[key] = "1"
        on, by, _warn = cfgmod._backtest_store_switch("1")
        assert on is True and key in by, "环境变量必须压过停机文件"
        # ④ 都没有 -> 代码默认值, 且 by 为空 (= "没人另外定过", 日志会打"代码默认值")
        del os.environ[key]
        if made_off:
            os.remove(off_file)
            made_off = False
        assert cfgmod._backtest_store_switch("0") == (False, "", "")
        assert cfgmod._backtest_store_switch("1") == (True, "", "")
    finally:
        if made_off and os.path.exists(off_file):
            os.remove(off_file)
        if saved_env is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved_env


def test_run_pipeline_logs_backtest_price_switch(capsys=None):
    """run_pipeline 在回测/模拟盘/双周之前必须打一行"读库 开/关 (被谁定的)"。

    不许静默切换: 这一头切过去对外公布的胜率会动 (生产同款样本 win10 52.3%→53.2%),
    哪天有人拿着两份数字来问"这轮走的哪条路", 日志里必须当场答得出来。
    """
    import logging                                               # noqa: PLC0415
    import run_pipeline as rp                                    # noqa: PLC0415
    saved = dict(CONFIG["source"])
    saved_gate = ds.bars_from_store_on
    recs = []

    class _Cap(logging.Handler):
        def emit(self, r):
            recs.append((r.levelno, r.getMessage()))

    h = _Cap()
    rp.log.addHandler(h)
    lv = rp.log.level
    rp.log.setLevel(logging.INFO)
    try:
        # `bars_from_store_on` 还要求 data/pricestore.db **存在** —— 干净导出树里没有这个
        # 文件, 所以这里打桩, 让本用例只考"日志说没说清楚", 不去考环境有没有库。
        ds.bars_from_store_on = lambda: True                     # noqa: SLF001
        CONFIG["source"].update(bars="tushare", backtest_prices_from_store=True,
                                backtest_prices_from_store_off_by="",
                                backtest_prices_from_store_switch_warn="")
        assert rp.log_backtest_price_switch() is True
        assert any("回测取价: 读库 开 (代码默认值" in m for _l, m in recs), recs
        recs.clear()
        CONFIG["source"].update(backtest_prices_from_store=False,
                                backtest_prices_from_store_off_by="停机文件 x/backtest_store.off")
        assert rp.log_backtest_price_switch() is False
        assert any("读库 关 (停机文件" in m for _l, m in recs), recs
        recs.clear()
        # 强制关的**两个原因必须分开说**: 源不对 vs 源对但库文件不在。09-08 首版把后者也
        # 写成"不是 tushare", 在没有库的干净导出树上打出「bars='tushare' 不是 tushare」——
        # 自相矛盾的假话, 会把值班的人指到一个本来就对的配置上去。
        ds.bars_from_store_on = lambda: False                    # noqa: SLF001
        CONFIG["source"].update(bars="fuyao", backtest_prices_from_store=True,
                                backtest_prices_from_store_off_by="环境变量 X=1")
        assert rp.log_backtest_price_switch() is False
        assert any("'fuyao' 不是 tushare, 本条链被强制关" in m for _l, m in recs), recs
        recs.clear()
        CONFIG["source"].update(bars="tushare")
        assert rp.log_backtest_price_switch() is False
        assert not any("不是 tushare" in m for _l, m in recs), (
            "源是 tushare 却说它不是 tushare —— 这正是本条要拦的假话: %r" % (recs,))
        assert any("价格库文件不在" in m for _l, m in recs), recs
        recs.clear()
        # 环境变量写错值 -> 必须有一条 WARNING (config 里 log 不进 journal, 只能在这里打)
        CONFIG["source"].update(backtest_prices_from_store_switch_warn="值不认识")
        rp.log_backtest_price_switch()
        assert any(lv2 >= logging.WARNING and "值不认识" in m for lv2, m in recs), recs
    finally:
        ds.bars_from_store_on = saved_gate                       # noqa: SLF001
        rp.log.removeHandler(h)
        rp.log.setLevel(lv)
        CONFIG["source"].clear()
        CONFIG["source"].update(saved)


def test_demo_seed_snapshot_excluded_from_replay():
    """演示种子快照 (合成价) 不许进回放样本, 但**真快照一份都不许误伤**。

    为什么排除 (2026-09-08 实测, 首版这里的理由是编的): 种子 day_2026-06-30.json 借用了
    **真实存在的股票代码** (600111 等) 配假名字假价格 —— 放回样本重跑 build_and_run 会多出
    6 笔纯合成价造的假事件, 其中 600111 那笔的 busy_until 冷却又挡掉 1 笔真信号
    (600111 @ 2026-07-07)。首版写的"与 day_2026-07-01 撞 as_of, 会在'同一 (code, as_of)
    先到先得'里把 200 条真候选顶掉"两处都不成立: build_and_run 没有按 (code, as_of) 去重,
    只有按代码的事件冷却; 且种子的 14 个代码与 07-01 那份的 200 个代码交集为空。"""
    real = [{"code": "600000", "name": "浦发银行", "price": 10.0}]
    demo = [{"code": "600111", "name": "演示半导A", "price": 63.46},
            {"code": "300222", "name": "演示半导B", "price": 24.27}]
    # ① 已知的种子文件名 (按市场; 当前 Market 是 ashare)。**必须用真候选来考这一条** ——
    #    拿 demo 候选考等于同时命中判据③, 白名单这条路根本没被单独验到 (09-08 校验实证:
    #    把 DEMO_SNAPSHOT_FILES 清空, 首版那 7 条断言一条都不挂)。
    assert bt.is_demo_snapshot("x/day_2026-06-30.json", {}, real) is True
    assert bt.is_demo_snapshot("x/day_2026-06-30.json", {}, demo) is True
    # ② meta 显式标记 (以后新造种子请打这个标) —— 文件名不在白名单里也要认
    for key in ("demo", "seed", "demo_seed"):
        assert bt.is_demo_snapshot("x/day_2026-01-05.json", {key: True}, real) is True
    # ③ 兜底: 候选名全部以"演示"开头
    assert bt.is_demo_snapshot("x/day_2026-01-05.json", {}, demo) is True
    # 真快照一律 False —— 包括与种子同名日期但内容是真的、以及混进一条演示名的
    assert bt.is_demo_snapshot("x/day_2026-09-07.json", {}, real) is False
    assert bt.is_demo_snapshot("x/day_2026-01-05.json", {}, real + demo) is False
    assert bt.is_demo_snapshot("x/day_2026-01-05.json", {}, []) is False
    # 美股侧白名单是空的: 同名文件在美股仓不许被当成种子误删
    assert bt.DEMO_SNAPSHOT_FILES["us"] == set()


def test_demo_whitelist_is_load_bearing():
    """把白名单删空, 上面那条断言必须变红 —— 否则"三条判据"只是写在注释里。

    这是判据①的**存在性**证明: 真候选 + 种子文件名的组合只能靠白名单判 True, 兜底判据
    (候选名全以"演示"开头) 对真候选是 False。"""
    saved = dict(bt.DEMO_SNAPSHOT_FILES)
    try:
        bt.DEMO_SNAPSHOT_FILES = {"ashare": set(), "us": set()}
        real = [{"code": "600000", "name": "浦发银行", "price": 10.0}]
        assert bt.is_demo_snapshot("x/day_2026-06-30.json", {}, real) is False
    finally:
        bt.DEMO_SNAPSHOT_FILES = saved
    assert bt.is_demo_snapshot(
        "x/day_2026-06-30.json", {}, [{"code": "600000", "name": "浦发银行"}]) is True


def test_snapshot_data_date_fix_table():
    """标注日修正表: 只在读到"已知错值"时改, 已修正的副本是 no-op, 第三种值不许静默套。"""
    f24 = "d/day_2026-08-24.json"
    got, note, applied = bt.corrected_data_date(f24, "2026-08-21")
    assert (got, applied) == ("2026-08-24", True) and "2026-08-24" in note
    # 文件里已经是修正后的值 (PC / GitHub Pages 那份) -> 原样返回, 不重复报
    assert bt.corrected_data_date(f24, "2026-08-24") == ("2026-08-24", None, False)
    # 第三种值 = 文件被别人动过 -> 不改, 但必须给出说明 (调用方打 warning)
    got, note, applied = bt.corrected_data_date(f24, "2026-08-19")
    assert (got, applied) == ("2026-08-19", False) and note
    # 表外的文件一律不碰
    assert bt.corrected_data_date("d/day_2026-09-07.json", "2026-09-07") == (
        "2026-09-07", None, False)
    assert bt.corrected_data_date("d/day_2026-07-01.json", "2026-07-01")[0] == "2026-06-30"


def test_load_snapshots_applies_data_date_fix_on_stale_copy():
    """**生产落地的那条断言**: 磁盘上还是错值的那份副本, 经 load_snapshots 出来必须已修正。

    背景 (09-08 校验): 修正只写进文件到不了服务器 —— 回放读的 dashboard/history 是运行时
    目录 (gitignore), 服务器只靠 run_a.sh 的 `rsync -a --ignore-existing docs/history/
    dashboard/history/` 回种, 而 --ignore-existing 对已存在的文件一个字节都不写。所以修正
    必须走代码 (跟着 git reset 到位), 这条用例锁的就是这一点。"""
    import glob as _glob
    import json as _json
    d = tempfile.mkdtemp(prefix="snapfix_")
    hist = os.path.join(d, "history")
    os.makedirs(hist)
    cands = [{"code": "600000", "name": "浦发银行", "price": 10.0}]
    for fn, dd in (("day_2026-08-24.json", "2026-08-21"),      # 未修正的陈旧副本
                   ("day_2026-09-07.json", "2026-09-07")):     # 对照: 表外文件
        with open(os.path.join(hist, fn), "w", encoding="utf-8") as f:
            _json.dump({"meta": {"run_date": fn[4:14], "data_date": dd},
                        "candidates": cands}, f, ensure_ascii=False)
    saved = bt._paths
    try:
        bt._paths = lambda: (hist, os.path.join(d, "a.js"), os.path.join(d, "a.json"))
        snaps = bt.load_snapshots()
    finally:
        bt._paths = saved
    got = {s["run_date"]: s["as_of"] for s in snaps}
    assert got == {"2026-08-24": "2026-08-24", "2026-09-07": "2026-09-07"}, got
    # 文件本身一个字节都没被改 —— 修正只发生在装载处
    with open(os.path.join(hist, "day_2026-08-24.json"), encoding="utf-8") as f:
        assert _json.load(f)["meta"]["data_date"] == "2026-08-21"
    assert len(_glob.glob(os.path.join(hist, "*.json"))) == 2


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


# ---------------------------------------------------------------- 除权日快照 (卡 R3-4)

def _mk_xd_series(n: int = 30, div_at: int = 20, ratio: float = 0.97,
                  decoy_at: int = 16) -> tuple[dict, float]:
    """原始价恒 10.00, 第 `div_at` 根除权 (因子 f_pre -> f_pre/ratio)。

    模拟"写快照那天该票除权"的现场: 快照价取自 `div_at-1` 那根, 但当天导出的前复权基准
    已经换成除权后的因子 -> 存下来的是 `10.00 × ratio`。`decoy_at` 那根的**原始价**也恰好
    等于这个值 —— 于是拿 raw 去锚会锚到那根 (错 4 格), 正是 600061 在 2026-06-30 的形状。
    -> (ser, 快照价)
    """
    dates = [(dt.date(2026, 1, 5) + dt.timedelta(days=i)).isoformat() for i in range(n)]
    raw = np.full(n, 10.0)
    snap_px = round(10.0 * ratio, 4)
    raw[decoy_at] = snap_px                       # 诱饵: raw 锚定会命中这一根
    f = np.where(np.arange(n) < div_at, 1.0, 1.0 / ratio)        # 第 div_at 根起因子变大
    qfq_c = raw * f / f[-1]
    return ({"dates": dates, "ohlc": np.column_stack([qfq_c] * 4), "raw_close": raw},
            snap_px)


def test_xd_rebased_closes_is_raw_times_factor_ratio():
    """`xd_rebased_closes` = raw × f[i]/f[i+1], 且**只在除权前一根**与 raw 不同。

    这条序列是 XD 快照的兜底比价口径 (生成侧拿不到原始价时才用)。它必须做到两件事:
    ① 在除权前一根上给出"当天导出的复权后昨收" -> 锚回真正的那根;
    ② 在别的所有 bar 上**逐值退化成 raw** -> 对非除权样本零影响, 不会把本来锚得好好的
       样本带偏 (真实数据实测: 600061 九年序列里只有 15 根与 raw 不同, 全是除权前一根)。
    """
    ser, snap_px = _mk_xd_series()
    xs = bt.xd_rebased_closes(ser)
    raw = ser["raw_close"]
    diff = [i for i in range(len(raw)) if abs(float(xs[i]) - float(raw[i])) > 1e-9]
    assert diff == [19], f"只该在除权前一根 (idx 19) 与 raw 不同, 实得 {diff}"
    assert abs(float(xs[19]) - snap_px) < 1e-9, (float(xs[19]), snap_px)
    # 锚定: raw 锚到诱饵 (错), xd 序列锚回真正那根
    i_raw = bt.find_anchor(bt.anchor_closes(ser, xd=False), 19, snap_px)
    i_xd = bt.find_anchor(bt.anchor_closes(ser, xd=True), 19, snap_px)
    assert i_raw == 16, f"raw 锚定应被诱饵骗到第 16 根 (说明确实会错), 实得 {i_raw}"
    assert i_xd == 19, f"xd 锚定应命中除权前一根, 实得 {i_xd}"
    # 拿不到 raw_close 的序列 (美股/兜底/v1 老库) -> 逐字退回旧行为, 不许炸
    us_like = {"dates": ser["dates"], "ohlc": ser["ohlc"]}
    assert bt.xd_rebased_closes(us_like) is None
    assert np.allclose(bt.anchor_closes(us_like, xd=True), ser["ohlc"][:, 3])


def _mk_xd_store(path: str) -> None:
    """最小价格库 (2026-06-29/30 与 07-01 三根):
      600061  名字带 XD, 库里有 06-30 那根 (6.55), 因子 9.8854 -> 10.1171 (07-01 除权)
      603201  名字带 XD, **库里没有 06-30 那根** -> 库里读不到原始收盘 (basis=unknown)
      000002  名字**不带** XD, 但因子 1.0 -> 1.1 (07-01 除权) -> 只能靠因子那条证据判出来
      000001  两条证据都不命中 (对照组: 一个字段都不许加)
    """
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE bars_raw(code TEXT,d TEXT,o REAL,h REAL,l REAL,c REAL,v REAL,"
                 "amt REAL,PRIMARY KEY(code,d)) WITHOUT ROWID")
    conn.execute("CREATE TABLE adj(code TEXT,d TEXT,factor REAL,PRIMARY KEY(code,d)) "
                 "WITHOUT ROWID")
    rows_r, rows_a = [], []
    for d, f61 in (("2026-06-29", 9.8854), ("2026-06-30", 9.8854), ("2026-07-01", 10.1171)):
        rows_r.append(("600061", d, 6.5, 6.6, 6.4, 6.55 if d == "2026-06-30" else 6.6, 1.0, 1.0))
        rows_r.append(("000001", d, 10.0, 10.0, 10.0, 10.0, 1.0, 1.0))
        rows_r.append(("000002", d, 20.0, 20.0, 20.0, 20.0, 1.0, 1.0))
        rows_a.append(("600061", d, f61))
        rows_a.append(("000001", d, 3.0))
        rows_a.append(("000002", d, 1.0 if d < "2026-07-01" else 1.1))
        rows_a.append(("603201", d, 2.5229 if d < "2026-07-01" else 2.5764))
    conn.executemany("INSERT INTO bars_raw VALUES(?,?,?,?,?,?,?,?)", rows_r)
    conn.executemany("INSERT INTO adj VALUES(?,?,?)", rows_a)
    conn.commit()
    conn.close()


def _xd_cands() -> list:
    """生产形状的候选 —— **带齐同基准的兄弟价位字段**。

    首版的用例只给了 code/name/price 三个键, 于是"改 price 会把它与 support/box/plan 的
    基准拆成两套"这条**结构上根本考不到**, 校验员是拿真实快照记录才发现的。这里逐字照抄
    day_2026-07-01.json 里那两条真记录的字段, 谁再想动价, 用例立刻红。
    """
    return [
        {"code": "600061", "name": "XD国投资", "price": 6.40, "atr_pct": 3.1,
         "support_price": 6.4503, "breakdown_price": 5.82, "dist_support_pct": -0.7865,
         "box_hi": 6.72, "box_lo": 6.18, "high_52w": 8.5, "low_52w": 5.4,
         "plan": {"kind": "pullback", "mode": "support", "entry_ref": 6.4503,
                  "entry_low": 6.3729, "entry_high": 6.5471, "stop_price": 5.7909}},
        {"code": "603201", "name": "XD常润股", "price": 13.67, "atr_pct": 4.2,
         "support_price": 13.89, "breakdown_price": 10.0007},
        {"code": "000002", "name": "万科A", "price": 20.00, "atr_pct": 2.0,
         "support_price": 19.5, "breakdown_price": 18.0},
        {"code": "000001", "name": "平安银行", "price": 10.00, "atr_pct": 1.5,
         "support_price": 9.8, "breakdown_price": 9.0},
    ]


def _run_xd_fix(slim: dict, store_path: str) -> dict:
    from ashare import export_data as ex                         # noqa: PLC0415
    saved_bars = CONFIG["source"].get("bars")
    try:
        CONFIG["source"]["bars"] = "tushare"
        with _use_store(store_path):
            return ex.xd_fix_snapshot_prices(slim)
    finally:
        CONFIG["source"]["bars"] = saved_bars


def test_xd_snapshot_only_flags_never_reprices():
    """**生成侧只打 `xd` 标记, 一个价都不许改** (2026-09-08 复检后砍掉的那条改价路)。

    首版把除权日候选的 `price` 改记原始收盘。校验实测这会出两条人命:
      ① `price` 与 `support_price` / `breakdown_price` / `box_hi` / `box_lo` / 存档 `plan`
         的四个价位出自**同一条前复权序列**, 只换 price 的基准 -> 下游
         `scale = qfq[anchor]/snap_px` 把计划价位整体平移一个除权因子比 (实测 600061
         -2.29%, 603201 的 mode 由 market 翻成 support), 而 `0.2<scale<5.0` 拦不住;
      ② 拿去查库的 `data_date` 不过修正表, 在服务器那两份错值副本上写进的是**错的那一天**
         的收盘 (600061 会被写成 07-01 的 6.64 —— 一个**未来一天**的价)。
    所以现在只剩"打标记"这一条路。本用例的候选带齐了兄弟价位字段, 谁再动价立刻红。
    """
    path = os.path.join(tempfile.mkdtemp(prefix="cardR34_"), "pricestore.db")
    _mk_xd_store(path)
    cands = _xd_cands()
    before = copy.deepcopy(cands)
    slim = {"meta": {"run_date": "2026-07-01", "data_date": "2026-06-30"},
            "candidates": cands}
    res = _run_xd_fix(slim, path)

    by_code = {c["code"]: c for c in cands}
    # ---- 1. 每一条候选的**所有原有字段逐值不变**, 新增的只许是 {"xd": True}
    for old in before:
        now = by_code[old["code"]]
        added = set(now) - set(old)
        assert added <= {"xd"}, f"{old['code']} 多出了字段 {added} —— 只许加 xd"
        for k, v in old.items():
            assert now[k] == v, f"{old['code']}.{k} 被改动: {v!r} -> {now[k]!r}"
    assert by_code["600061"]["price"] == 6.40, "**改价路已删**: 6.40 不许变成 6.55/6.64"
    for c in cands:                    # 首版留下的两个字段必须彻底消失
        assert "price_basis" not in c and "price_qfq_rebased" not in c, c

    # ---- 2. 命中的三只都打上标记, 没命中的一个字段都不加
    assert by_code["600061"].get("xd") is True and by_code["603201"].get("xd") is True
    assert by_code["000002"].get("xd") is True, "名字不带 XD, 只能靠因子那条证据判出来"
    assert set(by_code["000001"]) == set(next(
        c for c in before if c["code"] == "000001")), "没除权的候选一个字段都不该加"

    # ---- 3. meta 汇总 (证据留在这里, 不需要靠改价来留痕)
    assert res["n_xd"] == 3 and res["n_flagged"] == 3, res
    assert res["by_name"] == 2 and res["by_factor"] == 3, res
    assert "n_repriced" not in res, "改价路连计数都不该再有"
    diag = {r["code"]: r for r in res["codes"]}
    assert diag["600061"]["store_raw_close"] == 6.55
    assert diag["600061"]["basis"] == "rebased", "6.40 vs 6.55 差 2.29% -> 是除权后的昨收"
    assert diag["603201"]["basis"] == "unknown", "库里没有那根 -> 判不了, 不许瞎猜"
    assert diag["000002"]["by"] == "factor" and diag["600061"]["by"] == "name+factor"
    assert slim["meta"]["xd_fix"]["n_xd"] == 3, "meta 必须留汇总"


def test_xd_generation_reads_corrected_data_date():
    """拿去查库的 `data_date` **必须过 `SNAPSHOT_DATA_DATE_FIX` 修正表**。

    首版直接读 `meta["data_date"]`, 而那张表存在的唯一理由就是这个字段错过两次。后果在
    服务器那两份仍是错值的副本上是实打实的: `day_2026-07-01.json` 的 meta 写着 07-01,
    于是因子窗口 [07-01, 07-01] 塌成一天 (因子判全灭), 而库里查到的"那天的收盘"是 6.64
    —— 比快照晚一天的价。修正表把它拨回 06-30 之后, 查到的才是 6.55。
    """
    path = os.path.join(tempfile.mkdtemp(prefix="cardR34fix_"), "pricestore.db")
    _mk_xd_store(path)
    slim = {"meta": {"run_date": "2026-07-01", "data_date": "2026-07-01"},   # <- 已知错值
            "candidates": _xd_cands()}
    res = _run_xd_fix(slim, path)
    assert res["data_date"] == "2026-06-30", (
        f"标注日没过修正表: 实得 {res['data_date']!r}, 该是 2026-06-30")
    assert res["data_date_note"], "修正了就必须留说明 (静默改日子和静默改价一样禁止)"
    diag = {r["code"]: r for r in res["codes"]}
    assert diag["600061"]["store_raw_close"] == 6.55, (
        "查的是修正后那天的 bar; 6.6 = 07-01 的收盘 = 比快照晚一天的价")
    assert res["by_factor"] == 3, "窗口 [06-30, 07-01] 张得开, 三只的因子都变过"


def test_xd_flag_is_harmless_when_price_is_plain_raw():
    """多打的 `xd` 标记必须**无害** —— 这是"宁可多标"这个取舍成立的前提。

    生产上 36 份快照里 33 份 `data_date == run_date`, 那种天因子窗口塌成一天、因子判恒 0,
    只剩名称前缀在判; 而名称前缀那条会把"当天除权、快照价本来就是当天原始收盘"的票也标上。
    这种票走 `xd_rebased_closes` 必须锚到与不打标记**完全相同**的那根 bar, 否则"多标一个
    没关系"就不成立了 —— 601995 在 day_2026-08-24.json 里正是这种形状 (33.62 本来就对)。
    """
    n = 30
    dates = [(dt.date(2026, 1, 5) + dt.timedelta(days=i)).isoformat() for i in range(n)]
    raw = np.array([10.0 + 0.1 * i for i in range(n)])       # 逐根不同, 锚定不会歧义
    f = np.where(np.arange(n) < 12, 1.0, 1.0 / 0.97)         # 除权在第 12 根 (远离锚点)
    ser = {"dates": dates, "ohlc": np.column_stack([raw * f / f[-1]] * 4), "raw_close": raw}
    for i in (19, 20, 25):
        snap_px = float(raw[i])                              # 快照价 = 该根的原始收盘
        a_off = bt.find_anchor(bt.anchor_closes(ser, xd=False), i, snap_px)
        a_on = bt.find_anchor(bt.anchor_closes(ser, xd=True), i, snap_px)
        assert a_off == a_on == i, f"idx {i}: 打标记 {a_on} vs 不打 {a_off}, 都该是 {i}"


def test_xd_flag_survives_into_anchoring():
    """快照里的 `xd` 标记要真的被回测/模拟盘读到 (光在生成侧打标记等于没做)。"""
    ser, snap_px = _mk_xd_series()
    as_of = ser["dates"][19]
    base = {"code": "000001", "name": "T", "atr_pct": 3.0, "price": snap_px}
    eps_no = bt.build_and_run([{"as_of": as_of, "cands": [dict(base)]}], {"000001": ser})
    eps_xd = bt.build_and_run([{"as_of": as_of, "cands": [dict(base, xd=True)]}],
                              {"000001": ser})
    assert len(eps_no) == 1 and len(eps_xd) == 1
    assert eps_no[0]["fill_date"] == ser["dates"][17], (
        f"没有 xd 标记时锚在诱饵上 -> 次日成交 {eps_no[0]['fill_date']}")
    assert eps_xd[0]["fill_date"] == ser["dates"][20], (
        f"带 xd 标记时应锚回第 19 根 -> 次日成交 {eps_xd[0]['fill_date']}")


def test_xd_flag_does_not_move_plan_levels():
    """打标记只该改"锚到哪根 bar", **计划价位与 scale 的基准不许动**。

    这是首版改价路真正的杀伤面: `reconstruct_plan` 吃 price 与 support/box/plan 的绝对
    价位, `simulate` 再拿 `scale = qfq[anchor]/snap_px` 把它们搬进 qfq 空间。price 换了
    基准而计划价位没换 -> 入场带与止损位整体平移。用**无量纲的 level/snap_px** 比 (它就是
    乘 scale 之后的相对位置), 过完生成侧修法必须逐值不变。
    """
    path = os.path.join(tempfile.mkdtemp(prefix="cardR34plan_"), "pricestore.db")
    _mk_xd_store(path)
    before = _xd_cands()
    slim = {"meta": {"run_date": "2026-07-01", "data_date": "2026-06-30"},
            "candidates": copy.deepcopy(before)}
    _run_xd_fix(slim, path)
    for old, new in zip(before, slim["candidates"]):
        p0, p1 = bt.reconstruct_plan(old), bt.reconstruct_plan(new)
        assert (p0 is None) == (p1 is None), old["code"]
        if p0 is None:
            continue
        assert p0.get("mode") == p1.get("mode"), (
            f"{old['code']}: 入场剧本被改了 {p0.get('mode')} -> {p1.get('mode')}")
        for k in ("entry_ref", "entry_low", "entry_high", "stop"):
            r0, r1 = p0[k] / float(old["price"]), p1[k] / float(new["price"])
            assert abs(r0 - r1) < 1e-12, (
                f"{old['code']}.{k} 相对 snap_px 偏移了 {(r1 / r0 - 1) * 100:+.3f}% "
                f"—— 说明 price 与计划价位的基准被拆成了两套")


def test_paper_registration_carries_xd_flag():
    """模拟盘那条链**注册时**就得把 `xd` 抄进账本 —— 否则 `_simulate_signal` 里读它恒为假。

    `_latest_signals` 用 `CAND_KEYS` 白名单给候选瘦身, 首版没把 "xd" 加进去, 于是写进
    `data/paper_portfolio.json` 的 cand 里永远没有这个键, 锚定侧那行 `cand.get("xd")`
    对新注册的信号是死代码。账本是三条消费链里**唯一注册一次之后再也不回看快照**的一条,
    锚错了事后只能靠迁移脚本补 —— 正是本卡在补的那 62 条的同一失败形态。
    """
    from leftside_core import paper as pp                        # noqa: PLC0415
    assert "xd" in pp.CAND_KEYS, "CAND_KEYS 少了 xd -> 账本里永远拿不到这个标记"
    tmpd = tempfile.mkdtemp(prefix="cardR34pp_")
    cand = {"code": "600061", "name": "XD国投资", "tag": "🕳 深跌抄底", "price": 6.4,
            "atr_pct": 3.1, "support_price": 6.4503, "xd": True}
    old_ls, old_paths = pp.bt.load_snapshots, pp._paths
    try:
        pp.bt.load_snapshots = lambda: [{"as_of": "2026-06-30", "cands": [cand]}]
        pp._paths = lambda: (tmpd, os.path.join(tmpd, "p.json"),
                             os.path.join(tmpd, "p.js"))
        _as_of, sigs = pp._latest_signals()
        state = {"signals": [], "daily": []}
        n = pp._register(state, sigs)
    finally:
        pp.bt.load_snapshots, pp._paths = old_ls, old_paths
    assert n == 1 and state["signals"], sigs
    assert state["signals"][0]["cand"].get("xd") is True, (
        "账本里那份 cand 丢了 xd: %r" % state["signals"][0]["cand"])


# ---------------------------------------------------------------- 模拟盘 sig_date 迁移

def test_migrate_paper_sigdate_classify():
    """迁移脚本的判定必须**两个条件都要**: 等于 08-24 的价 **且** 不等于 08-21 的价。

    只判"等于 08-24"会把两天收盘恰好相同的票也迁走 (账本里实测 5 条属于这种), 那是把
    一个数据问题换成另一个。判定用精确相等而不是容差: 这里问的是"这条记录是从哪份文件
    抄来的", 不是"两个价差不多"。
    """
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "tools"))
    import migrate_paper_sigdate as mg                           # noqa: PLC0415
    state = {"signals": [
        {"id": "dip:A:2026-08-21", "cat": "dip", "code": "A", "sig_date": "2026-08-21",
         "cand": {"code": "A", "price": 2.0}},                    # 只等于 08-24 -> 迁
        {"id": "dip:B:2026-08-21", "cat": "dip", "code": "B", "sig_date": "2026-08-21",
         "cand": {"code": "B", "price": 5.0}},                    # 两天同价 -> 不动
        {"id": "dip:C:2026-08-21", "cat": "dip", "code": "C", "sig_date": "2026-08-21",
         "cand": {"code": "C", "price": 9.0}},                    # 只等于 08-21 -> 不动
        {"id": "quality:D:2026-08-21", "cat": "quality", "code": "D",
         "sig_date": "2026-08-21", "cand": {"code": "D"}},        # 无价 -> 不判
        {"id": "dip:E:2026-08-21", "cat": "dip", "code": "E", "sig_date": "2026-08-21",
         "cand": {"code": "E", "price": 7.0}},                    # 会撞 id -> 交人工
        {"id": "dip:E:2026-08-24", "cat": "dip", "code": "E", "sig_date": "2026-08-24",
         "cand": {"code": "E", "price": 7.0}},
        {"id": "dip:F:2026-08-25", "cat": "dip", "code": "F", "sig_date": "2026-08-25",
         "cand": {"code": "F", "price": 1.0}},                    # 不是那一天 -> 不看
    ]}
    px_wrong = {"B": 5.0, "C": 9.0}
    px_right = {"A": 2.0, "B": 5.0, "C": 1.0, "E": 7.0}
    r = mg.classify(state, px_wrong, px_right)
    assert [x["code"] for x in r["move"]] == ["A"], r["move"]
    assert r["move"][0]["id_new"] == "dip:A:2026-08-24"
    assert r["move"][0]["only_in_0824"] is True
    assert sorted(x["code"] for x in r["keep"]) == ["B", "C"]
    assert {x["code"]: x["verdict"] for x in r["keep"]} == {"B": "both_match", "C": "only_0821"}
    assert [x["code"] for x in r["quality"]] == ["D"]
    assert [x["code"] for x in r["id_clash"]] == ["E"], "撞 id 的不许自动改, 要交给人"
    # 判定是纯函数: 不许碰传进来的账本
    assert state["signals"][0]["sig_date"] == "2026-08-21"
    assert state["signals"][0]["id"] == "dip:A:2026-08-21"


def test_migrate_paper_sigdate_drops_final_cache():
    """改判 `sig_date` 的同时**必须删掉 `final` 冻结缓存**, 否则这条迁移等于白做。

    `leftside_core.paper.update_portfolio` 对 `s.get("final")` 为真的信号**直接返回缓存,
    不再重新模拟**。首版的 --apply 只写 sig_date / id / 留痕字段, 于是任何已被冻结的迁移
    对象会带着按**错误信号日**算出来的成交日/出场日/盈亏一直留在账本里 —— 而迁移的全部
    目的就是修这个。缓存是可再生的 (走完完整窗口会自己写回来), 但删之前要留痕。
    这里在**一次性临时账本**上真跑 --apply (真账本 data/paper_portfolio.json 一个字节不碰)。
    """
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "tools"))
    import migrate_paper_sigdate as mg                           # noqa: PLC0415
    repo = tempfile.mkdtemp(prefix="cardR34mg_")
    hist = os.path.join(repo, "dashboard", "history")
    os.makedirs(hist)
    os.makedirs(os.path.join(repo, "data"))
    for day, cands in (("2026-08-21", [{"code": "C", "price": 9.0}]),
                       ("2026-08-24", [{"code": "A", "price": 2.0},
                                       {"code": "C", "price": 1.0}])):
        with open(os.path.join(hist, "day_%s.json" % day), "w", encoding="utf-8") as f:
            json.dump({"candidates": cands}, f)
    frozen = {"status": "stopped", "fill_date": "2026-08-24", "ret": -0.07}
    ledger = os.path.join(repo, "data", "paper_portfolio.json")
    with open(ledger, "w", encoding="utf-8") as f:
        json.dump({"signals": [
            {"id": "dip:A:2026-08-21", "cat": "dip", "code": "A",
             "sig_date": "2026-08-21", "cand": {"code": "A", "price": 2.0},
             "final": frozen},                                    # 冻结的 -> 要被解冻
            {"id": "dip:C:2026-08-21", "cat": "dip", "code": "C",
             "sig_date": "2026-08-21", "cand": {"code": "C", "price": 9.0},
             "final": dict(frozen)},                              # 不迁 -> 缓存不许动
        ]}, f)
    argv = sys.argv
    try:
        sys.argv = ["migrate_paper_sigdate.py", "--repo", repo, "--apply"]
        assert mg.main() == 0
    finally:
        sys.argv = argv
    with open(ledger, encoding="utf-8") as f:
        after = {s["code"]: s for s in json.load(f)["signals"]}
    a = after["A"]
    assert a["sig_date"] == "2026-08-24" and a["id"] == "dip:A:2026-08-24"
    assert a["sig_date_migrated_from"] == "2026-08-21"
    assert "final" not in a, "改了日子却留着 final -> update_portfolio 永远返回旧结果"
    assert a["final_dropped"] == frozen, "删掉的缓存必须原样留痕, 否则事后对不了账"
    c = after["C"]
    assert c["sig_date"] == "2026-08-21" and c["final"] == frozen, (
        "没被改判的信号缓存一个字都不许动")
    # 备份确实先写了, 且是改动前那一份
    baks = sorted(os.listdir(os.path.join(repo, "data", "backups")))
    assert baks, "--apply 必须先备份"
    with open(os.path.join(repo, "data", "backups", baks[-1]), encoding="utf-8") as f:
        assert json.load(f)["signals"][0]["final"] == frozen, "备份该是改动**前**的账本"


TESTS = [test_anchor_uses_raw_not_qfq, test_anchor_closes_tolerates_nan,
         test_default_is_off_until_boss_signs_off,
         test_return_across_ex_div_uses_qfq, test_store_hit_and_per_code_fallback,
         test_unknown_code_falls_back_not_treated_as_delisted,
         test_store_stale_falls_back_whole_batch, test_switch_off_disables_store_path,
         test_demo_seed_snapshot_excluded_from_replay,
         test_demo_whitelist_is_load_bearing,
         test_snapshot_data_date_fix_table,
         test_load_snapshots_applies_data_date_fix_on_stale_copy,
         test_market_hook_signature_is_backward_compatible,
         test_backtest_store_switch_three_layers,
         test_run_pipeline_logs_backtest_price_switch,
         test_xd_rebased_closes_is_raw_times_factor_ratio,
         test_xd_snapshot_only_flags_never_reprices,
         test_xd_generation_reads_corrected_data_date,
         test_xd_flag_is_harmless_when_price_is_plain_raw,
         test_xd_flag_survives_into_anchoring,
         test_xd_flag_does_not_move_plan_levels,
         test_paper_registration_carries_xd_flag,
         test_migrate_paper_sigdate_classify,
         test_migrate_paper_sigdate_drops_final_cache]


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
