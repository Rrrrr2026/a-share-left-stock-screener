#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网关 HTTP 403 = 该站该接口无权限 (2026-10-28 卡 FAILOVER-2) 离线自测: 假传输层 (打桩 requests.Session; tests/conftest.py 另掐 DNS), 不联网,
不真等 (退避 sleep 打桩), 不出现任何真 token。

事实依据 (10-28 本机 13:30 跑批 update.log 实录): 13:33:52 主站 3 次 daily Read timed out → 基址切换到备用 datahubco; 13:35-13:37 备用正常服务
daily / adj_factor / stock_basic; 13:37:07-13:37:14 备用对 namechange 连回 3 次 HTTP 403 (旧口径当传输层失败: 白重试 3 次 2s/4s 退避, 计满「备用连续
3 次失败」); 13:37:16 回切到正在退化的主站; 13:38-13:41 namechange 在主站 3 次 Read timed out 再切到备用, FAILOVER-1 正确拒绝第三轮, 月刷失败;
13:43-13:46 daily_basic 又在主站烧 3 次超时才切回备用。结论: 备用对 namechange 的 403 是**该站该接口**的确定性无权限, 不是站级传输故障;
权限按站不同 (主站能服务 namechange 而备用不能, 反过来主站缺 income_vip)。

覆盖 (卡上的 ①-⑦):
  ① 主站 403: 一次就抛 TushareNoPermission (code=403), 不重试, 主站连续失败计数原样 (不清零也不计数), 不切换; 没配备用键也一样
  ② 备用 403 非 opt-in: 记 1 次备用失败, 不重试 3 次, 不回切, 抛 NoPermission; 与 JSON 无权限同一分支: 满 3 次才试探主站, 回来了本次在主站重来,
     没回来仍抛 NoPermission
  ③ 备用 403 + opt-in (failover_once): 单次去主站, 主站成功 → 返回数据, 全局状态仍是备用 (不回切、不翻转)
  ④ 备用 403 + opt-in 且主站传输失败: 抛原错误类 TushareNoPermission, 消息点名两站, __cause__ 是主站的 Transport
  ⑤ 主站 JSON 无权限 + opt-in: 单次去备用 (两向对称); 主站 HTTP 403 + opt-in 同样去备用; 两站都无权限 → NoPermission 点名两站; 非 opt-in 照旧一次就抛
  ⑥ 异常跨进程 pickle 往返 (pickle.loads(pickle.dumps(exc))) 保留类 / api / code=403; 线程回退路径 (_TC_POOL disabled → _thread_call) 原样带回主线程
  ⑦ 回放 10-28 13:33-13:46: 在备用上 namechange 403 不得触发回切, 其后 daily_basic 直接在备用发起 (不再回主站烧 3 次超时)
  ⑧ 主站站级 403 (同日返工 1, 校验员): 主站 403 进独立的连续计数 forbidden (不碰传输层 / 硬期限计数), 连续 6 次**且覆盖 ≥ 2 个不同接口** → 切到备用并在
     备用重来 (以前 3 次 403 就切, 一刀切成「不计」会让成批调用在主站被封时整轮全败); 同一接口连续 403 多少次都不切 (① 语义不变); 备用 / role None
     不计; 主站任何 JSON 应答与回切清零; 续页 (pin) 碰 403 照计并 TushareTruncated

运行:  python -X utf8 -m pytest tests/test_http403_nopermission.py -q -p no:cacheprovider
"""
from __future__ import annotations

import logging
import os
import pickle
import sys
import threading
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
HTTP403_MSG = "HTTP 403 (该站不提供此接口或无权限)"
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
    """按剧本依次返回; 记录每次请求 (url / 请求体 / 超时 / 发请求的线程名)。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.headers = {}

    def post(self, url, json=None, timeout=None):      # noqa: A002
        self.calls.append({"url": url, "json": json, "timeout": timeout, "thread": threading.current_thread().name})
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


def _cal_ok(day="20261028"):
    return _ok(["cal_date", "is_open"], [[day, 1]])


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


def _nop_lines(caplog):
    """卡 FAILOVER-2 的那行 WARNING (GM 按它 grep): 「tushare <api> 该站无权限 (HTTP 403, <host>), 不重试: …」。"""
    return [r for r in caplog.records if "该站无权限 (HTTP 403" in r.getMessage()]


def _transport_lines(caplog):
    return [r for r in caplog.records if "传输失败" in r.getMessage()]


def _is_probe(call):
    j = call["json"]
    return (j["api_name"] == "trade_cal" and j["params"]["start_date"] == j["params"]["end_date"]
            and call["timeout"] == tc.PROBE_TIMEOUT_SEC)


def _on_backup():
    """把全局状态摆成「已切到备用」(不到 20 次 / 10 分钟的试探点)。"""
    with tc._FO_LOCK:
        tc._FO.update(active=True, reason="test", switched_at=time.time(), last_probe_at=time.monotonic())


# ---------------------------------------------------------------------------------------------- ① 主站 403
def test_primary_http403_raises_once_keeps_primary_counters_and_does_not_switch(caplog):
    """主站回 HTTP 403: 一次就抛 TushareNoPermission (api / code=403), 不进 3 次重试、不退避; 主站连续传输层 / 硬期限计数**原样** (答了 403 不算
    「答了 JSON」, 不清零; 也不是传输层失败, 不计 fails), 只进独立的 forbidden=1 (返工 1; 单一接口不切), 不切换; 一行 WARNING「tushare namechange 该站无权限
    (HTTP 403, primary.invalid), 不重试」, 没有「传输失败」行。"""
    s, naps = _install([_http(403)])
    with tc._FO_LOCK:
        tc._FO.update(fails=2, timeouts=1)          # 已经攒了 2 次传输层失败 / 1 次超时: 403 既不是第 3 次, 也不把它们清零
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        with pytest.raises(tc.TushareNoPermission) as ei:
            tc.query("namechange", fields=FIELDS)
    e = ei.value
    assert type(e) is tc.TushareNoPermission and e.api == "namechange" and e.code == 403
    assert str(e) == f"namechange: {HTTP403_MSG}"
    assert _urls(s) == [PRIMARY] and naps == []                       # 恰一次 HTTP
    assert tc.failover_state() == {**_FO_INITIAL, "fails": 2, "timeouts": 1, "forbidden": 1, "forbidden_apis": ("namechange",)}
    assert not _switch_lines(caplog) and not _return_lines(caplog) and not _transport_lines(caplog)
    lines = _nop_lines(caplog)
    assert len(lines) == 1 and lines[0].levelno == logging.WARNING
    assert lines[0].getMessage() == f"tushare namechange 该站无权限 (HTTP 403, primary.invalid), 不重试: namechange: {HTTP403_MSG}"
    assert not any("永久性失败" in r.getMessage() for r in caplog.records)
    assert TOKEN not in caplog.text and "/tushare/sdk" not in caplog.text
    # 计数没被清零: 紧接着主站再一次真传输层失败 = 第 3 次 → 仍由原规则切换
    s2 = _session([_http(502), _ok(["a"], [[1]])])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df = tc.query("daily", retries=1)
    assert len(df) == 1 and _urls(s2) == [PRIMARY, BACKUP]
    st = tc.failover_state()
    assert st["active"] is True and "连续 3 次传输层失败" in st["reason"] and len(_switch_lines(caplog)) == 1


@pytest.mark.parametrize("backup", ["", PRIMARY], ids=["no_key", "backup_equals_primary"])
def test_primary_http403_without_backup_raises_once(backup, caplog):
    """没配备用键 (或备用 = 主站): 403 同样一次就抛 NoPermission; opt-in 也连 try 都不进 (没有第二站)。"""
    s, naps = _install([_http(403)], backup=backup)
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        with pytest.raises(tc.TushareNoPermission) as ei:
            tc.query("namechange", fields=FIELDS, failover_once=True)
    assert ei.value.code == 403 and str(ei.value) == f"namechange: {HTTP403_MSG}"
    assert _urls(s) == [PRIMARY] and naps == []
    assert tc.failover_state() == _FO_INITIAL and len(_nop_lines(caplog)) == 1 and not _fo1_lines(caplog)


# ---------------------------------------------------------------------------------------------- ② 备用 403 (非 opt-in)
def test_backup_http403_non_opt_in_counts_one_backup_failure_no_retry_no_return(caplog):
    """已在备用: 403 记 **1 次**备用失败 (backup_fails=1), 恰一次 HTTP (不重试 3 次), 不试探、不回切, 抛 NoPermission; 下一次普通调用仍在备用。"""
    s, naps = _install([_http(403)])
    _on_backup()
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        with pytest.raises(tc.TushareNoPermission) as ei:
            tc.query("namechange", fields=FIELDS)
    assert ei.value.code == 403 and ei.value.api == "namechange"
    assert _urls(s) == [BACKUP] and naps == [] and not any(_is_probe(c) for c in s.calls)
    st = tc.failover_state()
    assert st["active"] is True and st["backup_fails"] == 1 and st["backup_timeouts"] == 0 and st["probes"] == 0 and st["returns"] == 0
    assert not _return_lines(caplog) and not _transport_lines(caplog)
    lines = _nop_lines(caplog)
    assert len(lines) == 1 and lines[0].getMessage() == f"tushare namechange 该站无权限 (HTTP 403, backup.invalid), 不重试: namechange: {HTTP403_MSG}"
    assert TOKEN not in caplog.text and "/api/tushare" not in caplog.text
    s2 = _session([_ok(["a"], [[1]])])                               # 没回切: 下一次普通调用仍走备用; 备用答了 JSON 清零 backup_fails
    tc.query("daily_basic", retries=1)
    assert _urls(s2) == [BACKUP] and tc.failover_state()["active"] is True and tc.failover_state()["backup_fails"] == 0


def test_backup_http403_third_failure_probes_primary_exactly_like_json_no_permission(caplog):
    """与 JSON 无权限**完全相同**的分支 (卡 TS-FALLBACK-3): 403 是第 3 次备用失败 → 试探主站; 回来了 → 回切并在主站重来本次 (成功, 不打「不重试」行);
    没回来 (试探 502) → 留在备用, 仍抛 NoPermission (不是传输错), 备用计数清零重数。"""
    s, naps = _install([_http(403), _cal_ok(), _ok(COLS, ROWS)])
    _on_backup()
    with tc._FO_LOCK:
        tc._FO["backup_fails"] = 2
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df = tc.query("namechange", fields=FIELDS)
    assert len(df) == 2 and _urls(s) == [BACKUP, PRIMARY, PRIMARY] and _is_probe(s.calls[1]) and naps == []
    st = tc.failover_state()
    assert st["active"] is False and st["returns"] == 1 and st["probes"] == 1 and st["backup_fails"] == 0
    ret = _return_lines(caplog)
    assert len(ret) == 1 and "备用连续 3 次失败, 最后一次: namechange HTTP 403 (备用无权限或没这个接口)" in ret[0].getMessage()
    assert any(r.getMessage() == f"tushare namechange 在备用无权限 (backup.invalid), 主站已回来, 本次在主站重来: namechange: {HTTP403_MSG}"
               for r in caplog.records)
    assert not _nop_lines(caplog) and not any("永久性失败" in r.getMessage() for r in caplog.records)
    caplog.clear()
    s, naps = _install([_http(403), _http(502)])
    _on_backup()
    with tc._FO_LOCK:
        tc._FO["backup_fails"] = 2
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        with pytest.raises(tc.TushareNoPermission) as ei:
            tc.query("namechange", fields=FIELDS)
    assert ei.value.code == 403 and _urls(s) == [BACKUP, PRIMARY] and _is_probe(s.calls[1]) and naps == []
    st = tc.failover_state()
    assert st["active"] is True and st["probes"] == 1 and st["backup_fails"] == 0 and st["returns"] == 0
    assert len(_nop_lines(caplog)) == 1 and not _return_lines(caplog)
    assert any("试探失败" in r.getMessage() and "备用连续 3 次失败" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------------------------- ③ ④ 备用 403 + opt-in
def test_backup_http403_opt_in_retries_once_on_primary_state_stays_backup(caplog):
    """已在备用 + failover_once: 备用 403 (记 1 次备用失败) → 立即把同一调用在主站重来一次 → 成功返回; 全局状态仍是备用 (active True, 不回切、不试探);
    INFO 一行「单次调用备用站重试: namechange 备用 backup.invalid 失败 (HTTP 403 …) → 主站 primary.invalid 成功 2 行」。"""
    s, naps = _install([_http(403), _ok(COLS, ROWS)])
    _on_backup()
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df = tc.query("namechange", fields=FIELDS, failover_once=True)
    assert len(df) == 2 and list(df.columns) == COLS
    assert _urls(s) == [BACKUP, PRIMARY] and naps == [] and s.calls[1]["json"] == s.calls[0]["json"]
    st = tc.failover_state()
    assert st["active"] is True and st["backup_fails"] == 1 and st["fails"] == 0 and st["timeouts"] == 0
    assert st["returns"] == 0 and st["probes"] == 0 and st["switches"] == 0
    lines = _fo1_lines(caplog)
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    assert lines[0].getMessage().startswith(
        f"tushare 单次调用备用站重试: namechange 备用 backup.invalid 失败 ({HTTP403_MSG}) → 主站 primary.invalid 成功 2 行 (")
    assert len(_nop_lines(caplog)) == 1 and not _return_lines(caplog) and not _switch_lines(caplog)
    assert TOKEN not in caplog.text and "/api/tushare" not in caplog.text and "/tushare/sdk" not in caplog.text
    s2 = _session([_ok(["a"], [[1]])])                               # 全局状态没被翻转: 下一次普通调用仍走备用
    tc.query("daily_basic", retries=1)
    assert _urls(s2) == [BACKUP] and tc.failover_state()["active"] is True


def test_backup_http403_opt_in_primary_transport_fails_raises_no_permission_naming_both(caplog):
    """备用 403 + opt-in, 主站那轮传输层失败 (retries 跑完): 按**原错误类** TushareNoPermission 抛 (code 403), 消息点名两站与各自原因,
    __cause__ 是主站的 TushareTransport; WARNING 一行「也失败」; 全局状态: 仍在备用, backup_fails=1, 主站那轮 (role None) 不计 fails。"""
    s, naps = _install([_http(403)] + [_http(502)] * 3)
    _on_backup()
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        with pytest.raises(tc.TushareNoPermission) as ei:
            tc.query("namechange", fields=FIELDS, failover_once=True)
    e = ei.value
    assert type(e) is tc.TushareNoPermission and e.api == "namechange" and e.code == 403
    assert str(e) == (f"namechange: 备用 backup.invalid 与 主站 primary.invalid 都失败 — 备用: {HTTP403_MSG}; "
                      "主站: TushareTransport HTTP 502")
    assert isinstance(e.__cause__, tc.TushareTransport) and str(e.__cause__) == "HTTP 502"
    assert _urls(s) == [BACKUP] + [PRIMARY] * 3 and naps == [tc.BACKOFF_SEC, tc.BACKOFF_SEC * 2]   # 主站那轮自己的退避; 换站瞬间不退避
    st = tc.failover_state()
    assert st["active"] is True and st["backup_fails"] == 1 and st["fails"] == 0 and st["timeouts"] == 0 and st["returns"] == 0
    lines = _fo1_lines(caplog)
    assert len(lines) == 1 and lines[0].levelno == logging.WARNING
    assert lines[0].getMessage().startswith(
        f"tushare 单次调用备用站重试: namechange 备用 backup.invalid 失败 ({HTTP403_MSG}) → 主站 primary.invalid 也失败 "
        "(TushareTransport: HTTP 502); 两站都失败, 按原错误类型 TushareNoPermission 抛出 (共 ")
    assert not _return_lines(caplog) and not _switch_lines(caplog) and TOKEN not in caplog.text


# ---------------------------------------------------------------------------------------------- ⑤ 主站无权限 + opt-in (对称)
def test_primary_no_permission_opt_in_goes_to_backup_once(caplog):
    """主站 JSON 无权限 (40203) + opt-in → 单次去备用成功 (主站答了 JSON, 计数清零; 备用那轮 role None 不计: 状态与初始态逐键相同);
    主站网关 403 + opt-in 同样去备用 (主站那次进 forbidden=1, 备用那轮 role None 不计); 两站都无权限 → 抛 NoPermission (原错误类) 点名两站,
    code 保留主站那次的 403。"""
    s, naps = _install([_err("抱歉，您没有接口访问权限，请确认积分", 40203), _ok(COLS, ROWS)])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df = tc.query("namechange", fields=FIELDS, failover_once=True)
    assert len(df) == 2 and _urls(s) == [PRIMARY, BACKUP] and naps == []
    assert tc.failover_state() == _FO_INITIAL
    msg = _fo1_lines(caplog)[0].getMessage()
    assert msg.startswith("tushare 单次调用备用站重试: namechange 主站 primary.invalid 失败 (无权限/积分不足 (抱歉，您没有接口访问权限，请确认积分)) "
                          "→ 备用 backup.invalid 成功 2 行 (")
    assert any("tushare namechange 永久性失败, 不重试: " in r.getMessage() for r in caplog.records)     # JSON 路径的行一字不差
    caplog.clear()
    s, naps = _install([_http(403), _ok(COLS, ROWS)])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df = tc.query("namechange", fields=FIELDS, failover_once=True)
    assert len(df) == 2 and _urls(s) == [PRIMARY, BACKUP] and naps == []
    assert tc.failover_state() == {**_FO_INITIAL, "forbidden": 1, "forbidden_apis": ("namechange",)}     # 主站 403 计 1 次; 备用那轮 role None 不计
    assert _fo1_lines(caplog)[0].getMessage().startswith(
        f"tushare 单次调用备用站重试: namechange 主站 primary.invalid 失败 ({HTTP403_MSG}) → 备用 backup.invalid 成功 2 行 (")
    assert len(_nop_lines(caplog)) == 1
    caplog.clear()
    s, naps = _install([_http(403), _err("[unknown_api] 接口不存在或未开放", 404)])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        with pytest.raises(tc.TushareNoPermission) as ei:
            tc.query("namechange", fields=FIELDS, failover_once=True)
    e = ei.value
    assert type(e) is tc.TushareNoPermission and e.code == 403
    assert str(e) == (f"namechange: 主站 primary.invalid 与 备用 backup.invalid 都失败 — 主站: {HTTP403_MSG}; "
                      "备用: TushareNoPermission 无权限/积分不足 ([unknown_api] 接口不存在或未开放)")
    assert isinstance(e.__cause__, tc.TushareNoPermission) and _urls(s) == [PRIMARY, BACKUP] and naps == []
    assert tc.failover_state() == {**_FO_INITIAL, "forbidden": 1, "forbidden_apis": ("namechange",)}
    assert len(_fo1_lines(caplog)) == 1 and _fo1_lines(caplog)[0].levelno == logging.WARNING


def test_non_opt_in_json_no_permission_unchanged(caplog):
    """没开 opt-in 的调用: 主站 JSON 无权限照旧一次就抛、不换站 (与 7b92bb6 版一字不差); 备用上照旧记一次备用失败。"""
    s, naps = _install([_err("抱歉，您没有接口访问权限", 40203)])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        with pytest.raises(tc.TushareNoPermission) as ei:
            tc.query("namechange", fields=FIELDS)
    assert str(ei.value) == "namechange: 无权限/积分不足 (抱歉，您没有接口访问权限)" and ei.value.code == 40203
    assert _urls(s) == [PRIMARY] and naps == [] and tc.failover_state() == _FO_INITIAL and not _fo1_lines(caplog)
    s, naps = _install([_err("抱歉，您没有接口访问权限", 40203)])
    _on_backup()
    with pytest.raises(tc.TushareNoPermission):
        tc.query("namechange", fields=FIELDS)
    assert _urls(s) == [BACKUP] and tc.failover_state()["backup_fails"] == 1 and tc.failover_state()["active"] is True


def test_eligibility_helper_accepts_no_permission_both_forms():
    ok = tc._failover_once_eligible
    assert ok(tc.TushareNoPermission(HTTP403_MSG, "http", 403))
    assert ok(tc.TushareNoPermission("x: 无权限/积分不足 (抱歉，您没有接口访问权限)", "x", 40203))
    assert ok(tc._classify("x", 404, "[unknown_api] 接口不存在或未开放"))
    assert ok(tc.TushareTransport("x: HTTP 503", "x", 503))
    assert not ok(tc._classify("x", 2002, "token已过期", with_backup=True))          # TushareBadToken
    assert not ok(tc.TushareRateLimited("x: 超频", "x"))
    assert not ok(tc.TushareBadParams("x: 参数", "x"))
    assert not ok(tc.TushareTruncated("x: 分页截断", "x"))
    assert not ok(tc.TushareUnavailable("x: 没 token", "x"))


# ---------------------------------------------------------------------------------------------- ⑥ 跨进程 pickle / 线程回退
def test_http403_exception_survives_pickle_round_trip_and_thread_fallback():
    """_post_json 对 403 抛的 TushareNoPermission: 构造签名 (message, api, code) 与 TushareRateLimited 同, pickle 往返 (spawn 工作进程 → 主进程
    就是这一步) 保留类 / 文本 / api / code=403; 线程回退路径 (_TC_POOL disabled → _thread_call) 从工作线程原样带回主线程, 仍走同一分支。"""
    tc._SESSION = FakeSession([_http(403)])
    with pytest.raises(tc.TushareNoPermission) as ei:
        tc._post_json(BACKUP, {"api_name": "namechange", "token": TOKEN, "params": {}, "fields": FIELDS}, 5)
    e = ei.value
    assert str(e) == HTTP403_MSG and e.api == "http" and e.code == 403 and TOKEN not in str(e)
    assert isinstance(e, tc.TushareError) and not isinstance(e, tc.TushareTransport)
    e2 = pickle.loads(pickle.dumps(e))
    assert type(e2) is tc.TushareNoPermission and str(e2) == HTTP403_MSG and e2.api == "http" and e2.code == 403
    r = pickle.loads(pickle.dumps(tc.TushareRateLimited("HTTP 429 限流", "http", 429)))
    assert type(r) is tc.TushareRateLimited and r.code == 429                    # 同一构造签名, 429 那条照旧
    # 线程回退路径: USE_PROCESS_DEADLINE=True 但本进程 Tushare 侧池已判不可用 → _thread_call 在守护线程里发请求, 异常经队列带回
    s, naps = _install([_http(403)])
    tc.USE_PROCESS_DEADLINE = True
    tc._TC_POOL.update(disabled=True, reason="test")
    _on_backup()
    with pytest.raises(tc.TushareNoPermission) as ei:
        tc.query("namechange", fields=FIELDS)
    assert ei.value.code == 403 and str(ei.value) == f"namechange: {HTTP403_MSG}"
    assert _urls(s) == [BACKUP] and naps == []
    assert s.calls[0]["thread"] != threading.main_thread().name and s.calls[0]["thread"].startswith("tushare-")
    assert tc.failover_state()["backup_fails"] == 1 and tc.failover_state()["active"] is True


# ---------------------------------------------------------------------------------------------- ⑦ 回放 10-28 13:33-13:46
def test_replay_2026_10_28_namechange_403_on_backup_does_not_bounce_back(monkeypatch, caplog):
    """13:33 主站 3 次 daily Read timed out → 切备用; 13:35-13:37 备用服务 adj_factor / stock_basic; 13:37 备用对 namechange 回 403 (月刷钩子, opt-in):
    一次就认、不重试 3 次、不计满备用 3 连败、**不回切**; 单次换站去主站 —— 主站当时 3 次 Read timed out (与实录一致) → 两站都失败按 NoPermission 抛,
    点名两站; 13:43 daily_basic **直接在备用发起** (不再回主站烧 3 次超时)。整段没有「基址回切」, 没有试探, 备用上没有白重试的「传输失败」行。"""
    import ashare.market as amkt
    monkeypatch.setattr(amkt, "_tushare_on", lambda: True)
    read_timeout = OSError("HTTPSConnectionPool(host='primary.invalid', port=443): Read timed out. (read timeout=60)")
    daily_cols = ["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount"]
    s, naps = _install([read_timeout] * 3 + [_ok(daily_cols, [["000001.SZ", "20261028", 1, 1, 1, 1, 1, 1]])])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df = tc.query("daily", trade_date="20261028", fields=",".join(daily_cols))
    assert len(df) == 1 and _urls(s) == [PRIMARY] * 3 + [BACKUP] and len(_switch_lines(caplog)) == 1
    assert tc.failover_state()["active"] is True and "连续 3 次传输层失败" in tc.failover_state()["reason"]
    for api in ("adj_factor", "stock_basic"):
        s2 = _session([_ok(["ts_code"], [["000001.SZ"]])])
        tc.query(api, trade_date="20261028")
        assert _urls(s2) == [BACKUP]
    naps.clear()
    s3 = _session([_http(403)] + [read_timeout] * 3)
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        with pytest.raises(tc.TushareNoPermission) as ei:
            amkt.fetch_namechange_rows()
    assert _urls(s3) == [BACKUP] + [PRIMARY] * 3 and naps == [tc.BACKOFF_SEC, tc.BACKOFF_SEC * 2]
    assert not any(_is_probe(c) for c in s3.calls)
    assert ei.value.code == 403 and "备用 backup.invalid 与 主站 primary.invalid 都失败" in str(ei.value)
    assert "备用: HTTP 403 (该站不提供此接口或无权限); 主站: TushareTransport HTTPSConnectionPool" in str(ei.value) and "Read timed out" in str(ei.value)
    assert isinstance(ei.value.__cause__, tc.TushareTransport) and "Read timed out" in str(ei.value.__cause__)
    st = tc.failover_state()
    assert st["active"] is True and st["backup_fails"] == 1 and st["returns"] == 0 and st["probes"] == 0
    assert st["fails"] == 3                 # 13:33 切换时攒下的 3 次 (切换不清, 回切才清); 主站那轮 role None 一次都没加 (不是 6)
    assert not _return_lines(caplog) and len(_switch_lines(caplog)) == 1
    nop = _nop_lines(caplog)
    assert len(nop) == 1 and nop[0].getMessage().startswith("tushare namechange 该站无权限 (HTTP 403, backup.invalid), 不重试")
    assert not [r for r in _transport_lines(caplog) if "backup.invalid" in r.getMessage()]     # 备用上没有白重试的传输失败行
    assert len(_fo1_lines(caplog)) == 1 and _fo1_lines(caplog)[0].levelno == logging.WARNING
    assert TOKEN not in caplog.text
    s4 = _session([_ok(["ts_code", "trade_date", "pe"], [["000001.SZ", "20261028", 10.0]])])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df = tc.query("daily_basic", trade_date="20261028")
    assert len(df) == 1 and _urls(s4) == [BACKUP]                     # 直接在备用发起, 主站一次都没碰
    st = tc.failover_state()
    assert st["active"] is True and st["backup_fails"] == 0 and st["returns"] == 0 and not _return_lines(caplog)


# ---------------------------------------------------------------------------------------------- ⑧ 主站站级 403 (返工 1)
def test_primary_http403_across_two_apis_switches_to_backup_and_retries_there(caplog):
    """校验员 (返工 1): 主站被站级封禁 (CDN/WAF 封 IP / 网关按 key 封 / 整站下线回 403) 时 daily / adj_factor / daily_basic 会连着 403 —— 独立的连续计数
    forbidden 满 FAILOVER_AFTER_FORBIDDEN=6 次**且覆盖 ≥ FAILOVER_FORBIDDEN_MIN_APIS=2 个不同接口** → 切到备用, 触发切换的那次调用在备用重来并成功;
    之前每次都是一次就抛 NoPermission (恰一次 HTTP, 不退避, 一行「不重试」), 传输层 / 硬期限计数不动; 之后的调用直接走备用, 备用上没有「传输失败」行。"""
    assert tc.FAILOVER_AFTER_FORBIDDEN == 6 and tc.FAILOVER_FORBIDDEN_MIN_APIS == 2
    s, naps = _install([_http(403)] * 6 + [_ok(["ts_code"], [["000001.SZ"]])])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        for i, day in enumerate(("20261022", "20261023", "20261024", "20261027")):     # 校验员的剧本: 四个交易日各调一次 daily
            with pytest.raises(tc.TushareNoPermission) as ei:
                tc.query("daily", trade_date=day)
            assert ei.value.code == 403 and ei.value.api == "daily"
            st = tc.failover_state()
            assert st["active"] is False and st["forbidden"] == i + 1 and st["forbidden_apis"] == ("daily",)
            assert st["fails"] == 0 and st["timeouts"] == 0 and st["switches"] == 0
        with pytest.raises(tc.TushareNoPermission):
            tc.query("adj_factor", trade_date="20261027")              # 第 5 次, 第 2 个接口: 还差一次
        st = tc.failover_state()
        assert st["active"] is False and st["forbidden"] == 5 and st["forbidden_apis"] == ("adj_factor", "daily")
        df = tc.query("daily_basic", trade_date="20261027")             # 第 6 次 (覆盖 3 个接口) → 切换, 本次在备用重来 → 成功
    assert len(df) == 1 and _urls(s) == [PRIMARY] * 6 + [BACKUP] and naps == []
    assert s.calls[6]["json"] == s.calls[5]["json"]                     # 备用上重来的是同一个请求体 (同一把 key)
    st = tc.failover_state()
    assert st["active"] is True and st["switches"] == 1 and st["returns"] == 0 and st["probes"] == 0
    assert st["forbidden"] == 6 and st["forbidden_apis"] == ("adj_factor", "daily", "daily_basic")     # 切换时不清 (回切才清), 与 fails 同口径
    assert st["fails"] == 0 and st["timeouts"] == 0 and st["backup_fails"] == 0
    assert st["reason"] == "主站连续 6 次 HTTP 403 覆盖 3 个接口 (adj_factor, daily, daily_basic), 最后一次: daily_basic HTTP 403"
    lines = _switch_lines(caplog)
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    assert lines[0].getMessage() == f"tushare 基址切换: 主站 primary.invalid → 备用 backup.invalid (原因: {st['reason']})"
    assert len(_nop_lines(caplog)) == 5                                 # 前 5 次各一行「不重试」; 切换那次打的是「本次在备用重来」
    hop = [r for r in caplog.records if "在主站 HTTP 403 (primary.invalid)" in r.getMessage()]
    assert len(hop) == 1 and hop[0].levelno == logging.WARNING and hop[0].getMessage().endswith(
        f"主站连续 403 已覆盖多个接口 = 站级封禁, 已切到备用, 本次在备用重来: daily_basic: {HTTP403_MSG}")
    assert not _transport_lines(caplog) and not _return_lines(caplog)
    assert TOKEN not in caplog.text and "/tushare/sdk" not in caplog.text and "/api/tushare" not in caplog.text
    s2 = _session([_ok(["ts_code"], [["000001.SZ"]])])                  # 本进程余下调用直接走备用, 主站不再碰
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        tc.query("daily", trade_date="20261028", retries=1)
    assert _urls(s2) == [BACKUP] and tc.failover_state()["active"] is True and len(_switch_lines(caplog)) == 1


def test_primary_http403_same_api_repeated_never_switches(caplog):
    """① 的语义不变 (校验员要求): 主站对**同一接口**连续 403 多少次都不切 —— 按接口的无权限只会卡在同一个接口上 (切过去另一站多半也没有, 还会跟备用的
    3 连败试探乒乓); forbidden 照计 (8 > 6), forbidden_apis 只有一个; 每次恰一次 HTTP、一行「不重试」, 没有切换行; 之后主站 JSON 应答清零。"""
    s, naps = _install([_http(403)] * 8)
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        for i in range(8):
            with pytest.raises(tc.TushareNoPermission) as ei:
                tc.query("income_vip", ts_code=f"00000{i}.SZ")
            assert ei.value.code == 403 and str(ei.value) == f"income_vip: {HTTP403_MSG}"
    st = tc.failover_state()
    assert st["active"] is False and st["switches"] == 0 and st["forbidden"] == 8 and st["forbidden_apis"] == ("income_vip",)
    assert st["fails"] == 0 and st["timeouts"] == 0 and st["probes"] == 0
    assert _urls(s) == [PRIMARY] * 8 and naps == [] and not _switch_lines(caplog) and len(_nop_lines(caplog)) == 8
    assert not any("本次在备用重来" in r.getMessage() for r in caplog.records) and not _transport_lines(caplog)
    s2 = _session([_ok(["ts_code"], [["000001.SZ"]])])
    tc.query("daily", trade_date="20261028", retries=1)
    assert _urls(s2) == [PRIMARY] and tc.failover_state() == _FO_INITIAL     # 主站答了 JSON → forbidden / forbidden_apis 清零


def test_forbidden_counter_is_primary_only_and_cleared_by_json_or_return():
    """forbidden 只在主站 (role "primary") 计: 备用上的 403 记 backup_fails 不碰它; 单次换站那轮 (role None) 不计; 主站 5 次 403 覆盖 2 个接口 (差一次)
    不切, 中间夹一次传输层失败各算各的 (fails=1, forbidden 不清); 主站任何 JSON 应答 (哪怕超频错误码) 把 fails / forbidden 一起清零; 回切也清零
    (切换时攒下的 forbidden 与 fails 同口径, 留到回切)。"""
    s, naps = _install([_http(403)])                                    # 备用上: 不碰 forbidden
    _on_backup()
    with pytest.raises(tc.TushareNoPermission):
        tc.query("namechange", fields=FIELDS)
    st = tc.failover_state()
    assert _urls(s) == [BACKUP] and st["backup_fails"] == 1 and st["forbidden"] == 0 and st["forbidden_apis"] == ()
    s, naps = _install([_http(403), _http(403)])                        # 单次换站那轮 (role None): 备用 403 → 主站 403, 主站那轮不计
    _on_backup()
    with pytest.raises(tc.TushareNoPermission):
        tc.query("namechange", fields=FIELDS, failover_once=True)
    st = tc.failover_state()
    assert _urls(s) == [BACKUP, PRIMARY] and st["active"] is True and st["backup_fails"] == 1 and st["forbidden"] == 0 and st["forbidden_apis"] == ()
    s, naps = _install([_http(403), _http(403), _http(502), _http(403), _http(403), _http(403)])   # 主站: 5 次 403 / 2 个接口 + 夹 1 次 502
    for api in ("daily", "adj_factor"):
        with pytest.raises(tc.TushareNoPermission):
            tc.query(api, trade_date="20261027", retries=1)
    with pytest.raises(tc.TushareTransport):
        tc.query("daily_basic", trade_date="20261027", retries=1)
    for api in ("daily", "daily", "adj_factor"):
        with pytest.raises(tc.TushareNoPermission):
            tc.query(api, trade_date="20261028", retries=1)
    st = tc.failover_state()
    assert st["active"] is False and st["switches"] == 0 and st["forbidden"] == 5 and st["forbidden_apis"] == ("adj_factor", "daily") and st["fails"] == 1
    assert _urls(s) == [PRIMARY] * 6 and naps == []
    _session([_err("抱歉，您每分钟最多访问该接口500次")])                  # 主站答了 JSON (超频错误码也算) → fails / forbidden 一起清零
    with pytest.raises(tc.TushareRateLimited):
        tc.query("daily", trade_date="20261028", retries=1)
    assert tc.failover_state() == _FO_INITIAL
    s, naps = _install([_http(403), _cal_ok(), _ok(["ts_code"], [["000001.SZ"]])])     # 回切清零: 备用第 3 次失败 → 试探成功 → 回切, 本次在主站重来
    _on_backup()
    with tc._FO_LOCK:
        tc._FO.update(forbidden=6, forbidden_apis=("adj_factor", "daily"), backup_fails=2, fails=3)
    df = tc.query("daily", trade_date="20261028", retries=1)
    assert len(df) == 1 and _urls(s) == [BACKUP, PRIMARY, PRIMARY] and _is_probe(s.calls[1])
    st = tc.failover_state()
    assert st["active"] is False and st["returns"] == 1 and st["fails"] == 0 and st["forbidden"] == 0 and st["forbidden_apis"] == ()


def test_primary_http403_on_continuation_page_counts_and_truncates(caplog):
    """续页 (pin=(主站, "primary")) 碰 403: 失败计数照记 (forbidden=1; 第一页的 JSON 应答已把之前的清零), 本页一次就抛 NoPermission →
    _paginate 抛 TushareTruncated (__cause__ 是它, rows = 已拿到的行数), 不跨站拼页, 不切换。"""
    rows = [["000001.SZ", "20261028"], ["000002.SZ", "20261028"]]
    s, naps = _install([_ok(["ts_code", "trade_date"], rows), _http(403)])
    tc._PAGE_LIMIT = 2                                                  # 本站单页上限 2: 第一页恰满 → 续页 offset=2
    with tc._FO_LOCK:
        tc._FO.update(forbidden=3, forbidden_apis=("adj_factor",))
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        with pytest.raises(tc.TushareTruncated) as ei:
            tc.query("daily", trade_date="20261028")
    e = ei.value
    assert isinstance(e.__cause__, tc.TushareNoPermission) and e.__cause__.code == 403 and e.rows == 2 and e.pages == 1
    assert "续页 offset=2 失败 (TushareNoPermission: daily: HTTP 403" in str(e)
    assert _urls(s) == [PRIMARY, PRIMARY] and s.calls[1]["json"]["params"] == {"trade_date": "20261028", "offset": 2} and naps == []
    st = tc.failover_state()
    assert st["active"] is False and st["forbidden"] == 1 and st["forbidden_apis"] == ("daily",) and st["fails"] == 0 and st["switches"] == 0
    assert len(_nop_lines(caplog)) == 1 and not _switch_lines(caplog) and any("分页截断" in r.getMessage() for r in caplog.records)
    assert TOKEN not in caplog.text
