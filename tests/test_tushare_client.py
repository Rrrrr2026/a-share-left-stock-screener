#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tushare 客户端离线自测 (不联网, 打桩 requests.Session)。

覆盖: 正常返回成表 / 请求体结构 / 错误文本分类 (超频文本里也含"权限" -> 必须判超频) /
权限与必填参数不重试 / 传输错误退避重试 / 令牌桶限频 / ts_code 与日期归一 /
index_daily 升序 / trade_cal 转 ISO / 硬期限走 datasource 的**进程级**执行器 /
备用基址自动切换 (文末 pytest 风格: 主站 3 次传输层失败 / token 失效立即切 / 业务错误不切 /
硬期限超时单独计阈值 6 / HTTP 429 按超频退避不计 / 备用三连败试探主站回切 / 每 20 次调用或 10 分钟试探主站 /
一次调用最多两轮 / 没配备用键分类顺序与异常类型同 7b92bb6 版 (逐字节钉住) / unknown_api·invalid_params 不重试 /
镜像回显的 token 进不了日志 / --probe-backup 真验收; 基址由主进程选好传给 _post_json; 不写任何 token)。
运行:  python tests/test_tushare_client.py    或    python -m pytest tests/test_tushare_client.py -q
"""
from __future__ import annotations
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ashare import tushare_client as tc  # noqa: E402

PASS, FAIL = 0, 0
TOKEN = "TEST-TOKEN-NOT-A-REAL-ONE"


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ FAIL: {name}")


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
    """按剧本依次返回; 记录每次请求体 (用于断言结构与 token 传递)。"""

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


_REAL_SLEEP = tc.time.sleep


def _install(script):
    """装上假 session + 假凭据, 并关掉进程池 (子进程里 mock 不生效)。
    顺手还原被上一个用例打桩掉的 time.sleep —— pytest 下用例之间没有清理钩子。"""
    tc.time.sleep = _REAL_SLEEP
    tc._SESSION = FakeSession(script)
    tc._CRED = (TOKEN, "https://example.invalid/")
    tc.USE_PROCESS_DEADLINE = False
    tc._BUCKET = tc.TokenBucket(100000)                # 限频单独测, 别拖慢其它用例
    return tc._SESSION


def _ok(fields, items):
    return FakeResp({"code": 0, "msg": "", "data": {"fields": fields, "items": items}})


def _err(msg, code=40203):
    return FakeResp({"code": code, "msg": msg, "data": None})


def _no_sleep():
    """退避 sleep 打桩, 记录睡了多久 (退避逻辑要能被断言而不用真等)。"""
    naps = []
    tc.time.sleep = lambda s: naps.append(s)           # noqa: SLF001
    return naps


# ---------------------------------------------------------------------------
def test_query_ok():
    print("[query 正常路径]")
    s = _install([_ok(["ts_code", "trade_date", "close"],
                      [["000300.SH", "20260904", 4548.05], ["000300.SH", "20260903", 4552.58]])])
    df = tc.query("index_daily", ts_code="000300.SH", start_date="20260901", end_date=None)
    check("返回 DataFrame 两行", len(df) == 2)
    check("列名来自 data.fields", list(df.columns) == ["ts_code", "trade_date", "close"])
    body = s.calls[0]["json"]
    check("请求体四件套齐全", set(body) == {"api_name", "token", "params", "fields"})
    check("api_name 正确", body["api_name"] == "index_daily")
    check("token 原样带上", body["token"] == TOKEN)
    check("None 参数被丢弃", body["params"] == {"ts_code": "000300.SH", "start_date": "20260901"})
    check("URL = secrets 里的基址", s.calls[0]["url"] == "https://example.invalid/")


def test_empty_result_keeps_columns():
    print("[空结果仍带列名]")
    _install([_ok(["ts_code", "trade_date"], [])])
    df = tc.query("daily", trade_date="20260906")
    check("空表", len(df) == 0)
    check("列名保留", list(df.columns) == ["ts_code", "trade_date"])


def test_classify_order():
    print("[错误文本分类: 超频 > 参数 > 权限]")
    rate = "抱歉，您每分钟最多访问该接口500次，权限的具体详情访问：https://tushare.pro/document/1"
    check("超频文本含'权限'仍判超频", isinstance(tc._classify("daily", -1, rate), tc.TushareRateLimited))
    check("必填参数 -> BadParams",
          isinstance(tc._classify("forecast", -1, "必填参数 标的 不能为空"), tc.TushareBadParams))
    check("无权限 -> NoPermission",
          isinstance(tc._classify("us_daily", -1, "抱歉，您没有接口访问权限，请确认积分"),
                     tc.TushareNoPermission))
    check("其它 -> Transport(可重试)",
          isinstance(tc._classify("daily", -1, "系统内部错误"), tc.TushareTransport))


def test_permission_not_retried():
    print("[权限/参数错误不重试]")
    s = _install([_err("抱歉，您没有接口访问权限，请确认您的积分"),
                  _err("抱歉，您没有接口访问权限，请确认您的积分"),
                  _err("抱歉，您没有接口访问权限，请确认您的积分")])
    naps = _no_sleep()
    try:
        tc.query("us_daily", ts_code="AAPL")
        check("应抛 TushareNoPermission", False)
    except tc.TushareNoPermission:
        check("抛 TushareNoPermission", True)
    check("只请求一次 (不重试)", len(s.calls) == 1)
    check("没有退避 sleep", not naps)

    s = _install([_err("必填参数 标的"), _err("必填参数 标的")])
    try:
        tc.query("forecast", period="20250630")
        check("应抛 TushareBadParams", False)
    except tc.TushareBadParams:
        check("抛 TushareBadParams", True)
    check("必填参数也只请求一次", len(s.calls) == 1)


def test_transient_retry_then_success():
    print("[瞬时错误退避重试后成功]")
    s = _install([FakeResp({}, status=502),
                  _err("系统内部错误", code=-1),
                  _ok(["cal_date"], [["20260904"]])])
    naps = _no_sleep()
    df = tc.query("trade_cal", exchange="SSE")
    check("第三次成功", len(df) == 1)
    check("共请求三次", len(s.calls) == 3)
    check("两次指数退避 (2s, 4s)", naps == [tc.BACKOFF_SEC, tc.BACKOFF_SEC * 2])


def test_rate_limited_backoff_and_exhaust():
    print("[超频: 线性退避, 重试耗尽后抛出]")
    rate = "抱歉，您每分钟最多访问该接口500次"
    s = _install([_err(rate), _err(rate), _err(rate)])
    naps = _no_sleep()
    try:
        tc.query("daily", trade_date="20260904")
        check("应抛 TushareRateLimited", False)
    except tc.TushareRateLimited:
        check("抛 TushareRateLimited", True)
    check("重试满 3 次", len(s.calls) == tc.MAX_RETRIES)
    check("超频用线性退避 (12s, 24s)", naps == [tc.BACKOFF_RATE_SEC, tc.BACKOFF_RATE_SEC * 2])


def test_network_exception_is_transient():
    print("[requests 抛异常 = 传输层, 可重试]")
    s = _install([OSError("Connection reset by peer"), _ok(["a"], [[1]])])
    _no_sleep()
    df = tc.query("stock_basic", list_status="L")
    check("重试后成功", len(df) == 1 and len(s.calls) == 2)


def test_no_token():
    print("[没配 token]")
    tc._CRED = ("", tc.DEFAULT_BASE)
    try:
        tc.query("daily", trade_date="20260904")
        check("应抛 TushareUnavailable", False)
    except tc.TushareUnavailable:
        check("抛 TushareUnavailable", True)
    check("available() 为假", tc.available() is False)
    tc._CRED = (TOKEN, "https://example.invalid/")
    check("有 token 时 available() 为真", tc.available() is True)


def test_token_bucket():
    print("[令牌桶限频]")
    b = tc.TokenBucket(60)                 # 1 次/秒, 桶容量 60
    for _ in range(60):
        b.acquire()
    check("桶被取空", b.tokens < 1.0)
    # 假时钟: sleep 只拨快 monotonic, 不真等。原版只打桩 sleep 不打桩 monotonic, acquire 的 while 循环按真时钟空转
    # 约 1 s 才攒出一个令牌 (卡 TEST-SPEED 10-17 --durations 实测 1.02 s, 本文件唯一上榜的用例)
    slept, clock = [], [tc.time.monotonic()]
    real_sleep, real_monotonic = tc.time.sleep, tc.time.monotonic
    tc.time.sleep = lambda s: (slept.append(s), clock.__setitem__(0, clock[0] + s))
    tc.time.monotonic = lambda: clock[0]
    try:
        waited = b.acquire()               # 令牌不足 -> 睡到够 (假时钟下一两轮就够)
    finally:
        tc.time.sleep, tc.time.monotonic = real_sleep, real_monotonic
    check("空桶时会等待", bool(slept) and waited > 0)
    b2 = tc.TokenBucket(6000)              # 100 次/秒, 不该等
    check("充裕时零等待", b2.acquire() == 0.0)


def test_helpers():
    print("[ts_code / 日期 归一]")
    check("sh000300 -> 000300.SH", tc.to_ts_code("sh000300") == "000300.SH")
    check("sz399006 -> 399006.SZ", tc.to_ts_code("sz399006") == "399006.SZ")
    check("000300 -> 000300.SH (宽基特判)", tc.to_ts_code("000300") == "000300.SH")
    check("600519 -> 600519.SH", tc.to_ts_code("600519") == "600519.SH")
    check("000001 (指数/平安同码) -> 000001.SH", tc.to_ts_code("000001") == "000001.SH")
    check("已是 ts_code 原样", tc.to_ts_code("000905.SH") == "000905.SH")
    check("日期去横杠", tc._ymd("2026-09-04") == "20260904")
    check("日期已紧凑则不变", tc._ymd("20260904") == "20260904")
    check("None -> 空串", tc._ymd(None) == "")


def test_index_daily_sorted():
    print("[index_daily 升序 + ts_code 转换]")
    s = _install([_ok(["ts_code", "trade_date", "close"],
                      [["000300.SH", "20260904", 4548.05],
                       ["000300.SH", "20260902", 4547.96],
                       ["000300.SH", "20260903", 4552.58]])])
    df = tc.index_daily("sh000300", "2026-09-01", "2026-09-04")
    check("按 trade_date 升序", list(df["trade_date"]) == ["20260902", "20260903", "20260904"])
    check("ts_code 已转换", s.calls[0]["json"]["params"]["ts_code"] == "000300.SH")
    check("日期已归一", s.calls[0]["json"]["params"]["start_date"] == "20260901")


def test_trade_cal_iso():
    print("[trade_cal -> ISO 日期列表]")
    s = _install([_ok(["cal_date", "is_open"],
                      [["20260904", 1], ["20260902", 1], ["20260903", 1]])])
    days = tc.trade_cal("2026-09-01", "2026-09-04")
    check("升序 ISO", days == ["2026-09-02", "2026-09-03", "2026-09-04"])
    check("只问开市日", s.calls[0]["json"]["params"]["is_open"] == "1")


def test_deadline_goes_through_datasource_pool():
    print("[硬期限走 datasource 的进程级执行器]")
    _install([_ok(["a"], [[1]])])
    tc.USE_PROCESS_DEADLINE = True
    seen = {}

    stub = types.ModuleType("ashare.datasource")

    def _runner(fn, args, kwargs, deadline):
        seen["deadline"] = deadline
        seen["fn"] = getattr(fn, "__name__", "")
        return fn(*args, **kwargs)

    stub._call_with_deadline = _runner

    def _boom(*a, **k):
        raise AssertionError("不该走 call_with_retry (它会误标东财不可用)")

    stub.call_with_retry = _boom
    # 只换 sys.modules 不够: `from . import datasource` 走的是 _handle_fromlist ——
    # 只要 ashare 包上已经有 datasource 这个**属性** (同进程里别的用例导过就有), 它就直接
    # 拿属性, 根本不看 sys.modules, 打桩被绕过 -> _post_json 被丢进真的进程池打真网络。
    # 2026-09-07: 单跑本文件绿、`pytest tests/` 红两个月, 根因就是这里。两处都要换。
    import ashare as _pkg
    saved = sys.modules.get("ashare.datasource")
    saved_attr = getattr(_pkg, "datasource", None)
    sys.modules["ashare.datasource"] = stub
    _pkg.datasource = stub
    try:
        df = tc.query("daily", trade_date="20260904", deadline_sec=42)
    finally:
        if saved is not None:
            sys.modules["ashare.datasource"] = saved
        else:
            sys.modules.pop("ashare.datasource", None)
        if saved_attr is not None:
            _pkg.datasource = saved_attr
        else:
            try:
                del _pkg.datasource
            except AttributeError:
                pass
        tc.USE_PROCESS_DEADLINE = False
    check("结果正常", len(df) == 1)
    check("走的是 _call_with_deadline", seen.get("fn") == "_post_json")
    check("期限透传", seen.get("deadline") == 42)


def test_post_json_layer():
    print("[_post_json 传输层]")
    tc._SESSION = FakeSession([FakeResp({"code": 0, "data": {"fields": [], "items": []}})])
    check("200 返回 JSON", tc._post_json("https://x/", {"a": 1}, 5)["code"] == 0)
    tc._SESSION = FakeSession([FakeResp({}, status=503)])
    try:
        tc._post_json("https://x/", {}, 5)
        check("5xx 应抛 TushareTransport", False)
    except tc.TushareTransport:
        check("5xx -> TushareTransport", True)
    tc._SESSION = FakeSession([FakeResp(ValueError("no json"), status=200, text="<html>502</html>")])
    try:
        tc._post_json("https://x/", {}, 5)
        check("非 JSON 应抛 TushareTransport", False)
    except tc.TushareTransport as e:
        check("非 JSON -> TushareTransport", "非 JSON" in str(e))


TESTS = [test_query_ok, test_empty_result_keeps_columns, test_classify_order,
         test_permission_not_retried, test_transient_retry_then_success,
         test_rate_limited_backoff_and_exhaust, test_network_exception_is_transient,
         test_no_token, test_token_bucket, test_helpers, test_index_daily_sorted,
         test_trade_cal_iso, test_deadline_goes_through_datasource_pool, test_post_json_layer]


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:                  # noqa: BLE001
            pass
    import time as _t
    _real_sleep = _t.sleep
    for t in TESTS:
        tc.time.sleep = _real_sleep
        t()
    tc.time.sleep = _real_sleep
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


def test_trade_cal_filters_is_open_locally_even_when_server_ignores_the_param(monkeypatch):
    """镜像 tl.kaixin8.top (2026-10-10 起) 对 start==end 的单日查询不套 is_open=1, 把 is_open=0 的行
    原样返回; trade_cal 必须在本地再过滤, 否则周六被当成开市日 (就绪闸门等 60 分钟拒发)。"""
    import pandas as pd
    from ashare import tushare_client as tc

    def fake_query(api, **kw):
        assert api == "trade_cal" and kw.get("is_open") == "1"
        return pd.DataFrame({"cal_date": ["20261009", "20261010"], "is_open": [1, 0]})

    monkeypatch.setattr(tc, "query", fake_query)
    assert tc.trade_cal("2026-10-09", "2026-10-10") == ["2026-10-09"]
    # 单日查询只回 is_open=0 的那一行 -> 空
    monkeypatch.setattr(tc, "query", lambda api, **kw: pd.DataFrame({"cal_date": ["20261010"], "is_open": [0]}))
    assert tc.trade_cal("2026-10-10", "2026-10-10") == []
    # is_open 列缺失时照旧不过滤 (只能信服务端)
    monkeypatch.setattr(tc, "query", lambda api, **kw: pd.DataFrame({"cal_date": ["20261012"]}))
    assert tc.trade_cal("2026-10-12", "2026-10-12") == ["2026-10-12"]


# ---------------------------------------------------------------------------
# 备用基址自动切换 (2026-10-12 卡 TS-FALLBACK; 同日第二轮 TS-FALLBACK-2 按校验员 1 MEDIUM + 5 LOW 回修)。
# pytest 风格 (monkeypatch / caplog), 不联网, 不出现任何真 token。
# 背景: 10-10 换到镜像 tl.kaixin8.top 后老板另给了备用站 (同一 key, 15000 积分档); 旧镜像的 token 10-08 起 code=2002。
import datetime as _dt
import logging
import pytest

PRIMARY = "https://primary.invalid/tushare/sdk"
BACKUP = "https://backup.invalid/api/tushare"
_FO_INITIAL = tc._FO_INITIAL


@pytest.fixture(autouse=True)
def _failover_clean():
    """每条用例后把切换状态与 sleep 桩复位 (pytest 下用例之间没有别的清理钩子)。"""
    yield
    tc.failover_reset()
    tc.time.sleep = _REAL_SLEEP


def _install_fo(script, backup=BACKUP):
    """假 session + 三元组凭据 (含备用) + 切换状态复位 + 退避 sleep 打桩 -> (session, naps)。"""
    s = _install(script)
    tc._CRED = (TOKEN, PRIMARY, backup)
    tc.failover_reset()
    return s, _no_sleep()


def _fo_session(script):
    """只换剧本, 不碰凭据与切换状态 (验证「切换后余下调用」)。"""
    tc._SESSION = FakeSession(script)
    return tc._SESSION


def _urls(s):
    return [c["url"] for c in s.calls]


def _switch_lines(caplog):
    return [r for r in caplog.records if "基址切换" in r.getMessage()]


def _return_lines(caplog):
    return [r for r in caplog.records if "基址回切" in r.getMessage()]


def _cal_ok(day="20261012"):
    return _ok(["cal_date", "is_open"], [[day, 1]])


def _is_probe(call):
    """试探主站那一次: 单日 trade_cal, 单次, 试探自己的 socket 超时。"""
    j = call["json"]
    return (j["api_name"] == "trade_cal" and j["params"]["start_date"] == j["params"]["end_date"]
            and j["fields"] == "cal_date,is_open" and call["timeout"] == tc.PROBE_TIMEOUT_SEC)


def test_failover_after_three_transport_failures_on_primary(caplog):
    """主站连续 3 次 5xx → 切备用; 触发切换的那次调用在备用上重来并成功 (不退避); 之后的调用直接走备用。
    journal 只有一行 INFO, 只写 host (不写完整地址, 更不写 token)。"""
    s, naps = _install_fo([FakeResp({}, status=502), FakeResp({}, status=503), FakeResp({}, status=502),
                           _cal_ok()])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df = tc.query("trade_cal", exchange="SSE")
    assert len(df) == 1
    assert _urls(s) == [PRIMARY, PRIMARY, PRIMARY, BACKUP]
    assert naps == [tc.BACKOFF_SEC, tc.BACKOFF_SEC * 2]        # 主站两次退避; 切换瞬间不退避
    assert s.calls[3]["json"]["token"] == TOKEN                 # 备用站用同一个 key
    st = tc.failover_state()
    assert st["active"] is True and "连续 3 次传输层失败" in st["reason"] and TOKEN not in st["reason"]
    assert st["switches"] == 1 and st["calls_on_backup"] == 0 and st["last_probe_at"] is not None
    lines = _switch_lines(caplog)
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    msg = lines[0].getMessage()
    assert "tushare 基址切换: 主站 primary.invalid → 备用 backup.invalid (原因: " in msg
    assert "/tushare/sdk" not in msg and "/api/tushare" not in msg and TOKEN not in caplog.text
    # 本进程余下调用走备用, 主站不再碰 (到 20 次 / 10 分钟之前不试探), 也不再打切换行
    s2 = _fo_session([_ok(["a"], [[1]])])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        tc.query("daily", trade_date="20261012")
    assert _urls(s2) == [BACKUP] and len(_switch_lines(caplog)) == 1 and not _return_lines(caplog)


def test_failover_counter_spans_calls_and_resets_on_any_primary_reply(monkeypatch):
    """连续计数跨调用累计 (retries=1 的多次调用也能触发); 5xx / 连接错 / 非 JSON 三种传输层失败都算同一个计数;
    主站任何一次 JSON 应答 (哪怕是超频错误码) 都把计数清零 —— 要的是「连续」。硬期限超时**另计** (见下一条)。"""
    s, _ = _install_fo([FakeResp({}, status=502)])
    with pytest.raises(tc.TushareTransport):
        tc.query("daily", retries=1)
    _fo_session([OSError("Connection refused")])
    with pytest.raises(tc.TushareTransport):
        tc.query("daily", retries=1)
    assert tc.failover_state()["fails"] == 2 and tc.failover_state()["active"] is False
    _fo_session([_err("抱歉，您每分钟最多访问该接口500次")])            # 超频 = 主站传输层正常
    with pytest.raises(tc.TushareRateLimited):
        tc.query("daily", retries=1)
    assert tc.failover_state()["fails"] == 0 and tc.failover_state()["active"] is False
    _fo_session([FakeResp(ValueError("no json"), status=200, text="<html>502</html>")])
    with pytest.raises(tc.TushareTransport):
        tc.query("daily", retries=1)
    assert tc.failover_state()["fails"] == 1
    # 硬期限超时不进这个计数 (单独计): 中间夹一次超时, fails 仍是 1, timeouts 变 1
    real_call = tc._deadline_call

    def timeout_on_primary(fn, args, kwargs, dl):
        if args[0] == PRIMARY:
            raise TimeoutError("_post_json 超过 42s 硬期限 (工作进程已终止)")
        return real_call(fn, args, kwargs, dl)

    monkeypatch.setattr(tc, "_deadline_call", timeout_on_primary)
    _fo_session([])
    with pytest.raises(tc.TushareTransport):
        tc.query("daily", retries=1, deadline_sec=42)
    assert tc.failover_state()["fails"] == 1 and tc.failover_state()["timeouts"] == 1
    assert tc.failover_state()["active"] is False
    monkeypatch.setattr(tc, "_deadline_call", real_call)
    _fo_session([FakeResp({}, status=502)])
    with pytest.raises(tc.TushareTransport):
        tc.query("daily", retries=1)
    assert tc.failover_state()["fails"] == 2 and tc.failover_state()["active"] is False
    s = _fo_session([FakeResp({}, status=502), _ok(["a"], [[1]])])      # 第 3 次 → 切换, 同一次调用在备用上成功
    df = tc.query("daily", retries=1)
    assert len(df) == 1 and _urls(s) == [PRIMARY, BACKUP]
    st = tc.failover_state()
    assert st["active"] is True and "连续 3 次传输层失败" in st["reason"] and "HTTP 502" in st["reason"]


def test_hard_deadline_timeouts_counted_separately_with_higher_threshold(monkeypatch, caplog):
    """进程级硬期限超时**单独计**, 阈值 FAILOVER_AFTER_TIMEOUTS=6 (校验员 LOW: datasource 的 spawn 池 akshare/Tushare 共用,
    别人的超时 terminate 整池, 在途的主站请求一起被记成超时 —— 一次池重置顶多连带几条, 3 次就切会误切)。
    5 次超时不切; 2 次传输层失败 + 5 次超时也不切 (两个阈值各算各的); 第 6 次超时切, 原因写「硬期限超时」;
    主站一次 JSON 应答把两个计数都清零。"""
    s, _ = _install_fo([])
    real_call = tc._deadline_call

    def timeout_on_primary(fn, args, kwargs, dl):
        if args[0] == PRIMARY:
            raise TimeoutError("_post_json 超过 42s 硬期限 (工作进程已终止)")
        return real_call(fn, args, kwargs, dl)

    monkeypatch.setattr(tc, "_deadline_call", timeout_on_primary)
    for _ in range(5):
        with pytest.raises(tc.TushareTransport):
            tc.query("daily", retries=1, deadline_sec=42)
    st = tc.failover_state()
    assert st == {**_FO_INITIAL, "timeouts": 5} and len(s.calls) == 0        # 超时在执行器层, 假 session 没被碰
    monkeypatch.setattr(tc, "_deadline_call", real_call)
    _fo_session([FakeResp({}, status=502), FakeResp({}, status=503)])
    for _ in range(2):
        with pytest.raises(tc.TushareTransport):
            tc.query("daily", retries=1)
    assert tc.failover_state()["fails"] == 2 and tc.failover_state()["timeouts"] == 5
    assert tc.failover_state()["active"] is False                              # 2 + 5 都没到各自阈值
    monkeypatch.setattr(tc, "_deadline_call", timeout_on_primary)
    s = _fo_session([_ok(["a"], [[1]])])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df = tc.query("daily", retries=1, deadline_sec=42)                     # 第 6 次超时 → 切, 本次在备用成功
    assert len(df) == 1 and _urls(s) == [BACKUP]
    st = tc.failover_state()
    assert st["active"] is True and "连续 6 次硬期限超时" in st["reason"] and "42s 硬期限" in st["reason"]
    assert len(_switch_lines(caplog)) == 1
    # 清零: 回到初始态后 3 次超时 + 主站一次 JSON 应答 (业务错误码) → timeouts 归零
    tc.failover_reset()
    for _ in range(3):
        with pytest.raises(tc.TushareTransport):
            tc.query("daily", retries=1, deadline_sec=42)
    assert tc.failover_state()["timeouts"] == 3
    monkeypatch.setattr(tc, "_deadline_call", real_call)
    _fo_session([_err("抱歉，您没有接口访问权限", code=40203)])
    with pytest.raises(tc.TushareNoPermission):
        tc.query("daily", retries=1)
    assert tc.failover_state()["timeouts"] == 0 and tc.failover_state()["fails"] == 0


def test_http_429_is_rate_limit_backoff_not_a_transport_failure(caplog):
    """网关层 HTTP 429 (校验员 LOW): 按超频处理 —— 线性退避 12s/24s 重试, 抛 TushareRateLimited; **不计入切换也不清零**;
    没配备用时同样 (429 以前被当传输错误 2s/4s 退避并抛 TushareTransport, 这是本轮刻意改的)。"""
    s, naps = _install_fo([FakeResp({}, status=429), FakeResp({}, status=429), _ok(["a"], [[1]])])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df = tc.query("daily")
    assert len(df) == 1 and _urls(s) == [PRIMARY] * 3
    assert naps == [tc.BACKOFF_RATE_SEC, tc.BACKOFF_RATE_SEC * 2]
    assert tc.failover_state() == _FO_INITIAL and not _switch_lines(caplog)
    assert [r for r in caplog.records if "限流" in r.getMessage() and "HTTP 429" in r.getMessage()]
    # 已有 2 次传输层失败, 来一个 429: 既不加到 3 (不切), 也不清零
    s, naps = _install_fo([FakeResp({}, status=502)] * 2 + [FakeResp({}, status=429)])
    for _ in range(2):
        with pytest.raises(tc.TushareTransport):
            tc.query("daily", retries=1)
    with pytest.raises(tc.TushareRateLimited):
        tc.query("daily", retries=1)
    assert tc.failover_state()["fails"] == 2 and tc.failover_state()["active"] is False
    # 429 耗尽 → TushareRateLimited (没配备用也一样)
    s, naps = _install_fo([FakeResp({}, status=429)] * 3, backup="")
    with pytest.raises(tc.TushareRateLimited) as ei:
        tc.query("daily")
    assert ei.value.code == 429 and _urls(s) == [PRIMARY] * 3 and naps == [tc.BACKOFF_RATE_SEC, tc.BACKOFF_RATE_SEC * 2]
    # 传输层本身: 429 抛的是 TushareRateLimited, 其它非 200 仍是 TushareTransport
    tc._SESSION = FakeSession([FakeResp({}, status=429), FakeResp({}, status=503)])
    with pytest.raises(tc.TushareRateLimited):
        tc._post_json(PRIMARY, {"token": TOKEN}, 5)
    with pytest.raises(tc.TushareTransport):
        tc._post_json(PRIMARY, {"token": TOKEN}, 5)


@pytest.mark.parametrize("code,msg", [(2002, "token已过期"), (40101, "token无效"), ("2002", "token已过期"),
                                      (2002, "认证失败"), (40101, "Unauthorized"),       # 只看码, 文案没有 token 字样
                                      (-1, "抱歉，您输入的TOKEN无效，请确认后重试。"),   # 只看文案, 码不在表里
                                      (40101, "抱歉，您的token无效，没有接口访问权限"),  # 配了备用: token 文案压过权限文案
                                      (2002, "token已过期，请确认您的积分")])
def test_token_error_on_primary_switches_immediately(code, msg, caplog):
    """主站返回 token 失效类错误 (code 2002 / 40101, 字符串码也认, 或文案 token+无效/过期) → 不等 3 次、不退避, 立即切备用。
    配了备用时 token 失效判在权限/参数文案之前 (没配备用时则按 7b92bb6 顺序, 见 test_classify_without_backup_matches_7b92bb6)。"""
    s, naps = _install_fo([_err(msg, code=code), _cal_ok()])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df = tc.query("trade_cal", exchange="SSE")
    assert len(df) == 1 and _urls(s) == [PRIMARY, BACKUP]
    assert naps == []
    st = tc.failover_state()
    assert st["active"] is True and "token 失效类错误" in st["reason"] and f"code={code}" in st["reason"]
    assert len(_switch_lines(caplog)) == 1 and TOKEN not in caplog.text


def test_business_errors_do_not_trigger_failover(caplog):
    """无权限 40203 (15000 档权限比主站少, 切过去只会更糟) / 必填参数 / 超频 → 不切换; 下一次调用仍打主站。"""
    s, naps = _install_fo([_err("抱歉，您没有接口访问权限，请确认您的积分", code=40203)])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        with pytest.raises(tc.TushareNoPermission):
            tc.query("fina_indicator_vip", period="20250630")
        assert _urls(s) == [PRIMARY]
        s = _fo_session([_err("必填参数 标的", code=40001)])
        with pytest.raises(tc.TushareBadParams):
            tc.query("forecast")
        assert _urls(s) == [PRIMARY]
        s = _fo_session([_err("抱歉，您每分钟最多访问该接口500次")] * 3)
        with pytest.raises(tc.TushareRateLimited):
            tc.query("daily")
        assert _urls(s) == [PRIMARY] * 3
        s = _fo_session([_ok(["a"], [[1]])])
        tc.query("daily")
    assert _urls(s) == [PRIMARY]
    assert tc.failover_state() == _FO_INITIAL and not _switch_lines(caplog)


@pytest.mark.parametrize("backup", ["", BACKUP])
def test_unknown_api_and_invalid_params_are_permanent_not_retried(backup):
    """kaixin8 的两种文案 (10-13 卡 TS-PROBE 发现, 以前被当传输错误白重试 3 次): 「[unknown_api] 接口不存在或未开放」(code 404)
    → NoPermission, 「[invalid_params] 业务参数缺失或格式不合法」(code 400) → BadParams; 一次就抛, 不退避; 有没有备用都一样;
    它们是 JSON 应答, 不触发切换 (反而清零)。"""
    s, naps = _install_fo([_err("[unknown_api] 接口不存在或未开放", code=404)] * 3, backup=backup)
    with pytest.raises(tc.TushareNoPermission) as ei:
        tc.query("fina_indicator_vip", period="20250630")
    assert ei.value.code == 404 and len(s.calls) == 1 and naps == []
    s, naps = _install_fo([FakeResp({}, status=502),
                           _err("[invalid_params] 业务参数缺失或格式不合法", code=400)] * 2, backup=backup)
    with pytest.raises(tc.TushareBadParams) as ei:
        tc.query("index_classify", level="L1", src="SW2021")
    assert ei.value.code == 400 and len(s.calls) == 2 and naps == [tc.BACKOFF_SEC]
    assert tc.failover_state() == _FO_INITIAL                   # 502 计了 1 次, JSON 应答清零 (没备用则根本不计)


# (code, msg, 类名, 异常文本): 由 `git show 7b92bb6:ashare/tushare_client.py` 的 _classify 实跑得出 (2026-10-12),
# 7b92bb6 = 备用基址机制之前的最后一版。没配备用时 _classify 必须与它**逐字节一致** (校验员 LOW「没配备用键一切照旧」说过头:
# 6d2da19 把 token 判到了权限/参数之前, 「token无效, 没有接口访问权限」从 NoPermission×1 变成 BadToken×3)。
_CLASSIFY_7B92BB6 = [
    (40101, "抱歉，您的token无效，没有接口访问权限", "TushareNoPermission", "daily: 无权限/积分不足 (抱歉，您的token无效，没有接口访问权限)"),
    (2002, "token已过期，请确认您的积分", "TushareNoPermission", "daily: 无权限/积分不足 (token已过期，请确认您的积分)"),
    (-1, "token无效: invalid param", "TushareBadParams", "daily: 参数不合法 (token无效: invalid param)"),
    (2002, "token已过期", "TushareTransport", "daily: code=2002 token已过期"),
    (40203, "抱歉，您没有接口访问权限", "TushareNoPermission", "daily: 无权限/积分不足 (抱歉，您没有接口访问权限)"),
    (40101, "token无效", "TushareTransport", "daily: code=40101 token无效"),
    ("2002", "token已过期", "TushareTransport", "daily: code=2002 token已过期"),
    (2002, "认证失败", "TushareTransport", "daily: code=2002 认证失败"),
    (40101, "Unauthorized", "TushareTransport", "daily: code=40101 Unauthorized"),
    (-1, "抱歉，您输入的TOKEN无效，请确认后重试。", "TushareTransport", "daily: code=-1 抱歉，您输入的TOKEN无效，请确认后重试。"),
    (-1, "抱歉，您每分钟最多访问该接口500次，权限的具体详情访问：https://tushare.pro/document/1", "TushareRateLimited",
     "daily: 超频 (抱歉，您每分钟最多访问该接口500次，权限的具体详情访问：https://tushare.pro/document/1)"),
    (-1, "必填参数 标的 不能为空", "TushareBadParams", "daily: 参数不合法 (必填参数 标的 不能为空)"),
    (-1, "抱歉，您没有接口访问权限，请确认积分", "TushareNoPermission", "daily: 无权限/积分不足 (抱歉，您没有接口访问权限，请确认积分)"),
    (-1, "系统内部错误", "TushareTransport", "daily: code=-1 系统内部错误"),
    (2001, "认证失败", "TushareTransport", "daily: code=2001 认证失败"),
    (40203, "token 字样但只是说明: 请检查权限", "TushareNoPermission", "daily: 无权限/积分不足 (token 字样但只是说明: 请检查权限)"),
    (None, None, "TushareTransport", "daily: code=None "),
    (0, "", "TushareTransport", "daily: code=0 "),
    (-1, "每小时最多访问该接口 100 次", "TushareRateLimited", "daily: 超频 (每小时最多访问该接口 100 次)"),
    (-1, "抽取失败", "TushareRateLimited", "daily: 超频 (抽取失败)"),
    (-1, "rate limit exceeded", "TushareRateLimited", "daily: 超频 (rate limit exceeded)"),
    (-1, "缺少参数 ts_code", "TushareBadParams", "daily: 参数不合法 (缺少参数 ts_code)"),
    (-1, "invalid param: ts_code", "TushareBadParams", "daily: 参数不合法 (invalid param: ts_code)"),
    (-1, "permission denied", "TushareNoPermission", "daily: 无权限/积分不足 (permission denied)"),
    (-1, "该接口未开通", "TushareNoPermission", "daily: 无权限/积分不足 (该接口未开通)"),
    (-1, "积分不足", "TushareNoPermission", "daily: 无权限/积分不足 (积分不足)"),
]


@pytest.mark.parametrize("code,msg,cls,text", _CLASSIFY_7B92BB6)
def test_classify_without_backup_matches_7b92bb6(code, msg, cls, text):
    """没配备用 (默认 with_backup=False): 类型与异常文本与 7b92bb6 版逐字节一致, 顺序 超频 → 参数 → 权限 → 传输。"""
    e = tc._classify("daily", code, msg)
    assert type(e).__name__ == cls and str(e) == text and e.code == code and e.api == "daily"
    assert not isinstance(e, tc.TushareBadToken)               # 没配备用时这个子类根本不出现


@pytest.mark.parametrize("code,msg,exc,n", [
    (40101, "抱歉，您的token无效，没有接口访问权限", "TushareNoPermission", 1),
    (2002, "token已过期，请确认您的积分", "TushareNoPermission", 1),
    (-1, "token无效: invalid param", "TushareBadParams", 1),
    (2002, "token已过期", "TushareTransport", 3),
])
def test_without_backup_request_counts_match_7b92bb6(code, msg, exc, n):
    """校验员场景 L 的三种形态 + 10-08 实际形态: 没配备用时请求次数 / 退避 / 异常类型 (精确类, 不是子类) 与 7b92bb6 版一样。"""
    s, naps = _install_fo([_err(msg, code=code)] * 3, backup="")
    with pytest.raises(tc.TushareError) as ei:
        tc.query("daily")
    assert type(ei.value).__name__ == exc and len(s.calls) == n
    assert naps == ([tc.BACKOFF_SEC, tc.BACKOFF_SEC * 2] if n == 3 else [])
    assert tc.failover_state() == _FO_INITIAL


def test_bad_backup_three_failures_then_primary_probe_returns_and_call_retries_on_primary(caplog):
    """校验员 MEDIUM 的后半句: 坏备用不得把主站一次抖动放大成整轮停摆 —— 已在备用上, 备用连续 3 次失败 → 立即试探主站
    (单日 trade_cal, 单次, PROBE_TIMEOUT_SEC) → 成功即回切, **本次调用在主站重来一轮**并成功; 打一行「基址回切」INFO。"""
    s, naps = _install_fo([_err("token已过期", code=2002), _ok(["a"], [[1]])])
    tc.query("daily")                                           # 主站 2002 → 切备用, 备用成功
    assert tc.failover_state()["active"] is True
    naps.clear()
    s = _fo_session([FakeResp({}, status=502)] * 3 + [_cal_ok("20261013"), _ok(["a"], [[1]])])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df = tc.query("daily")
    assert len(df) == 1
    assert _urls(s) == [BACKUP] * 3 + [PRIMARY, PRIMARY]        # 备用三连败 → 试探主站 → 回切 → 本次在主站重来一轮
    assert _is_probe(s.calls[3]) and s.calls[3]["json"]["token"] == TOKEN
    assert not _is_probe(s.calls[4]) and s.calls[4]["json"]["api_name"] == "daily"
    assert naps == [tc.BACKOFF_SEC, tc.BACKOFF_SEC * 2]        # 备用上两次退避; 回切瞬间不退避
    st = tc.failover_state()
    assert st["active"] is False and st["backup_fails"] == 0 and st["fails"] == 0 and st["timeouts"] == 0
    assert st["probes"] == 1 and st["returns"] == 1 and st["switches"] == 1 and st["probing"] is False
    lines = _return_lines(caplog)
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    msg = lines[0].getMessage()
    assert "tushare 基址回切: 备用 backup.invalid → 主站 primary.invalid (原因: 备用连续 3 次失败" in msg
    assert "主站试探成功" in msg and "/api/tushare" not in msg and TOKEN not in caplog.text
    # 回到主站之后的调用直接打主站
    s = _fo_session([_ok(["a"], [[1]])])
    tc.query("daily")
    assert _urls(s) == [PRIMARY]


def test_backup_failures_count_consecutively_across_calls_and_timeouts_count_too(monkeypatch):
    """备用的连续失败也跨调用累计 (retries=1 的三次调用也会触发试探), 硬期限超时在备用上照计;
    备用一次 JSON 应答 (哪怕业务错误) 把 backup_fails 清零。"""
    s, _ = _install_fo([_err("token已过期", code=2002), _ok(["a"], [[1]])])
    tc.query("daily")
    assert tc.failover_state()["active"] is True
    _fo_session([FakeResp({}, status=502)])
    with pytest.raises(tc.TushareTransport):
        tc.query("daily", retries=1)
    _fo_session([_err("抱歉，您没有接口访问权限", code=40203)])  # 备用答了 JSON → 清零
    with pytest.raises(tc.TushareNoPermission):
        tc.query("daily", retries=1)
    assert tc.failover_state()["backup_fails"] == 0 and tc.failover_state()["active"] is True
    _fo_session([FakeResp({}, status=502)])
    with pytest.raises(tc.TushareTransport):
        tc.query("daily", retries=1)
    real_call = tc._deadline_call

    def timeout_on_backup(fn, args, kwargs, dl):
        if args[0] == BACKUP:
            raise TimeoutError("_post_json 超过 42s 硬期限 (工作进程已终止)")
        return real_call(fn, args, kwargs, dl)

    monkeypatch.setattr(tc, "_deadline_call", timeout_on_backup)
    with pytest.raises(tc.TushareTransport):
        tc.query("daily", retries=1, deadline_sec=42)
    assert tc.failover_state()["backup_fails"] == 2 and tc.failover_state()["probes"] == 0
    monkeypatch.setattr(tc, "_deadline_call", real_call)
    s = _fo_session([FakeResp({}, status=502), _cal_ok(), _ok(["a"], [[1]])])   # 第 3 次 → 试探主站 → 回切 → 本次在主站成功
    df = tc.query("daily", retries=1)
    assert len(df) == 1 and _urls(s) == [BACKUP, PRIMARY, PRIMARY] and _is_probe(s.calls[1])
    assert tc.failover_state()["active"] is False and tc.failover_state()["returns"] == 1


def test_both_sites_dead_stays_on_backup_no_pingpong(caplog):
    """两站都不可用: 主站 3 次 → 切备用 → 备用 3 次 → 试探主站 (也坏) → 留在备用并抛 TushareTransport;
    打一行 WARNING「两站都不可用」; 不回切、不乒乓; 之后每满 3 次备用失败再试探一次主站。"""
    s, naps = _install_fo([FakeResp({}, status=502)] * 7)
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        with pytest.raises(tc.TushareTransport):
            tc.query("daily")
    assert _urls(s) == [PRIMARY] * 3 + [BACKUP] * 3 + [PRIMARY] and _is_probe(s.calls[6])
    assert naps == [tc.BACKOFF_SEC, tc.BACKOFF_SEC * 2] * 2
    st = tc.failover_state()
    assert st["active"] is True and st["probes"] == 1 and st["returns"] == 0 and st["backup_fails"] == 0
    warn = [r for r in caplog.records if "试探失败" in r.getMessage()]
    assert len(warn) == 1 and warn[0].levelno == logging.WARNING and "继续用备用 backup.invalid" in warn[0].getMessage()
    assert len(_switch_lines(caplog)) == 1 and not _return_lines(caplog)
    s = _fo_session([FakeResp({}, status=502)] * 4)
    with pytest.raises(tc.TushareTransport):
        tc.query("daily")
    assert _urls(s) == [BACKUP] * 3 + [PRIMARY] and _is_probe(s.calls[3])
    assert tc.failover_state()["active"] is True and tc.failover_state()["probes"] == 2
    # 备用上的权限错误照旧不重试、不切
    s = _fo_session([_err("抱歉，您没有接口访问权限", code=40203)])
    with pytest.raises(tc.TushareNoPermission):
        tc.query("daily")
    assert _urls(s) == [BACKUP]


def test_at_most_two_rounds_per_call_even_if_primary_recovers_midway():
    """一次调用最多两轮: 主站 3 次 → 备用 3 次 → 试探主站成功 → 状态回主站, 但本次调用**不来第三轮**, 抛备用那一轮的错;
    下一次调用直接打主站。(防两站都抖时一次调用无限乒乓; 最坏墙钟因此有界: 2×366 + 2×30 s)"""
    s, naps = _install_fo([FakeResp({}, status=502)] * 6 + [_cal_ok()])
    with pytest.raises(tc.TushareTransport) as ei:
        tc.query("daily")
    assert _urls(s) == [PRIMARY] * 3 + [BACKUP] * 3 + [PRIMARY] and _is_probe(s.calls[6])
    assert "HTTP 502" in str(ei.value) and naps == [tc.BACKOFF_SEC, tc.BACKOFF_SEC * 2] * 2
    st = tc.failover_state()
    assert st["active"] is False and st["returns"] == 1 and st["switches"] == 1
    s = _fo_session([_ok(["a"], [[1]])])
    tc.query("daily")
    assert _urls(s) == [PRIMARY]


def test_on_backup_probe_primary_every_20_calls_and_return(caplog):
    """校验员 MEDIUM「不回切」: 在备用上每 PROBE_EVERY_CALLS=20 次调用试探主站一次 (轻量单日 trade_cal, 单次);
    前 19 次不碰主站; 第 20 次先试探, 成功即回切 (INFO「基址回切 … 在备用 20 次调用」), 本次调用直接打主站; 之后计数从头。"""
    s, naps = _install_fo([FakeResp({}, status=502)] * 3 + [_ok(["a"], [[1]])])
    tc.query("daily")
    assert tc.failover_state()["active"] is True
    s = _fo_session([_ok(["a"], [[1]])] * 19)
    for _ in range(19):
        tc.query("daily")
    assert _urls(s) == [BACKUP] * 19
    assert tc.failover_state()["calls_on_backup"] == 19 and tc.failover_state()["probes"] == 0
    s = _fo_session([_cal_ok("20261013"), _ok(["a"], [[1]])])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df = tc.query("daily")
    assert len(df) == 1 and _urls(s) == [PRIMARY, PRIMARY] and _is_probe(s.calls[0]) and not _is_probe(s.calls[1])
    st = tc.failover_state()
    assert st["active"] is False and st["probes"] == 1 and st["returns"] == 1 and st["calls_on_backup"] == 0
    assert st["fails"] == 0 and st["timeouts"] == 0 and st["backup_fails"] == 0
    lines = _return_lines(caplog)
    assert len(lines) == 1 and "在备用 20 次调用, 主站试探成功" in lines[0].getMessage()
    assert "; 在备用 20 次调用 / " in lines[0].getMessage()        # 回切行末尾的计数在清零之前取
    assert "primary.invalid" in lines[0].getMessage() and TOKEN not in caplog.text
    # 回切后又连败 3 次会再切一次 (计数从头算, 不是「切过就不再切」)
    s = _fo_session([FakeResp({}, status=502)] * 3 + [_ok(["a"], [[1]])])
    tc.query("daily")
    assert _urls(s) == [PRIMARY] * 3 + [BACKUP] and tc.failover_state()["switches"] == 2


def test_return_resets_primary_counters_so_one_failure_does_not_reswitch():
    """回切时主站的两个连续计数必须清零: 回切 (试探成功, 试探本身不走 _note_primary_ok) 之后主站紧接着失败 1-2 次
    不能立刻再切 (要重新累计满 3 次); 否则切换前攒下的 fails=3 会让回切后的第一次失败就再切 —— 变异 M14 形态。"""
    s, naps = _install_fo([FakeResp({}, status=502)] * 3 + [_ok(["a"], [[1]])])
    tc.query("daily")
    assert tc.failover_state()["active"] is True and tc.failover_state()["fails"] == 3
    with tc._FO_LOCK:
        tc._FO["last_probe_at"] = tc.time.monotonic() - tc.PROBE_EVERY_SEC - 1
    naps.clear()
    s = _fo_session([_cal_ok(), FakeResp({}, status=502), FakeResp({}, status=503), _ok(["a"], [[1]])])
    df = tc.query("daily")                                      # 试探成功 → 回切 → 本次在主站 502, 503, 成功
    assert len(df) == 1 and _urls(s) == [PRIMARY] * 4 and _is_probe(s.calls[0])
    assert naps == [tc.BACKOFF_SEC, tc.BACKOFF_SEC * 2]
    st = tc.failover_state()
    assert st["active"] is False and st["switches"] == 1 and st["returns"] == 1 and st["fails"] == 0
    # 同样: 回切后硬期限超时也从 0 开始计
    tc.failover_reset()
    s, _ = _install_fo([_err("token已过期", code=2002), _ok(["a"], [[1]])])
    tc.query("daily")
    with tc._FO_LOCK:
        tc._FO["timeouts"] = 5                                  # 切换前攒下的超时计数 (切换时不清, 回切时必须清)
        tc._FO["last_probe_at"] = tc.time.monotonic() - tc.PROBE_EVERY_SEC - 1
    s = _fo_session([_cal_ok(), _ok(["a"], [[1]])])
    tc.query("daily")
    assert tc.failover_state()["active"] is False and tc.failover_state()["timeouts"] == 0


def test_on_backup_probe_primary_every_10_minutes_and_stay_if_probe_fails(caplog):
    """在备用上距上次试探 ≥ PROBE_EVERY_SEC=600 秒也试探一次 (先到先试); 试探失败 (主站仍 5xx) → 留在备用, 打一行 INFO
    「试探失败 … 继续用备用」, 本次调用照走备用; 计时与计数从头 → 紧接着的调用不再试探; 试探 code≠0 (token 仍失效) 也不算恢复。"""
    s, naps = _install_fo([FakeResp({}, status=502)] * 3 + [_ok(["a"], [[1]])])
    tc.query("daily")
    assert tc.failover_state()["active"] is True
    with tc._FO_LOCK:
        tc._FO["last_probe_at"] = tc.time.monotonic() - tc.PROBE_EVERY_SEC - 1     # 当成已在备用上 10 分钟
    s = _fo_session([FakeResp({}, status=502), _ok(["a"], [[1]])])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df = tc.query("daily")
    assert len(df) == 1 and _urls(s) == [PRIMARY, BACKUP] and _is_probe(s.calls[0])
    st = tc.failover_state()
    assert st["active"] is True and st["probes"] == 1 and st["returns"] == 0 and st["calls_on_backup"] == 0
    assert tc.time.monotonic() - st["last_probe_at"] < 60
    lines = [r for r in caplog.records if "试探失败" in r.getMessage()]
    assert len(lines) == 1 and lines[0].levelno == logging.INFO and "在备用 6" in lines[0].getMessage()
    assert "继续用备用 backup.invalid" in lines[0].getMessage() and not _return_lines(caplog)
    s = _fo_session([_ok(["a"], [[1]])])
    tc.query("daily")
    assert _urls(s) == [BACKUP]                                 # 刚试探过: 不再试探
    # 试探回的是 JSON 但 code≠0 (token 在主站仍失效) → 不算恢复
    with tc._FO_LOCK:
        tc._FO["last_probe_at"] = tc.time.monotonic() - tc.PROBE_EVERY_SEC - 1
    s = _fo_session([_err("token已过期", code=2002), _ok(["a"], [[1]])])
    tc.query("daily")
    assert _urls(s) == [PRIMARY, BACKUP] and tc.failover_state()["active"] is True
    assert tc.failover_state()["probes"] == 2


def test_probe_runs_once_under_concurrency():
    """多线程同时到点: 只有一个线程试探 (probing 位子), 其余照走备用; 回切只打一次。"""
    import threading
    s, _ = _install_fo([FakeResp({}, status=502)] * 3 + [_ok(["a"], [[1]])])
    tc.query("daily")
    assert tc.failover_state()["active"] is True
    with tc._FO_LOCK:
        tc._FO["last_probe_at"] = tc.time.monotonic() - tc.PROBE_EVERY_SEC - 1
    results, lock = [], threading.Lock()
    probe_seen = []

    def fake_probe(token, base):
        with lock:
            probe_seen.append(base)
        tc.time.sleep(0)                                        # 让别的线程有机会撞进来
        return True, "trade_cal 20261013 应答正常 0.1s"

    real = tc._probe_primary
    tc._probe_primary = fake_probe
    try:
        def worker():
            r = tc._maybe_probe_primary(TOKEN, PRIMARY, BACKUP)
            with lock:
                results.append(r)
        ts = [threading.Thread(target=worker) for _ in range(16)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
    finally:
        tc._probe_primary = real
    assert len(probe_seen) == 1 and results.count(True) == 1 and len(results) == 16
    assert tc.failover_state()["active"] is False and tc.failover_state()["returns"] == 1


def test_backup_failure_raises_like_before_and_never_flaps_back_when_primary_dead():
    """备用也坏 (主站也坏) → 备用上一轮完整重试 + 退避后抛 TushareTransport; 主站试探不通就不回切;
    备用上的权限错误也按原逻辑不重试; 备用上的 token 失效 (配了备用 → BadToken ⊂ Transport) 按传输层重试后抛。"""
    s, naps = _install_fo([FakeResp({}, status=502)] * 7)
    with pytest.raises(tc.TushareTransport):
        tc.query("daily")
    assert _urls(s) == [PRIMARY] * 3 + [BACKUP] * 3 + [PRIMARY]
    assert naps == [tc.BACKOFF_SEC, tc.BACKOFF_SEC * 2, tc.BACKOFF_SEC, tc.BACKOFF_SEC * 2]
    assert tc.failover_state()["active"] is True
    s = _fo_session([FakeResp({}, status=502)] * 4)
    with pytest.raises(tc.TushareTransport):
        tc.query("daily")
    assert _urls(s) == [BACKUP] * 3 + [PRIMARY]
    s = _fo_session([_err("抱歉，您没有接口访问权限", code=40203)])
    with pytest.raises(tc.TushareNoPermission):
        tc.query("daily")
    assert _urls(s) == [BACKUP]
    s = _fo_session([_err("token已过期", code=2002)] * 3 + [FakeResp({}, status=502)])
    with pytest.raises(tc.TushareBadToken):                       # 备用不认这把 key: 算备用失败, 重试 3 次 → 试探主站 (坏) → 抛
        tc.query("daily")
    assert _urls(s) == [BACKUP] * 3 + [PRIMARY] and _is_probe(s.calls[3]) and tc.failover_state()["active"] is True
    # 备用不认 key 而主站已恢复: 三次 BadToken → 试探成功 → 回切 → 本次在主站重来一轮
    s = _fo_session([_err("token已过期", code=2002)] * 3 + [_cal_ok(), _ok(["a"], [[1]])])
    assert len(tc.query("daily")) == 1
    assert _urls(s) == [BACKUP] * 3 + [PRIMARY, PRIMARY] and tc.failover_state()["active"] is False


@pytest.mark.parametrize("backup", ["", None, PRIMARY])
def test_without_backup_key_nothing_changes(backup, caplog):
    """没配 tushare_base_url_backup (空 / 缺 / 与主站相同) → 不计数、不切换、不试探、不打切换行, 退避与异常与以前一样;
    2002 + 「token已过期」没备用时仍按传输层错误重试 3 次后抛, 类型就是 TushareTransport 本身 (不是 BadToken 子类)。"""
    s, naps = _install_fo([FakeResp({}, status=502)] * 3, backup=backup)
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        with pytest.raises(tc.TushareTransport):
            tc.query("daily")
        assert _urls(s) == [PRIMARY] * 3 and naps == [tc.BACKOFF_SEC, tc.BACKOFF_SEC * 2]
        assert tc.failover_state() == _FO_INITIAL
        s = _fo_session([_err("token已过期", code=2002)] * 3)
        with pytest.raises(tc.TushareTransport) as ei:
            tc.query("trade_cal")
    assert type(ei.value) is tc.TushareTransport and _urls(s) == [PRIMARY] * 3
    assert tc.failover_state() == _FO_INITIAL and not _switch_lines(caplog) and not _return_lines(caplog)
    # 旧形态 2 元组凭据 (老用例直接塞的) 等价于没配备用
    tc._CRED = (TOKEN, PRIMARY)
    assert tc.credentials() == (TOKEN, PRIMARY, "")


def test_credentials_backup_key_and_env_override(monkeypatch):
    """备用基址从 secrets.json 的 tushare_base_url_backup 读, 环境变量 TUSHARE_BASE_URL_BACKUP 覆盖;
    没这个键 → ""; refresh=True 把切换状态复位 (新凭据从主站重新开始)。"""
    for k in ("TUSHARE_TOKEN", "TUSHARE_BASE_URL", "TUSHARE_BASE_URL_BACKUP"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(tc, "_read_secrets", lambda: {"tushare_token": TOKEN, "tushare_base_url": PRIMARY,
                                                      "tushare_base_url_backup": BACKUP})
    assert tc.credentials(refresh=True) == (TOKEN, PRIMARY, BACKUP)
    monkeypatch.setenv("TUSHARE_BASE_URL_BACKUP", "https://env-backup.invalid/x")
    assert tc.credentials(refresh=True) == (TOKEN, PRIMARY, "https://env-backup.invalid/x")
    monkeypatch.delenv("TUSHARE_BASE_URL_BACKUP")
    monkeypatch.setattr(tc, "_read_secrets", lambda: {"tushare_token": TOKEN, "tushare_base_url": PRIMARY})
    assert tc.credentials(refresh=True) == (TOKEN, PRIMARY, "")
    monkeypatch.setattr(tc, "_read_secrets", lambda: {"tushare_token": TOKEN, "tushare_base_url": PRIMARY,
                                                      "tushare_base_url_backup": BACKUP})
    tc.credentials(refresh=True)
    assert tc._note_primary_token_error(PRIMARY, BACKUP, "x") is True and tc.failover_state()["active"] is True
    tc.credentials(refresh=True)
    assert tc.failover_state() == _FO_INITIAL
    assert tc.available() is True
    tc._CRED = (TOKEN, "https://example.invalid/")              # 还原老用例的凭据形态


def test_switch_decided_in_main_process_and_passed_to_post_json(monkeypatch):
    """_post_json 被 pickle 到工作进程跑: 基址由主进程选好后当**参数**传下去 (工作进程里的模块副本不参与判定)。
    用假的进程级执行器记录每次传给 _post_json 的 base; _post_json 自己不看切换状态; 仍可 pickle。"""
    seen = []
    real_call = tc._deadline_call

    def runner(fn, args, kwargs, dl):
        seen.append((getattr(fn, "__name__", ""), args[0]))
        return real_call(fn, args, kwargs, dl)

    monkeypatch.setattr(tc, "_deadline_call", runner)
    s, _ = _install_fo([FakeResp({}, status=502)] * 3 + [_ok(["a"], [[1]])])
    tc.query("daily")
    assert seen == [("_post_json", PRIMARY)] * 3 + [("_post_json", BACKUP)]
    s = _fo_session([_ok(["a"], [[1]])])
    assert tc.failover_state()["active"] is True
    tc._post_json(PRIMARY, {"api_name": "daily", "token": TOKEN, "params": {}, "fields": ""}, 5)
    assert _urls(s) == [PRIMARY]                                # 给哪个基址就打哪个, 不看状态
    import pickle
    pickle.dumps((tc._post_json, (BACKUP, {"api_name": "daily"}, 5), {}))


def test_switch_happens_once_under_concurrency():
    """多线程同时报主站失败: 只切换一次 (一个 True), 计数停在 FAILOVER_AFTER, 之后的失败不再累计。"""
    import threading
    tc._CRED = (TOKEN, PRIMARY, BACKUP)
    tc.failover_reset()
    results = []
    lock = threading.Lock()

    def worker():
        r = tc._note_primary_transport_failure(PRIMARY, BACKUP, "daily HTTP 502")
        with lock:
            results.append(r)

    ts = [threading.Thread(target=worker) for _ in range(32)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert results.count(True) == 1 and len(results) == 32
    st = tc.failover_state()
    assert st["active"] is True and st["fails"] == tc.FAILOVER_AFTER and st["switches"] == 1
    assert tc._active_base(PRIMARY, BACKUP) == (BACKUP, "backup")
    assert tc._active_base(PRIMARY, "") == (PRIMARY, None)      # 没配备用: 状态无关紧要, 永远主站, 什么都不计
    tc.failover_reset()
    assert tc._active_base(PRIMARY, BACKUP) == (PRIMARY, "primary")
    tc._CRED = (TOKEN, "https://example.invalid/")


def test_classify_token_errors():
    """配了备用 (with_backup=True): code 2002 / 40101 (含字符串码) 或文案 token+无效/过期 → TushareBadToken (仍是 Transport 子类),
    判在权限/参数文案之前; 其它文案的分类不变。没配备用: 同样的输入按 7b92bb6 顺序 (见 _CLASSIFY_7B92BB6)。"""
    e = tc._classify("trade_cal", 2002, "token已过期", with_backup=True)
    assert isinstance(e, tc.TushareBadToken) and isinstance(e, tc.TushareTransport) and e.code == 2002
    assert isinstance(tc._classify("daily", "40101", "invalid token", with_backup=True), tc.TushareBadToken)
    assert isinstance(tc._classify("daily", 2002, "认证失败", with_backup=True), tc.TushareBadToken)          # 只凭码
    assert isinstance(tc._classify("daily", "40101", "Unauthorized", with_backup=True), tc.TushareBadToken)   # 只凭码 (字符串)
    assert type(tc._classify("daily", 2001, "认证失败", with_backup=True)) is tc.TushareTransport  # 码不在表里、文案无 token
    assert isinstance(tc._classify("daily", -1, "抱歉，您输入的TOKEN无效，请确认后重试。", with_backup=True), tc.TushareBadToken)
    assert isinstance(tc._classify("daily", 40101, "抱歉，您的token无效，没有接口访问权限", with_backup=True), tc.TushareBadToken)
    assert type(tc._classify("daily", 40101, "抱歉，您的token无效，没有接口访问权限")) is tc.TushareNoPermission
    assert type(tc._classify("daily", 2002, "token已过期")) is tc.TushareTransport
    assert type(tc._classify("daily", -1, "系统内部错误", with_backup=True)) is tc.TushareTransport
    assert isinstance(tc._classify("daily", -1, "抱歉，您没有接口访问权限，请确认积分", with_backup=True), tc.TushareNoPermission)
    assert isinstance(tc._classify("daily", -1, "抱歉，您每分钟最多访问该接口500次，权限的具体详情访问：…", with_backup=True),
                      tc.TushareRateLimited)
    assert isinstance(tc._classify("daily", 40203, "token 字样但只是说明: 请检查权限", with_backup=True), tc.TushareNoPermission)
    assert isinstance(tc._classify("daily", 404, "[unknown_api] 接口不存在或未开放", with_backup=True), tc.TushareNoPermission)
    assert isinstance(tc._classify("daily", 400, "[invalid_params] 业务参数缺失或格式不合法", with_backup=True), tc.TushareBadParams)


def test_token_echoed_by_mirror_never_reaches_logs_or_exceptions(caplog):
    """镜像的 msg / 网关错误页回显请求体里的 token: 进异常文本与日志之前先抹成 <token> (先抹再截断)。"""
    s, naps = _install_fo([_err(f"bad token {TOKEN} rejected: 没有接口访问权限", code=40203)])
    with caplog.at_level(logging.DEBUG, logger="ashare.tushare"):
        with pytest.raises(tc.TushareNoPermission) as ei:
            tc.query("daily")
    assert TOKEN not in str(ei.value) and "<token>" in str(ei.value) and TOKEN not in caplog.text
    s, naps = _install_fo([_err(f"系统繁忙 {TOKEN}", code=-1)] * 3)
    with caplog.at_level(logging.DEBUG, logger="ashare.tushare"):
        with pytest.raises(tc.TushareTransport) as ei:
            tc.query("daily")
    assert TOKEN not in str(ei.value) and TOKEN not in caplog.text and TOKEN not in tc.failover_state()["reason"]
    tc._SESSION = FakeSession([FakeResp(ValueError("no json"), status=200, text="<html>" + TOKEN + "</html>")])
    with pytest.raises(tc.TushareTransport) as ei:
        tc._post_json(PRIMARY, {"api_name": "daily", "token": TOKEN, "params": {}, "fields": ""}, 5)
    assert TOKEN not in str(ei.value) and "<token>" in str(ei.value)
    # token 跨在 80 字截断边界上也抹得掉 (先抹再截断; TS-PROBE 校验员在探针里抓到的「先截断后抹」形态)
    tc._SESSION = FakeSession([FakeResp(ValueError("no json"), status=200, text="<html>" + "x" * 70 + TOKEN + "</html>")])
    with pytest.raises(tc.TushareTransport) as ei:
        tc._post_json(PRIMARY, {"api_name": "daily", "token": TOKEN, "params": {}, "fields": ""}, 5)
    assert TOKEN not in str(ei.value) and TOKEN[:10] not in str(ei.value)
    assert tc._redact(None, TOKEN) == "" and tc._redact("abc", "") == "abc"


# ---------------------------------------------------------------------------
# --probe-backup 真验收 (校验员 MEDIUM: 原验收 `pricestore ready` 在库已追平时 0 次调用, 且主站健康时请求到不了备用站)
_DAILY_COLS = tc.PROBE_DAILY_FIELDS.split(",")
_TODAY = _dt.date(2026, 10, 13)
_CAL = [["20260922", 1], ["20260923", 1], ["20260924", 1], ["20260925", 1], ["20260926", 0], ["20260927", 0],
        ["20260928", 1], ["20260929", 1], ["20260930", 1], ["20261001", 0], ["20261008", 0], ["20261009", 1],
        ["20261010", 0], ["20261012", 1], ["20261013", 1], ["20261014", 1], ["20261015", 1]]
_OPEN = sorted(f"{d[:4]}-{d[4:6]}-{d[6:]}" for d, o in _CAL if o == 1)


def _daily_rows(n):
    return [[f"{600000 + i:06d}.SH", "20261013", 1.0, 1.1, 0.9, 1.0, 100.0, 1000.0] for i in range(n)]


def _probe_setup(monkeypatch, script, backup=BACKUP):
    for k in ("TUSHARE_TOKEN", "TUSHARE_BASE_URL", "TUSHARE_BASE_URL_BACKUP"):
        monkeypatch.delenv(k, raising=False)
    sec = {"tushare_token": TOKEN, "tushare_base_url": PRIMARY}
    if backup:
        sec["tushare_base_url_backup"] = backup
    monkeypatch.setattr(tc, "_read_secrets", lambda: sec)
    s, naps = _install_fo(script)
    lines = []
    return s, naps, lines


def test_probe_backup_real_calls_and_pass_verdict(monkeypatch):
    """4 次调用: 主站 trade_cal + daily 走生产路径 query() (经切换机制), 备用同两项直调 (带同一个 token, 服务端过滤 is_open=1);
    备用日历与主站一致、daily 行数 ≥ 90% → rc 0; 输出只有 host / 行数 / 耗时, 没有 token 没有完整地址。"""
    s, naps, lines = _probe_setup(monkeypatch, [_ok(["cal_date", "is_open"], _CAL), _ok(_DAILY_COLS, _daily_rows(5000)),
                                                _ok(["cal_date", "is_open"], _CAL), _ok(_DAILY_COLS, _daily_rows(4600))])
    rc = tc.probe_backup(out=lines.append, today=_TODAY)
    text = "\n".join(lines)
    assert rc == 0, text
    assert _urls(s) == [PRIMARY, PRIMARY, BACKUP, BACKUP]
    apis = [c["json"]["api_name"] for c in s.calls]
    assert apis == ["trade_cal", "daily", "trade_cal", "daily"]
    assert s.calls[0]["json"]["params"] == {"exchange": "SSE", "start_date": "20260922", "end_date": "20261020", "is_open": "1"}
    assert s.calls[2]["json"]["params"] == s.calls[0]["json"]["params"]
    assert s.calls[1]["json"]["params"] == {"trade_date": "20261013"} == s.calls[3]["json"]["params"]
    assert s.calls[1]["json"]["fields"] == tc.PROBE_DAILY_FIELDS == s.calls[3]["json"]["fields"]
    assert all(c["json"]["token"] == TOKEN for c in s.calls)
    assert naps == [] and tc.failover_state()["active"] is False
    assert "结论 (rc 0): 备用 backup.invalid 可用" in text and f"日历一致 {len(_OPEN)} 天" in text
    assert "daily 2026-10-13 4600/5000 行" in text and "切换状态 active=False" in text
    assert TOKEN not in text and "/tushare/sdk" not in text and "/api/tushare" not in text


def test_probe_backup_fails_when_backup_rejects_key_or_disagrees(monkeypatch):
    """备用不认这把 key (2002) / 日历不一致 / daily 行数不够 / 打不通 → rc 1, 结论点名原因; 主站那两项照常。"""
    s, naps, lines = _probe_setup(monkeypatch, [_ok(["cal_date", "is_open"], _CAL), _ok(_DAILY_COLS, _daily_rows(100)),
                                                _err("token已过期", code=2002), _err("抱歉，您没有接口访问权限", code=40203)])
    rc = tc.probe_backup(out=lines.append, today=_TODAY)
    text = "\n".join(lines)
    assert rc == 1 and _urls(s) == [PRIMARY, PRIMARY, BACKUP, BACKUP] and naps == []    # 直调不重试
    assert "结论 (rc 1): 备用 backup.invalid 不可用" in text
    assert "trade_cal 打不通或不认这把 key" in text and "daily 打不通或不认这把 key" in text and TOKEN not in text
    s, naps, lines = _probe_setup(monkeypatch, [_ok(["cal_date", "is_open"], _CAL), _ok(_DAILY_COLS, _daily_rows(100)),
                                                _ok(["cal_date", "is_open"], _CAL[:-2]), _ok(_DAILY_COLS, _daily_rows(80))])
    rc = tc.probe_backup(out=lines.append, today=_TODAY)
    text = "\n".join(lines)
    assert rc == 1 and "日历与主站不一致" in text and "daily 行数 80 < 主站 100 × 0.9" in text
    s, naps, lines = _probe_setup(monkeypatch, [_ok(["cal_date", "is_open"], _CAL), _ok(_DAILY_COLS, _daily_rows(100)),
                                                OSError("Connection refused"), _ok(["ts_code", "close"], [["600000.SH", 1.0]] * 100)])
    rc = tc.probe_backup(out=lines.append, today=_TODAY)
    text = "\n".join(lines)
    assert rc == 1 and "备用 trade_cal 失败: OSError" in text and "缺列" in text


def test_probe_backup_cannot_judge_without_backup_or_when_primary_itself_fails(monkeypatch):
    """没配备用 (或与主站相同) → rc 2 并提示环境变量临时给; 没 token → rc 2; 主站 trade_cal 自己失败 → rc 2;
    验收期间主站就切到了备用 (主站 3 次 5xx) → rc 2 点名切换原因 (备用成功也不算验过)。"""
    s, naps, lines = _probe_setup(monkeypatch, [], backup="")
    assert tc.probe_backup(out=lines.append, today=_TODAY) == 2 and "TUSHARE_BASE_URL_BACKUP" in lines[-1]
    assert len(s.calls) == 0
    s, naps, lines = _probe_setup(monkeypatch, [], backup=PRIMARY)
    assert tc.probe_backup(out=lines.append, today=_TODAY) == 2 and "没配备用基址" in lines[-1]
    monkeypatch.setattr(tc, "_read_secrets", lambda: {"tushare_base_url": PRIMARY, "tushare_base_url_backup": BACKUP})
    lines = []
    assert tc.probe_backup(out=lines.append, today=_TODAY) == 2 and "没配 tushare_token" in lines[-1]
    s, naps, lines = _probe_setup(monkeypatch, [_err("抱歉，您没有接口访问权限", code=40203)])
    assert tc.probe_backup(out=lines.append, today=_TODAY) == 2 and "主站 primary.invalid trade_cal 失败: TushareNoPermission" in lines[-1]
    assert len(s.calls) == 1
    s, naps, lines = _probe_setup(monkeypatch, [FakeResp({}, status=502)] * 3 + [_ok(["cal_date", "is_open"], _CAL)])
    rc = tc.probe_backup(out=lines.append, today=_TODAY)
    assert rc == 2 and "验收期间主站就切到了备用" in lines[-1] and "连续 3 次传输层失败" in lines[-1]
    assert _urls(s) == [PRIMARY] * 3 + [BACKUP] and TOKEN not in "\n".join(lines)


def test_main_cli_arguments(monkeypatch, capsys):
    """命令行: --state 打印状态 (不联网); --probe-backup 走 probe_backup 并把退出码带回; 不带参数打帮助退 2; 不打印 token。"""
    tc._CRED = (TOKEN, PRIMARY, BACKUP)
    tc.failover_reset()
    assert tc.main(["--state"]) == 0
    out = capsys.readouterr().out
    assert "'active': False" in out and TOKEN not in out
    monkeypatch.setattr(tc, "probe_backup", lambda out=print, today=None: 1)
    assert tc.main(["--probe-backup"]) == 1
    assert tc.main([]) == 2
    assert TOKEN not in capsys.readouterr().out
    tc._CRED = (TOKEN, "https://example.invalid/")
