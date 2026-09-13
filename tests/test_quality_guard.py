#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
优质榜 空榜/缺期不发布 + 业绩报表逐期落盘回退 离线自测 (不联网; 2026-09-09 卡 QL-EMPTY)。

事故: 09-09 14:00 东财 stock_yjbb_em 限频, 20 个报告期里 10 期抓取失败 (含 2022-2025 四个年报),
每只票的单季差分与年度同比都算不出 → 全市场 11350 / 入池 0 的空榜被 rsync 发布, 盖掉了 09-08 的榜。
离线重放 PC 13:30 那份 20 期全到的报表 (repro 见 CHRONICLE): 全到 154 只入池; 只缺最新一期 145 (旧榜
冒充新榜); 只缺一个年报 53; 服务器那 10 期 → 0。所以守卫要盯的是**覆盖**, 不只是"池空不空"。

覆盖:
  · quality._coverage_problem: 最新期缺失 / 任一期失败 / 到齐 < N-1 → 不发 (返回写明期号的理由);
    新报告期源站答空 (季末后头两周) 允许少一期; 回退到更早一天的期算到齐。
  · quality.build_quality (全打桩): 真榜写三个文件 + meta.periods_*; 缺最新期 / 入池 0 → 返回 None、
    三个文件一个不写、error 行含期号与入池数; 不发布时看板摆回上一版真榜 (三处候选取最新非空,
    当前文件已是最新则一字不写; 空榜文件不算候选)。
  · datasource.fetch_profit_reports_ex (假东财 + 临时目录): 逐期落盘; 同日复用不再打端点; 老期按 TTL;
    抓取失败 → 回退同期旧文件 / data/cache 旧整批缓存 (并种进逐期库); 两处都没有才判缺失;
    源站答空但盘上有数 → 按失败回退 (数据不会消失); 新期答空 → empty 不算失败。
  · datasource.call_with_retry 的 _retries/_backoff 只给自己用、不透传给 fn; 硬期限超时仍不重试。
  · 季初「最新期源站无数据」(2026-09-13 回修 HIGH): 真 akshare 对没有数据的报告期不返回空表, 抛 TypeError
    ('NoneType' object is not subscriptable, 09-13 实测 20260930/20261231); 三条同时成立才记 empty —— 答复形态是无数据
    (_yjbb_no_data_answer) + 期末 45 天内 + 盘上/旧整批都没有该期; 只放行最新一期。真限频的两种形态 (09-09 服务器的
    150s 硬期限 TimeoutError / 源站吐 HTML 的 requests JSONDecodeError) 照旧 failed → 拒发。全链路: 假东财 → 真
    fetch_profit_reports_ex → 真 build_quality, 10-01 形态出 19/20 的榜。
  · 抛错日 (2026-09-13 回修 MEDIUM): build_quality 中途抛错 (09-04/05/07 那种 NaN 型 TypeError 或任何异常) → 看板先摆回
    最新非空榜 (不是 reset 后的 08-28 HEAD 版), 三个产物一个不写, 异常照旧上抛; 沿用旧榜自己失败不盖住原异常; 只剩
    HEAD 版可沿用时 warning 点名。

变异 (去掉守卫必红, 09-09 实测): build_quality 里去掉 `if problem: return _refuse(...)` → test_build_refuses_*
红; 去掉 `if not rows:` → test_build_refuses_when_pool_empty 红; fetch 里去掉回退分支 → test_fetch_falls_back_* 红;
_coverage_problem 恒返 None → 六条覆盖用例红。
变异 (09-13 回修实测): _yjbb_no_data_answer 恒返 False (去掉识别) → 季初两条 (test_fetch_new_quarter_* /
test_build_new_quarter_end_to_end_*) 红; 去掉判据 ② (年龄) → test_fetch_no_data_answer_after_45_days_* 红; 去掉判据 ③
(盘上/旧缓存) → test_fetch_no_data_answer_with_*_falls_back 红; build_quality 不套 try/except → test_build_exception_*
红; _coverage_problem 去掉年龄核 → test_cov_latest_empty_tolerated_only_within_45_days 红。
运行:  python -m pytest tests/test_quality_guard.py -q
"""
from __future__ import annotations
import datetime as dt
import json
import logging
import os
import pickle
import sys
import time

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ashare import datasource as ds        # noqa: E402
from ashare import quality as q            # noqa: E402
from ashare.config import CONFIG           # noqa: E402

TODAY = dt.date.today().isoformat()
PERIODS = ds._report_periods(20)           # 新→旧 'YYYYMMDD'


def _cov(ok=(), fb=(), empty=(), failed=(), expected=PERIODS):
    return {"expected": list(expected), "ok": list(ok), "fallback": list(fb),
            "empty": list(empty), "failed": list(failed), "source": {}}


# ============================================================ _coverage_problem (纯函数)
def test_cov_all_ok_is_fine():
    assert q._coverage_problem(_cov(ok=PERIODS), 20) is None


def test_cov_latest_failed_blocks_and_names_period():
    msg = q._coverage_problem(_cov(ok=PERIODS[1:], failed=PERIODS[:1]), 20)
    assert msg and PERIODS[0] in msg and "最新" in msg


def test_cov_any_failed_blocks_and_names_period():
    gone = PERIODS[2]                           # 一个中间期 (年报)
    msg = q._coverage_problem(_cov(ok=[p for p in PERIODS if p != gone], failed=[gone]), 20)
    assert msg and gone in msg


def test_cov_server_0909_shape_blocks():
    """服务器 16:12 那轮: 10 期失败 (含最新期) → 必不发, 理由点名最新期。"""
    failed = [PERIODS[i] for i in (0, 2, 4, 5, 6, 8, 10, 12, 14)]
    msg = q._coverage_problem(_cov(ok=[p for p in PERIODS if p not in failed], failed=failed), 20)
    assert msg and PERIODS[0] in msg


def _soon_after(p: str, days: int = 10) -> dt.date:
    """报告期期末后 days 天 (季初形态的"今天"); 不跨下一个期末, 所以 _report_periods 在这天算出的列表与 PERIODS 相同。"""
    return dt.date(int(p[:4]), int(p[4:6]), int(p[6:8])) + dt.timedelta(days=days)


def _pin_today(monkeypatch, day: dt.date):
    monkeypatch.setattr(ds, "_today", lambda: day)
    monkeypatch.setattr(q, "_today", lambda: day)


def test_cov_new_quarter_empty_is_allowed_once():
    """季末后头两周: 最新期源站还没有数据 (empty, 不是失败) → 19/20 可以出榜; 空两期就不行。"""
    soon = _soon_after(PERIODS[0])
    assert q._coverage_problem(_cov(ok=PERIODS[1:], empty=PERIODS[:1]), 20, today=soon) is None
    msg = q._coverage_problem(_cov(ok=PERIODS[2:], empty=PERIODS[:2]), 20, today=soon)
    assert msg and PERIODS[1] in msg


def test_cov_fallback_counts_as_covered():
    assert q._coverage_problem(_cov(ok=PERIODS[3:], fb=PERIODS[:3]), 20) is None
    assert q._coverage_problem(_cov(ok=PERIODS[1:], fb=[], failed=PERIODS[:1]), 20) is not None


def test_cov_empty_expected_blocks():
    assert q._coverage_problem(_cov(expected=[]), 20)


# ============================================================ 合成报表 (20 期, 稳定增长)
def _synthetic_reports(n_codes=30, growth=1.2, base=1e8, periods=PERIODS):
    """每年增长 growth 倍、季内逐季递增的累计口径报表 → q4/y4 全正, ROE 20。"""
    reports = {}
    for k in range(n_codes):
        code = f"{600000 + k:06d}"
        plist, ni, rev, roe = [], [], [], []
        for p in sorted(periods):
            y, m = int(p[:4]), p[4:6]
            qi = {"03": 1, "06": 2, "09": 3, "12": 4}[m]
            yr = base * (1 + k * 0.01) * (growth ** (y - 2020))
            cum = sum(yr * (1 + 0.1 * j) for j in range(1, qi + 1))
            plist.append(f"{p[:4]}-{p[4:6]}-{p[6:]}")
            ni.append(cum)
            rev.append(cum * 10)
            roe.append(20.0)
        reports[code] = {"periods": plist, "ni_cum": ni, "rev_cum": rev, "roe_cum": roe}
    return reports


def _spot(codes, pe=15.0, mv=500e8, pb=3.0):
    return pd.DataFrame({"code": list(codes), "name": [f"N{c}" for c in codes],
                         "pe_ttm": [pe] * len(codes), "total_mv": [mv] * len(codes),
                         "pb": [pb] * len(codes), "industry": ["化学制药"] * len(codes)})


def test_score_rows_synthetic_all_pass_and_none_pass():
    reps = _synthetic_reports()
    rows = q._score_rows(reps, {c: r for c, r in _spot(reps).set_index("code", drop=False).to_dict("index").items()}, {})
    assert len(rows) == len(reps)
    assert all(r["gates"]["q4"] and r["gates"]["y4"] and r["gates"]["roe"] and r["gates"]["pe"] and r["gates"]["cap"]
               for r in rows)
    bad = _synthetic_reports(growth=0.8)     # 逐年下滑: q4/y4 全负; 再配高 PE / 小市值 → 一条门都过不了
    spot_bad = {c: r for c, r in _spot(bad, pe=100.0, mv=1e8).set_index("code", drop=False).to_dict("index").items()}
    for rep in bad.values():
        rep["roe_cum"] = [5.0] * len(rep["roe_cum"])
    assert q._score_rows(bad, spot_bad, {}) == []


# ============================================================ build_quality (全打桩)
def _write_js(path, date, picks):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("window.__QL__ = ")
        json.dump({"meta": {"date": date}, "picks": picks}, f, ensure_ascii=False)
        f.write(";\n")


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """输出目录全部指到临时目录; 网络函数全部打桩; 返回一个可改 reports/cov/spot 的盒子。"""
    import ashare.prob20 as prob20
    box = {"reports": _synthetic_reports(), "cov": _cov(ok=PERIODS), "spot": None}
    box["spot"] = _spot(box["reports"])
    dash = tmp_path / "dashboard"
    data = tmp_path / "data"
    dash.mkdir()
    data.mkdir()
    monkeypatch.setattr(q, "DASHBOARD_DIR", str(dash))
    monkeypatch.setattr(q, "QL_JS", str(dash / "quality_data.js"))
    monkeypatch.setattr(q, "QL_JSON", str(data / "quality_result.json"))
    monkeypatch.setattr(q, "QL_LAST_GOOD", str(data / "quality_last_good.js"))
    monkeypatch.setattr(q, "QL_DOCS_JS", str(tmp_path / "docs" / "quality_data.js"))
    monkeypatch.setattr(ds, "fetch_profit_reports_ex", lambda n: (box["reports"], box["cov"]))
    monkeypatch.setattr(ds, "fetch_spot_snapshot", lambda force=False: box["spot"])
    monkeypatch.setattr(ds, "fetch_industry_list", lambda: None)
    monkeypatch.setattr(q, "_rd_intensity", lambda code: None)
    monkeypatch.setattr(q, "_deep_profiles", lambda picks: {})
    monkeypatch.setattr(q, "_drawer_profiles", lambda picks, reports=None: {})
    monkeypatch.setattr(q, "_merge_fundamental_fields", lambda profiles: 0)
    monkeypatch.setattr(prob20, "annotate", lambda *a, **k: None)
    box["dash"], box["data"], box["tmp"] = dash, data, tmp_path
    return box


def _outputs(box):
    return (os.path.exists(q.QL_JS), os.path.exists(q.QL_JSON),
            os.path.exists(os.path.join(str(box["dash"]), "history", f"quality_{TODAY}.json")))


def test_build_publishes_true_board_with_period_meta(sandbox):
    res = q.build_quality()
    assert res is not None and len(res["picks"]) == q.TOP_N
    assert _outputs(sandbox) == (True, True, True)
    m = res["meta"]
    assert m["periods_expected"] == 20 and m["periods_ok"] == PERIODS
    assert m["periods_failed"] == [] and m["periods_fallback"] == [] and m["periods_empty"] == []
    hist = json.load(open(os.path.join(str(sandbox["dash"]), "history", f"quality_{TODAY}.json"), encoding="utf-8"))
    assert hist["date"] == TODAY and len(hist["picks"]) == q.TOP_N
    js = q._parse_ql_js(q.QL_JS)
    assert js["meta"]["periods_ok"] == PERIODS and len(js["picks"]) == q.TOP_N
    assert open(q.QL_LAST_GOOD, encoding="utf-8").read() == open(q.QL_JS, encoding="utf-8").read()


def test_build_publishes_with_fallback_periods_and_records_them(sandbox):
    sandbox["cov"] = _cov(ok=PERIODS[3:], fb=PERIODS[:3])
    res = q.build_quality()
    assert res is not None
    assert res["meta"]["periods_fallback"] == PERIODS[:3] and res["meta"]["periods_ok"] == PERIODS[3:]
    assert _outputs(sandbox) == (True, True, True)


def test_build_refuses_when_latest_period_missing(sandbox, caplog):
    sandbox["cov"] = _cov(ok=PERIODS[1:], failed=PERIODS[:1])
    with caplog.at_level(logging.ERROR, logger="ashare.quality"):
        assert q.build_quality() is None
    assert _outputs(sandbox) == (False, False, False), "不发布时三个文件一个都不能写"
    errs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errs) == 1 and PERIODS[0] in errs[0] and "入池 30" in errs[0] and "不发布" in errs[0]


def test_build_refuses_when_any_period_failed(sandbox, caplog):
    gone = PERIODS[6]
    sandbox["cov"] = _cov(ok=[p for p in PERIODS if p != gone], failed=[gone])
    with caplog.at_level(logging.ERROR, logger="ashare.quality"):
        assert q.build_quality() is None
    assert _outputs(sandbox) == (False, False, False)
    assert any(gone in r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)


def test_build_refuses_when_pool_empty(sandbox, caplog):
    """报告期全到, 但没有一只票过 4 条门槛 (逐年下滑 + 高 PE + 小市值 + 低 ROE) → 入池 0 → 不发。"""
    sandbox["reports"] = _synthetic_reports(growth=0.8)
    for rep in sandbox["reports"].values():
        rep["roe_cum"] = [5.0] * len(rep["roe_cum"])
    sandbox["spot"] = _spot(sandbox["reports"], pe=100.0, mv=1e8)
    with caplog.at_level(logging.ERROR, logger="ashare.quality"):
        assert q.build_quality() is None
    assert _outputs(sandbox) == (False, False, False)
    errs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errs) == 1 and "入池 0" in errs[0]


def test_refusal_carries_newest_nonempty_board_to_dashboard(sandbox):
    """服务器形态: reset 后看板文件是 08-28 的老榜, docs 副本是 PC 的 09-08 榜 → 摆回 09-08 那份。"""
    _write_js(q.QL_JS, "2026-08-28", [{"code": "000001"}])
    _write_js(q.QL_DOCS_JS, "2026-09-08", [{"code": "600519"}])
    sandbox["cov"] = _cov(ok=PERIODS[1:], failed=PERIODS[:1])
    assert q.build_quality() is None
    got = q._parse_ql_js(q.QL_JS)
    assert got["meta"]["date"] == "2026-09-08" and got["picks"][0]["code"] == "600519"
    assert not os.path.exists(q.QL_JSON)


def test_refusal_keeps_dashboard_when_it_is_already_newest(sandbox):
    _write_js(q.QL_JS, "2026-09-08", [{"code": "600519"}])
    _write_js(q.QL_DOCS_JS, "2026-09-01", [{"code": "000001"}])
    _write_js(q.QL_LAST_GOOD, "2026-09-07", [{"code": "000002"}])
    before = open(q.QL_JS, encoding="utf-8").read()
    os.utime(q.QL_JS, (1_600_000_000, 1_600_000_000))
    sandbox["cov"] = _cov(ok=PERIODS[1:], failed=PERIODS[:1])
    assert q.build_quality() is None
    assert open(q.QL_JS, encoding="utf-8").read() == before
    assert os.path.getmtime(q.QL_JS) == 1_600_000_000, "当前文件已是最新非空榜时一字不写"


def test_refusal_ignores_empty_board_candidates(sandbox):
    """09-09 的空榜文件 (picks=[]) 不算候选: 看板上是空榜、docs 里有老一点的真榜 → 摆回真榜。"""
    _write_js(q.QL_JS, "2026-09-09", [])
    _write_js(q.QL_DOCS_JS, "2026-09-08", [{"code": "600519"}])
    sandbox["cov"] = _cov(ok=PERIODS[1:], failed=PERIODS[:1])
    assert q.build_quality() is None
    got = q._parse_ql_js(q.QL_JS)
    assert got["meta"]["date"] == "2026-09-08" and got["picks"]


def test_refusal_without_any_candidate_writes_nothing(sandbox, caplog):
    sandbox["cov"] = _cov(ok=PERIODS[1:], failed=PERIODS[:1])
    with caplog.at_level(logging.WARNING, logger="ashare.quality"):
        assert q.build_quality() is None
    assert not os.path.exists(q.QL_JS)
    assert any("没有可沿用的旧榜" in r.getMessage() for r in caplog.records)


def test_pick_carry_prefers_newest_nonempty_then_order():
    a = ("dashboard", "a", {"meta": {"date": "2026-09-08"}, "picks": [1]})
    b = ("last_good", "b", {"meta": {"date": "2026-09-08"}, "picks": [1]})
    c = ("docs", "c", {"meta": {"date": "2026-09-09"}, "picks": []})
    d = ("docs", "d", None)
    assert q._pick_carry([a, b, c, d])[1] == "dashboard"
    assert q._pick_carry([b, a, c])[1] == "last_good"
    assert q._pick_carry([c, d]) is None


# ============================================================ fetch_profit_reports_ex (假东财 + 临时目录)
def _frame(codes, scale=1.0):
    return pd.DataFrame({"股票代码": list(codes),
                         "净利润-净利润": [1e8 * scale * (i + 1) for i in range(len(codes))],
                         "营业总收入-营业总收入": [1e9 * scale * (i + 1) for i in range(len(codes))],
                         "净资产收益率": [15.0] * len(codes)})


class _FakeAk:
    """stock_yjbb_em 的假源: script[期] = DataFrame | Exception | None(空表); 记录每期被调用几次。"""
    def __init__(self, script=None, default=None):
        self.script = dict(script or {})
        self.default = default
        self.calls = []

    def stock_yjbb_em(self, date=None, **kw):
        assert not kw, f"call_with_retry 的私有关键字不该透传给 fn: {kw}"
        self.calls.append(date)
        v = self.script.get(date, self.default)
        if isinstance(v, Exception):
            raise v
        if v is None:
            return pd.DataFrame()
        return v


@pytest.fixture
def yjbb_box(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "YJBB_PERIOD_DIR", str(tmp_path / "yjbb_periods"))
    monkeypatch.setattr(ds, "_CACHE_DIR", str(tmp_path / "cache"))
    os.makedirs(str(tmp_path / "cache"))
    monkeypatch.setattr(ds, "_LEGACY_BULK_MEMO", {})
    monkeypatch.setattr(ds, "_call_with_deadline", lambda fn, args, kwargs, deadline: fn(*args, **kwargs))
    monkeypatch.setattr(ds.time, "sleep", lambda s: None)
    monkeypatch.setitem(CONFIG["source"], "use_cache", True)
    monkeypatch.setitem(CONFIG["fetch"], "sleep_sec", 0)
    fake = _FakeAk(default=_frame(["000001", "600519"]))
    monkeypatch.setattr(ds, "_ak", lambda: fake)
    return fake


def _seed_period(p, rows, fetched_on):
    ds._yjbb_period_save(p, rows, fetched_on)


def test_fetch_all_ok_saves_every_period_once(yjbb_box):
    res, cov = ds.fetch_profit_reports_ex(20)
    assert cov["ok"] == PERIODS and not cov["failed"] and not cov["fallback"] and not cov["empty"]
    assert yjbb_box.calls == PERIODS
    assert sorted(os.listdir(ds.YJBB_PERIOD_DIR)) == sorted(f"{p}.pkl" for p in PERIODS)
    assert len(res["600519"]["periods"]) == 20 and res["600519"]["periods"] == sorted(res["600519"]["periods"])
    assert q._coverage_problem(cov, 20) is None
    # 同一天第二次 (阶段B 预热 n=12 之后优质榜 n=20 的形态): 一次端点都不打, 覆盖照样全到
    yjbb_box.calls.clear()
    res2, cov2 = ds.fetch_profit_reports_ex(20)
    assert yjbb_box.calls == [] and cov2["ok"] == PERIODS and res2 == res


def test_fetch_n12_then_n20_only_fetches_the_missing_eight(yjbb_box):
    ds.fetch_profit_reports_ex(12)
    assert yjbb_box.calls == PERIODS[:12]
    yjbb_box.calls.clear()
    _, cov = ds.fetch_profit_reports_ex(20)
    assert yjbb_box.calls == PERIODS[12:] and cov["ok"] == PERIODS


def test_fetch_failure_without_disk_is_failed_and_omitted(yjbb_box):
    gone = PERIODS[2]
    yjbb_box.script[gone] = ConnectionError("Response ended prematurely")
    res, cov = ds.fetch_profit_reports_ex(20)
    assert cov["failed"] == [gone] and gone not in cov["ok"]
    assert f"{gone[:4]}-{gone[4:6]}-{gone[6:]}" not in res["600519"]["periods"]
    assert not os.path.exists(ds._yjbb_period_path(gone))
    assert q._coverage_problem(cov, 20) and gone in q._coverage_problem(cov, 20)
    # 失败的期按 YJBB_RETRIES 试过 (走 call_with_retry 的带退避重试, 不是裸循环); 其它期一次
    assert yjbb_box.calls.count(gone) == ds.YJBB_RETRIES and yjbb_box.calls.count(PERIODS[0]) == 1


def test_fetch_falls_back_to_yesterdays_period_file(yjbb_box, caplog):
    gone = PERIODS[0]
    old_rows = {"000001": (5.0, 50.0, 12.0), "600519": (7.0, 70.0, 30.0)}
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    _seed_period(gone, old_rows, yesterday)
    yjbb_box.script[gone] = ConnectionError("boom")
    with caplog.at_level(logging.INFO, logger="ashare.datasource"):
        res, cov = ds.fetch_profit_reports_ex(20)
    assert cov["fallback"] == [gone] and not cov["failed"] and cov["source"][gone] == f"disk {yesterday}"
    pd_date = f"{gone[:4]}-{gone[4:6]}-{gone[6:]}"
    i = res["600519"]["periods"].index(pd_date)
    assert res["600519"]["ni_cum"][i] == 7.0 and res["600519"]["roe_cum"][i] == 30.0
    assert any("回退" in r.getMessage() and gone in r.getMessage() for r in caplog.records)
    assert q._coverage_problem(cov, 20) is None
    # 回退文件不被今天的失败覆盖: fetched_on 仍是昨天, 明天还会再试
    assert ds._yjbb_period_load(gone)["fetched_on"] == yesterday


def test_fetch_falls_back_to_legacy_bulk_cache_and_seeds_period_store(yjbb_box):
    """第一天的底子: 逐期库还是空的, 但 data/cache 里有旧的整批 yjbb_bulk_*.pkl (只在全到时落过盘)。"""
    gone = PERIODS[3]
    pd_date = f"{gone[:4]}-{gone[4:6]}-{gone[6:]}"
    bulk = {"600519": {"periods": [pd_date, "2019-12-31"], "ni_cum": [9.0, 1.0], "rev_cum": [90.0, 10.0],
                       "roe_cum": [33.0, 1.0]},
            "000001": {"periods": ["2019-12-31"], "ni_cum": [1.0], "rev_cum": [10.0], "roe_cum": [1.0]}}
    path = os.path.join(ds._CACHE_DIR, "yjbb_bulk_deadbeef00000000.pkl")
    with open(path, "wb") as f:
        pickle.dump(bulk, f)
    stamp = time.mktime(dt.datetime(2026, 9, 8, 15, 29).timetuple())
    os.utime(path, (stamp, stamp))
    yjbb_box.script[gone] = ConnectionError("boom")
    res, cov = ds.fetch_profit_reports_ex(20)
    assert cov["fallback"] == [gone] and not cov["failed"] and cov["source"][gone] == "legacy-bulk 2026-09-08"
    i = res["600519"]["periods"].index(pd_date)
    assert res["600519"]["ni_cum"][i] == 9.0 and res["600519"]["roe_cum"][i] == 33.0
    assert "000001" in res and pd_date not in res["000001"]["periods"]
    seeded = ds._yjbb_period_load(gone)
    assert seeded and seeded["fetched_on"] == "2026-09-08" and seeded["rows"]["600519"] == (9.0, 90.0, 33.0)


def test_fetch_fresh_tier_refreshes_daily_old_tier_by_ttl(yjbb_box):
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    ten_days = (dt.date.today() - dt.timedelta(days=10)).isoformat()
    forty_days = (dt.date.today() - dt.timedelta(days=40)).isoformat()
    rows = {"600519": (1.0, 10.0, 1.0)}
    _seed_period(PERIODS[0], rows, yesterday)      # 最新期, 昨天的 → 今天要刷新
    _seed_period(PERIODS[1], rows, TODAY)          # 次新期, 今天已抓 → 不刷
    _seed_period(PERIODS[5], rows, ten_days)       # 老期 10 天 → 不刷
    _seed_period(PERIODS[6], rows, forty_days)     # 老期 40 天 → 刷
    _, cov = ds.fetch_profit_reports_ex(20)
    assert PERIODS[0] in yjbb_box.calls and PERIODS[6] in yjbb_box.calls
    assert PERIODS[1] not in yjbb_box.calls and PERIODS[5] not in yjbb_box.calls
    assert cov["ok"] == PERIODS and cov["source"][PERIODS[5]] == f"disk {ten_days}"
    assert ds._yjbb_period_load(PERIODS[6])["fetched_on"] == TODAY


def test_fetch_no_cache_refetches_but_still_falls_back(yjbb_box, monkeypatch):
    monkeypatch.setitem(CONFIG["source"], "use_cache", False)
    _seed_period(PERIODS[4], {"600519": (2.0, 20.0, 2.0)}, TODAY)
    yjbb_box.script[PERIODS[4]] = ConnectionError("boom")
    _, cov = ds.fetch_profit_reports_ex(20)
    assert yjbb_box.calls.count(PERIODS[4]) == ds.YJBB_RETRIES     # 不看盘, 真去抓了
    assert cov["fallback"] == [PERIODS[4]] and not cov["failed"]    # 抓不到照样回退


def test_fetch_source_empty_with_disk_data_is_a_failure_not_a_deletion(yjbb_box, monkeypatch, caplog):
    """数据不会消失 (判据 ③): 季初、盘上有昨天抓到的最新期, 今天源站答空表 (限频/截断) → 按抓取失败回退, 不能把空表当真。"""
    soon = _soon_after(PERIODS[0])
    _pin_today(monkeypatch, soon)
    yesterday = (soon - dt.timedelta(days=1)).isoformat()
    _seed_period(PERIODS[0], {"600519": (3.0, 30.0, 3.0)}, yesterday)
    yjbb_box.script[PERIODS[0]] = None
    with caplog.at_level(logging.WARNING, logger="ashare.datasource"):
        res, cov = ds.fetch_profit_reports_ex(20)
    assert cov["fallback"] == [PERIODS[0]] and not cov["empty"] and not cov["failed"]
    assert cov["source"][PERIODS[0]] == f"disk {yesterday}"
    assert any("源站返回空表" in r.getMessage() and "盘上有该期" in r.getMessage() for r in caplog.records)
    pd_date = f"{PERIODS[0][:4]}-{PERIODS[0][4:6]}-{PERIODS[0][6:]}"
    assert pd_date in res["600519"]["periods"]


def test_fetch_source_empty_without_disk_data_is_empty_not_failed(yjbb_box, monkeypatch):
    _pin_today(monkeypatch, _soon_after(PERIODS[0]))
    yjbb_box.script[PERIODS[0]] = None
    res, cov = ds.fetch_profit_reports_ex(20)
    assert cov["empty"] == [PERIODS[0]] and not cov["failed"] and cov["ok"] == PERIODS[1:]
    assert cov["source"][PERIODS[0]] == "source-empty"
    assert not os.path.exists(ds._yjbb_period_path(PERIODS[0]))
    assert q._coverage_problem(cov, 20) is None            # 新季头两周的正常形态
    assert yjbb_box.calls.count(PERIODS[0]) == 1           # 空表不是异常, 不触发重试


def test_fetch_compat_wrapper_returns_reports_only(yjbb_box):
    res = ds.fetch_profit_reports(20)
    assert isinstance(res, dict) and len(res["600519"]["periods"]) == 20


# ============================================================ call_with_retry 私有关键字
def test_call_with_retry_private_kwargs_not_forwarded_and_retry_count(monkeypatch):
    monkeypatch.setattr(ds, "_call_with_deadline", lambda fn, args, kwargs, deadline: fn(*args, **kwargs))
    monkeypatch.setattr(ds.time, "sleep", lambda s: None)
    monkeypatch.setitem(CONFIG["fetch"], "max_retries", 2)
    seen = []

    def flaky(date=None, **kw):
        seen.append((date, dict(kw)))
        raise ConnectionError("boom")

    with pytest.raises(ConnectionError):
        ds.call_with_retry(flaky, date="20260630", _retries=3, _backoff=0.0)
    assert seen == [("20260630", {})] * 3, "私有关键字不得透传, 总尝试次数按 _retries"
    seen.clear()
    with pytest.raises(ConnectionError):
        ds.call_with_retry(flaky, date="20260630")
    assert len(seen) == 2, "缺省仍按 CONFIG max_retries"
    # 成功一次就返回, 不多试
    seen.clear()
    assert ds.call_with_retry(lambda date=None: date, date="x", _retries=5) == "x"


def test_call_with_retry_timeout_still_not_retried(monkeypatch):
    monkeypatch.setattr(ds, "_call_with_deadline",
                        lambda fn, args, kwargs, deadline: (_ for _ in ()).throw(TimeoutError("硬期限")))
    monkeypatch.setattr(ds.time, "sleep", lambda s: None)
    monkeypatch.setattr(ds, "_em_realtime_down", False)
    monkeypatch.setattr(ds, "_em_hist_down", False)
    n = {"c": 0}

    def fn(**kw):
        n["c"] += 1
    with pytest.raises(TimeoutError):
        ds.call_with_retry(fn, _retries=5)
    assert n["c"] == 0 and ds._em_realtime_down is True


def test_retry_sleeps_only_between_attempts(monkeypatch):
    """最后一次失败后不再空等退避 (以前 max_retries=2 会在第二次失败后白睡 2s)。"""
    monkeypatch.setattr(ds, "_call_with_deadline",
                        lambda fn, args, kwargs, deadline: (_ for _ in ()).throw(ValueError("x")))
    monkeypatch.setitem(CONFIG["fetch"], "sleep_sec", 0)
    slept = []
    monkeypatch.setattr(ds.time, "sleep", lambda s: slept.append(s))
    with pytest.raises(ValueError):
        ds.call_with_retry(lambda: None, _retries=3, _backoff=3.0)
    assert [s for s in slept if s] == [3.0, 6.0]


# ============================================================ 季初「最新期源站无数据」(2026-09-13 回修 HIGH)
REAL_FETCH = ds.fetch_profit_reports_ex        # sandbox 会把它打桩掉, 全链路用例要真的那个
Q_START = dt.date(2026, 10, 1)                 # 09-30 期末刚过, 三季报 ~10/10 才有第一家披露
# 真 akshare 1.18.60 / 服务器 1.18.94 对没有数据的报告期抛的原样 (09-13 本机实测 20260930 / 20261231):
NO_DATA_TYPEERROR = TypeError("'NoneType' object is not subscriptable")
# 09-09 服务器限频日的原样 (20 期缺 10 就是它):
RATE_LIMIT_TIMEOUT = TimeoutError("stock_yjbb_em 超过 150s 硬期限 (工作进程已终止)")


def _json_decode_error():
    """源站限频吐 HTML 页 / 空响应体时 r.json() 抛的: 同时继承 ValueError 与 OSError。"""
    import requests
    return requests.exceptions.JSONDecodeError("Expecting value", "<html>Too many requests</html>", 0)


def _write_legacy_bulk(period, rows, when: dt.datetime, name="yjbb_bulk_cafebabe00000000.pkl"):
    pd_date = f"{period[:4]}-{period[4:6]}-{period[6:]}"
    bulk = {code: {"periods": [pd_date], "ni_cum": [t[0]], "rev_cum": [t[1]], "roe_cum": [t[2]]}
            for code, t in rows.items()}
    path = os.path.join(ds._CACHE_DIR, name)
    with open(path, "wb") as f:
        pickle.dump(bulk, f)
    stamp = time.mktime(when.timetuple())
    os.utime(path, (stamp, stamp))
    return path


def _frames_from_reports(reports):
    """合成报表 → 假东财每期一张表 (与 _frame 同列名), 让真 fetch_profit_reports_ex 走一遍。"""
    by_period: dict = {}
    for code, rep in reports.items():
        for pd_date, ni, rev, roe in zip(rep["periods"], rep["ni_cum"], rep["rev_cum"], rep["roe_cum"]):
            by_period.setdefault(pd_date.replace("-", ""), []).append((code, ni, rev, roe))
    return {p: pd.DataFrame({"股票代码": [r[0] for r in rows], "净利润-净利润": [r[1] for r in rows],
                             "营业总收入-营业总收入": [r[2] for r in rows], "净资产收益率": [r[3] for r in rows]})
            for p, rows in by_period.items()}


def test_no_data_answer_classifier_by_exception_class():
    """判据 ①: 按异常类判 —— 解析答复时的 TypeError/ValueError/LookupError 是"无数据"; OSError 家族 (网络/限频/超时,
    含 requests 的 JSONDecodeError, 它同时是 ValueError) 都不是。"""
    yes = [NO_DATA_TYPEERROR, ValueError("No objects to concatenate"),
           ValueError("Length mismatch: Expected axis has 1 elements, new values have 38 elements"),
           KeyError("result"), IndexError("list index out of range")]
    no = [_json_decode_error(), ConnectionError("Response ended prematurely"), RATE_LIMIT_TIMEOUT,
          OSError("socket"), RuntimeError("pool broken")]
    assert all(ds._yjbb_no_data_answer(e) for e in yes)
    assert not any(ds._yjbb_no_data_answer(e) for e in no)
    assert isinstance(_json_decode_error(), ValueError) and isinstance(_json_decode_error(), OSError)


def test_not_new_empty_period_three_conditions(yjbb_box):
    """判据 ②③ 逐条: 期末 45 天内 (第 45 天含, 第 46 天不含) / 盘上无该期 / 旧整批缓存无该期; 期号解析不了也不算。"""
    p = "20260930"
    assert ds._yjbb_not_new_empty_period(p, None, dt.date(2026, 9, 30)) is None       # 期末当天 (列表里当天就有它)
    assert ds._yjbb_not_new_empty_period(p, None, dt.date(2026, 11, 14)) is None      # 第 45 天
    why = ds._yjbb_not_new_empty_period(p, None, dt.date(2026, 11, 15))               # 第 46 天
    assert why and "46 天" in why
    assert "解析" in ds._yjbb_not_new_empty_period("garbage!", None, Q_START)
    have = {"period": p, "fetched_on": "2026-10-12", "rows": {"600519": (1.0, 2.0, 3.0)}}
    why = ds._yjbb_not_new_empty_period(p, have, dt.date(2026, 10, 13))
    assert why and "盘上有该期" in why and "2026-10-12" in why
    _write_legacy_bulk(p, {"600519": (9.0, 90.0, 33.0)}, dt.datetime(2026, 9, 30, 15, 0))
    why = ds._yjbb_not_new_empty_period(p, None, Q_START)
    assert why and "旧整批" in why


def test_fetch_new_quarter_no_data_answer_is_empty_not_failed(yjbb_box, monkeypatch, caplog):
    """季初 (10-01): 最新期 20260930 源站答 result=null → akshare 抛 TypeError → 三条判据都成立 → 记 empty (不落盘,
    明天再试), 其余 19 期到齐 → 覆盖够出榜; 全程无 warning (这是正常形态)。"""
    _pin_today(monkeypatch, Q_START)
    periods = ds._report_periods(20)
    assert periods[0] == "20260930" and periods[1] == "20260630"
    yjbb_box.script["20260930"] = NO_DATA_TYPEERROR
    with caplog.at_level(logging.INFO, logger="ashare.datasource"):
        res, cov = REAL_FETCH(20)
    assert cov["empty"] == ["20260930"] and cov["failed"] == [] and cov["fallback"] == []
    assert cov["ok"] == periods[1:] and cov["source"]["20260930"] == "source-no-data (TypeError)"
    assert not os.path.exists(ds._yjbb_period_path("20260930"))
    assert len(res["600519"]["periods"]) == 19 and "2026-09-30" not in res["600519"]["periods"]
    assert q._coverage_problem(cov, 20) is None
    assert yjbb_box.calls.count("20260930") == ds.YJBB_RETRIES      # 仍走 call_with_retry 那条链, 不另开旁路
    msgs = caplog.records
    assert any(r.levelno == logging.INFO and "20260930 源站没有数据" in r.getMessage() and "TypeError" in r.getMessage()
               for r in msgs)
    assert any("空 1, 缺失 0 — 空: 20260930" in r.getMessage() for r in msgs)
    assert not any(r.levelno >= logging.WARNING for r in msgs), "季初形态是正常形态, 不该有 warning"


def test_fetch_no_data_answer_after_45_days_is_failed(yjbb_box, monkeypatch):
    """判据 ②: 期末第 46 天源站还答无数据 → 不可能是新报告期, 记 failed → 拒发 (理由点名最新期)。"""
    _pin_today(monkeypatch, dt.date(2026, 11, 15))
    yjbb_box.script["20260930"] = NO_DATA_TYPEERROR
    _, cov = REAL_FETCH(20)
    assert cov["failed"] == ["20260930"] and cov["empty"] == []
    msg = q._coverage_problem(cov, 20)
    assert msg and "20260930" in msg and "最新" in msg


def test_fetch_no_data_answer_with_disk_data_falls_back(yjbb_box, monkeypatch, caplog):
    """判据 ③: 昨天已抓到 20260930 (披露已开始), 今天源站答无数据 → 数据不会消失, 按抓取失败回退到昨天的文件。"""
    _pin_today(monkeypatch, dt.date(2026, 10, 13))
    _seed_period("20260930", {"600519": (7.0, 70.0, 30.0)}, "2026-10-12")
    yjbb_box.script["20260930"] = NO_DATA_TYPEERROR
    with caplog.at_level(logging.WARNING, logger="ashare.datasource"):
        res, cov = REAL_FETCH(20)
    assert cov["fallback"] == ["20260930"] and not cov["empty"] and not cov["failed"]
    assert cov["source"]["20260930"] == "disk 2026-10-12"
    assert any("20260930 源站答复无数据" in r.getMessage() and "盘上有该期" in r.getMessage() for r in caplog.records)
    i = res["600519"]["periods"].index("2026-09-30")
    assert res["600519"]["ni_cum"][i] == 7.0
    assert q._coverage_problem(cov, 20) is None


def test_fetch_no_data_answer_with_legacy_bulk_falls_back(yjbb_box, monkeypatch):
    """判据 ③ 之二: 逐期库没有, 但 data/cache 旧整批缓存里有该期 → 回退到它 (并种进逐期库), 不记 empty。"""
    _pin_today(monkeypatch, Q_START)
    _write_legacy_bulk("20260930", {"600519": (9.0, 90.0, 33.0)}, dt.datetime(2026, 9, 30, 15, 0))
    yjbb_box.script["20260930"] = NO_DATA_TYPEERROR
    res, cov = REAL_FETCH(20)
    assert cov["fallback"] == ["20260930"] and not cov["empty"] and cov["source"]["20260930"] == "legacy-bulk 2026-09-30"
    assert ds._yjbb_period_load("20260930")["rows"]["600519"] == (9.0, 90.0, 33.0)


def test_fetch_rate_limit_forms_stay_failed_in_new_quarter(yjbb_box, monkeypatch):
    """真限频的两种形态在季初也是 failed → 拒发: 09-09 服务器那种 150s 硬期限 TimeoutError (call_with_retry 不重试,
    标记东财不可用), 与源站吐 HTML 页时的 requests JSONDecodeError (是 ValueError 但也是 OSError)。"""
    _pin_today(monkeypatch, Q_START)
    monkeypatch.setattr(ds, "_em_realtime_down", False)
    monkeypatch.setattr(ds, "_em_hist_down", False)
    for exc, n_calls in ((RATE_LIMIT_TIMEOUT, 1), (_json_decode_error(), ds.YJBB_RETRIES)):
        yjbb_box.script["20260930"] = exc
        yjbb_box.calls.clear()
        _, cov = REAL_FETCH(20)
        assert cov["failed"] == ["20260930"] and cov["empty"] == [], type(exc).__name__
        assert yjbb_box.calls.count("20260930") == n_calls, type(exc).__name__
        msg = q._coverage_problem(cov, 20)
        assert msg and "20260930" in msg and "最新" in msg, type(exc).__name__


def test_fetch_source_empty_table_after_45_days_is_failed(yjbb_box, monkeypatch):
    """源站真给空表这条路 (真实 akshare 不走, 留着不依赖实现细节) 同样受判据 ② 约束。"""
    _pin_today(monkeypatch, dt.date(2026, 11, 15))
    yjbb_box.script["20260930"] = None
    _, cov = REAL_FETCH(20)
    assert cov["failed"] == ["20260930"] and not cov["empty"]


def test_cov_latest_empty_tolerated_only_within_45_days(monkeypatch):
    """quality 第二道防线: 最新期 empty 只在期末 45 天内放行 (第 45 天含); 不是最新期的 empty 视同失败; 期号解析不了不放行。"""
    _pin_today(monkeypatch, Q_START)
    exp = ds._report_periods(20)
    cov = _cov(ok=exp[1:], empty=exp[:1], expected=exp)
    assert q._coverage_problem(cov, 20) is None
    assert q._coverage_problem(cov, 20, today=dt.date(2026, 11, 14)) is None
    msg = q._coverage_problem(cov, 20, today=dt.date(2026, 11, 15))
    assert msg and "20260930" in msg and "46 天" in msg
    cov2 = _cov(ok=[p for p in exp if p != exp[3]], empty=[exp[3]], expected=exp)
    msg = q._coverage_problem(cov2, 20)
    assert msg and exp[3] in msg
    cov3 = _cov(ok=exp[1:], empty=["garbage!"], expected=["garbage!"] + exp[1:])
    assert "解析" in q._coverage_problem(cov3, 20)
    assert q.EMPTY_LATEST_MAX_AGE_DAYS == ds.YJBB_EMPTY_MAX_AGE_DAYS == 45


def test_build_new_quarter_end_to_end_publishes_19_of_20(sandbox, yjbb_box, monkeypatch, caplog):
    """季初全链路 (10-01, 假东财 → 真 fetch_profit_reports_ex → 真 build_quality): 最新期 20260930 答 result=null,
    其余 19 期到齐 → 正常出榜, meta.periods_empty == ['20260930'], 三个产物都写, 日志「到齐 19/20 (回退 0, 空 1)」,
    全程无 warning。这就是 09-30 (期末当天起) 到首家披露前服务器 14:00 应有的形态。"""
    _pin_today(monkeypatch, Q_START)
    periods = ds._report_periods(20)
    reports = _synthetic_reports(periods=periods[1:])
    yjbb_box.script = _frames_from_reports(reports)
    yjbb_box.script["20260930"] = NO_DATA_TYPEERROR
    yjbb_box.default = ConnectionError("unexpected period")
    monkeypatch.setattr(ds, "fetch_profit_reports_ex", REAL_FETCH)
    sandbox["spot"] = _spot(reports)
    with caplog.at_level(logging.INFO):
        res = q.build_quality()
    assert res is not None and len(res["picks"]) == q.TOP_N
    m = res["meta"]
    assert m["date"] == "2026-10-01" and m["periods_empty"] == ["20260930"] and m["periods_failed"] == []
    assert m["periods_ok"] == periods[1:] and m["periods_fallback"] == [] and m["periods_expected"] == 20
    assert os.path.exists(q.QL_JS) and os.path.exists(q.QL_JSON)
    assert os.path.exists(os.path.join(str(sandbox["dash"]), "history", "quality_2026-10-01.json"))
    assert q._parse_ql_js(q.QL_JS)["meta"]["periods_empty"] == ["20260930"]
    assert any("报告期 到齐 19/20 (回退 0, 空 1)" in r.getMessage() for r in caplog.records)
    assert not any(r.levelno >= logging.WARNING for r in caplog.records if r.name.startswith("ashare"))


# ============================================================ 抛错日也沿用旧榜 (2026-09-13 回修 MEDIUM)
NAN_TYPEERROR = TypeError("argument of type 'float' is not iterable")      # 09-04/05/07 三天的原样


def _no_history_written(box) -> bool:
    hdir = os.path.join(str(box["dash"]), "history")
    return not os.path.exists(hdir) or not [n for n in os.listdir(hdir) if n.startswith("quality_")]


def test_build_exception_carries_last_good_and_writes_nothing(sandbox, monkeypatch, caplog):
    """服务器形态: reset 后看板是 08-28 的 HEAD 版, docs 副本是 09-11 的真榜; build_quality 中途抛错 (NaN 型) →
    看板摆回 09-11 那份, 三个产物一个不写, 异常照旧上抛 (run_pipeline 只 warning 不阻断), 一行 error 点名异常。"""
    _pin_today(monkeypatch, dt.date(2026, 9, 13))
    _write_js(q.QL_JS, "2026-08-28", [{"code": "000001"}])
    _write_js(q.QL_DOCS_JS, "2026-09-11", [{"code": "600519"}])
    monkeypatch.setattr(q, "_score_rows", lambda *a, **k: (_ for _ in ()).throw(NAN_TYPEERROR))
    with caplog.at_level(logging.INFO, logger="ashare.quality"), pytest.raises(TypeError, match="float"):
        q.build_quality()
    got = q._parse_ql_js(q.QL_JS)
    assert got["meta"]["date"] == "2026-09-11" and got["picks"][0]["code"] == "600519"
    assert not os.path.exists(q.QL_JSON) and _no_history_written(sandbox)
    assert not os.path.exists(q.QL_LAST_GOOD), "抛错日不许把任何东西写成'真榜副本'"
    errs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errs) == 1 and "抛错" in errs[0] and "TypeError" in errs[0] and "float" in errs[0]
    carried = [r for r in caplog.records if "沿用 2026-09-11 的榜" in r.getMessage()]
    assert len(carried) == 1 and carried[0].levelno == logging.INFO


def test_build_exception_keeps_dashboard_when_already_newest(sandbox, monkeypatch):
    """看板上已是最新非空榜: 抛错 (这次抛在快照那一步, 任何异常都算) → 一字不写。"""
    _write_js(q.QL_JS, "2026-09-11", [{"code": "600519"}])
    before = open(q.QL_JS, encoding="utf-8").read()
    os.utime(q.QL_JS, (1_600_000_000, 1_600_000_000))
    monkeypatch.setattr(ds, "fetch_spot_snapshot",
                        lambda force=False: (_ for _ in ()).throw(RuntimeError("spot down")))
    with pytest.raises(RuntimeError, match="spot down"):
        q.build_quality()
    assert open(q.QL_JS, encoding="utf-8").read() == before and os.path.getmtime(q.QL_JS) == 1_600_000_000
    assert not os.path.exists(q.QL_JSON) and _no_history_written(sandbox)


def test_build_exception_without_any_candidate_leaves_no_board(sandbox, monkeypatch, caplog):
    monkeypatch.setattr(q, "_score_rows", lambda *a, **k: (_ for _ in ()).throw(NAN_TYPEERROR))
    with caplog.at_level(logging.WARNING, logger="ashare.quality"), pytest.raises(TypeError):
        q.build_quality()
    assert not os.path.exists(q.QL_JS)
    assert any("没有可沿用的旧榜" in r.getMessage() for r in caplog.records)


def test_build_exception_carry_failure_does_not_mask_original(sandbox, monkeypatch, caplog):
    """沿用旧榜自己也炸了 (磁盘满之类): 记一行 warning, 上抛的仍是原来的 TypeError。"""
    monkeypatch.setattr(q, "_score_rows", lambda *a, **k: (_ for _ in ()).throw(NAN_TYPEERROR))
    monkeypatch.setattr(q, "_carry_last_good", lambda: (_ for _ in ()).throw(OSError("disk full")))
    with caplog.at_level(logging.WARNING, logger="ashare.quality"), pytest.raises(TypeError, match="float"):
        q.build_quality()
    assert any("沿用旧榜也失败" in r.getMessage() and "disk full" in r.getMessage() for r in caplog.records)


def test_carry_warns_when_only_stale_head_board_is_available(sandbox, monkeypatch, caplog):
    """三处只剩 reset 恢复出来的 08-28 HEAD 版 (data/ 与 docs 副本都不在): 照样沿用它, 但 warning 点名"多半是 HEAD 版",
    不能一行 info 装作正常; 正常的上一版榜 (2 天前) 仍是 info。"""
    _pin_today(monkeypatch, dt.date(2026, 9, 13))
    _write_js(q.QL_JS, "2026-08-28", [{"code": "000001"}])
    sandbox["cov"] = _cov(ok=PERIODS[1:], failed=PERIODS[:1])
    with caplog.at_level(logging.INFO, logger="ashare.quality"):
        assert q.build_quality() is None
    warn = [r for r in caplog.records if r.levelno == logging.WARNING and "保留 2026-08-28 的榜" in r.getMessage()]
    assert len(warn) == 1 and "16 天旧" in warn[0].getMessage() and "HEAD 版" in warn[0].getMessage()
    caplog.clear()
    _write_js(q.QL_DOCS_JS, "2026-09-11", [{"code": "600519"}])
    with caplog.at_level(logging.INFO, logger="ashare.quality"):
        assert q.build_quality() is None
    info = [r for r in caplog.records if "沿用 2026-09-11 的榜" in r.getMessage()]
    assert len(info) == 1 and info[0].levelno == logging.INFO
    assert q._parse_ql_js(q.QL_JS)["meta"]["date"] == "2026-09-11"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
