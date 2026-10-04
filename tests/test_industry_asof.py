#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
行业指数末日 = data_date 守卫 (2026-10-04 卡 IND-T1) —— 离线, 零联网
====================================================================
病灶: 行业景气榜的趋势/动量吃行业指数日线的最后一根。同花顺年度日线文件里当日那根北京 ~21 点后才落地, 而跑批在北京
17:30 (服务器) / 19:30 (PC) —— 每个交易日的景气榜、「观察·景气冷」标签、综合分里 20% 的景气分用的都是 **T-1** 的行业
指数, 看板与快照却按 data_date = T 标 (docs/a/history 逐日快照: 09-28 那份 90 个行业的 idx_close 与 09-25 休市重跑
那份逐个相同; 09-30 那份是 09-29 收盘, 10-01 重跑才变成 09-30 收盘)。

这里锁:
  ① 末日 == data_date: 不告警、不联网 (含休市日: data_date 本身停在最后交易日);
  ② 落后 -> Tushare ths_daily 补当日那根, 补完逐位等于「日线自带当日」的结果;
  ③ ths_daily 没出 -> 同花顺行业一览当日涨跌幅补根 (close = 昨收 × (1 + 涨跌幅%)), 三道闸 (只差一根 / 时钟 / 不是同一天);
  ④ 都补不上 -> 照旧按旧一日算, 但 idx_date / meta.industry_asof 留痕 + 两条 WARNING, 看板标「行业数据截至 X」;
  ⑤ 末日比 data_date 还新 -> 截到 data_date;
  ⑥ 取数层 (ths_daily 解析 / 行业一览的涨跌幅列不许取成领涨股的) 、库 (老库补列) 、导出 meta、看板那一小块的契约。
日期全部是**过去的真实交易日** (2026-09-25 中秋休市), 不依赖今天是哪天。
"""
from __future__ import annotations
import datetime as dt
import logging
import math
import os
import sqlite3
import sys
import tempfile

import numpy as np
import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ashare.market as amkt                                     # noqa: E402,F401 (注入 Market)
from ashare import datadate as dd                                # noqa: E402
from ashare import datasource as ds                              # noqa: E402
from ashare import db                                            # noqa: E402
from ashare import export_data as ex                             # noqa: E402
from ashare import module1_industry as m1                        # noqa: E402
from ashare.config import CONFIG                                 # noqa: E402

BJ = dt.timezone(dt.timedelta(hours=8))
CEST = dt.timezone(dt.timedelta(hours=2))
#: 真实日历: 工作日里去掉 2026-09-25 (中秋)。末三个交易日 09-28 / 09-29 / 09-30, 之后国庆休市到 10-07。
CAL = [d for d in pd.bdate_range(end="2026-09-30", periods=181).strftime("%Y-%m-%d") if d != "2026-09-25"]
T, T1, T2 = CAL[-1], CAL[-2], CAL[-3]
assert (T, T1, T2) == ("2026-09-30", "2026-09-29", "2026-09-28")
NAMES = ["半导体", "白酒", "银行", "电池", "化学制药", "通用设备", "港口航运", "饮料制造"]
CODES = {n: "8811%02d" % i for i, n in enumerate(NAMES)}
AFTER_CLOSE = dt.datetime(2026, 9, 30, 17, 30, tzinfo=BJ)        # 服务器 11:30 CEST 跑批 = 北京 17:30


def _hist(seed: int, dates=CAL) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = np.round(1000.0 * (1 + seed % 5) * np.exp(np.cumsum(rng.normal(0.0004 * (seed % 7 - 3), 0.013, len(dates)))), 3)
    return pd.DataFrame({"date": list(dates), "open": np.round(close * 0.998, 3), "high": np.round(close * 1.01, 3),
                         "low": np.round(close * 0.99, 3), "close": close, "amount": np.full(len(dates), 3e8)})


FULL = {n: _hist(11 + i) for i, n in enumerate(NAMES)}           # 日线自带当日 (到 T)


def _lagging(k: int = 1) -> dict:
    """同花顺年度文件还没落地当日那根的形态: 每个行业少最后 k 根。"""
    return {n: h.iloc[:-k].reset_index(drop=True) for n, h in FULL.items()}


def _ts_bars(day: str = T, names=NAMES) -> dict:
    """Tushare ths_daily 那一天的行 (fetch_industry_bars_tushare 的返回形态)。"""
    out = {}
    for n in names:
        h = FULL[n]
        i = int(h.index[h["date"] == day][0])
        out[CODES[n]] = {"date": day, "open": float(h["open"][i]), "high": float(h["high"][i]), "low": float(h["low"][i]),
                         "close": float(h["close"][i]), "pre_close": float(h["close"][i - 1])}
    return out


def _trunc2(x: float) -> float:
    """同花顺行业一览的涨跌幅: 2 位小数, **截断**不是四舍五入 (09-30 实测 0.8152 -> 0.81)。"""
    return math.trunc(x * 100.0) / 100.0


def _summary(day: str = T, names=NAMES, at: dt.datetime = AFTER_CLOSE) -> pd.DataFrame:
    rows = []
    for n in names:
        h = FULL[n]
        i = int(h.index[h["date"] == day][0])
        rows.append({"industry": n, "pct_chg": _trunc2((h["close"][i] / h["close"][i - 1] - 1.0) * 100.0),
                     "net_inflow": 1.0})
    df = pd.DataFrame(rows)
    df.attrs["fetched_at"] = at.timestamp()
    return df


class _Net:
    """把两条兜底的取数口换成假的, 并记下被调了几次 (「不该联网」的用例要断言 0 次)。"""

    def __init__(self, monkeypatch, bars=None, summary=None):
        self.bars, self.summary, self.ts_calls, self.sum_calls = bars or {}, summary, [], 0
        monkeypatch.setattr(ds, "fetch_industry_bars_tushare", self._bars)
        monkeypatch.setattr(ds, "fetch_industry_summary_ths", self._summary)

    def _bars(self, day):
        self.ts_calls.append(str(day)[:10])
        return self.bars.get(str(day)[:10], {})

    def _summary(self, *a, **k):
        self.sum_calls += 1
        return self.summary


def _cal_fn(a, b):
    return [d for d in CAL if a <= d <= b]


def _msgs(caplog, level):
    return [r.getMessage() for r in caplog.records if r.levelno == level and r.name == "ashare.module1"]


@pytest.fixture(autouse=True)
def _log_level(caplog):
    caplog.set_level(logging.DEBUG, logger="ashare.module1")


# ---------------------------------------------------------------- ① 末日 == data_date

def test_equal_is_quiet_and_offline(monkeypatch, caplog):
    net = _Net(monkeypatch)
    out, info = m1.align_industry_tails(dict(FULL), CODES, T, "store", cal=CAL, now=AFTER_CLOSE, trading_days=_cal_fn)
    assert info["asof"] == T and info["n_lag"] == 0 and info["filled"] == {} and info["fill_by"] == {}
    assert all(out[n] is FULL[n] for n in NAMES)                 # 原表原样, 一根没动
    assert net.ts_calls == [] and net.sum_calls == 0
    assert not _msgs(caplog, logging.WARNING)
    assert any("行业指数末日 %s = data_date (%d 个行业, 无需兜底)" % (T, len(NAMES)) in m for m in _msgs(caplog, logging.INFO))


def test_holiday_rerun_does_not_false_alarm(monkeypatch, caplog):
    """休市日重跑 (10-01..10-07): data_date 停在最后交易日 09-30, 行业日线末日也是 09-30 -> 不告警、不联网。
    守卫比的是 data_date, 不是挂钟上的今天 (拿今天比, 国庆七天会连报七天「落后」)。"""
    net = _Net(monkeypatch)
    for holiday in (dt.datetime(2026, 10, 1, 17, 30, tzinfo=BJ), dt.datetime(2026, 10, 5, 17, 30, tzinfo=BJ)):
        caplog.clear()
        out, info = m1.align_industry_tails(dict(FULL), CODES, T, "store", cal=CAL, now=holiday, trading_days=_cal_fn)
        assert info["asof"] == T and info["n_lag"] == 0
        assert not _msgs(caplog, logging.WARNING)
    assert net.ts_calls == [] and net.sum_calls == 0


def test_unknown_expect_skips_guard(monkeypatch, caplog):
    """既没读价格库也没拿到基准指数: 判不了该是哪一天 -> 不守卫 (不拿 run_date 冒充, 它可能是休市日)。"""
    net = _Net(monkeypatch)
    out, info = m1.align_industry_tails(_lagging(), CODES, None, "none")
    assert info["asof"] == T1 and info["n_lag"] == 0 and net.ts_calls == [] and net.sum_calls == 0
    assert not _msgs(caplog, logging.WARNING)
    assert any("判不了" in m for m in _msgs(caplog, logging.INFO))


# ---------------------------------------------------------------- ② 落后 -> Tushare ths_daily

def test_lagging_filled_by_ths_daily_exactly(monkeypatch, caplog):
    net = _Net(monkeypatch, bars={T: _ts_bars()})
    src = _lagging()
    out, info = m1.align_industry_tails(src, CODES, T, "store", cal=CAL, now=AFTER_CLOSE, trading_days=_cal_fn)
    assert info["asof"] == T and info["n_lag"] == 0
    assert info["filled"] == {m1.FILL_TS: len(NAMES)} and set(info["fill_by"].values()) == {m1.FILL_TS}
    assert info["raw_last"] == {T1: len(NAMES)}
    assert net.ts_calls == [T] and net.sum_calls == 0            # 一次调用; ① 补齐了就不碰同花顺
    for n in NAMES:
        assert len(src[n]) == len(CAL) - 1                       # 传进来的表没被改
        got, want = out[n], FULL[n]
        assert list(got["date"]) == list(want["date"])
        assert np.array_equal(got["close"].to_numpy(float), want["close"].to_numpy(float))   # 逐位相同
        assert got["open"].iloc[-1] == want["open"].iloc[-1] and math.isnan(got["amount"].iloc[-1])
    warns = _msgs(caplog, logging.WARNING)
    assert len(warns) == 1 and "行业指数末日落后 data_date %s" % T in warns[0] and T1 in warns[0], warns
    assert any("行业指数末日 %s = data_date (兜底补齐 %d 个行业: Tushare ths_daily %d 个 / 行业一览涨跌幅 0 个)"
               % (T, len(NAMES), len(NAMES)) in m for m in _msgs(caplog, logging.INFO)), _msgs(caplog, logging.INFO)


def test_ths_daily_chains_two_missing_days(monkeypatch):
    """同花顺年度文件连着两天没更新: 两根都从 ths_daily 接, 每根的 pre_close 都得接得上。"""
    net = _Net(monkeypatch, bars={T1: _ts_bars(T1), T: _ts_bars(T)})
    out, info = m1.align_industry_tails(_lagging(2), CODES, T, "store", cal=CAL, now=AFTER_CLOSE, trading_days=_cal_fn)
    assert net.ts_calls == [T1, T] and net.sum_calls == 0
    assert info["asof"] == T and info["filled"] == {m1.FILL_TS: len(NAMES)}
    for n in NAMES:
        assert np.array_equal(out[n]["close"].to_numpy(float), FULL[n]["close"].to_numpy(float))


def test_ths_daily_preclose_mismatch_is_not_appended(monkeypatch, caplog):
    """pre_close 接不上日线末根 (中间还缺别的根 / 不是同一个指数) -> 不接; 行业一览也救不了时留痕落后。"""
    bars = _ts_bars()
    bad = NAMES[2]
    bars[CODES[bad]]["pre_close"] += 5.0
    net = _Net(monkeypatch, bars={T: bars}, summary=None)
    out, info = m1.align_industry_tails(_lagging(), CODES, T, "store", cal=CAL, now=AFTER_CLOSE, trading_days=_cal_fn)
    assert m1._last_date(out[bad]) == T1 and bad not in info["fill_by"]
    assert info["asof"] == T1 and info["n_lag"] == 1 and info["filled"] == {m1.FILL_TS: len(NAMES) - 1}
    assert any("pre_close 接不上 1 个" in m for m in _msgs(caplog, logging.INFO)), _msgs(caplog, logging.INFO)
    assert any("仍落后" in m and "1/%d 个行业停在 %s" % (len(NAMES), T1) in m for m in _msgs(caplog, logging.WARNING))


def test_no_board_code_skips_ths_daily(monkeypatch):
    """行业列表不是同花顺口径 (没有 881xxx 代码): ① 一次都不调, 直接走 ②。"""
    net = _Net(monkeypatch, bars={T: _ts_bars()}, summary=_summary())
    out, info = m1.align_industry_tails(_lagging(), {}, T, "store", cal=CAL, now=AFTER_CLOSE, trading_days=_cal_fn)
    assert net.ts_calls == [] and net.sum_calls == 1
    assert info["filled"] == {m1.FILL_SUMMARY: len(NAMES)} and info["asof"] == T


# ---------------------------------------------------------------- ③ ths_daily 没出 -> 行业一览涨跌幅补根

def test_synth_close_arithmetic():
    assert m1.synth_close(1000.0, 1.23) == 1012.3
    assert m1.synth_close(16576.2, -2.91) == round(16576.2 * (1 - 0.0291), 3)
    assert m1.synth_close(2000.0, 0.0) == 2000.0
    assert m1.synth_close(1234.567, -10.0) == 1111.11            # 3 位小数
    # 09-30 实测形态: 港口航运 09-29 收 1247.781, 一览 0.81 (官方 0.8152, 截断) -> 与官方收盘 1257.953 差 < 0.07 点
    assert abs(m1.synth_close(1247.781, 0.81) - 1257.953) < 0.07


def test_append_bar_keeps_input_and_columns():
    h = FULL[NAMES[0]].iloc[:-1].reset_index(drop=True)
    before = h.copy()
    out = m1._append_bar(h, T, 1234.5)
    assert h.equals(before) and len(out) == len(h) + 1
    assert list(out.columns) == list(h.columns) and out["date"].iloc[-1] == T and out["close"].iloc[-1] == 1234.5
    assert math.isnan(out["open"].iloc[-1]) and math.isnan(out["amount"].iloc[-1])
    assert out["close"].dtype == float


def test_summary_fallback_fills_when_ths_daily_absent(monkeypatch, caplog):
    net = _Net(monkeypatch, bars={}, summary=_summary())
    out, info = m1.align_industry_tails(_lagging(), CODES, T, "store", cal=CAL, now=AFTER_CLOSE, trading_days=_cal_fn)
    assert net.ts_calls == [T] and net.sum_calls == 1
    assert info["asof"] == T and info["n_lag"] == 0 and info["filled"] == {m1.FILL_SUMMARY: len(NAMES)}
    for n in NAMES:
        got, want = out[n], FULL[n]
        assert got["date"].iloc[-1] == T and len(got) == len(want)
        prev = float(want["close"].iloc[-2])
        pct = _trunc2((float(want["close"].iloc[-1]) / prev - 1.0) * 100.0)
        assert got["close"].iloc[-1] == round(prev * (1.0 + pct / 100.0), 3)          # 算术就是 昨收 × (1 + 涨跌幅%)
        assert abs(got["close"].iloc[-1] / want["close"].iloc[-1] - 1.0) < 1.1e-4     # 与官方收盘差 ≤ ~1 bp (截断 0.01%)
        assert math.isnan(got["open"].iloc[-1])                                       # 没有的不编
    assert any("Tushare ths_daily: 补齐 0/%d" % len(NAMES) in m and "当日还没入库或调用失败" in m
               for m in _msgs(caplog, logging.INFO)), _msgs(caplog, logging.INFO)
    assert any("行业一览涨跌幅补根: 补齐 %d/%d" % (len(NAMES), len(NAMES)) in m for m in _msgs(caplog, logging.INFO))
    assert len(_msgs(caplog, logging.WARNING)) == 1              # 只有「落后」那一条, 没有「仍落后」


def test_summary_same_session_as_last_bar_is_refused(monkeypatch, caplog):
    """行业一览还是日线末根那个交易日的 (没翻到 data_date): 涨跌幅与末根自己的逐行业相同 -> 不补 (再乘一遍 = 同一天算两次)。"""
    net = _Net(monkeypatch, bars={}, summary=_summary(day=T1))
    out, info = m1.align_industry_tails(_lagging(), CODES, T, "store", cal=CAL, now=AFTER_CLOSE, trading_days=_cal_fn)
    assert info["asof"] == T1 and info["n_lag"] == len(NAMES) and info["filled"] == {}
    assert all(m1._last_date(out[n]) == T1 for n in NAMES)
    warns = _msgs(caplog, logging.WARNING)
    assert any("%d/%d 个行业相同" % (len(NAMES), len(NAMES)) in w and "没翻到 data_date" in w for w in warns), warns
    assert any("兜底后仍落后" in w and "meta.industry_asof = %s" % T1 in w and "行业数据截至 %s" % T1 in w for w in warns), warns


@pytest.mark.parametrize("now, cal_fn, frag", [
    (dt.datetime(2026, 9, 30, 14, 0, tzinfo=BJ), _cal_fn, "还没收盘"),                       # 盘中: 半天的涨跌幅
    (dt.datetime(2026, 9, 30, 10, 0, tzinfo=BJ), None, "还没收盘"),
    (dt.datetime(2026, 10, 8, 10, 0, tzinfo=BJ),                                             # 复市日盘中: 一览已是 10-08 的
     lambda a, b: [d for d in CAL + ["2026-10-08"] if a <= d <= b], "之后已有交易日 2026-10-08"),
    (dt.datetime(2026, 10, 1, 10, 0, tzinfo=BJ), None, "按工作日近似"),                      # 日历取不到 + 之后有工作日: 从严
])
def test_summary_clock_gate(monkeypatch, caplog, now, cal_fn, frag):
    net = _Net(monkeypatch, bars={}, summary=_summary())
    out, info = m1.align_industry_tails(_lagging(), CODES, T, "store", cal=CAL, now=now,
                                        trading_days=cal_fn if cal_fn is not None else (lambda a, b: None))
    assert info["n_lag"] == len(NAMES) and info["filled"] == {}, info
    assert any("不是 data_date %s 的收盘值" % T in w and frag in w for w in _msgs(caplog, logging.WARNING)), _msgs(caplog, logging.WARNING)


def test_summary_ok_on_holiday_evening_with_calendar(monkeypatch):
    """10-01 (休市) 晚上才发现 09-30 那根没落地: 日历说 09-30 之后没开过市 -> 一览仍是 09-30 的收盘值, 可以补。"""
    net = _Net(monkeypatch, bars={}, summary=_summary())
    out, info = m1.align_industry_tails(_lagging(), CODES, T, "store", cal=CAL,
                                        now=dt.datetime(2026, 10, 1, 17, 30, tzinfo=BJ), trading_days=_cal_fn)
    assert info["asof"] == T and info["filled"] == {m1.FILL_SUMMARY: len(NAMES)}


def test_summary_uses_fetch_time_when_now_not_injected(monkeypatch):
    """生产路径: 时钟闸看的是行业一览的**取数时刻** (attrs.fetched_at), 不是别的。"""
    intraday = _summary(at=dt.datetime(2026, 9, 30, 13, 0, tzinfo=BJ))
    net = _Net(monkeypatch, bars={}, summary=intraday)
    out, info = m1.align_industry_tails(_lagging(), CODES, T, "store", cal=CAL, trading_days=_cal_fn)
    assert info["filled"] == {} and info["n_lag"] == len(NAMES)
    net2 = _Net(monkeypatch, bars={}, summary=_summary(at=AFTER_CLOSE))
    out, info = m1.align_industry_tails(_lagging(), CODES, T, "store", cal=CAL, trading_days=_cal_fn)
    assert info["filled"] == {m1.FILL_SUMMARY: len(NAMES)}


def test_summary_cannot_fill_two_missing_days(monkeypatch, caplog):
    """差两根: 单日涨跌幅补不了 (ths_daily 也没出) -> 留痕落后。没有交易日历可核时同样不补。"""
    net = _Net(monkeypatch, bars={}, summary=_summary())
    out, info = m1.align_industry_tails(_lagging(2), CODES, T, "store", cal=CAL, now=AFTER_CLOSE, trading_days=_cal_fn)
    assert info["asof"] == T2 and info["n_lag"] == len(NAMES) and net.sum_calls == 0
    assert any("恰好只差 data_date 一根" in m for m in _msgs(caplog, logging.INFO))
    net = _Net(monkeypatch, bars={}, summary=_summary())
    out, info = m1.align_industry_tails(_lagging(), CODES, T, "store", cal=None, now=AFTER_CLOSE, trading_days=_cal_fn)
    assert info["n_lag"] == len(NAMES) and net.sum_calls == 0


def test_summary_garbage_pct_is_skipped(monkeypatch):
    s = _summary()
    s.loc[s["industry"] == NAMES[0], "pct_chg"] = 250.0          # 脏数
    s.loc[s["industry"] == NAMES[1], "pct_chg"] = float("nan")
    net = _Net(monkeypatch, bars={}, summary=s)
    out, info = m1.align_industry_tails(_lagging(), CODES, T, "store", cal=CAL, now=AFTER_CLOSE, trading_days=_cal_fn)
    assert info["n_lag"] == 2 and info["filled"] == {m1.FILL_SUMMARY: len(NAMES) - 2} and info["asof"] == T1
    assert m1._last_date(out[NAMES[0]]) == T1 and m1._last_date(out[NAMES[1]]) == T1


# ---------------------------------------------------------------- ④ 都补不上 -> 留痕

def test_both_fallbacks_fail_leaves_trace(monkeypatch, caplog):
    net = _Net(monkeypatch, bars={}, summary=None)
    src = _lagging()
    out, info = m1.align_industry_tails(src, CODES, T, "store", cal=CAL, now=AFTER_CLOSE, trading_days=_cal_fn)
    assert info["asof"] == T1 and info["n_lag"] == len(NAMES) and info["filled"] == {} and info["fill_by"] == {}
    assert all(out[n] is src[n] for n in NAMES)                  # 照旧用旧一日的, 一根不编
    warns = _msgs(caplog, logging.WARNING)
    assert any("行业指数末日落后 data_date %s" % T in w for w in warns)
    assert any("同花顺行业一览取不到" in w for w in warns)
    assert any("兜底后仍落后 data_date %s: %d/%d 个行业停在 %s" % (T, len(NAMES), len(NAMES), T1) in w
               and "meta.industry_asof = %s" % T1 in w for w in warns), warns


# ---------------------------------------------------------------- ⑤ 末日比 data_date 还新

def test_ahead_is_truncated_to_data_date(monkeypatch, caplog):
    """价格库旧了 (data_date = 09-29) 而行业日线已到 09-30: 截到 09-29 —— 快照标的是 09-29 的行情, 不许带之后的行业数据。"""
    net = _Net(monkeypatch)
    out, info = m1.align_industry_tails(dict(FULL), CODES, T1, "store", cal=CAL, now=AFTER_CLOSE, trading_days=_cal_fn)
    assert info["truncated"] == len(NAMES) and info["asof"] == T1 and info["n_lag"] == 0
    for n in NAMES:
        assert m1._last_date(out[n]) == T1 and len(out[n]) == len(CAL) - 1 and len(FULL[n]) == len(CAL)
    assert any("晚于 data_date %s" % T1 in w and "截到 data_date" in w for w in _msgs(caplog, logging.WARNING))
    assert net.ts_calls == [] and net.sum_calls == 0


# ---------------------------------------------------------------- datadate: peek / session_settled

def _bench(last=T):
    days = [d for d in CAL if d <= last]
    return pd.DataFrame({"date": days, "close": [4000.0 + i for i in range(len(days))]})


def test_peek_data_date_same_definition_as_resolve(monkeypatch):
    monkeypatch.setattr(ds, "bars_from_store_on", lambda: True)
    monkeypatch.setattr(ds, "_store_max_date", lambda: T)
    assert dd.peek_data_date(_bench(T1)) == (T, "store")          # 库末日优先 (基准落后也以库为准)
    assert dd.peek_data_date(_bench(T1))[0] == dd.resolve(T, T1, "2026-10-04")[0]
    monkeypatch.setattr(ds, "bars_from_store_on", lambda: False)
    assert dd.peek_data_date(_bench(T1)) == (T1, "bench")
    assert dd.peek_data_date(None) == (None, "none")              # 不拿 run_date 冒充
    assert dd.peek_data_date(pd.DataFrame({"date": [], "close": []}), "2026-10-04") == (None, "none")


def test_session_settled_pure():
    f = dd.session_settled
    assert f(T, dt.datetime(2026, 9, 30, 15, 0, tzinfo=BJ), _cal_fn)[0] is True
    assert f(T, dt.datetime(2026, 9, 30, 11, 30, tzinfo=CEST), _cal_fn)[0] is True       # 11:30 CEST = 北京 17:30
    assert f(T, dt.datetime(2026, 9, 30, 14, 59, tzinfo=BJ), _cal_fn)[0] is False
    assert f(T, dt.datetime(2026, 9, 29, 20, 0, tzinfo=BJ), _cal_fn)[0] is False         # 时钟早于 data_date
    assert f(T, dt.datetime(2026, 10, 4, 12, 0, tzinfo=BJ), _cal_fn)[0] is True          # 长假: 日历说之后没开过市
    cal8 = lambda a, b: [d for d in CAL + ["2026-10-08"] if a <= d <= b]                  # noqa: E731
    assert f(T, dt.datetime(2026, 10, 7, 23, 0, tzinfo=BJ), cal8)[0] is True
    assert f(T, dt.datetime(2026, 10, 8, 8, 0, tzinfo=BJ), cal8)[0] is False             # 复市日: 开盘前也从严
    ok, why = f(T2, dt.datetime(2026, 9, 29, 16, 0, tzinfo=BJ), _cal_fn)
    assert ok is False and "2026-09-29" in why
    # 日历取不到 / 抛错 / 结果里没有 data_date 自己 -> 工作日近似, 从严
    def boom(a, b):
        raise RuntimeError("trade_cal 500")
    assert f("2026-09-18", dt.datetime(2026, 9, 20, 12, 0, tzinfo=BJ), boom)[0] is True  # 周五之后只有周末
    assert f("2026-09-18", dt.datetime(2026, 9, 21, 9, 0, tzinfo=BJ), None)[0] is False  # 周一
    assert f(T, dt.datetime(2026, 10, 1, 12, 0, tzinfo=BJ), lambda a, b: [])[0] is False
    assert f(None, AFTER_CLOSE, _cal_fn)[0] is False


# ---------------------------------------------------------------- 端到端: compute_industry_scores

def _stub_pipeline(monkeypatch, hists, bench_last=T, with_codes=True):
    lst = pd.DataFrame({"industry": NAMES, "board_code": [CODES[n] if with_codes else "BK%04d" % i
                                                          for i, n in enumerate(NAMES)]})
    monkeypatch.setattr(ds, "fetch_industry_list", lambda: lst)
    monkeypatch.setattr(ds, "fetch_industry_hist", lambda name: hists.get(name))
    monkeypatch.setattr(ds, "fetch_industry_cons", lambda name: None)          # 服务器形态: 东财成分不可达, 广度支柱为空
    monkeypatch.setattr(ds, "fetch_benchmark_close", lambda: _bench(bench_last))
    monkeypatch.setattr(ds, "fetch_industry_fund_flow", lambda: pd.DataFrame(
        {"industry": NAMES, "net_inflow": np.linspace(5e8, -5e8, len(NAMES))}))
    monkeypatch.setattr(ds, "bars_from_store_on", lambda: False)               # data_date = 基准末日 (干净导出树本来就没库)
    monkeypatch.setitem(CONFIG["fetch"], "max_workers", 2)


SCORE_COLS = ["industry", "prosperity_score", "trend", "momentum", "capital", "idx_close", "ma120", "eligible", "selected"]


def _by_name(df):
    return df.sort_values("industry").reset_index(drop=True)


def test_scores_after_ths_daily_fill_equal_native_same_day(monkeypatch):
    """T-1 日线 + ths_daily 当日那根 算出来的景气榜, 与「日线自带当日」逐字段相同; 且**不等于** T-1 口径的旧榜。"""
    _stub_pipeline(monkeypatch, FULL)
    _Net(monkeypatch)
    native = m1.compute_industry_scores()
    assert set(native["idx_date"]) == {T} and native["idx_fill"].isna().all()
    assert native.attrs["industry_asof"]["asof"] == T and native.attrs["industry_asof"]["filled"] == {}

    _stub_pipeline(monkeypatch, _lagging())
    net = _Net(monkeypatch, bars={T: _ts_bars()})
    fixed = m1.compute_industry_scores()
    assert net.ts_calls == [T]
    assert set(fixed["idx_date"]) == {T} and set(fixed["idx_fill"]) == {m1.FILL_TS}
    pd.testing.assert_frame_equal(_by_name(fixed)[SCORE_COLS], _by_name(native)[SCORE_COLS])
    a = fixed.attrs["industry_asof"]
    assert a["expect"] == T and a["basis"] == "bench" and a["asof"] == T and a["n_lag"] == 0 and "fill_by" not in a

    # 旧行为 (两条兜底都不可用): 榜是 T-1 口径的 —— idx_close 是 T-1 收盘, 且逐行业留痕
    _stub_pipeline(monkeypatch, _lagging())
    _Net(monkeypatch, bars={}, summary=None)
    stale = m1.compute_industry_scores()
    assert set(stale["idx_date"]) == {T1} and stale["idx_fill"].isna().all()
    assert stale.attrs["industry_asof"]["asof"] == T1 and stale.attrs["industry_asof"]["n_lag"] == len(NAMES)
    s, f = _by_name(stale), _by_name(native)
    assert list(s["idx_close"]) == [float(FULL[n]["close"].iloc[-2]) for n in s["industry"]]
    assert list(f["idx_close"]) == [float(FULL[n]["close"].iloc[-1]) for n in f["industry"]]
    assert not np.allclose(s["idx_close"], f["idx_close"])


def test_scores_after_summary_fill_track_native(monkeypatch):
    """行业一览涨跌幅补根: idx_close 与官方收盘差 ≤ ~1 bp, 逐行业标 ths_summary。"""
    _stub_pipeline(monkeypatch, FULL)
    _Net(monkeypatch)
    native = _by_name(m1.compute_industry_scores())
    _stub_pipeline(monkeypatch, _lagging(), with_codes=False)                  # 没有同花顺代码: ① 跳过
    net = _Net(monkeypatch, summary=_summary(at=AFTER_CLOSE))
    monkeypatch.setattr(m1, "_market_calendar", lambda: _cal_fn)
    real = m1.align_industry_tails
    monkeypatch.setattr(m1, "align_industry_tails",
                        lambda h, c, e, b, cal=None: real(h, c, e, b, cal=cal, now=AFTER_CLOSE))
    fixed = _by_name(m1.compute_industry_scores())
    assert net.ts_calls == [] and net.sum_calls == 1
    assert set(fixed["idx_date"]) == {T} and set(fixed["idx_fill"]) == {m1.FILL_SUMMARY}
    assert np.allclose(fixed["idx_close"], native["idx_close"], rtol=1.1e-4, atol=0)
    assert list(fixed["industry"]) == list(native["industry"])


def test_compute_passes_bench_calendar_and_board_codes(monkeypatch):
    """接线: 交易日历 = 基准指数日期 ∪ {data_date}; 代码表只收 6 位数字 (东财 BKxxxx 不算)。"""
    assert m1._board_codes(pd.DataFrame({"industry": ["a", "b", "c"], "board_code": ["881121", "BK0420", "88112"]})) == {"a": "881121"}
    assert m1._board_codes(pd.DataFrame({"industry": ["a"]})) == {} and m1._board_codes(None) == {}
    def never(a, b):
        raise AssertionError("基准已到 data_date, 不该问开市日历")
    cal = m1._bench_calendar(_bench(T), T, trading_days=never)
    assert cal[-2:] == [T1, T] and "2026-09-25" not in cal and len(cal) == 60
    assert m1._bench_calendar(_bench(T), T1, trading_days=never)[-1] == T1       # 基准比 data_date 新: 只留到 data_date
    assert m1._bench_calendar(None, T) is None and m1._bench_calendar(pd.DataFrame({"date": [], "close": []}), T) is None
    # 基准末日 < data_date: 中间这一段问开市日历补齐; 问不到 / 结果里没有 data_date -> None (不知道), 不许只塞一个 data_date 进去
    assert m1._bench_calendar(_bench(T1), T, trading_days=_cal_fn)[-2:] == [T1, T]
    assert m1._bench_calendar(_bench(T2), T, trading_days=_cal_fn)[-3:] == [T2, T1, T]
    assert m1._bench_calendar(_bench(T2), T, trading_days=lambda a, b: None) is None

    def boom(a, b):
        raise RuntimeError("trade_cal 500")
    assert m1._bench_calendar(_bench(T2), T, trading_days=boom) is None
    assert m1._bench_calendar(_bench(T2), T, trading_days=lambda a, b: [T2, T1]) is None
    assert m1._cal_between(cal, T2, T) == [T1, T] and m1._cal_between(cal, T, T) == [] and m1._cal_between(None, T2, T) is None
    seen = {}
    _stub_pipeline(monkeypatch, _lagging())
    _Net(monkeypatch, bars={T: _ts_bars()})
    real = m1.align_industry_tails

    def spy(hists, codes, expect, basis, cal=None, **kw):
        seen.update(codes=codes, expect=expect, basis=basis, cal=cal)
        return real(hists, codes, expect, basis, cal=cal, **kw)
    monkeypatch.setattr(m1, "align_industry_tails", spy)
    m1.compute_industry_scores()
    assert seen["codes"] == CODES and seen["expect"] == T and seen["basis"] == "bench"
    assert callable(seen["cal"]) and seen["cal"]()[-2:] == [T1, T]             # 惰性: 真落后了才求值


def test_calendar_is_lazy_when_nothing_lags(monkeypatch):
    net = _Net(monkeypatch)

    def never():
        raise AssertionError("没有行业落后, 不该去数「差几根」")
    out, info = m1.align_industry_tails(dict(FULL), CODES, T, "store", cal=never)
    assert info["n_lag"] == 0 and info["asof"] == T


def test_bench_two_days_behind_never_lets_summary_multiply_across_a_gap(monkeypatch, caplog):
    """指数腿落后两天 (基准末日 = T-2) + 行业日线也停在 T-2 + ths_daily 不可用: 「只差一根」那道闸必须数出差两根。
    日历问不到 -> 不知道 -> 不补; 问得到 -> 差 [T-1, T] 两根 -> 不补。绝不许把 T 的单日涨跌幅乘到 T-2 的收盘上。"""
    for cal_fn in (lambda a, b: None, _cal_fn):
        net = _Net(monkeypatch, bars={}, summary=_summary())
        out, info = m1.align_industry_tails(
            _lagging(2), CODES, T, "store", now=AFTER_CLOSE, trading_days=_cal_fn,
            cal=lambda: m1._bench_calendar(_bench(T2), T, trading_days=cal_fn))
        assert net.sum_calls == 0 and info["filled"] == {} and info["n_lag"] == len(NAMES) and info["asof"] == T2
        assert all(m1._last_date(out[n]) == T2 for n in NAMES)


def test_guard_failure_never_breaks_the_run(monkeypatch, caplog):
    """守卫自己出错 (任何异常): 模块1 照出榜, 按原始日线算, idx_date 照记, 一条 WARNING。"""
    _stub_pipeline(monkeypatch, _lagging())
    _Net(monkeypatch, bars={T: _ts_bars()})

    def boom(*a, **k):
        raise KeyError("date")
    monkeypatch.setattr(m1, "align_industry_tails", boom)
    df = m1.compute_industry_scores()
    assert len(df) == len(NAMES) and set(df["idx_date"]) == {T1} and df["idx_fill"].isna().all()
    assert df["prosperity_score"].between(0, 100).all()
    assert any("行业指数末日守卫出错" in w for w in _msgs(caplog, logging.WARNING))
    assert df.attrs["industry_asof"]["error"]


# ---------------------------------------------------------------- 取数层

class _FakeAk:
    def __init__(self, frame):
        self.frame, self.calls = frame, 0

    def stock_board_industry_summary_ths(self):
        self.calls += 1
        return self.frame


def _raw_summary():
    """akshare stock_board_industry_summary_ths 的真实列 (2026-10-04 实测 12 列): 「涨跌幅」与「领涨股-涨跌幅」并存。"""
    return pd.DataFrame({
        "序号": [1, 2, 3], "板块": ["生物制品", "医疗服务", "白酒"], "涨跌幅": [4.63, 3.11, 2.82],
        "总成交量": [878.68, 1427.02, 175.21], "总成交额": [236.62, 376.82, 107.47], "净流入": [14.36, 20.08, 16.07],
        "上涨家数": [53, 50, 19], "下跌家数": [2, 6, 0], "均价": [26.93, 26.41, 61.34],
        "领涨股": ["康希诺", "南模生物", "金徽酒"], "领涨股-最新价": [102.64, 64.98, 17.40], "领涨股-涨跌幅": [20.00, 19.87, 9.99]})


@pytest.fixture
def _summary_env(monkeypatch):
    ak = _FakeAk(_raw_summary())
    monkeypatch.setattr(ds, "_ak", lambda: ak)
    monkeypatch.setattr(ds, "call_with_retry", lambda fn, *a, **k: fn(*a, **k))
    monkeypatch.setattr(ds, "_ths_summary_memo", {})
    return ak


def test_summary_takes_industry_pct_not_leader_pct(_summary_env):
    t0 = dt.datetime.now().timestamp()
    df = ds.fetch_industry_summary_ths()
    assert list(df["industry"]) == ["生物制品", "医疗服务", "白酒"]
    assert list(df["pct_chg"]) == [4.63, 3.11, 2.82]             # 不是领涨股的 20.00 / 19.87 / 9.99
    assert list(df["net_inflow"]) == [14.36, 20.08, 16.07]
    assert t0 - 5 <= df.attrs["fetched_at"] <= dt.datetime.now().timestamp() + 5


def test_summary_is_fetched_once_per_process_window(_summary_env):
    a = ds.fetch_industry_summary_ths()
    flow = ds._fund_flow_ths()                                   # 资金支柱与补根共用同一次调用
    b = ds.fetch_industry_summary_ths()
    assert _summary_env.calls == 1 and a is b and list(flow["net_inflow"]) == [14.36, 20.08, 16.07]
    ds.fetch_industry_summary_ths(max_age_sec=-1)                # 过期 -> 重取
    assert _summary_env.calls == 2


def test_summary_failure_returns_none(monkeypatch):
    def boom():
        raise RuntimeError("ths 403")
    monkeypatch.setattr(ds, "_ak", lambda: type("A", (), {"stock_board_industry_summary_ths": staticmethod(boom)})())
    monkeypatch.setattr(ds, "call_with_retry", lambda fn, *a, **k: fn(*a, **k))
    monkeypatch.setattr(ds, "_ths_summary_memo", {})
    assert ds.fetch_industry_summary_ths() is None and ds._fund_flow_ths() is None


def _ts_frame():
    return pd.DataFrame({
        "ts_code": ["881121.TI", "881273.TI", "700331.TI", "881155.TI", "881156.TI", "000300.SH"],
        "trade_date": ["20260930", "20260930", "20260930", "20260929", "20260930", "20260930"],
        "open": [16500.0, 1910.0, 1070.7609, 1.0, None, 4500.0], "high": [16600.0, 1960.0, 1083.0037, 1.0, None, 4520.0],
        "low": [16000.0, 1900.0, 1070.7609, 1.0, None, 4480.0], "close": [16092.609, 1957.999, 1083.0037, 1.0, None, 4500.0],
        "pre_close": [16576.2, 1904.228, 1066.5374, 1.0, None, 4490.0], "pct_change": [-2.9174, 2.8238, 1.5439, 0.0, None, 0.2]})


def test_fetch_industry_bars_tushare_parses_and_filters(monkeypatch, caplog):
    asked = []

    def q(api, **kw):
        asked.append((api, kw))
        return _ts_frame()
    monkeypatch.setattr(ds, "_ts_query", q)
    monkeypatch.setattr(ds, "_ts_available", lambda: True)
    out = ds.fetch_industry_bars_tushare("2026-09-30")
    assert asked[0][0] == "ths_daily" and asked[0][1]["trade_date"] == "20260930"
    assert set(asked[0][1]["fields"].split(",")) >= {"ts_code", "trade_date", "close", "pre_close"}
    # 只留 .TI、只认问的那一天、close 为空的不要
    assert set(out) == {"881121", "881273", "700331"}
    assert out["881121"] == {"date": "2026-09-30", "open": 16500.0, "high": 16600.0, "low": 16000.0,
                             "close": 16092.609, "pre_close": 16576.2}

    def boom(api, **kw):
        raise RuntimeError("HTTP 502")
    monkeypatch.setattr(ds, "_ts_query", boom)
    caplog.set_level(logging.DEBUG, logger="ashare.datasource")
    assert ds.fetch_industry_bars_tushare("20260930") == {}
    assert any("ths_daily 20260930 失败" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    monkeypatch.setattr(ds, "_ts_query", lambda api, **kw: pd.DataFrame(columns=["ts_code", "trade_date", "close"]))
    assert ds.fetch_industry_bars_tushare("20260930") == {}      # 当日还没入库
    monkeypatch.setattr(ds, "_ts_available", lambda: False)
    monkeypatch.setattr(ds, "_ts_query", boom)                   # 没 token: 根本不调
    assert ds.fetch_industry_bars_tushare("20260930") == {}


# ---------------------------------------------------------------- 库 + 导出 meta

@pytest.fixture
def _tmp_db(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="indt1_")
    monkeypatch.setattr(db, "DB_PATH", os.path.join(tmp, "ashare.db"))
    monkeypatch.setattr(ex, "HISTORY_DIR", os.path.join(tmp, "history"))
    monkeypatch.setattr(ds, "fetch_benchmark_close", lambda: None)
    from ashare import earnings_cal
    monkeypatch.setattr(earnings_cal, "fetch_appoint_map", lambda: {})
    monkeypatch.setitem(CONFIG["source"], "use_cache", False)
    return tmp


def _score_df(idx_date, idx_fill=None):
    return pd.DataFrame({"industry": NAMES, "prosperity_score": np.linspace(90, 10, len(NAMES)),
                         "trend": 50.0, "momentum": 50.0, "breadth": np.nan, "capital": 50.0, "fundamental": np.nan,
                         "idx_close": 1000.0, "ma120": 900.0, "above_ma120": True, "eligible": True, "selected": True,
                         "idx_date": idx_date, "idx_fill": idx_fill})


def test_old_db_gets_columns_and_rows_persist(_tmp_db):
    """老库 (industry_score 没有 idx_date / idx_fill 两列) init_db 后补列; 老行为 NULL, 新行照存。"""
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute("CREATE TABLE industry_score(run_date TEXT, industry TEXT, prosperity_score REAL, trend REAL, momentum REAL, "
                 "breadth REAL, capital REAL, fundamental REAL, idx_close REAL, ma120 REAL, above_ma120 INTEGER, "
                 "eligible INTEGER, selected INTEGER, PRIMARY KEY(run_date, industry))")
    conn.execute("INSERT INTO industry_score(run_date, industry, prosperity_score) VALUES('2026-09-29', '银行', 55.0)")
    conn.commit()
    conn.close()
    db.init_db()
    assert {"idx_date", "idx_fill"} <= set(db._cols("industry_score"))
    fills = [m1.FILL_TS] * 3 + [None] * (len(NAMES) - 3)
    db.save_industry_scores(T, _score_df(T, fills))
    rows = {r["industry"]: r for r in db.fetch_table("industry_score", T)}
    assert all(r["idx_date"] == T for r in rows.values())
    assert [rows[n]["idx_fill"] for n in NAMES] == fills
    old = db.fetch_table("industry_score", "2026-09-29")[0]
    assert old["idx_date"] is None and old["idx_fill"] is None and old["prosperity_score"] == 55.0
    db.save_industry_scores("2026-09-28", _score_df(float("nan"), float("nan")))     # NaN 不许存成字符串 'nan'
    assert all(r["idx_date"] is None and r["idx_fill"] is None for r in db.fetch_table("industry_score", "2026-09-28"))


def test_industry_asof_meta_pure():
    f = ex.industry_asof_meta
    rows = [{"industry": n, "idx_date": T, "idx_fill": None} for n in NAMES]
    assert f(rows, T) == {"industry_asof": T, "industry_asof_n_lag": 0, "industry_fill": {}}
    rows[0]["idx_date"], rows[1]["idx_fill"], rows[2]["idx_fill"] = T1, m1.FILL_TS, m1.FILL_SUMMARY
    assert f(rows, T) == {"industry_asof": T1, "industry_asof_n_lag": 1,
                          "industry_fill": {m1.FILL_TS: 1, m1.FILL_SUMMARY: 1}}
    assert f([{"industry": "银行", "idx_date": T1}], T)["industry_asof_n_lag"] == 1
    assert f([{"industry": "银行", "idx_date": T}], T)["industry_asof_n_lag"] == 0       # 相等不算落后
    assert f([{"industry": "银行", "idx_date": None}], T) == {"industry_asof": None}     # 老快照 / 演示数据: 没记
    assert f([], T) == {"industry_asof": None} and f(None, T) == {"industry_asof": None}


@pytest.mark.parametrize("idx_date, n_lag", [(T, 0), (T1, len(NAMES))])
def test_payload_and_snapshot_meta_carry_industry_asof(_tmp_db, idx_date, n_lag):
    import json
    db.init_db()
    db.save_industry_scores(T, _score_df(idx_date, m1.FILL_TS if idx_date == T else None))
    db.log_run(T, "%s 11:31:04" % T, "%s 12:56:37" % T, 4881, 0, ["银行"], "ok", data_date=T,
               n_pool_raw=4906, scan_basis="store_universe")
    meta = ex.build_payload(T)["meta"]
    assert meta["data_date"] == T and meta["industry_asof"] == idx_date and meta["industry_asof_n_lag"] == n_lag
    assert meta["industry_fill"] == ({m1.FILL_TS: len(NAMES)} if idx_date == T else {})
    snap = json.load(open(ex.write_history_snapshot(T), encoding="utf-8"))
    assert snap["meta"]["industry_asof"] == idx_date and snap["meta"]["industry_asof_n_lag"] == n_lag
    assert {r["idx_date"] for r in snap["industries"]} == {idx_date}


def test_payload_of_old_run_has_null_industry_asof(_tmp_db):
    db.init_db()
    old = _score_df(None, None).drop(columns=["idx_date", "idx_fill"])
    db.save_industry_scores(T1, old)
    db.log_run(T1, "%s 11:30:55" % T1, "%s 12:42:19" % T1, 4900, 0, ["银行"], "ok", data_date=T1)
    meta = ex.build_payload(T1)["meta"]
    assert "industry_asof" in meta and meta["industry_asof"] is None and "industry_asof_n_lag" not in meta


# ---------------------------------------------------------------- 看板那一小块的契约

def test_dashboard_title_block_contract():
    src = open(os.path.join(ROOT, "dashboard", "index.html"), encoding="utf-8").read()
    assert src.count('id="indAsof"') == 1
    i = src.index('data-i18n="sec_board"')
    assert 'id="indAsof" class="hidden' in src[i:i + 200]        # 紧挨着景气榜标题, 默认隐藏
    assert src.count("function renderIndAsof(){") == 1
    body = src[src.index("function renderIndAsof(){"):src.index("function renderIndustryChart(){")]
    assert "m.industry_asof" in body and "m.data_date||m.run_date" in body and "String(a)<String(dd)" in body
    assert "industry_asof_n_lag" in body and 't("ind_asof")' in body and 't("ind_asof_part")' in body
    j = src.index("function renderIndustryChart(){")
    assert "renderIndAsof();" in src[j:j + 80]                   # 每次换数据 / 换语言 / 换主题都会重画
    assert src.count('ind_asof:"') == 2 and src.count('ind_asof_part:"') == 2          # 中英各一条
    assert 'ind_asof:"行业数据截至 "' in src and 'ind_asof:"Industry data as of "' in src
