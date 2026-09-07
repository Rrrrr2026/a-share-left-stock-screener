#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
价格库 schema v2 + A股 Tushare 取数分支 离线自测 (不联网, 临时库 + 假 Market)。

覆盖 (设计 design/tushare_adapter_design.md §P1):
  · 建表与 **v1 老库就地升级** (bars 补 amt 列, 老数据不动)
  · 前复权物化: base = 该股最新因子, 因子前向填充, 除权日前后水平比为常数
  · load(adjust=qfq|hfq|raw) 三口径 + 老库无 bars_raw 时退回 qfq (不静默返回空)
  · universe_at(date): list_date <= d < delist_date (退市股点时进出); **list_date 缺失时**
    落 NULL 而不是 '1970-01-01' 哨兵, 改以 "库内首根 bar" 当在市起点 (前视污染修复)
  · update_daily 按 trade_date 增量: 因子未变只补当日, 因子变了整段重物化;
    **未就绪守卫** (当日行数 < 在市股 90% 就地停下, 不写残缺日, 不越过它);
    钩子未启用时 v1 老库回退逐股路径, 而 **v2 库一律拒写** (逐股给的是另一套复权基准)
  · 阶段A 取数改道 (2026-09-07 换库): datasource.fetch_hist 直读库; 库判退市/次新的票
    不再逐股联网 (东财快照里 196 只历史遗留退市码曾把阶段A 从 98 秒拖到 30 分钟+)
  · 候选池按库裁 (2026-09-08): store_verdict / store_universe_filter —— 在池 / 不在池但
    K线新鲜 / 次新不足 60 根 / 退市无 K线 四种情形, 与 fetch_hist 共用同一份口径
  · ashare/market: 单位换算 (vol 手×100=股, amount 千元×1000=元)、北交所/B股剔除、
    个股 000001 必须是 SZ (不能被指数特判成 SH)、源开关未翻时按日钩子恒返回 None

运行:  python tests/test_pricestore_v2.py    或    python -m pytest tests/test_pricestore_v2.py -q
"""
from __future__ import annotations
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import types
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ashare.market as amkt                                    # noqa: E402  (注入 Market)
from ashare.config import CONFIG                                # noqa: E402
from leftside_core import pricestore as ps                      # noqa: E402
from leftside_core.market import Market, set_market             # noqa: E402

PASS, FAIL = 0, 0
_SAVED_MARKET = None


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ FAIL: {name}")


def close(a, b, tol=1e-9):
    return a is not None and b is not None and abs(a - b) <= tol * max(1.0, abs(b))


# ---------------------------------------------------------------- 夹具


class FakeMarket:
    """按日拉全市场的假源: days -> {code: (o,h,l,c,v,amt)} / {code: factor}。"""

    def __init__(self, bars, facs, days, universe=None, enabled=True):
        self.bars, self.facs, self.days = bars, facs, days
        self.universe = universe or []
        self.enabled = enabled
        self.legacy_calls = []

    def market(self, data_dir):
        return Market(
            name="ashare", dashboard_dir=data_dir, data_dir=data_dir,
            db_path=os.path.join(data_dir, "x.db"),
            fetch_bars_bulk=self._legacy_bulk,
            fetch_index_bars=lambda s: [],
            fetch_bars_by_date=lambda d: (self.bars.get(d, {}) if self.enabled else None),
            fetch_adj_by_date=lambda d: (self.facs.get(d, {}) if self.enabled else None),
            trading_days=lambda a, b: ([d for d in self.days if a <= d <= b]
                                       if self.enabled else None),
            fetch_universe_rows=lambda: self.universe)

    def _legacy_bulk(self, codes, start):
        self.legacy_calls.append((tuple(codes), start))
        return {}


def use_tmp_market(fake: FakeMarket) -> str:
    d = tempfile.mkdtemp(prefix="ps2_")
    set_market(fake.market(d))
    return d


def seed_v1_db(path):
    """造一个 v1 老库 (bars 只有 6 列), 用来测就地升级。"""
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE bars(code TEXT, d TEXT, o REAL, h REAL, l REAL, c REAL, v REAL, "
              "PRIMARY KEY(code, d)) WITHOUT ROWID")
    c.execute("CREATE TABLE idx_bars(d TEXT PRIMARY KEY, o REAL, h REAL, l REAL, c REAL, "
              "v REAL) WITHOUT ROWID")
    c.executemany("INSERT INTO bars VALUES(?,?,?,?,?,?,?)",
                  [("000001", f"2026-01-{i:02d}", 10.0, 11.0, 9.0, 10.5, 1000.0)
                   for i in range(1, 29)])
    c.commit()
    c.close()


def fill_v2(path, code="000001", n=80, factors=None):
    """直接写 bars_raw/adj, 返回 (dates, closes, factor_map)。"""
    conn = sqlite3.connect(path)
    ps._ensure_schema(conn)
    dates = [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(n)]
    rows, frows = [], []
    for i, d in enumerate(dates):
        c = 10.0 + i * 0.1
        rows.append((code, d, c - 0.2, c + 0.3, c - 0.4, c, 1000.0 + i, 12345.0 + i))
        f = (factors or {}).get(d)
        if f is not None:
            frows.append((code, d, f))
    conn.executemany("INSERT OR REPLACE INTO bars_raw VALUES(?,?,?,?,?,?,?,?)", rows)
    if frows:
        conn.executemany("INSERT OR REPLACE INTO adj VALUES(?,?,?)", frows)
    conn.commit()
    conn.close()
    return dates, [r[5] for r in rows]


# ---------------------------------------------------------------- 用例


def test_schema_and_v1_upgrade():
    print("\n[建表 / v1 老库就地升级]")
    fake = FakeMarket({}, {}, [])
    d = use_tmp_market(fake)
    path = os.path.join(d, "pricestore.db")
    seed_v1_db(path)
    conn = ps._conn()
    tabs = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    check("v2 表齐全", {"bars", "bars_raw", "adj", "adj_base", "idx_bars", "idx_multi",
                        "universe", "meta"} <= tabs)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(bars)")]
    check("老库 bars 补出 amt 列且顺序在最后", cols[:7] == ["code", "d", "o", "h", "l", "c", "v"]
          and cols[-1] == "amt")
    n, amt = conn.execute("SELECT COUNT(*), amt FROM bars WHERE code='000001'").fetchone()
    check("老数据 28 行原样保留, amt 为 NULL", n == 28 and amt is None)
    conn.close()
    check("coverage 认得出 schema v1", ps.coverage()["schema"] == "v1")
    shutil.rmtree(d, ignore_errors=True)


def test_qfq_math():
    print("\n[前复权物化: 基准=最新因子, 因子前向填充]")
    fake = FakeMarket({}, {}, [])
    d = use_tmp_market(fake)
    path = os.path.join(d, "pricestore.db")
    # 因子只在 3 个日子给出 (模拟 Tushare 只在有变动的日子…实际每日都有, 这里测前向填充)
    dates, closes = fill_v2(path, n=80, factors={"2026-01-01": 1.0, "2026-01-15": 1.0,
                                                 "2026-02-04": 2.0})
    n = ps.materialize()
    check("物化返回 1 只", n == 1)
    got = ps.load(["000001"])["000001"]
    i15, i04 = dates.index("2026-01-15"), dates.index("2026-02-04")
    base = 2.0
    check("除权前: qfq = raw × 1.0 / 2.0", close(got["ohlcv"][i15][3], closes[i15] * 1.0 / base))
    check("除权后: qfq = raw (因子=基准)", close(got["ohlcv"][i04][3], closes[i04]))
    conn = sqlite3.connect(path)
    bd, bf = conn.execute("SELECT d, factor FROM adj_base WHERE code='000001'").fetchone()
    check("adj_base 记住基准日与因子", bd == "2026-02-04" and close(bf, 2.0))
    conn.close()
    check("coverage 认得出 schema v2", ps.coverage()["schema"] == "v2")
    shutil.rmtree(d, ignore_errors=True)


def test_load_adjust_modes():
    print("\n[load(adjust=qfq|hfq|raw)]")
    fake = FakeMarket({}, {}, [])
    d = use_tmp_market(fake)
    path = os.path.join(d, "pricestore.db")
    dates, closes = fill_v2(path, n=80, factors={"2026-01-01": 1.0, "2026-02-04": 2.0})
    ps.materialize()
    i0 = dates.index("2026-01-10")
    qfq = ps.load(["000001"], "qfq")["000001"]["ohlcv"][i0][3]
    hfq = ps.load(["000001"], "hfq")["000001"]["ohlcv"][i0][3]
    raw = ps.load(["000001"], "raw")["000001"]["ohlcv"][i0][3]
    check("raw = 原始收盘", close(raw, closes[i0]))
    check("hfq = raw × factor", close(hfq, closes[i0] * 1.0))
    check("qfq = raw × factor / 基准", close(qfq, closes[i0] / 2.0))
    check("hfq/qfq = 基准", close(hfq / qfq, 2.0))
    amt = ps.load(["000001"], with_amt=True)["000001"]["amt"]
    check("with_amt 带出成交额", len(amt) == 80 and close(float(amt[0]), 12345.0))
    check("ohlcv 仍是 5 列 (消费者不变)", ps.load(["000001"])["000001"]["ohlcv"].shape == (80, 5))
    try:
        ps.load(["000001"], "hfq2")
        check("非法 adjust 应报错", False)
    except ValueError:
        check("非法 adjust 报 ValueError", True)
    shutil.rmtree(d, ignore_errors=True)


def test_load_v1_fallback():
    print("\n[v1 老库 (无 bars_raw) 请求 hfq -> 退回 qfq, 不静默返回空]")
    fake = FakeMarket({}, {}, [])
    d = use_tmp_market(fake)
    seed_v1_db(os.path.join(d, "pricestore.db"))
    conn = ps._conn()
    conn.executemany("INSERT OR REPLACE INTO bars(code,d,o,h,l,c,v) VALUES(?,?,?,?,?,?,?)",
                     [("000002", f"2026-{3 + i // 28:02d}-{1 + i % 28:02d}", 1, 2, 0.5, 1.5, 9)
                      for i in range(70)])
    conn.commit()
    conn.close()
    got = ps.load(["000001"], "hfq")
    check("退回 qfq 后仍能装出 v1 数据", "000001" in got and len(got["000001"]["dates"]) == 28
          or "000001" not in got)     # 28 < MIN_BARS -> 不返回; 关键是不炸
    got2 = ps.load(["000002"], "raw")
    check("v1 库 raw 请求也拿得到 (退回 qfq)", "000002" in got2)
    shutil.rmtree(d, ignore_errors=True)


def test_universe_at():
    print("\n[universe_at(date): list_date <= d < delist_date]")
    fake = FakeMarket({}, {}, [])
    d = use_tmp_market(fake)
    conn = ps._conn()
    conn.executemany("INSERT OR REPLACE INTO universe VALUES(?,?,?,?,?)", [
        ("000001", "平安银行", "1991-04-03", "", "L"),
        ("000004", "国华退", "1990-12-01", "2026-07-14", "D"),
        ("301999", "次新", "2026-08-01", "", "L")])
    conn.commit()
    conn.close()
    a = ps.universe_at("2026-06-30")
    b = ps.universe_at("2026-07-14")
    c = ps.universe_at("2026-08-05")
    check("退市前在池", "000004" in a)
    check("退市当日出池 (d < delist_date)", "000004" not in b)
    check("上市前不在池", "301999" not in a and "301999" in c)
    check("在市股恒在池", all("000001" in x for x in (a, b, c)))
    check("空 universe 返回 []", isinstance(ps.universe_at("1990-01-01"), list))
    shutil.rmtree(d, ignore_errors=True)


def test_universe_null_list_date():
    print("\n[list_date 缺失: 落 NULL 不落 1970 哨兵, universe_at 以首根 bar 兜底]")
    # 背景 (2026-09-07 实测): 镜像的 stock_basic 对刚上市的次新股把 list_date 返回成 epoch 0
    # 的 '19700101'。它是个**合法日期字符串**, 落库后 universe_at(任意历史日) 都会把这些
    # 2026 年才上市的票算成 "1970 年就在市" —— 点时股票池被前视污染, 九年研究的分母全歪。
    check("norm_date 认得各路空值/哨兵", all(ps.norm_date(x) is None for x in (
        "19700101", "1970-01-01", "", None, "0", "00000000", "0000-00-00", "nan", "NaT",
        float("nan"), "1899-12-30", "garbage")))
    check("norm_date 正常日期照常规整",
          ps.norm_date("20260907") == ps.norm_date("2026-09-07") == ps.norm_date("2026/9/7")
          == "2026-09-07")

    raw = [("301688", "C格林", "19700101", None, "L"),          # 源没给上市日, 有 bar
           ("301699", "洛轴股份", "19700101", None, "L"),        # 源没给上市日, 且一根 bar 都没有
           ("000693", "长期停牌", "19970226", "", "l"),          # 有上市日, 但首根 bar 很晚
           ("000004", "国华退", "19901201", "20260714", "D"),    # 退市股
           ("000001", "平安银行", "19910403", None, "L")]
    norm = ps.normalize_universe_rows(raw)
    check("1970 哨兵 -> None (不是 '1970-01-01' 也不是 '')", norm[0][2] is None)
    check("空 delist_date -> None; 有值原样带上",
          norm[0][3] is None and norm[3][3] == "2026-07-14")
    check("状态归一到大写", norm[2][4] == "L")

    fake = FakeMarket({}, {}, [], universe=raw)
    d = use_tmp_market(fake)
    conn = ps._conn()
    conn.executemany(
        "INSERT OR REPLACE INTO bars_raw VALUES(?,?,?,?,?,?,?,?)",
        [("000001", f"2026-0{m}-01", 10, 11, 9, 10.5, 100, 1000) for m in (1, 6, 9)]
        + [("301688", "2026-09-02", 10, 11, 9, 10.5, 100, 1000)]
        + [("000693", "2026-09-01", 10, 11, 9, 10.5, 100, 1000)]
        + [("000004", "2026-01-05", 10, 11, 9, 10.5, 100, 1000)])
    conn.commit()
    n = ps._refresh_universe(ps.current(), conn)
    got = dict(conn.execute("SELECT code, list_date FROM universe"))
    check("刷新写入 5 只", n == 5 and len(got) == 5)
    check("库里一条 1970 都没有", conn.execute(
        "SELECT COUNT(*) FROM universe WHERE list_date LIKE '1970%'").fetchone()[0] == 0)
    check("源没给的上市日在库里是 NULL", got["301688"] is None and got["301699"] is None)
    check("有上市日的照旧", got["000001"] == "1991-04-03")

    before, after = ps.universe_at("2026-06-30", conn), ps.universe_at("2026-09-02", conn)
    check("没上市日的票: 首根 bar 之前不入池", "301688" not in before)
    check("没上市日的票: 首根 bar 当天起入池", "301688" in after)
    check("没上市日又一根 bar 都没有 -> 永不入池",
          "301699" not in before and "301699" not in after)
    check("有上市日但首根 bar 还没到 -> 当日买不到, 不入池",
          "000693" not in before and "000693" in after)
    check("退市股仍按 delist_date 点时进出", "000004" in before and "000004" not in after)
    check("正常票不受影响", "000001" in before and "000001" in after)

    # 缓存: universe_at 会把 universe 行 + 首根 bar 表按库指纹缓存 (九年重放要按天调上千次),
    # 库一改必须自动失效, 否则新写的行看不见。
    conn.execute("INSERT OR REPLACE INTO universe VALUES('302000','新票',NULL,NULL,'L')")
    conn.execute("INSERT OR REPLACE INTO bars_raw VALUES('302000','2026-09-01',"
                 "10,11,9,10.5,100,1000)")
    conn.commit()
    check("库一改, 点时缓存自动失效", "302000" in ps.universe_at("2026-09-02", conn))
    check("first_bar_dates 报得出首根 bar",
          ps.first_bar_dates(conn).get("301688") == "2026-09-02")
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


def _snapshot_bars(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT code,d,o,h,l,c,v,amt FROM bars ORDER BY code,d").fetchall()
    finally:
        conn.close()


def _day_fixture():
    """两只票三天: 000001 因子不变, 000002 第三天除权 (因子 1.0 -> 1.25)。"""
    days = ["2026-09-03", "2026-09-04", "2026-09-07"]
    bars = {d: {"000001": (10.0 + i, 11.0 + i, 9.0 + i, 10.5 + i, 100.0, 1000.0),
                "000002": (20.0 + i, 21.0 + i, 19.0 + i, 20.5 + i, 200.0, 2000.0)}
            for i, d in enumerate(days)}
    facs = {days[0]: {"000001": 3.0, "000002": 1.0},
            days[1]: {"000001": 3.0, "000002": 1.0},
            days[2]: {"000001": 3.0, "000002": 1.25}}
    return days, bars, facs


def test_update_daily_by_date():
    print("\n[update_daily 按 trade_date 增量]")
    days, bars, facs = _day_fixture()
    uni = [("000001", "甲", "2020-01-01", "", "L"), ("000002", "乙", "2020-01-01", "", "L")]
    fake = FakeMarket(bars, facs, days, universe=uni)
    d = use_tmp_market(fake)
    path = os.path.join(d, "pricestore.db")
    # 先灌前两天并物化 (模拟"库已建好")
    conn = ps._conn()
    for dd in days[:2]:
        conn.executemany("INSERT OR REPLACE INTO bars_raw VALUES(?,?,?,?,?,?,?,?)",
                         [(c, dd, *v) for c, v in bars[dd].items()])
        conn.executemany("INSERT OR REPLACE INTO adj VALUES(?,?,?)",
                         [(c, dd, f) for c, f in facs[dd].items()])
    conn.commit()
    ps.materialize(None, conn)
    conn.close()

    n = ps.update_daily()
    check("增量返回当日 bar 数", n == 2)
    conn = sqlite3.connect(path)
    got = dict(conn.execute("SELECT code, COUNT(*) FROM bars_raw GROUP BY code").fetchall())
    check("bars_raw 三天齐", got == {"000001": 3, "000002": 3})
    # 000001 因子没变: 基准 3.0, qfq = raw
    r1 = conn.execute("SELECT d, c FROM bars WHERE code='000001' ORDER BY d").fetchall()
    check("因子未变的票: 增量只补当日, 历史不动",
          len(r1) == 3 and close(r1[0][1], 10.5) and close(r1[2][1], 12.5))
    # 000002 除权: 基准 1.0 -> 1.25, 历史整体 ×0.8
    r2 = conn.execute("SELECT d, c FROM bars WHERE code='000002' ORDER BY d").fetchall()
    check("除权的票: 历史整段重物化 (×1.0/1.25)",
          close(r2[0][1], 20.5 * 0.8) and close(r2[1][1], 21.5 * 0.8)
          and close(r2[2][1], 22.5))
    bf = dict(conn.execute("SELECT code, factor FROM adj_base").fetchall())
    check("adj_base 跟着基准走", close(bf["000001"], 3.0) and close(bf["000002"], 1.25))
    mt = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    check("meta 记录末日与单位", mt.get("max_trade_date") == "2026-09-07"
          and mt.get("unit_v") == "股" and mt.get("unit_amt") == "元")
    nu = conn.execute("SELECT COUNT(*) FROM universe").fetchone()[0]
    check("universe 顺带刷新", nu == 2)
    conn.close()
    check("重复跑无新交易日 -> 0", ps.update_daily() == 0)
    check("增量期间没碰旧逐股路径", not fake.legacy_calls)
    shutil.rmtree(d, ignore_errors=True)


def test_update_daily_falls_back():
    print("\n[钩子未启用 (返回 None) + v1 老库 -> 回退旧逐股增量]")
    days, bars, facs = _day_fixture()
    fake = FakeMarket(bars, facs, days, enabled=False)
    d = use_tmp_market(fake)
    conn = ps._conn()
    # v1 老库: 只有物化 bars, 没有 bars_raw —— 美股库与 A 股换库前都是这个形态
    conn.executemany("INSERT OR REPLACE INTO bars(code,d,o,h,l,c,v) VALUES(?,?,?,?,?,?,?)",
                     [("000001", days[0], 1, 2, 0.5, 1.5, 9)])
    conn.commit()
    conn.close()
    ps.update_daily()
    check("v1 老库仍走旧路径 (fetch_bars_bulk 被调用)", bool(fake.legacy_calls))
    shutil.rmtree(d, ignore_errors=True)


def test_update_daily_v2_refuses_legacy():
    """2026-09-07 换库后的硬规矩: v2 库上, 按日路径没启用就什么都不写。

    旧逐股路径 _upsert 的是数据源直给的前复权价 (基准 = 抓取那天), 与 v2 的 adj_base 无关;
    一旦写进 bars 就会把物化 qfq 口径改花、amt 抹成 NULL、bars 与 bars_raw/adj 脱钩。
    r1shadow / factor_export 在开关还没切 tushare 时也会调 update_daily —— 必须挡住。"""
    print("\n[v2 库 + 钩子未启用 -> 拒绝写库, 不回退逐股]")
    days, bars, facs = _day_fixture()
    fake = FakeMarket(bars, facs, days, enabled=False)
    d = use_tmp_market(fake)
    path = os.path.join(d, "pricestore.db")
    conn = ps._conn()
    conn.executemany("INSERT OR REPLACE INTO bars_raw VALUES(?,?,?,?,?,?,?,?)",
                     [(c, days[0], *v) for c, v in bars[days[0]].items()])
    conn.executemany("INSERT OR REPLACE INTO bars(code,d,o,h,l,c,v,amt) "
                     "VALUES(?,?,?,?,?,?,?,?)",
                     [(c, days[0], *v) for c, v in bars[days[0]].items()])
    conn.commit()
    conn.close()
    before = _snapshot_bars(path)
    check("返回 0 (什么都没写)", ps.update_daily() == 0)
    check("没有回退到逐股路径", not fake.legacy_calls)
    check("bars 逐值未被改动", _snapshot_bars(path) == before)
    shutil.rmtree(d, ignore_errors=True)


def test_update_daily_not_ready_guard():
    """当日行数 < 在市股 90% -> 打 'Tushare 当日未就绪, 沿用昨日库' 并**就地停下**。

    必须是停下不是跳过: days 是连续的, 跳过 D 却写了 D+1, MAX(d) 就越过 D, 那天永远补不回。"""
    print("\n[未就绪守卫: 半天的行情不许进库]")
    days, bars, facs = _day_fixture()
    bars = {k: dict(v) for k, v in bars.items()}
    bars[days[1]].pop("000002")            # 第二天只回来 1/2 只 = 50% < 90%
    uni = [("000001", "甲", "2020-01-01", "", "L"), ("000002", "乙", "2020-01-01", "", "L")]
    fake = FakeMarket(bars, facs, days, universe=uni)
    d = use_tmp_market(fake)
    path = os.path.join(d, "pricestore.db")
    conn = ps._conn()
    conn.executemany("INSERT OR REPLACE INTO bars_raw VALUES(?,?,?,?,?,?,?,?)",
                     [(c, days[0], *v) for c, v in bars[days[0]].items()])
    conn.executemany("INSERT OR REPLACE INTO adj VALUES(?,?,?)",
                     [(c, days[0], f) for c, f in facs[days[0]].items()])
    conn.executemany("INSERT OR REPLACE INTO universe(code,name,list_date,delist_date,status) "
                     "VALUES(?,?,?,?,?)", uni)
    conn.commit()
    ps.materialize(None, conn)
    conn.close()
    n = ps.update_daily()
    conn = sqlite3.connect(path)
    mx = conn.execute("SELECT MAX(d) FROM bars_raw").fetchone()[0]
    got = dict(conn.execute("SELECT d, COUNT(*) FROM bars_raw GROUP BY d").fetchall())
    conn.close()
    check("残缺日一根都没写", got == {days[0]: 2})
    check("末日仍停在最后一个完整日", mx == days[0])
    check("残缺日之后的日子也不许抢跑 (停下而非跳过)", days[2] not in got)
    check("返回 0 根", n == 0)
    shutil.rmtree(d, ignore_errors=True)


def test_market_units_and_filters():
    print("\n[ashare/market: 单位换算与代码过滤]")
    import pandas as pd
    df = pd.DataFrame([
        {"ts_code": "000001.SZ", "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
         "vol": 1234.0, "amount": 5678.0},
        {"ts_code": "600000.SH", "open": 1, "high": 2, "low": 0.5, "close": 1.5,
         "vol": 10.0, "amount": 20.0},
        {"ts_code": "830799.BJ", "open": 1, "high": 2, "low": 0.5, "close": 1.5,
         "vol": 10.0, "amount": 20.0},
        {"ts_code": "920002.BJ", "open": 1, "high": 2, "low": 0.5, "close": 1.5,
         "vol": 10.0, "amount": 20.0},
        {"ts_code": "900901.SH", "open": 1, "high": 2, "low": 0.5, "close": 1.5,
         "vol": 10.0, "amount": 20.0},
        {"ts_code": "200011.SZ", "open": 1, "high": 2, "low": 0.5, "close": 1.5,
         "vol": 10.0, "amount": 20.0},
        {"ts_code": "300001.SZ", "open": 1, "high": 2, "low": 0.5, "close": 0.0,
         "vol": 10.0, "amount": 20.0},
    ])
    out = amkt._rows_from_daily(df)
    check("只留 0/3/6 主板创业板科创板", set(out) == {"000001", "600000"})
    check("vol 手 ×100 = 股", close(out["000001"][4], 123400.0))
    check("amount 千元 ×1000 = 元", close(out["000001"][5], 5678000.0))
    check("收盘价 <=0 的脏行被丢", "300001" not in out)
    check("北交所 8 前缀剔除", amkt.keep_a_code("830799.BJ") is None)
    check("北交所 920 前缀剔除", amkt.keep_a_code("920002.SZ") is None)
    check("B股 900/200 剔除", amkt.keep_a_code("900901.SH") is None
          and amkt.keep_a_code("200011.SZ") is None)
    check("个股 000001 是 SZ (不能被指数特判成 SH)", amkt._stock_ts("000001") == "000001.SZ")
    check("科创板 688 -> SH", amkt._stock_ts("688049") == "688049.SH")
    check("_iso 不做时区换算", amkt._iso("20260907") == "2026-09-07")


def test_source_switch_default():
    print("\n[源开关: 生产默认已切 tushare (2026-09-07 换库), 开关仍可回落 fuyao]")
    saved = CONFIG["source"].get("bars")
    try:
        check("生产默认是 tushare", str(saved or "").lower() == "tushare")
        CONFIG["source"]["bars"] = "fuyao"
        check("回落 fuyao 后按日钩子全部熄火", amkt._bars_source() == "fuyao"
              and not amkt._tushare_on())
        check("未启用时 fetch_bars_by_date 返回 None (不是 {})",
              amkt.fetch_bars_by_date("2026-09-07") is None)
        check("未启用时 fetch_adj_by_date 返回 None",
              amkt.fetch_adj_by_date("2026-09-07") is None)
        check("未启用时 trading_days 返回 None", amkt.trading_days("2026-09-01",
                                                                  "2026-09-07") is None)
        check("未启用时 fetch_universe_rows 返回 []", amkt.fetch_universe_rows() == [])
        CONFIG["source"]["bars"] = "tushare"
        check("切开关立刻生效 (不用重启进程)", amkt._bars_source() == "tushare")
    finally:
        CONFIG["source"]["bars"] = saved


def test_stage_a_reads_store():
    """阶段A 取数改道 (2026-09-07 换库): datasource.fetch_hist 直读库, 且不再为退市票联网。"""
    print("\n[阶段A: fetch_hist 直读价格库 / 退市票不回落联网]")
    from ashare import datasource as ds
    days, bars, facs = _day_fixture()
    fake = FakeMarket(bars, facs, days)
    d = use_tmp_market(fake)
    path = os.path.join(d, "pricestore.db")
    conn = ps._conn()
    today = dt.date.today()
    ds_dates = [(today - dt.timedelta(days=i)).isoformat()
                for i in range(400, 0, -1)]
    conn.executemany("INSERT OR REPLACE INTO bars(code,d,o,h,l,c,v,amt) VALUES(?,?,?,?,?,?,?,?)",
                     [("000001", dd, 10.0, 11.0, 9.0, 10.5, 1000.0, 12345.0) for dd in ds_dates]
                     + [("000002", dd, 20.0, 21.0, 19.0, 20.5, 2000.0, 42345.0)
                        for dd in ds_dates[:30]])          # 000002 只有 30 根 = 次新股形态
    conn.executemany("INSERT OR REPLACE INTO universe(code,name,list_date,delist_date,status) "
                     "VALUES(?,?,?,?,?)",
                     [("000001", "甲", "2020-01-01", None, "L"),
                      ("000002", "乙", "2026-08-01", None, "L"),
                      ("000009", "丙", "2001-01-01", "2024-05-01", "D")])
    conn.commit()
    conn.close()

    saved_dir, saved_src, saved_cache = ds.DATA_DIR, CONFIG["source"].get("bars"), \
        CONFIG["source"]["use_cache"]
    ds.DATA_DIR, CONFIG["source"]["bars"], CONFIG["source"]["use_cache"] = d, "tushare", False
    ds._STORE_TLS = threading.local()
    ds._store_status = None
    ds._store_pit = None
    ds._store_stat.clear()
    net = []
    saved_fuyao = sys.modules.get("ashare.fuyao")
    stub = types.ModuleType("ashare.fuyao")
    stub.available = lambda: (net.append("fuyao"), True)[1]
    stub.hist = lambda code, years=2.5: net.append(("hist", code))
    sys.modules["ashare.fuyao"] = stub
    try:
        check("开关生效", ds.bars_from_store_on())
        df = ds.fetch_hist("000001")
        check("在市票直读库 (400 根)", df is not None and len(df) == 400)
        check("列齐 (含 amount, module2 的流动性门要用)",
              df is not None and {"date", "open", "high", "low", "close", "volume",
                                  "amount"} <= set(df.columns))
        check("直读没碰网络", not net)
        check("退市票直接判无数据", ds.fetch_hist("000009") is None)
        check("次新股 (库里 30 根) 也不联网 —— 联网也凑不出 255 根",
              ds.fetch_hist("000002") is None)
        check("全程零联网", not net)
        st = ds.store_stats()
        check("统计: 命中 1 / 跳过 2 / 回落 0",
              st.get("hit") == 1 and st.get("skipped") == 2 and not st.get("fallback"))
    finally:
        ds.DATA_DIR, CONFIG["source"]["bars"], CONFIG["source"]["use_cache"] = \
            saved_dir, saved_src, saved_cache
        if saved_fuyao is not None:
            sys.modules["ashare.fuyao"] = saved_fuyao
        else:
            sys.modules.pop("ashare.fuyao", None)
        ds._STORE_TLS = threading.local()
        ds._store_status = None
        ds._store_pit = None
        ds._store_stat.clear()
        shutil.rmtree(d, ignore_errors=True)


def test_store_universe_filter():
    """候选池按库裁 (2026-09-08): 东财快照的候选池 -> 库内在市股。

    老板 09-07 夜拍板接受口径断层 (对外扫描数 5180 -> ~4950)。四种情形必须都判对, 且判据
    与 fetch_hist 是**同一个** store_verdict —— 两处口径一分叉, 分母就又不诚实了。
    """
    print("\n[候选池按库裁: universe_at + K线在市证据]")
    from ashare import datasource as ds

    # ---- 纯函数先单独判 (无 I/O): 四种情形 + "有K线但不在 universe 表" 那个口子
    fresh = "2026-08-25"
    check("① 在池 + 够根数 -> keep",
          ds.store_verdict(300, "2026-09-07", True, fresh) == "keep")
    check("② 不在池, 但 ≥60 根且末根新鲜 -> keep (在市证据以K线为准)",
          ds.store_verdict(300, "2026-09-07", False, fresh) == "keep")
    check("②' 不在池 + 末根过期 -> stale (陈年K线不算在市)",
          ds.store_verdict(300, "2026-06-30", False, fresh) == "stale")
    check("③ 次新 (1..59 根) -> too_new, 在不在池都一样",
          ds.store_verdict(30, "2026-09-07", True, fresh) == "too_new"
          and ds.store_verdict(1, "2026-09-07", False, fresh) == "too_new")
    check("④ 无 bar: 在池=gap(值得回落联网) / 不在池=absent(退市老代码)",
          ds.store_verdict(0, None, True, fresh) == "gap"
          and ds.store_verdict(0, None, False, fresh) == "absent")
    check("留在池里的裁决 = keep + gap (gap 在 universe_at 里, 按卡的规则必须留)",
          tuple(ds.STORE_KEEP_VERDICTS) == ("keep", "gap"))

    # ---- 再用临时最小 v2 库跑一遍真链路
    fake = FakeMarket({}, {}, [])
    d = use_tmp_market(fake)
    conn = ps._conn()
    today = dt.date.today()
    days = [(today - dt.timedelta(days=i)).isoformat() for i in range(400, -1, -1)]
    conn.executemany("INSERT OR REPLACE INTO idx_bars(d,o,h,l,c,v) VALUES(?,?,?,?,?,?)",
                     [(dd, 1.0, 1.0, 1.0, 1.0, 1.0) for dd in days])   # 库自己的交易日历

    def _bars(code, dates):
        rows = [(code, dd, 10.0, 11.0, 9.0, 10.5, 1000.0, 12345.0) for dd in dates]
        conn.executemany("INSERT OR REPLACE INTO bars(code,d,o,h,l,c,v,amt) "
                         "VALUES(?,?,?,?,?,?,?,?)", rows)
        conn.executemany("INSERT OR REPLACE INTO bars_raw(code,d,o,h,l,c,v,amt) "
                         "VALUES(?,?,?,?,?,?,?,?)", rows)

    _bars("600000", days)                 # ① 在池老票, 400 根到今天
    _bars("000022", days)                 # ② 有K线但 universe 表没这一行 (改过代码的票)
    _bars("600001", days[:200])           # ②' 有K线但最后一根停在 200 天前 = 已退市/长停
    _bars("301999", days[-30:])           # ③ 次新: 只有 30 根
    conn.executemany("INSERT OR REPLACE INTO universe(code,name,list_date,delist_date,status) "
                     "VALUES(?,?,?,?,?)",
                     [("600000", "老票", "2015-01-05", None, "L"),
                      ("301999", "次新", days[-30], None, "L"),
                      ("600002", "在池无K线", "2015-01-05", None, "L"),
                      ("000004", "国华退", "1990-12-01", days[100], "D")])
    conn.commit()
    conn.close()

    saved_dir, saved_src, saved_cache = ds.DATA_DIR, CONFIG["source"].get("bars"), \
        CONFIG["source"]["use_cache"]
    ds.DATA_DIR, CONFIG["source"]["bars"], CONFIG["source"]["use_cache"] = d, "tushare", False
    ds._STORE_TLS = threading.local()
    ds._store_status = None
    ds._store_pit = None
    try:
        codes = ["600000", "000022", "600001", "301999", "000004", "600002"]
        keep, dropped, degraded = ds.store_universe_filter(codes)
        by = {k: sorted(x["code"] for x in v) for k, v in dropped.items()}
        check("过滤器真的跑完了 (第三个返回值 None = 不是降级放行)", degraded is None)
        check("① 在池老票留下", "600000" in keep)
        check("② 不在 universe 表但K线新鲜 -> 留下 (000022 那三只)", "000022" in keep)
        check("②' 有K线但过期 -> 裁 (stale)", by.get("stale") == ["600001"])
        check("③ 次新 30 根 -> 裁 (too_new, 在池也照裁: 60 根是 module2 的硬起步)",
              by.get("too_new") == ["301999"])
        check("④ 退市无K线 -> 裁 (absent)", by.get("absent") == ["000004"])
        # 09-08 首版把 gap 也裁了 —— 与卡的规则("保留 universe_at 内的代码")相反, 且阶段A
        # 从此不会为它调 fetch_hist, fetch_hist 里那条"只有 gap 才回落联网"的分支成了死代码。
        check("④' 在池却一根K线都没有 -> **留下** (gap: 库说它今天在市, 由 fetch_hist 回落联网)",
              "600002" in keep and "gap" not in dropped)
        check("裁后剩三只, 且保留入参顺序", keep == ["600000", "000022", "600002"])
        check("裁前裁后数对得上", len(keep) + sum(len(v) for v in dropped.values()) == len(codes))
        check("gap 票在取数层仍会回落联网 (没有库就没有 'skipped')",
              ds._store_verdict_one("600002", 913) == "gap")
        meta = ds.store_pool_meta()
        check("留痕: 新鲜度截止日取自库内交易日历 (末 10 个交易日)",
              meta["fresh_after"] == days[-10] and meta["min_bars"] == 60)
        check("留痕: 点时股票池只数 = universe_at(今日)",
              meta["n_universe"] == len(ps.universe_at(today.isoformat())))
        # fetch_hist 与裁池共用同一判据 (store_verdict): 被裁的票在取数层同样是"无数据"。
        # 例外是 stale (600001): 它库里有 200 根旧 bar, fetch_hist 的直读快路会照样把这段陈年
        # K 线交出来 —— 所以"退市/长停"这一刀**只有裁池能挡**, 这正是裁池的价值所在。
        check("同一口径: 次新/退市无K线的票 fetch_hist 也拿不到",
              all(ds.fetch_hist(c) is None for c in ("301999", "000004")))
        check("stale 票的旧K线只有裁池能挡 (fetch_hist 直读快路仍会给)",
              ds.fetch_hist("600001") is not None)
        # 开关关掉 = 一行回滚
        saved_sw = CONFIG["tech"].get("pool_by_store")
        try:
            CONFIG["tech"]["pool_by_store"] = False
            import run_pipeline as rp
            uni = [(c, c, None) for c in codes]
            out, basis = rp.trim_universe_by_store(uni, "2026-09-08")
            check("回滚开关: pool_by_store=False 原样放行, 口径标回 raw_spot",
                  len(out) == len(codes) and basis == "raw_spot")
        finally:
            CONFIG["tech"]["pool_by_store"] = saved_sw
    finally:
        ds.DATA_DIR, CONFIG["source"]["bars"], CONFIG["source"]["use_cache"] = \
            saved_dir, saved_src, saved_cache
        ds._STORE_TLS = threading.local()
        ds._store_status = None
        ds._store_pit = None
        ds._store_stat.clear()
        shutil.rmtree(d, ignore_errors=True)


def test_pool_trim_basis_and_rollback():
    """裁池的**对外口径 (scan_basis)** 与**回滚口子** —— 09-08 复检补的两处。

    ① scan_basis 存在的唯一理由是让前端/历史快照分清"这一天的分母是新口径还是老口径"。
       首版 `if not dropped: return universe, ("store_universe" if len(keep)==raw_n ...)`
       把"没什么可裁"和"没能裁"混为一谈: 库读不出来那天一只都没裁 (分母还是老口径的 5180),
       却自称 store_universe、n_pool_raw == n_scanned —— 事后对账的人会把 5180 当成"裁后的
       在市股数", 比没有这个字段更糟。两条降级路径必须标回 raw_spot。
    ② 回滚开关必须在**服务器上**按得下去: run_a.sh 跑之前 `git reset -q --hard origin/main`,
       config.py 是被跟踪文件, 值班的人在服务器上改 False 会在下一次 stock-a 启动的头几秒被
       抹掉且毫无提示 —— 他会以为关掉了其实没关 (09-03/09-07 两次静默事故的同一形态)。
       所以要有环境变量与停机文件两条不经过 git 的路, 且关闭必须打日志。
    """
    print("\n[裁池对外口径 scan_basis + 回滚口子]")
    import json as _json
    from ashare import datasource as ds
    import ashare.config as cfgmod
    import run_pipeline as rp

    uni = [(f"{600000 + i:06d}", f"票{i}", "行业") for i in range(5180)]
    codes = [c for (c, _, _) in uni]
    saved = (ds.bars_from_store_on, ds.store_universe_filter, ds.store_pool_meta,
             rp.DATA_DIR, CONFIG["tech"].get("pool_by_store"),
             CONFIG["tech"].get("pool_by_store_off_by"))
    tmp = tempfile.mkdtemp(prefix="pooltrim_")
    try:
        ds.bars_from_store_on = lambda: True
        ds.store_pool_meta = lambda: {"n_universe": 5216, "fresh_after": "2026-08-25",
                                      "min_bars": 60, "fresh_trade_days": 10}
        rp.DATA_DIR = tmp
        CONFIG["tech"]["pool_by_store"] = True

        # ---- 降级① 点时股票池读不出来 (universe 表空 / 老库 / universe_at 抛错)
        ds.store_universe_filter = lambda cs, days=None: (list(cs), {}, "价格库点时股票池不可用")
        out, basis = rp.trim_universe_by_store(uni, "2026-09-08")
        check("降级①(点时股票池不可用): 一只没裁, 口径必须标回 raw_spot 而不是 store_universe",
              len(out) == len(uni) and basis == "raw_spot")
        # ---- 降级② bars 统计 SQL 失败 (库被锁 / 库文件坏)
        ds.store_universe_filter = lambda cs, days=None: (
            list(cs), {}, "价格库 bar 统计失败: database is locked")
        out, basis = rp.trim_universe_by_store(uni, "2026-09-08")
        check("降级②(bar 统计失败): 一只没裁, 口径标回 raw_spot",
              len(out) == len(uni) and basis == "raw_spot")

        # ---- 真的裁: 这时候口径才配叫 store_universe, 且名单要落盘
        cut = {"absent": [{"code": c, "n_bars": 0, "last_bar": None, "status": "D"}
                          for c in codes[:177]],
               "too_new": [{"code": c, "n_bars": 25, "last_bar": "2026-09-07", "status": "L"}
                           for c in codes[177:210]]}
        gone = {x["code"] for v in cut.values() for x in v}
        ds.store_universe_filter = lambda cs, days=None: (
            [c for c in cs if c not in gone], cut, None)
        out, basis = rp.trim_universe_by_store(uni, "2026-09-08")
        check("真的裁: 5180 -> 4970, 口径 store_universe",
              len(out) == 4970 and basis == "store_universe")
        p = os.path.join(tmp, "pool_cut", "2026-09-08.json")
        rec = _json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}
        check("裁掉的名单按原因落到 data/pool_cut/<run_date>.json (data/ 不进 git)",
              rec.get("n_pool_raw") == 5180 and rec.get("n_kept") == 4970
              and rec.get("n_dropped") == 210
              and sorted(rec.get("dropped") or {}) == ["absent", "too_new"])

        # ---- 过滤器真的跑完了、只是没什么可裁 -> 仍然是新口径
        ds.store_universe_filter = lambda cs, days=None: (list(cs), {}, None)
        out, basis = rp.trim_universe_by_store(uni, "2026-09-08")
        check("无可裁但过滤器跑完了: 口径 store_universe (这才是'没什么可裁')",
              len(out) == len(uni) and basis == "store_universe")

        # ---- 安全阀: 裁后太少 = 库/尺子出了问题, 不裁, 且口径标回老口径
        ds.store_universe_filter = lambda cs, days=None: (
            list(cs)[:100],
            {"absent": [{"code": c, "n_bars": 0, "last_bar": None, "status": None}
                        for c in codes[100:]]}, None)
        out, basis = rp.trim_universe_by_store(uni, "2026-09-08")
        check("安全阀: 裁后只剩 100/5180 -> 本轮不裁, 口径 raw_spot",
              len(out) == len(uni) and basis == "raw_spot")

        # ---- 回滚: 关掉之后必须有日志说"被谁关的" (只写 WARNING 的兜底 = 静默失败)
        import logging as _lg

        class _Cap(_lg.Handler):
            def __init__(self):
                super().__init__()
                self.msgs = []

            def emit(self, r):
                self.msgs.append(r.getMessage())

        cap = _Cap()
        rp.log.addHandler(cap)
        lvl = rp.log.level
        rp.log.setLevel(_lg.INFO)          # 根 logger 默认 WARNING, 不放行这条 INFO
        try:
            CONFIG["tech"]["pool_by_store"] = False
            CONFIG["tech"]["pool_by_store_off_by"] = "环境变量 ASHARE_POOL_BY_STORE=0"
            out, basis = rp.trim_universe_by_store(uni, "2026-09-08")
            check("回滚: 关掉后原样放行 + 口径 raw_spot",
                  len(out) == len(uni) and basis == "raw_spot")
            check("回滚不静默: 日志写明被谁关的 (值班的人能确认真的关上了)",
                  any("已关闭" in m and "ASHARE_POOL_BY_STORE=0" in m for m in cap.msgs))
        finally:
            rp.log.removeHandler(cap)
            rp.log.setLevel(lvl)
    finally:
        (ds.bars_from_store_on, ds.store_universe_filter, ds.store_pool_meta,
         rp.DATA_DIR, CONFIG["tech"]["pool_by_store"],
         CONFIG["tech"]["pool_by_store_off_by"]) = saved
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- 服务器上按得下去的两条路 (都不经过 git, run_a.sh 的 git reset --hard 抹不掉)
    saved_env = os.environ.get("ASHARE_POOL_BY_STORE")
    saved_data_dir = cfgmod.DATA_DIR
    d2 = tempfile.mkdtemp(prefix="poolsw_")
    try:
        cfgmod.DATA_DIR = d2
        os.environ.pop("ASHARE_POOL_BY_STORE", None)
        check("默认: 没有环境变量也没有停机文件 -> 开, off_by 为空",
              cfgmod._pool_by_store_switch(True) == (True, ""))
        os.environ["ASHARE_POOL_BY_STORE"] = "0"
        on, why = cfgmod._pool_by_store_switch(True)
        check("回滚①: 环境变量 ASHARE_POOL_BY_STORE=0 关掉 (systemctl edit stock-a 那条路)",
              on is False and "ASHARE_POOL_BY_STORE=0" in why)
        os.environ.pop("ASHARE_POOL_BY_STORE", None)
        open(os.path.join(d2, "pool_by_store.off"), "w").close()
        on, why = cfgmod._pool_by_store_switch(True)
        check("回滚②: 停机文件 data/pool_by_store.off 关掉 (stock 用户不需要 root)",
              on is False and "pool_by_store.off" in why)
        os.environ["ASHARE_POOL_BY_STORE"] = "1"
        check("优先级: 环境变量压过停机文件 (=1 时照开)",
              cfgmod._pool_by_store_switch(True) == (True, ""))
    finally:
        cfgmod.DATA_DIR = saved_data_dir
        if saved_env is None:
            os.environ.pop("ASHARE_POOL_BY_STORE", None)
        else:
            os.environ["ASHARE_POOL_BY_STORE"] = saved_env
        shutil.rmtree(d2, ignore_errors=True)


def test_zz_no_check_failures():
    """pytest 只看有没有抛异常, 而 check() 是打印不是断言 —— 没有这一条, 上面任何一条
    ✗ FAIL 在 `pytest -q` 里都会被算成绿。放在最后一个 (pytest 按文件顺序跑)。"""
    assert FAIL == 0, f"{FAIL} 条 check 未通过 (逐条见上面的 ✗ FAIL 行)"


TESTS = [test_schema_and_v1_upgrade, test_qfq_math, test_load_adjust_modes,
         test_load_v1_fallback, test_universe_at, test_universe_null_list_date,
         test_update_daily_by_date, test_update_daily_falls_back,
         test_update_daily_v2_refuses_legacy, test_update_daily_not_ready_guard,
         test_market_units_and_filters, test_source_switch_default,
         test_stage_a_reads_store, test_store_universe_filter,
         test_pool_trim_basis_and_rollback]


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:                  # noqa: BLE001
            pass
    for t in TESTS:
        t()
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)
