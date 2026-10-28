#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
单次调用备用站重试 `tushare_client.query(..., failover_once=True)` (2026-10-27 卡 FAILOVER-1) 离线自测:
不联网 (打桩 requests.Session; tests/conftest.py 另掐 DNS), 不真等 (退避 sleep 打桩), 不出现任何真 token。

背景: 改名表月刷 (leftside_core.pricestore._refresh_namechange_if_due -> ashare.market.fetch_namechange_rows -> query('namechange'))
一次 update 只打**一次** Tushare 调用; 全局切换机制只在主站**连续** 3 次传输层失败 / 6 次硬期限超时 / token 失效时才切 —— 单次调用的阶段永远
凑不齐: 10-26 / 10-27 服务器上 kaixin8 回 JSON code=503 [upstream_pool_exhausted] (答了 JSON = 传输层正常, 计数反而清零), PC 上 3 次 120s
硬期限超时 (< 6), 三天都没碰过已配好的备用站 datahubco (journal「namechange 月刷: 拉取失败 … code=503 [upstream_pool_exhausted]」)。

覆盖:
  · 主站 JSON 503 [upstream_pool_exhausted] / [minute_upstream_busy] 一类瞬时业务错误码 → 备用成功 (INFO 一行「tushare 单次调用备用站重试」, 全局状态不变)
  · 主站硬期限超时 (< 6 次) → 备用成功 (超时计数由原规则记 3 次, 重试那轮不加)
  · 没配备用键 (或备用 = 主站) → 与以前一样: 错误原样抛, 只打当前站, 不打任何重试行; retries=1 恰一次 HTTP
  · 两站都败 → 抛**原错误类型** (TushareTransport), 消息带两站各自原因, __cause__ 是另一站的异常; WARNING 一行; 全局状态不变 (备用上那轮不计 backup_fails)
    —— 备用无权限 (15000 档没这个接口) 也一样按原类型抛
  · 2002 / 40101 → 不走单次重试 (全局机制自己立即切, 行为与以前一样); 429 / 参数错 / 分页截断 → 不触发
    (无权限自 2026-10-28 卡 FAILOVER-2 起**也换站**: 权限按站不同 —— 主站能服务 namechange 而备用 datahubco 回 HTTP 403; 见 tests/test_http403_nopermission.py)
  · 没开 opt-in 的成批调用 → 行为逐字节不变
  · 重试那一站也按本站单页上限分页, 续页钉在**同一站** (offset=5000 的第二页仍打备用)
  · 已切到备用时在备用失败 → 去主站重来一次, 不翻转全局状态 (active 仍 True), 不触发回切
  · 全局计数不双记: 主站 2 次传输层失败 (retries=2) 由原规则记 fails=2, 重试那轮不加也不清零, 下一次普通调用再败 1 次就按原规则切换
  · 本次调用已经由全局切换在两站各来过一轮 → 不再重来 (第三轮无意义), WARNING 一行, 按原错误抛
  · 钩子 ashare.market.fetch_namechange_rows 真走这条路: 主站 503 → 备用行 → 六元组 (北交所剔掉), journal 行带 namechange / 两站 host / 行数

运行:  python -X utf8 -m pytest -c ../stock-core/pytest.ini --rootdir . tests/test_single_call_failover.py -q
"""
from __future__ import annotations

import inspect
import logging
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ashare import tushare_client as tc        # noqa: E402

TOKEN = "TEST-TOKEN-NOT-A-REAL-ONE"
PRIMARY = "https://primary.invalid/tushare/sdk"
BACKUP = "https://backup.invalid/api/tushare"
FIELDS = "ts_code,name,start_date,end_date,ann_date,change_reason"
COLS = FIELDS.split(",")
ROWS = [["000929.SZ", "*ST兰黄", "20250430", None, "20250429", "*ST"],
        ["600439.SH", "ST瑞贝卡", "20260901", "", "20260831", "ST"]]
_FO_INITIAL = tc._FO_INITIAL
_REAL_SLEEP = tc.time.sleep


class FakeResp:
    def __init__(self, payload, status=200, text=""):
        self._payload = payload
        self.status_code = status
        self.text = text or str(payload)[:200]

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    """按剧本依次返回; 记录每次请求 (url / 请求体 / 超时)。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.headers = {}

    def post(self, url, json=None, timeout=None):      # noqa: A002
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        assert self.script, "假 session 的剧本已用尽 (调用次数比预期多)"
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _ok(fields, items):
    return FakeResp({"code": 0, "msg": "", "data": {"fields": fields, "items": items}})


def _err(msg, code=40203):
    return FakeResp({"code": code, "msg": msg, "data": None})


def _http(status):
    return FakeResp({}, status=status)


def _pool_exhausted():
    return _err("[upstream_pool_exhausted] upstream pool exhausted, try later", 503)


@pytest.fixture(autouse=True)
def _clean():
    """每条用例前后: 存 / 还原客户端的模块级状态 (凭据 / session / 执行器开关 / 限频桶 / 单页上限 / sleep 桩), 切换状态与进程池开关复位。"""
    saved = (tc._CRED, tc._SESSION, tc.USE_PROCESS_DEADLINE, tc._BUCKET, tc._PAGE_LIMIT)
    tc.failover_reset()
    tc._pool_fallback_reset()
    yield
    tc._CRED, tc._SESSION, tc.USE_PROCESS_DEADLINE, tc._BUCKET, tc._PAGE_LIMIT = saved
    tc.failover_reset()
    tc._pool_fallback_reset()
    tc.time.sleep = _REAL_SLEEP


def _install(script, backup=BACKUP):
    """假 session + 三元组凭据 (含备用) + 切换状态复位 + 退避 sleep 打桩 -> (session, naps)。进程池关掉 (子进程里 mock 不生效)。"""
    tc.time.sleep = _REAL_SLEEP
    tc._SESSION = FakeSession(script)
    tc._CRED = (TOKEN, PRIMARY, backup)
    tc.USE_PROCESS_DEADLINE = False
    tc._BUCKET = tc.TokenBucket(100000)
    tc._PAGE_LIMIT = tc.PAGE_LIMIT_DEFAULT
    tc._pool_fallback_reset()
    tc.failover_reset()
    naps = []
    tc.time.sleep = lambda s: naps.append(s)
    return tc._SESSION, naps


def _session(script):
    """只换剧本, 不碰凭据与切换状态。"""
    tc._SESSION = FakeSession(script)
    return tc._SESSION


def _urls(s):
    return [c["url"] for c in s.calls]


def _fo1_lines(caplog):
    return [r for r in caplog.records if "单次调用备用站重试" in r.getMessage()]


def _switch_lines(caplog):
    return [r for r in caplog.records if "基址切换" in r.getMessage()]


def _return_lines(caplog):
    return [r for r in caplog.records if "基址回切" in r.getMessage()]


def _is_probe(call):
    j = call["json"]
    return (j["api_name"] == "trade_cal" and j["params"]["start_date"] == j["params"]["end_date"]
            and call["timeout"] == tc.PROBE_TIMEOUT_SEC)


def _on_backup():
    """把全局状态摆成「已切到备用」(不到 20 次 / 10 分钟的试探点)。"""
    with tc._FO_LOCK:
        tc._FO.update(active=True, reason="test", switched_at=time.time(), last_probe_at=time.monotonic())


# ---------------------------------------------------------------------------------------------- 主站瞬时业务错误码 / 硬期限 → 备用
@pytest.mark.parametrize("bad", [
    _pool_exhausted(),
    _err("[minute_upstream_busy] upstream busy this minute", 503),
    _err("[gateway_error] 502 bad gateway from upstream", 502),
], ids=["upstream_pool_exhausted", "minute_upstream_busy", "5xx_generic"])
def test_primary_transient_json_error_then_backup_ok(bad, caplog):
    """主站答了 JSON 但 code 是 5xx 类瞬时错 (全局机制把它当「传输层正常」清零, 永远不切): 当前站 retries 跑完 → 立即在备用整体重来一次 → 成功。
    一行 INFO (GM grep「单次调用备用站重试」), 只写 host 不写完整地址, 不写 token; 全局状态与初始态逐键相同。"""
    s, naps = _install([bad] * 3 + [_ok(COLS, ROWS)])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df = tc.query("namechange", fields=FIELDS, failover_once=True)
    assert len(df) == 2 and list(df.columns) == COLS
    assert _urls(s) == [PRIMARY] * 3 + [BACKUP]
    assert naps == [tc.BACKOFF_SEC, tc.BACKOFF_SEC * 2]            # 主站自己的退避; 换站重来不退避
    assert s.calls[3]["json"] == s.calls[0]["json"]               # 同一调用 (api / fields / params / 同一把 key) 原样在备用重来
    assert tc.failover_state() == _FO_INITIAL                       # 不计数、不切换、不试探
    lines = _fo1_lines(caplog)
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    msg = lines[0].getMessage()
    assert msg.startswith("tushare 单次调用备用站重试: namechange 主站 primary.invalid 失败 (code=")
    assert " → 备用 backup.invalid 成功 2 行 (" in msg
    assert "/tushare/sdk" not in msg and "/api/tushare" not in msg and TOKEN not in caplog.text
    assert not _switch_lines(caplog) and not _return_lines(caplog)


def test_upstream_pool_exhausted_log_line_format(caplog):
    """钉住 journal 行的格式 (GM 按它 grep): 原因 = 原异常文本去掉「namechange: 」前缀。"""
    s, _ = _install([_pool_exhausted()] * 3 + [_ok(COLS, ROWS)])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        tc.query("namechange", fields=FIELDS, failover_once=True)
    msg = _fo1_lines(caplog)[0].getMessage()
    assert msg.startswith("tushare 单次调用备用站重试: namechange 主站 primary.invalid 失败 "
                          "(code=503 [upstream_pool_exhausted] upstream pool exhausted, try later) → 备用 backup.invalid 成功 2 行 (")
    assert msg.endswith("s)") and "本次调用共" in msg


def test_primary_hard_deadline_timeout_then_backup_ok(monkeypatch, caplog):
    """主站进程级硬期限超时 (PC 10-26 / 10-27 形态: 3 次 120s, 不到全局阈值 6): retries 跑完 → 备用成功。
    超时计数由原规则记 3 次 (timeouts=3, 不切), 重试那轮一次都不加。"""
    real_call = tc._deadline_call

    def timeout_on_primary(fn, args, kwargs, dl):
        if args[0] == PRIMARY:
            raise TimeoutError("_post_json 超过 42s 硬期限 (工作进程已终止)")
        return real_call(fn, args, kwargs, dl)
    monkeypatch.setattr(tc, "_deadline_call", timeout_on_primary)
    s, naps = _install([_ok(COLS, ROWS)])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df = tc.query("namechange", fields=FIELDS, failover_once=True, deadline_sec=42)
    assert len(df) == 2 and _urls(s) == [BACKUP]                    # 主站 3 次都在执行器层超时, 假 session 只见到备用那一次
    assert naps == [tc.BACKOFF_SEC, tc.BACKOFF_SEC * 2]
    assert tc.failover_state() == {**_FO_INITIAL, "timeouts": 3}
    lines = _fo1_lines(caplog)
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    msg = lines[0].getMessage()
    assert "namechange 主站 primary.invalid 失败 (超过 42s 硬期限" in msg and "→ 备用 backup.invalid 成功 2 行" in msg
    assert not _switch_lines(caplog)


def test_primary_transport_failures_below_threshold_then_backup_ok_counters_not_double_counted(caplog):
    """主站传输层失败 (HTTP 502) 但 retries=2 不到全局阈值 3: 重试去备用成功; 原规则记的 fails=2 **原样保留** (重试那轮不加、不清零、不翻转);
    下一次普通调用主站再败 1 次 = 第 3 次 → 仍由原规则切换 (原规则照旧管全局状态)。"""
    s, naps = _install([_http(502), _http(502), _ok(COLS, ROWS)])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df = tc.query("namechange", fields=FIELDS, failover_once=True, retries=2)
    assert len(df) == 2 and _urls(s) == [PRIMARY, PRIMARY, BACKUP] and naps == [tc.BACKOFF_SEC]
    assert tc.failover_state() == {**_FO_INITIAL, "fails": 2}
    assert len(_fo1_lines(caplog)) == 1 and "失败 (HTTP 502) → 备用 backup.invalid 成功 2 行" in _fo1_lines(caplog)[0].getMessage()
    s2 = _session([_http(502), _ok(["a"], [[1]])])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df2 = tc.query("daily", retries=1)                           # 没开 opt-in 的普通调用
    assert len(df2) == 1 and _urls(s2) == [PRIMARY, BACKUP]
    st = tc.failover_state()
    assert st["active"] is True and "连续 3 次传输层失败" in st["reason"] and len(_switch_lines(caplog)) == 1
    assert len(_fo1_lines(caplog)) == 1                             # 普通调用没有再打重试行


# ---------------------------------------------------------------------------------------------- 没配备用: 一切照旧
@pytest.mark.parametrize("backup", ["", PRIMARY], ids=["no_key", "backup_equals_primary"])
def test_without_backup_behaves_exactly_as_before(backup, caplog):
    s, naps = _install([_pool_exhausted()] * 3, backup=backup)
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        with pytest.raises(tc.TushareTransport) as ei:
            tc.query("namechange", fields=FIELDS, failover_once=True)
    assert str(ei.value) == "namechange: code=503 [upstream_pool_exhausted] upstream pool exhausted, try later"
    assert type(ei.value) is tc.TushareTransport and ei.value.__cause__ is None
    assert _urls(s) == [PRIMARY] * 3 and naps == [tc.BACKOFF_SEC, tc.BACKOFF_SEC * 2]
    assert not _fo1_lines(caplog) and tc.failover_state() == _FO_INITIAL
    s, naps = _install([_http(503)], backup=backup)
    with pytest.raises(tc.TushareTransport):
        tc.query("namechange", fields=FIELDS, failover_once=True, retries=1)
    assert len(s.calls) == 1 and naps == []                          # 恰一次 HTTP, 没有第二站


# ---------------------------------------------------------------------------------------------- 两站都败
def test_both_sites_fail_raises_original_kind_with_both_hosts_and_state_unchanged(caplog):
    s, naps = _install([_pool_exhausted()] * 3 + [_http(502)] * 3)
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        with pytest.raises(tc.TushareTransport) as ei:
            tc.query("namechange", fields=FIELDS, failover_once=True)
    e = ei.value
    assert type(e) is tc.TushareTransport and e.api == "namechange"
    msg = str(e)
    assert msg.startswith("namechange: 主站 primary.invalid 与 备用 backup.invalid 都失败 — 主站: code=503 [upstream_pool_exhausted]")
    assert "; 备用: TushareTransport HTTP 502" in msg
    assert isinstance(e.__cause__, tc.TushareTransport) and "HTTP 502" in str(e.__cause__)
    assert _urls(s) == [PRIMARY] * 3 + [BACKUP] * 3
    assert naps == [tc.BACKOFF_SEC, tc.BACKOFF_SEC * 2] * 2         # 两站各自退避, 换站瞬间不退避
    assert tc.failover_state() == _FO_INITIAL                        # 备用上那轮不计 backup_fails (role None), 主站 JSON 应答 fails 本就 0
    lines = _fo1_lines(caplog)
    assert len(lines) == 1 and lines[0].levelno == logging.WARNING
    w = lines[0].getMessage()
    assert w.startswith("tushare 单次调用备用站重试: namechange 主站 primary.invalid 失败 (code=503 [upstream_pool_exhausted]")
    assert "→ 备用 backup.invalid 也失败 (TushareTransport: HTTP 502); 两站都失败, 按原错误类型 TushareTransport 抛出 (共 " in w
    assert TOKEN not in caplog.text and TOKEN not in msg and "/api/tushare" not in w


def test_backup_no_permission_still_raises_original_kind(caplog):
    """备用 15000 档没这个接口 (NoPermission, 一次就抛): 仍按**原错误类型** TushareTransport 抛, 消息里带备用的 NoPermission 原因。"""
    s, _ = _install([_pool_exhausted()] * 3 + [_err("抱歉，您没有接口访问权限，请确认积分", 40203)])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        with pytest.raises(tc.TushareTransport) as ei:
            tc.query("namechange", fields=FIELDS, failover_once=True)
    e = ei.value
    assert type(e) is tc.TushareTransport and "备用: TushareNoPermission 无权限/积分不足" in str(e)
    assert isinstance(e.__cause__, tc.TushareNoPermission)
    assert _urls(s) == [PRIMARY] * 3 + [BACKUP]
    assert tc.failover_state() == _FO_INITIAL                        # 备用无权限在 role None 下不计、不试探
    assert len(_fo1_lines(caplog)) == 1 and "也失败 (TushareNoPermission:" in _fo1_lines(caplog)[0].getMessage()


# ---------------------------------------------------------------------------------------------- 不触发的
def test_token_error_on_primary_is_left_to_the_global_rule(caplog):
    """2002 / 40101: 全局机制自己立即切 (与以前一样: 同一调用在备用重来一轮, 状态 active=True, 一行「基址切换」); 单次重试不插手、不打行。"""
    s, naps = _install([_err("token已过期", 2002), _ok(COLS, ROWS)])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df = tc.query("namechange", fields=FIELDS, failover_once=True)
    assert len(df) == 2 and _urls(s) == [PRIMARY, BACKUP] and naps == []
    st = tc.failover_state()
    assert st["active"] is True and "token 失效" in st["reason"] and st["switches"] == 1
    assert len(_switch_lines(caplog)) == 1 and not _fo1_lines(caplog)
    # 两站都不认这把 key: 抛的是 TushareBadToken (全局机制的路径), 不再多来一轮
    s, naps = _install([_err("token已过期", 2002), _err("invalid token", 40101)])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        with pytest.raises(tc.TushareBadToken):
            tc.query("namechange", fields=FIELDS, failover_once=True, retries=1)
    assert _urls(s) == [PRIMARY, BACKUP] and not _fo1_lines(caplog)
    assert tc.failover_state()["active"] is True and tc.failover_state()["backup_fails"] == 1


def test_rate_limit_and_permanent_errors_are_not_failed_over(caplog):
    s, naps = _install([_http(429)] * 3)
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        with pytest.raises(tc.TushareRateLimited):
            tc.query("namechange", fields=FIELDS, failover_once=True)
    assert _urls(s) == [PRIMARY] * 3 and naps == [tc.BACKOFF_RATE_SEC, tc.BACKOFF_RATE_SEC * 2]
    s, naps = _install([_err("抱歉，您每分钟最多访问该接口500次")] * 3)
    with pytest.raises(tc.TushareRateLimited):
        tc.query("namechange", fields=FIELDS, failover_once=True)
    assert _urls(s) == [PRIMARY] * 3
    # 无权限自 2026-10-28 卡 FAILOVER-2 起纳入可换站类型 (权限按站不同: 主站能服务 namechange 而备用 datahubco 回 HTTP 403),
    # 主站无权限 + opt-in → 单次去备用, 见 tests/test_http403_nopermission.py ⑤
    s, naps = _install([_err("[invalid_params] 业务参数缺失或格式不合法", 400)])
    with pytest.raises(tc.TushareBadParams):
        tc.query("namechange", fields=FIELDS, failover_once=True)
    assert _urls(s) == [PRIMARY] and naps == []
    assert not _fo1_lines(caplog) and tc.failover_state() == _FO_INITIAL


def test_truncated_on_primary_is_not_failed_over(caplog):
    """分页截断 (TushareTruncated) 不是传输抖动: 语义不变, 不换站重来。"""
    s, _ = _install([_ok(["ts_code", "trade_date"], [[f"{i:06d}.SZ", "20260929"] for i in range(5000)]),
                     _err("[invalid_params] offset 不支持", 400)])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        with pytest.raises(tc.TushareTruncated):
            tc.query("daily", trade_date="20260929", failover_once=True)
    assert _urls(s) == [PRIMARY, PRIMARY] and not _fo1_lines(caplog)


def test_bulk_call_without_opt_in_is_unchanged(caplog):
    s, naps = _install([_pool_exhausted()] * 3)
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        with pytest.raises(tc.TushareTransport) as ei:
            tc.query("daily", trade_date="20261027")
    assert str(ei.value) == "daily: code=503 [upstream_pool_exhausted] upstream pool exhausted, try later"
    assert _urls(s) == [PRIMARY] * 3 and naps == [tc.BACKOFF_SEC, tc.BACKOFF_SEC * 2]
    assert not _fo1_lines(caplog) and tc.failover_state() == _FO_INITIAL
    assert inspect.signature(tc.query).parameters["failover_once"].default is False


def test_eligibility_helper():
    ok = tc._failover_once_eligible
    assert ok(tc.TushareTransport("x: HTTP 503", "x", 503))
    assert ok(tc.TushareTransport("x: 超过 120s 硬期限 (…)", "x"))
    assert ok(tc._classify("x", 503, "[upstream_pool_exhausted] …"))
    assert ok(tc._classify("x", 503, "[minute_upstream_busy] …"))
    assert not ok(tc._classify("x", 2002, "token已过期", with_backup=True))          # TushareBadToken
    assert not ok(tc.TushareRateLimited("x: 超频", "x"))
    assert ok(tc.TushareNoPermission("x: 无权限", "x"))                     # 2026-10-28 卡 FAILOVER-2: 权限按站不同, 无权限也换站
    assert ok(tc.TushareNoPermission("HTTP 403 (该站不提供此接口或无权限)", "http", 403))
    assert ok(tc._classify("x", 404, "[unknown_api] 接口不存在或未开放"))
    assert not ok(tc.TushareBadParams("x: 参数", "x"))
    assert not ok(tc.TushareTruncated("x: 分页截断", "x"))
    assert not ok(tc.TushareUnavailable("x: 没 token", "x"))
    assert not ok(RuntimeError("x"))


# ---------------------------------------------------------------------------------------------- 分页钉站 / 在备用上 / 两轮之后
def test_retry_round_paginates_on_the_same_site(caplog):
    """备用 datahubco 单页 5000: 重试那轮第一页恰满 → offset=5000 续页仍打备用 (钉在同一站), 合并 5559 行; 全局状态不变。"""
    def rows(n, start=0):
        return [[f"{start + i:06d}.SZ", "20260929", 1.0] for i in range(n)]
    cols = ["ts_code", "trade_date", "close"]
    s, _ = _install([_pool_exhausted()] * 3 + [_ok(cols, rows(5000)), _ok(cols, rows(559, 5000))])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df = tc.query("daily", trade_date="20260929", failover_once=True)
    assert len(df) == 5559 and df.attrs.get("pages") == 2
    assert _urls(s) == [PRIMARY] * 3 + [BACKUP, BACKUP]
    assert s.calls[3]["json"]["params"] == {"trade_date": "20260929"}
    assert s.calls[4]["json"]["params"] == {"trade_date": "20260929", "offset": 5000}
    assert tc.failover_state() == _FO_INITIAL
    page = [r for r in caplog.records if ": 分页 " in r.getMessage()]
    assert len(page) == 1 and "tushare daily: 分页 2 页 5559 行 (backup.invalid)" in page[0].getMessage()
    assert "daily 主站 primary.invalid 失败" in _fo1_lines(caplog)[0].getMessage() and "成功 5559 行" in _fo1_lines(caplog)[0].getMessage()


def test_on_backup_retries_once_on_primary_without_flipping_state(caplog):
    """已切到备用: 备用一轮失败 (retries=1, backup_fails=1 < 3 不试探) → 去主站重来一次成功; 全局仍在备用 (active True), 没有回切行。"""
    s, naps = _install([_http(502), _ok(COLS, ROWS)])
    _on_backup()
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df = tc.query("namechange", fields=FIELDS, failover_once=True, retries=1)
    assert len(df) == 2 and _urls(s) == [BACKUP, PRIMARY] and naps == []
    st = tc.failover_state()
    assert st["active"] is True and st["backup_fails"] == 1 and st["fails"] == 0 and st["returns"] == 0
    msg = _fo1_lines(caplog)[0].getMessage()
    assert msg.startswith("tushare 单次调用备用站重试: namechange 备用 backup.invalid 失败 (HTTP 502) → 主站 primary.invalid 成功 2 行 (")
    assert not _return_lines(caplog) and not _switch_lines(caplog)
    s2 = _session([_ok(["a"], [[1]])])                               # 下一次普通调用仍走备用 (状态没被重试翻转)
    tc.query("daily", retries=1)
    assert _urls(s2) == [BACKUP]


def test_global_switch_mid_call_takes_precedence_no_third_round(caplog):
    """主站 3 连败 (传输层) 在本次调用里就触发了全局切换 → 同一调用在备用重来一轮 (原机制): 成功就不打重试行;
    备用那轮也败 (备用 3 连败 → 试探主站也败 → 留在备用) → 两站各来过一轮, 单次重试不再多来第三轮, WARNING 一行, 原错误原样抛。"""
    s, _ = _install([_http(502)] * 3 + [_ok(COLS, ROWS)])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df = tc.query("namechange", fields=FIELDS, failover_once=True)
    assert len(df) == 2 and _urls(s) == [PRIMARY] * 3 + [BACKUP]
    assert tc.failover_state()["active"] is True and len(_switch_lines(caplog)) == 1 and not _fo1_lines(caplog)
    s, _ = _install([_http(502)] * 7)                               # 主站 3 + 备用 3 + 试探主站 1
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        with pytest.raises(tc.TushareTransport) as ei:
            tc.query("namechange", fields=FIELDS, failover_once=True)
    assert len(s.calls) == 7 and _urls(s)[:6] == [PRIMARY] * 3 + [BACKUP] * 3 and _is_probe(s.calls[6])
    assert str(ei.value) == "HTTP 502" and "都失败" not in str(ei.value)       # 原异常原样 (_post_json 的 HTTP 层文本本来就不带 api 前缀)
    lines = _fo1_lines(caplog)
    assert len(lines) == 1 and lines[0].levelno == logging.WARNING
    assert "已经由全局切换在 主站 primary.invalid 与 备用 backup.invalid 各来过一轮都失败, 不再重来" in lines[0].getMessage()
    assert tc.failover_state()["active"] is True


# ---------------------------------------------------------------------------------------------- 钩子端到端
def test_namechange_hook_uses_single_call_failover(monkeypatch, caplog):
    import ashare.market as amkt
    monkeypatch.setattr(amkt, "_tushare_on", lambda: True)
    s, naps = _install([_pool_exhausted()] * 3 + [_ok(COLS, ROWS + [["430047.BJ", "北交所票", "20240101", "", "", "改名"]])])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        rows = amkt.fetch_namechange_rows()
    assert rows == [("000929", "*ST兰黄", "20250430", None, "20250429", "*ST"),
                    ("600439", "ST瑞贝卡", "20260901", "", "20260831", "ST")]
    assert _urls(s) == [PRIMARY] * 3 + [BACKUP]
    body = s.calls[3]["json"]
    assert body["api_name"] == "namechange" and body["fields"] == FIELDS and body["params"] == {} and body["token"] == TOKEN
    msg = _fo1_lines(caplog)[0].getMessage()
    assert msg.startswith("tushare 单次调用备用站重试: namechange 主站 primary.invalid 失败 (code=503 [upstream_pool_exhausted]")
    assert "→ 备用 backup.invalid 成功 3 行 (" in msg and TOKEN not in caplog.text
    assert tc.failover_state() == _FO_INITIAL
    # 两站都败: 钩子按约定往外抛 (核心 _refresh_namechange_if_due 记「namechange 月刷: 拉取失败」, 明天再试), 消息带两站
    s, _ = _install([_pool_exhausted()] * 3 + [_http(503)] * 3)
    with pytest.raises(tc.TushareTransport) as ei:
        amkt.fetch_namechange_rows()
    assert "主站 primary.invalid 与 备用 backup.invalid 都失败" in str(ei.value)
