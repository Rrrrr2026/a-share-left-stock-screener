#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
裁池 B2 复检 离线自测 (不联网, 纯函数 + 假快照 + 打桩)。

覆盖 (卡 B2, 2026-09-08 · GM 口径决定):
  · **uncovered / dead 分桶**: 库内一根 bar 都没有时, "库明说 status D/P (退市/暂停)" 与
    "库根本没覆盖这个码" 必须是两个桶、两个计数、两句日志 —— 09-08 首版合成一桶叫 absent,
    注释还统称"历史遗留退市码", 于是 uncovered 从 0 涨到几百只也没人看得见。
  · **B 股射程外** (CONFIG.tech.exclude_b_share): 沪B 900xxx / 深B 200xxx 在**候选池构建
    阶段**就剔掉 (两条路: ds.build_universe 全市场路 + run_pipeline 行业成分路), 所以它们
    既不进候选池、也不进 uncovered 统计。价格库对 B 股确实零覆盖, 但那是另一件事 ——
    这里要保证"策略不做它"不被写成"库没覆盖它"。
  · **回滚开关严格取值**: ASHARE_POOL_BY_STORE 只认 0/1/true/false/on/off (大小写不敏感),
    别的值不当数但必须 log.warning —— 值班的人写了个无效值, 不许和写对了长得一样。
  · **尺子自己有多旧**: 个股末日落后**库自己的交易日历** >3 个交易日 (或整库 >20 自然日
    一动不动) -> 照裁, 但 scan_basis 降成 'store_universe_stale'。判据**不看挂钟** ——
    09-08 首版用挂钟工作日, 复现结果是每个国庆/春节连报三天假警并把那几天的 scan_basis
    永久标成 stale (库停在末交易日是正确行为), 这里有一条专门的长假回归用例锁住。
  · **计数恒等式**: 纯 keep + gap + Σ dropped == 原池 (上一轮 absent 拆解 163+7+12 ≠ 177
    那种数字对不上, 不许再出现)。

运行:  python tests/test_pool_cut_b2.py   或   python -m pytest tests/test_pool_cut_b2.py -q
"""
from __future__ import annotations
import json
import logging
import os
import shutil
import sys
import tempfile
import datetime as dt

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ashare.config import CONFIG                                # noqa: E402
import ashare.config as cfgmod                                  # noqa: E402
from ashare import datasource as ds                             # noqa: E402
import run_pipeline as rp                                       # noqa: E402

FRESH = "2026-08-25"


class _Cap(logging.Handler):
    """抓某个 logger 的消息 —— "有没有响" 本身就是被测行为。"""

    def __init__(self, logger):
        super().__init__()
        self.msgs, self._lg = [], logger
        self._lvl = logger.level

    def __enter__(self):
        self._lg.addHandler(self)
        self._lg.setLevel(logging.INFO)
        return self

    def emit(self, r):
        self.msgs.append(r.getMessage())

    def __exit__(self, *a):
        self._lg.removeHandler(self)
        self._lg.setLevel(self._lvl)
        return False


# ---------------------------------------------------------------------------
#  1. uncovered / dead 分桶
# ---------------------------------------------------------------------------
def test_verdict_uncovered_vs_dead():
    """一根 bar 都没有、又不在点时股票池: status 决定它是 dead 还是 uncovered。"""
    v = lambda st: ds.store_verdict(0, None, False, FRESH, status=st)   # noqa: E731
    assert v("D") == "dead", "Tushare stock_basic 的 D = 退市, 库明说它死了"
    assert v("P") == "dead", "P = 暂停上市, 同样是库明说的"
    assert v("d") == "dead", "status 大小写不该改变裁决"
    assert v(None) == "uncovered", "universe 表里根本没有这一行 = 库未覆盖"
    assert v("") == "uncovered", "空 status 不是'库说它死了'"
    assert v("L") == "uncovered", (
        "status=L 却一根 bar 都没有、又不在点时股票池 (首根 bar 还没到): "
        "对'今天能不能扫'来说就是库没覆盖到它, 不能算退市")
    # 在池的优先级最高: 在池 + 无 bar = gap (真缺口, 留在池里回落联网), 与 status 无关
    assert ds.store_verdict(0, None, True, FRESH, status="D") == "gap"
    # 有 bar 的三条路不受 status 影响 (status 只在"一根都没有"时才被问到)
    assert ds.store_verdict(300, "2026-09-07", True, FRESH, status="D") == "keep"
    assert ds.store_verdict(300, "2026-06-30", False, FRESH, status="D") == "stale"
    assert ds.store_verdict(30, "2026-09-07", False, FRESH, status=None) == "too_new"
    # 不传 status 时按"库没说" -> uncovered (老调用点不会静默变成 dead)
    assert ds.store_verdict(0, None, False, FRESH) == "uncovered"


def test_drop_reason_labels_cover_every_verdict():
    """每个裁决都要有中文标签, 且 dead/uncovered 的措辞不许再互相冒充。"""
    for v in ("dead", "uncovered", "stale", "too_new", "gap"):
        assert v in ds.STORE_DROP_REASON_CN, f"{v} 没有中文标签, 日志里会露出英文 key"
    assert "absent" not in ds.STORE_DROP_REASON_CN, "absent 已拆桶, 不该再有这个 key"
    assert "退市" in ds.STORE_DROP_REASON_CN["dead"]
    assert "退市" not in ds.STORE_DROP_REASON_CN["uncovered"], (
        "uncovered 的标签里不许出现'退市' —— 它正是'库没听说过'那一桶")


# ---------------------------------------------------------------------------
#  2. B 股射程外 (候选池构建阶段就剔)
# ---------------------------------------------------------------------------
def _spot(codes_names):
    return pd.DataFrame({"code": [c for c, _ in codes_names],
                         "name": [n for _, n in codes_names]})


_MIXED = [("600000", "浦发银行"), ("000001", "平安银行"), ("300750", "宁德时代"),
          ("900919", "临港B股"), ("200019", "深粮B"), ("200553", "安道麦B"),
          ("831010", "北交所票"), ("920001", "北交新段"), ("430047", "北交老段")]


def test_is_b_share_prefixes():
    for c in ("900919", "200019", "200553", "900001"):
        assert ds.is_b_share(c), f"{c} 是 B 股"
    for c in ("600000", "000001", "300750", "920001", "831010", "002019"):
        assert not ds.is_b_share(c), f"{c} 不是 B 股 (920 是北交所, 别和 900 搞混)"


def test_build_universe_drops_b_share():
    """全市场那条路 (ds.build_universe): B 股与北交所一起在快照阶段剔掉。"""
    saved = CONFIG["tech"].get("exclude_b_share")
    try:
        CONFIG["tech"]["exclude_b_share"] = True
        out = list(ds.build_universe(_spot(_MIXED))["code"])
        assert out == ["600000", "000001", "300750"], out
        # 开关能关 (射程是策略决定, 要能被推翻)
        CONFIG["tech"]["exclude_b_share"] = False
        out2 = list(ds.build_universe(_spot(_MIXED))["code"])
        assert "900919" in out2 and "200019" in out2, out2
        assert "831010" not in out2, "北交所仍由 exclude_bj 剔"
    finally:
        CONFIG["tech"]["exclude_b_share"] = saved


def test_candidate_universe_industry_path_drops_b_share():
    """行业成分那条路 (run_pipeline.build_candidate_universe) 必须用**同一条**规则。

    两条路各写各的规则 = B 股在全市场日子里被剔、在行业成分日子里混进来, 分母口径逐日漂移。
    """
    saved_cons, saved_sw = ds.fetch_industry_cons, CONFIG["tech"].get("exclude_b_share")
    try:
        CONFIG["tech"]["exclude_b_share"] = True
        # 凑够 3000 只: 低于这个数 build_candidate_universe 会判"行业成分覆盖偏低"并去
        # 并全市场池 (那条路要联网/读快照缓存), 就测不到本用例要测的那一段了。
        filler = [(f"{600100 + i:06d}", f"填{i}") for i in range(3100)]
        cons = pd.DataFrame({"code": [c for c, _ in _MIXED + filler],
                             "name": [n for _, n in _MIXED + filler]})
        ds.fetch_industry_cons = lambda ind: cons
        ind_df = pd.DataFrame({"industry": ["银行"], "selected": [True]})
        uni, ind_to_codes = rp.build_candidate_universe(None, {}, ind_df, ["银行"])
        codes = [c for (c, _, _) in uni]
        assert codes[:3] == ["600000", "000001", "300750"], codes[:6]
        assert len(codes) == 3 + len(filler), len(codes)
        assert not any(ds.is_b_share(c) for c in codes), "B 股一只都不许进候选池"
        assert not any(str(c).startswith(("8", "4", "920")) for c in codes), "北交所照旧剔"
    finally:
        ds.fetch_industry_cons, CONFIG["tech"]["exclude_b_share"] = saved_cons, saved_sw


def test_b_share_marked_out_of_scope_if_switch_off():
    """万一射程开关被关掉、B 股又进了候选池: 留痕里必须标"策略射程外", 而不是混在
    uncovered 里被当成"库丢了一批码"。"""
    saved = (ds._store_pit_ctx, ds._store_status_map, ds._store_conn, ds.DATA_DIR)
    try:
        ds._store_pit_ctx = lambda: ({"600000"}, FRESH)
        ds._store_status_map = lambda: {"600000": "L", "000004": "D"}

        class _Conn:                                    # 只回答那句 GROUP BY
            def execute(self, sql, args=()):
                return [("600000", 300, "2026-09-07")]
        ds._store_conn = lambda: _Conn()
        keep, dropped, degraded, kept = ds.store_universe_filter(
            ["600000", "000004", "200019"], days=913)
        assert degraded is None and keep == ["600000"]
        assert [x["code"] for x in dropped["dead"]] == ["000004"]
        unc = dropped["uncovered"]
        assert [x["code"] for x in unc] == ["200019"]
        assert unc[0].get("note") == "策略射程外(B股)", unc[0]
        assert dropped["dead"][0].get("note") is None, "退市码不该被贴上 B 股标签"
    finally:
        (ds._store_pit_ctx, ds._store_status_map, ds._store_conn, ds.DATA_DIR) = saved


def test_config_does_not_claim_a_market_wide_b_share_count():
    """射程决定的理由里不许再写"在市 B 股只剩 7 只"—— 09-08 复检推翻: 那个 7 是空跑当天
    恰好抓到的 9/90 个行业成分表里的 B 股数 (同法 09-04=2 / 09-03=4 / 08-28=19), 不是全市场数。
    本仓根本拿不到权威数 (东财全A快照对 B 股恒 0 行, 库里也 0 行), 所以注释里只许写**下界**。
    一个错的市场事实被写进 config 当决定依据, 以后翻档案的人会照抄 —— 用例锁住它。
    """
    src = open(cfgmod.__file__, encoding="utf-8").read()
    i = src.find("在市 B 股只剩 7 只")
    assert i < 0 or "错的" in src[i:i + 60], "这句话只能作为'首版写错了'的引用出现"
    blk = src[src.find("---- 策略射程"):src.find('"exclude_b_share"')]
    assert "下界" in blk, "B 股只数只能写成下界"
    assert "60 只 distinct B 股代码" in blk, "写下界就要写清这 60 是从哪儿数出来的"


# ---------------------------------------------------------------------------
#  3. 回滚开关只认 6 个值
# ---------------------------------------------------------------------------
def test_switch_strict_values():
    saved_env, saved_dir = os.environ.get("ASHARE_POOL_BY_STORE"), cfgmod.DATA_DIR
    d = tempfile.mkdtemp(prefix="poolsw_b2_")
    try:
        cfgmod.DATA_DIR = d
        for v in ("0", "FALSE", "Off", " off "):
            os.environ["ASHARE_POOL_BY_STORE"] = v
            on, why, warn = cfgmod._pool_by_store_switch(True)
            assert on is False and not warn, (v, on, warn)
        for v in ("1", "TRUE", "On", " on "):
            os.environ["ASHARE_POOL_BY_STORE"] = v
            assert cfgmod._pool_by_store_switch(True) == (True, "", ""), v
        # 无效值: 不当数 (按默认 = 开), 但必须给出告警文本
        for v in ("yes", "no", "ture", "2", "关"):
            os.environ["ASHARE_POOL_BY_STORE"] = v
            on, why, warn = cfgmod._pool_by_store_switch(True)
            assert on is True and why == "", (v, on, why)
            assert warn and "0/1/true/false/on/off" in warn, (v, warn)
            assert repr(v) in warn, "告警要把他到底写了什么原样回显出来"
        os.environ.pop("ASHARE_POOL_BY_STORE", None)
        assert cfgmod._pool_by_store_switch(True) == (True, "", "")
    finally:
        cfgmod.DATA_DIR = saved_dir
        if saved_env is None:
            os.environ.pop("ASHARE_POOL_BY_STORE", None)
        else:
            os.environ["ASHARE_POOL_BY_STORE"] = saved_env


def test_switch_warn_is_logged_not_swallowed():
    """无效值的告警必须在裁池那一步真的打出来 (config 在 basicConfig 之前 import,
    只在那里 log 会掉进 lastResort, 进不了 journal 正文)。"""
    saved = (CONFIG["tech"].get("pool_by_store_switch_warn"),
             CONFIG["tech"].get("pool_by_store"))
    try:
        CONFIG["tech"]["pool_by_store_switch_warn"] = "ASHARE_POOL_BY_STORE='yes' 不是可识别的值"
        CONFIG["tech"]["pool_by_store"] = False        # 走最短的一条分支也要响
        with _Cap(rp.log) as cap:
            rp.trim_universe_by_store([("600000", "甲", None)], "2026-09-08")
        assert any("不是可识别的值" in m for m in cap.msgs), cap.msgs
    finally:
        (CONFIG["tech"]["pool_by_store_switch_warn"],
         CONFIG["tech"]["pool_by_store"]) = saved


# ---------------------------------------------------------------------------
#  4. 尺子自己有多旧
# ---------------------------------------------------------------------------
def test_weekday_and_calendar_lag_are_diagnostics_only():
    """两个 lag 辅助函数的算术。**它们只是留痕字段, 不是判据** (见下面的长假回归)。"""
    w = ds._weekdays_between
    assert w("2026-09-08", "2026-09-08") == 0
    assert w("2026-09-07", "2026-09-08") == 1
    assert w("2026-09-04", "2026-09-08") == 2            # 周五 -> 周二: 周一/周二
    assert w("2026-09-01", "2026-09-08") == 5            # 周末不算
    assert w("2026-09-08", "2026-09-07") == 0, "库比当日还新 (不该发生) 也不许算成负数"
    assert w(None, "2026-09-08") == 0 and w("x", "y") == 0, "日期坏掉时不许抛"
    c = ds._calendar_days_between
    assert c("2026-09-01", "2026-09-08") == 7
    assert c("2026-09-08", "2026-09-01") == 0 and c(None, "x") == 0


class _TmpStore:
    """一个只有 bars/idx_bars 两张表的临时价格库, 让尺子判据走**真 SQL** 而不是打桩。"""

    def __init__(self, bars_max: str, idx_days: list):
        self.dir = tempfile.mkdtemp(prefix="ruler_")
        self.path = os.path.join(self.dir, "pricestore.db")
        import sqlite3
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE bars(code TEXT, d TEXT, c REAL)")
        conn.execute("CREATE TABLE idx_bars(d TEXT PRIMARY KEY, c REAL)")
        if bars_max:
            conn.execute("INSERT INTO bars VALUES('600000',?,1.0)", (bars_max,))
        conn.executemany("INSERT INTO idx_bars VALUES(?,1.0)", [(d,) for d in idx_days])
        conn.commit()
        conn.close()

    def __enter__(self):
        self.saved = ds._store_path                              # noqa: SLF001
        ds._store_path = lambda: self.path                       # noqa: SLF001
        ds._STORE_TLS.conn = None                                # noqa: SLF001
        return self

    def __exit__(self, *a):
        conn = getattr(ds._STORE_TLS, "conn", None)              # noqa: SLF001
        if conn is not None:
            conn.close()
        ds._store_path = self.saved                              # noqa: SLF001
        ds._STORE_TLS.conn = None                                # noqa: SLF001
        shutil.rmtree(self.dir, ignore_errors=True)


#: 2026 国庆前的末交易日 + 假期后真实开市的几个交易日 (用来演长假那出戏)。
_SEP = ["2026-09-28", "2026-09-29", "2026-09-30"]
_OCT = ["2026-10-09", "2026-10-12", "2026-10-13", "2026-10-14"]


def test_ruler_does_not_cry_wolf_during_a_long_holiday():
    """**09-08 复检的核心回归**: 国庆休市里库停在 09-30 是**正确行为**, 不许报 stale。

    首版拿挂钟工作日当判据, 10-06/07/08 会连报三天"尺子自己旧了", 还把那三天的
    scan_basis 永久写成 'store_universe_stale' —— 系统当时完全健康。
    现在判据是"库自己的交易日历里比个股末日更晚的交易日数": 休市期间没有新交易日入库,
    lag 恒 0。挂钟工作日照样算给人看 (10-08 已经 6 个), 但它不参与判定。
    """
    with _TmpStore("2026-09-30", _SEP):
        for day, wd in (("2026-10-06", 4), ("2026-10-07", 5), ("2026-10-08", 6)):
            f = ds.store_ruler_freshness(day)
            assert f["lag_trade_days"] == 0, f
            assert f["stale"] is False, f"{day} 假期里误报: {f}"
            assert f["stale_reason"] == ""
            assert f["lag_weekdays"] == wd, f          # 诊断字段照记, 只是不当判据
            assert f["store_max_d"] == "2026-09-30" and f["idx_max_d"] == "2026-09-30"


def test_ruler_fires_when_the_stock_leg_falls_behind_the_index_leg():
    """市场照开而 `pricestore update` 连着几天没跑成: 指数腿往前走, 个股腿不动 -> 报。"""
    with _TmpStore("2026-09-30", _SEP + _OCT[:3]):     # 指数腿多走 3 个交易日 = 到线不过线
        f = ds.store_ruler_freshness("2026-10-13")
        assert f["lag_trade_days"] == 3 and f["stale"] is False, f
    with _TmpStore("2026-09-30", _SEP + _OCT):         # 多走 4 个 > 3
        f = ds.store_ruler_freshness("2026-10-14")
        assert f["lag_trade_days"] == 4, f
        assert f["stale"] is True and f["stale_reason"] == "index_ahead", f
    with _TmpStore("2026-10-14", _SEP + _OCT):         # 个股腿比指数腿还新: 不是 stale
        f = ds.store_ruler_freshness("2026-10-15")
        assert f["lag_trade_days"] == 0 and f["stale"] is False, f


def test_ruler_frozen_fallback_when_both_legs_stop():
    """两条腿一起冻住 (指数源也哑火): 交易日差恒 0, 只能靠自然日兜底, 且只在 >20 天后才响。"""
    with _TmpStore("2026-09-30", _SEP):
        f = ds.store_ruler_freshness("2026-10-20")     # 20 个自然日 = 到线不过线
        assert f["lag_calendar_days"] == 20 and f["stale"] is False, f
        f = ds.store_ruler_freshness("2026-10-21")     # 21 天 > 20
        assert f["stale"] is True and f["stale_reason"] == "frozen", f
    with _TmpStore("2026-09-30", []):                  # 老库: 根本没有 idx_bars 行
        f = ds.store_ruler_freshness("2026-10-08")
        assert f["idx_max_d"] is None and f["lag_trade_days"] is None, f
        assert f["stale"] is False, "没日历就说不出'旧了', 不许瞎报"
        assert ds.store_ruler_freshness("2026-10-25")["stale_reason"] == "frozen"


def test_ruler_on_empty_store():
    saved = ds._store_max_date
    try:
        ds._store_max_date = lambda: None              # 库空: 判不了, 不许瞎报 stale
        assert ds.store_ruler_freshness("2026-09-08") == {
            "store_max_d": None, "idx_max_d": None, "asof": "2026-09-08",
            "lag_trade_days": None, "lag_calendar_days": None, "lag_weekdays": None,
            "stale": False, "stale_reason": ""}
    finally:
        ds._store_max_date = saved


def test_trim_marks_scan_basis_stale():
    """尺子旧了: **照裁** (旧尺子好过没尺子), 但 scan_basis 必须说真话, 且日志要响。"""
    uni = [(f"{600000 + i:06d}", f"票{i}", None) for i in range(100)]
    saved = (ds.bars_from_store_on, ds.store_universe_filter, ds.store_pool_meta,
             ds.store_ruler_freshness, rp.DATA_DIR,
             CONFIG["tech"].get("pool_by_store"),
             CONFIG["tech"].get("pool_by_store_switch_warn"))
    tmp = tempfile.mkdtemp(prefix="poolstale_")
    try:
        CONFIG["tech"]["pool_by_store"] = True
        CONFIG["tech"]["pool_by_store_switch_warn"] = ""
        rp.DATA_DIR = tmp
        ds.bars_from_store_on = lambda: True
        ds.store_pool_meta = lambda asof=None: {"n_universe": 5216}
        ds.store_universe_filter = lambda cs, days=None: (
            list(cs)[:98],
            {"dead": [{"code": c, "n_bars": 0, "last_bar": None, "status": "D"}
                      for c in [x[0] for x in uni][98:]]}, None, {})
        ds.store_ruler_freshness = lambda asof=None: {
            "store_max_d": "2026-09-01", "idx_max_d": "2026-09-08", "asof": "2026-09-08",
            "lag_trade_days": 5, "lag_calendar_days": 7, "lag_weekdays": 5,
            "stale": True, "stale_reason": "index_ahead"}
        with _Cap(rp.log) as cap:
            out, basis = rp.trim_universe_by_store(uni, "2026-09-08")
        assert len(out) == 98, "尺子旧不等于不裁"
        assert basis == "store_universe_stale", basis
        assert any("尺子自己旧了" in m for m in cap.msgs), cap.msgs
        rec = json.load(open(os.path.join(tmp, "pool_cut", "2026-09-08.json"),
                             encoding="utf-8"))
        assert rec["scan_basis"] == "store_universe_stale"
        # 两条腿一起冻住是**另一种事故**, 值班要查的地方也不同 -> 必须是另一句话
        ds.store_ruler_freshness = lambda asof=None: {
            "store_max_d": "2026-08-10", "idx_max_d": "2026-08-10", "asof": "2026-09-08",
            "lag_trade_days": 0, "lag_calendar_days": 29, "lag_weekdays": 21,
            "stale": True, "stale_reason": "frozen"}
        with _Cap(rp.log) as cap2:
            _, basis2 = rp.trim_universe_by_store(uni, "2026-09-08")
        assert basis2 == "store_universe_stale", basis2
        assert any("整个库不动了" in m for m in cap2.msgs), cap2.msgs
        assert not any("尺子自己旧了" in m for m in cap2.msgs), "两种事故不许共用一句话"
    finally:
        (ds.bars_from_store_on, ds.store_universe_filter, ds.store_pool_meta,
         ds.store_ruler_freshness, rp.DATA_DIR, CONFIG["tech"]["pool_by_store"],
         CONFIG["tech"]["pool_by_store_switch_warn"]) = saved
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
#  5. 计数恒等式 (上一轮 163+7+12 != 177 的那道题)
# ---------------------------------------------------------------------------
def test_counts_identity_closes_and_screams_when_it_does_not():
    uni = [(f"{600000 + i:06d}", f"票{i}", None) for i in range(1000)]
    codes = [c for (c, _, _) in uni]
    saved = (ds.bars_from_store_on, ds.store_universe_filter, ds.store_pool_meta,
             ds.store_ruler_freshness, rp.DATA_DIR,
             CONFIG["tech"].get("pool_by_store"),
             CONFIG["tech"].get("pool_by_store_switch_warn"))
    tmp = tempfile.mkdtemp(prefix="poolident_")
    try:
        CONFIG["tech"]["pool_by_store"] = True
        CONFIG["tech"]["pool_by_store_switch_warn"] = ""
        rp.DATA_DIR = tmp
        ds.bars_from_store_on = lambda: True
        ds.store_pool_meta = lambda asof=None: {"n_universe": 900}
        ds.store_ruler_freshness = lambda asof=None: {
            "store_max_d": "2026-09-08", "asof": "2026-09-08",
            "lag_weekdays": 0, "stale": False}
        drop = {"dead": [{"code": c, "n_bars": 0, "last_bar": None, "status": "D"}
                         for c in codes[:60]],
                "uncovered": [{"code": c, "n_bars": 0, "last_bar": None, "status": None}
                              for c in codes[60:75]],
                "too_new": [{"code": c, "n_bars": 10, "last_bar": "2026-09-07",
                             "status": "L"} for c in codes[75:80]]}
        gap = [{"code": c, "n_bars": 0, "last_bar": None, "status": "L"}
               for c in codes[80:83]]
        ds.store_universe_filter = lambda cs, days=None: (
            [c for c in cs if c not in {x["code"] for v in drop.values() for x in v}],
            drop, None, {"gap": gap})
        with _Cap(rp.log) as cap:
            out, basis = rp.trim_universe_by_store(uni, "2026-09-08")
        assert basis == "store_universe" and len(out) == 920
        rec = json.load(open(os.path.join(tmp, "pool_cut", "2026-09-08.json"),
                             encoding="utf-8"))
        c = rec["counts"]
        assert c["dropped"] == {"dead": 60, "too_new": 5, "uncovered": 15}
        assert c["gap"] == 3 and c["keep"] == 917
        assert c["keep"] + c["gap"] + sum(c["dropped"].values()) == rec["n_pool_raw"] == 1000
        assert c["identity_ok"] is True
        assert not any("恒等式不闭合" in m for m in cap.msgs)
        # 日志里 dead 与 uncovered 必须**分别**报数, 不许再合成一个 "absent 75"
        line = [m for m in cap.msgs if "候选池按库裁: 东财" in m]
        assert line and "库明说退市/暂停(status D/P) 60" in line[0], line
        assert "库未覆盖(无此码且无K线) 15" in line[0], line
        assert "缺K线gap 3" in line[0], line
        assert "B 股" not in line[0], "这一池没有 B 股, 就不该凭空多出一句"

        # ---- 射程开关被关掉时: uncovered 里的 B 股要在日志里当场点出来
        drop2 = dict(drop, uncovered=[dict(x, note="策略射程外(B股)") if i < 4 else x
                                      for i, x in enumerate(drop["uncovered"])])
        ds.store_universe_filter = lambda cs, days=None: (
            [c for c in cs if c not in {x["code"] for v in drop2.values() for x in v}],
            drop2, None, {"gap": gap})
        with _Cap(rp.log) as cap3:
            rp.trim_universe_by_store(uni, "2026-09-08")
        line3 = [m for m in cap3.msgs if "候选池按库裁: 东财" in m]
        assert line3 and "库未覆盖(无此码且无K线) 15(其中 B 股 4 只·策略射程外)" in line3[0], line3

        # ---- 故意把数字弄错: 必须 error, 不许闷头写一份对不上的留痕
        ds.store_universe_filter = lambda cs, days=None: (
            list(cs), drop, None, {"gap": gap})          # 一只没裁却报了 80 只被裁
        with _Cap(rp.log) as cap2:
            rp.trim_universe_by_store(uni, "2026-09-08")
        assert any("恒等式不闭合" in m for m in cap2.msgs), cap2.msgs
    finally:
        (ds.bars_from_store_on, ds.store_universe_filter, ds.store_pool_meta,
         ds.store_ruler_freshness, rp.DATA_DIR, CONFIG["tech"]["pool_by_store"],
         CONFIG["tech"]["pool_by_store_switch_warn"]) = saved
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
#  6. 邮件正文那句"全市场扫描 N 只"
# ---------------------------------------------------------------------------
def test_mailer_scan_line_says_which_basis():
    from ashare import mailer
    base = {"candidates": [], "industries": []}
    new = mailer.build_summary_text(
        dict(base, meta={"run_date": "2026-09-08", "n_scanned": 4932, "n_hit": 380,
                         "scan_basis": "store_universe"}))
    assert "全市场扫描 4932 只 (库内在市 A 股; 09-08 前口径含退市老代码约 5180)" in new, new
    old = mailer.build_summary_text(
        dict(base, meta={"run_date": "2026-09-08", "n_scanned": 5180, "n_hit": 380,
                         "scan_basis": "raw_spot"}))
    assert "库内在市 A 股" not in old, "回滚那天分母是东财原池, 这句注解会变成假话"
    assert "东财快照原池" in old, old


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    for fn in TESTS:
        fn()
        print(f"  ok {fn.__name__}")
    print(f"\n全部通过: {len(TESTS)} 项  ({dt.datetime.now():%H:%M:%S})")
