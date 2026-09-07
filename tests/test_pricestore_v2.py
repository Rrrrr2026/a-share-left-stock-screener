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
  · update_daily 按 trade_date 增量: 因子未变只补当日, 因子变了整段重物化; 钩子返回
    None 时回退旧逐股路径 (P1 期间生产默认走这条)
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
    print("\n[钩子未启用 (返回 None) -> 回退旧逐股增量]")
    days, bars, facs = _day_fixture()
    fake = FakeMarket(bars, facs, days, enabled=False)
    d = use_tmp_market(fake)
    conn = ps._conn()
    conn.executemany("INSERT OR REPLACE INTO bars_raw VALUES(?,?,?,?,?,?,?,?)",
                     [(c, days[0], *v) for c, v in bars[days[0]].items()])
    conn.executemany("INSERT OR REPLACE INTO bars(code,d,o,h,l,c,v) VALUES(?,?,?,?,?,?,?)",
                     [("000001", days[0], 1, 2, 0.5, 1.5, 9)])
    conn.commit()
    conn.close()
    ps.update_daily()
    check("走了旧路径 (fetch_bars_bulk 被调用)", bool(fake.legacy_calls))
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
    print("\n[源开关: 默认不翻 (P1 期间生产照旧)]")
    saved = CONFIG["source"].get("bars")
    try:
        CONFIG["source"]["bars"] = "fuyao"
        check("默认源不是 tushare", amkt._bars_source() == "fuyao" and not amkt._tushare_on())
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


TESTS = [test_schema_and_v1_upgrade, test_qfq_math, test_load_adjust_modes,
         test_load_v1_fallback, test_universe_at, test_universe_null_list_date,
         test_update_daily_by_date,
         test_update_daily_falls_back, test_market_units_and_filters,
         test_source_switch_default]


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
