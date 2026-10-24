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
镜像回显的 token 进不了日志 / --probe-backup 真验收; 基址由主进程选好传给 _post_json; 不写任何 token) /
分页 (2026-10-24 卡 TS-PAGINATE: 恰满本站单页上限才带 offset 续页, has_more, 合并去重, 调用方自己翻页不插手, --probe-backup 的备用直调同样分页;
10-25 卡 TS-PAGINATE-2: 所有页钉在第一页那个站、切换/回切只在 query() 之间生效、截断一律抛 TushareTruncated、页上限按 host 配 {host: n};
同日 TS-PAGINATE-3: 续页与已拿到的行重复 = 行序不稳, 也抛 TushareTruncated) /
进程池起不来或挂住 → 线程版硬期限一次即永久 (文末; 10-25 TS-PAGINATE-3 看门线程分「取池 / 调用」两段计时 + os.getpid 二次探针, 慢 / 忙不误判)。
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
    tc._PAGE_LIMIT = tc.PAGE_LIMIT_DEFAULT             # 单页上限固定取缺省 5000 (不读真 secrets / 环境变量); 要别的值用例自己设
    tc._pool_fallback_reset()                          # 进程池回退开关复位 (一次即永久, 用例间要清)
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
    tc._pool_fallback_reset()
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


def test_backup_failures_count_consecutively_across_calls_and_timeouts_count_separately(monkeypatch):
    """备用的连续失败也跨调用累计 (retries=1 的三次调用也会触发试探); 备用上的硬期限超时**单独计** (backup_timeouts, 阈值 6,
    与主站同口径 —— 卡 TS-FALLBACK-3, 校验员 LOW「备用侧硬期限没有单独计」); 备用一次 JSON 应答 (参数错之类的业务错误) 把两个计数
    都清零; 备用无权限 40203 **算**一次备用失败 (不清零, 见 test_backup_no_permission_counts_toward_probe…)。"""
    s, _ = _install_fo([_err("token已过期", code=2002), _ok(["a"], [[1]])])
    tc.query("daily")
    assert tc.failover_state()["active"] is True
    _fo_session([FakeResp({}, status=502)])
    with pytest.raises(tc.TushareTransport):
        tc.query("daily", retries=1)
    assert tc.failover_state()["backup_fails"] == 1
    _fo_session([_err("必填参数 标的", code=40001)])          # 备用答了 JSON (参数错) → 两个计数清零
    with pytest.raises(tc.TushareBadParams):
        tc.query("daily", retries=1)
    assert tc.failover_state()["backup_fails"] == 0 and tc.failover_state()["backup_timeouts"] == 0
    assert tc.failover_state()["active"] is True
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
    st = tc.failover_state()
    assert st["backup_fails"] == 1 and st["backup_timeouts"] == 1 and st["probes"] == 0     # 超时不进 backup_fails
    monkeypatch.setattr(tc, "_deadline_call", real_call)
    _fo_session([FakeResp({}, status=502)])
    with pytest.raises(tc.TushareTransport):
        tc.query("daily", retries=1)
    assert tc.failover_state()["backup_fails"] == 2 and tc.failover_state()["probes"] == 0
    s = _fo_session([FakeResp({}, status=502), _cal_ok(), _ok(["a"], [[1]])])   # 第 3 次 → 试探主站 → 回切 → 本次在主站成功
    df = tc.query("daily", retries=1)
    assert len(df) == 1 and _urls(s) == [BACKUP, PRIMARY, PRIMARY] and _is_probe(s.calls[1])
    st = tc.failover_state()
    assert st["active"] is False and st["returns"] == 1 and st["backup_timeouts"] == 0 and st["backup_fails"] == 0


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
    # 备用上的权限错误照旧不重试、不切; 算一次备用失败 (1 < 3, 不试探), 见 test_backup_no_permission_counts_toward_probe…
    s = _fo_session([_err("抱歉，您没有接口访问权限", code=40203)])
    with pytest.raises(tc.TushareNoPermission):
        tc.query("daily")
    assert _urls(s) == [BACKUP] and tc.failover_state()["backup_fails"] == 1 and tc.failover_state()["probes"] == 2


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
    assert _urls(s) == [BACKUP] and tc.failover_state()["backup_fails"] == 1   # 无权限算一次备用失败 (TS-FALLBACK-3)
    s = _fo_session([_ok(["a"], [[1]])])                          # 备用成功一次 → 清零, 下面的三连从 0 数起
    tc.query("daily")
    assert _urls(s) == [BACKUP] and tc.failover_state()["backup_fails"] == 0
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


def _daily_rows(n, day="20261013", distinct=None):
    """n 行 daily; distinct=k 时只有 k 个不同代码 (重复行凑数的回包, 校验员 J2)。"""
    k = distinct or n
    return [[f"{600000 + (i % k):06d}.SH", day, 1.0, 1.1, 0.9, 1.0, 100.0, 1000.0] for i in range(n)]


_D, _P2 = "2026-10-13", "2026-09-28"        # _CAL 窗口里最近开市日 D, 与往前 PROBE_SECOND_DAY_BACK=5 个开市日的 P (10-13 ← 10-12, 10-09, 09-30, 09-29, 09-28)
_CAL_SPEC = _ok(["cal_date", "is_open"], _CAL)


def _probe_script(backup_daily_d=None, backup_daily_p=None, n_p=5000, n_p2=4990, n_b=4600):
    """6 次调用的标准剧本: 主站 trade_cal / daily D / daily P 走生产路径, 备用 trade_cal / daily D / daily P 直调。"""
    return [_CAL_SPEC, _ok(_DAILY_COLS, _daily_rows(n_p, "20261013")), _ok(_DAILY_COLS, _daily_rows(n_p2, "20260928")),
            _ok(["cal_date", "is_open"], _CAL),
            backup_daily_d if backup_daily_d is not None else _ok(_DAILY_COLS, _daily_rows(n_b, "20261013")),
            backup_daily_p if backup_daily_p is not None else _ok(_DAILY_COLS, _daily_rows(n_b, "20260928"))]


def _probe_setup(monkeypatch, script, backup=BACKUP, page_limit=6000):
    """page_limit: 旧剧本的 daily 恰 5000 行 (与备用站 datahubco 的单页上限撞数), 缺省把 secrets 键 tushare_page_limit 写成 6000 让它们
    不触发自动分页 (分页另有用例); None = 不写键 (客户端按缺省 5000 分页)。"""
    for k in ("TUSHARE_TOKEN", "TUSHARE_BASE_URL", "TUSHARE_BASE_URL_BACKUP", "TUSHARE_PAGE_LIMIT"):
        monkeypatch.delenv(k, raising=False)
    sec = {"tushare_token": TOKEN, "tushare_base_url": PRIMARY}
    if backup:
        sec["tushare_base_url_backup"] = backup
    if page_limit is not None:
        sec["tushare_page_limit"] = page_limit
    monkeypatch.setattr(tc, "_read_secrets", lambda: sec)
    s, naps = _install_fo(script)
    lines = []
    return s, naps, lines


def test_probe_backup_six_calls_and_pass_verdict(monkeypatch):
    """6 次调用: 主站 trade_cal + daily D + daily P 走生产路径 query() (经切换机制), 备用同三项直调 (同一个 token, 服务端过滤 is_open=1);
    备用日历与主站一致、两天行数 ≥ 90%、代码重合 ≥ 90%、回包日期全对 → rc 0; 输出只有 host / 行数 / 代码数 / 耗时, 没有 token 没有完整地址。
    P = D 往前 PROBE_SECOND_DAY_BACK=5 个开市日 (已知的过去开市日, 抓「一律回最新一天」的镜像)。"""
    s, naps, lines = _probe_setup(monkeypatch, _probe_script())
    rc = tc.probe_backup(out=lines.append, today=_TODAY)
    text = "\n".join(lines)
    assert rc == 0, text
    assert _urls(s) == [PRIMARY] * 3 + [BACKUP] * 3
    apis = [c["json"]["api_name"] for c in s.calls]
    assert apis == ["trade_cal", "daily", "daily", "trade_cal", "daily", "daily"]
    assert s.calls[0]["json"]["params"] == {"exchange": "SSE", "start_date": "20260922", "end_date": "20261020", "is_open": "1"}
    assert s.calls[3]["json"]["params"] == s.calls[0]["json"]["params"]
    assert s.calls[1]["json"]["params"] == {"trade_date": "20261013"} == s.calls[4]["json"]["params"]
    assert s.calls[2]["json"]["params"] == {"trade_date": "20260928"} == s.calls[5]["json"]["params"]
    assert all(c["json"]["fields"] == tc.PROBE_DAILY_FIELDS for c in s.calls if c["json"]["api_name"] == "daily")
    assert all(c["json"]["token"] == TOKEN for c in s.calls)
    assert all(not _is_probe(c) for c in s.calls)               # 验收不触发主站试探 (状态一直在主站)
    assert naps == [] and tc.failover_state()["active"] is False
    assert "共 6 次调用" in text and "结论 (rc 0): 备用 backup.invalid 可用" in text and f"日历一致 {len(_OPEN)} 天" in text
    assert f"daily {_D} 4600/5000 行" in text and f"daily {_P2} 4600/4990 行" in text and "两天回包日期都对" in text
    assert "切换状态 active=False" in text and f"主站 daily {_D}: 5000 行, 5000 个代码" in text
    assert TOKEN not in text and "/tushare/sdk" not in text and "/api/tushare" not in text


def test_probe_backup_second_day_falls_back_to_earliest_when_window_is_short(monkeypatch):
    """窗口里过去开市日 ≤ 5 天 (长假后 / 刚开市) → 第二个核对日取窗口里最早的过去开市日 (仍 ≠ D); 不足 2 天 → rc 2。"""
    today = _dt.date(2026, 9, 24)                               # _CAL 里 ≤ 09-24 的开市日只有 09-22/23/24 → D=24, P=22
    script = [_CAL_SPEC, _ok(_DAILY_COLS, _daily_rows(100, "20260924")), _ok(_DAILY_COLS, _daily_rows(100, "20260922")),
              _CAL_SPEC, _ok(_DAILY_COLS, _daily_rows(100, "20260924")), _ok(_DAILY_COLS, _daily_rows(100, "20260922"))]
    s, naps, lines = _probe_setup(monkeypatch, script)
    assert tc.probe_backup(out=lines.append, today=today) == 0, "\n".join(lines)
    assert s.calls[2]["json"]["params"] == {"trade_date": "20260922"} == s.calls[5]["json"]["params"]
    s, naps, lines = _probe_setup(monkeypatch, [_ok(["cal_date", "is_open"], [["20260922", 1], ["20260923", 1]])])
    assert tc.probe_backup(out=lines.append, today=_dt.date(2026, 9, 22)) == 2 and "过去开市日不足 2 天" in lines[-1]
    assert len(s.calls) == 1


def test_probe_backup_fails_when_backup_rejects_key_or_disagrees(monkeypatch):
    """备用不认这把 key (2002) / 日历不一致 / daily 行数不够 / 打不通 → rc 1, 结论点名原因与日子; 主站那三项照常; 直调不重试。"""
    s, naps, lines = _probe_setup(monkeypatch, _probe_script(n_p=100, n_p2=100)[:3] + [
        _err("token已过期", code=2002), _err("抱歉，您没有接口访问权限", code=40203), _err("抱歉，您没有接口访问权限", code=40203)])
    rc = tc.probe_backup(out=lines.append, today=_TODAY)
    text = "\n".join(lines)
    assert rc == 1 and _urls(s) == [PRIMARY] * 3 + [BACKUP] * 3 and naps == []    # 直调不重试
    assert "结论 (rc 1): 备用 backup.invalid 不可用" in text and TOKEN not in text
    assert "trade_cal 打不通或不认这把 key" in text
    assert f"daily {_D} 打不通或不认这把 key" in text and f"daily {_P2} 打不通或不认这把 key" in text
    s, naps, lines = _probe_setup(monkeypatch, _probe_script(n_p=100, n_p2=100)[:3] + [
        _ok(["cal_date", "is_open"], _CAL[:-2]), _ok(_DAILY_COLS, _daily_rows(80, "20261013")), _ok(_DAILY_COLS, _daily_rows(100, "20260928"))])
    rc = tc.probe_backup(out=lines.append, today=_TODAY)
    text = "\n".join(lines)
    assert rc == 1 and "日历与主站不一致" in text and f"daily {_D} 行数 80 < 主站 100 × 0.9" in text
    assert f"daily {_P2} 行数" not in text and f"daily {_P2} 与主站重合代码" not in text     # P 那天没毛病就不点名
    s, naps, lines = _probe_setup(monkeypatch, _probe_script(n_p=100, n_p2=100)[:3] + [
        OSError("Connection refused"), _ok(["ts_code", "close"], [["600000.SH", 1.0]] * 100), _ok(_DAILY_COLS, _daily_rows(100, "20260928"))])
    rc = tc.probe_backup(out=lines.append, today=_TODAY)
    text = "\n".join(lines)
    assert rc == 1 and "备用 trade_cal 失败: OSError" in text and "缺列" in text and "回包日期" not in text   # 没 trade_date 列只报缺列


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


# ---------------------------------------------------------------------------
# 卡 TS-FALLBACK-3 (2026-10-17, 对 verify:TS-FALLBACK-2 的 1 MEDIUM + 4 LOW 逐条)
def test_probe_backup_rejects_backup_that_ignores_or_mislabels_trade_date(monkeypatch):
    """校验员 MEDIUM (J1): 备用 daily 问 D 回的却是别的日子 (无视 / 错配 trade_date) → rc 1 并点名 —— 生产 _rows_from_daily /
    fetch_adj_by_date 按所问日期写价格库、不看回包日期, 这种备用切过去会静默写错 K 线和复权因子。
    「一律回最新一天」的镜像在 D 上与正确镜像一样, 靠第二个日子 P 抓出来。"""
    wrong_d = _ok(_DAILY_COLS, _daily_rows(5000, "20261012"))
    s, naps, lines = _probe_setup(monkeypatch, _probe_script(backup_daily_d=wrong_d))
    rc = tc.probe_backup(out=lines.append, today=_TODAY)
    text = "\n".join(lines)
    assert rc == 1, text
    assert f"daily {_D} 回包日期与所问不符 (备用无视或错配 trade_date, 生产会按所问日期写错价格库)" in text
    assert "回包日期 ['20261012'] 与所问 20261013 不符" in text
    assert f"daily {_P2} 回包日期" not in text                      # P 那天是对的
    # 「一律回最新一天」: D 对, P 却回了 D 的行
    latest_only = _ok(_DAILY_COLS, _daily_rows(4600, "20261013"))
    s, naps, lines = _probe_setup(monkeypatch, _probe_script(backup_daily_p=latest_only))
    rc = tc.probe_backup(out=lines.append, today=_TODAY)
    text = "\n".join(lines)
    assert rc == 1 and f"daily {_P2} 回包日期与所问不符" in text and f"daily {_D} 回包日期" not in text
    assert "回包日期 ['20261013'] 与所问 20260928 不符" in text
    # 混着几行别的日子也不行
    mixed = _ok(_DAILY_COLS, _daily_rows(4600, "20261013") + _daily_rows(3, "20261009"))
    s, naps, lines = _probe_setup(monkeypatch, _probe_script(backup_daily_d=mixed))
    assert tc.probe_backup(out=lines.append, today=_TODAY) == 1 and "['20261009']" in "\n".join(lines)


def test_probe_backup_rejects_duplicate_rows_padding(monkeypatch):
    """校验员 J2: 备用 5000 行但只有 10 个不同代码 (重复行凑数) → 行数够、日期对, 但「与主站重合代码」10 < 5000 × 0.9 → rc 1。"""
    dup = _ok(_DAILY_COLS, _daily_rows(5000, "20261013", distinct=10))
    s, naps, lines = _probe_setup(monkeypatch, _probe_script(backup_daily_d=dup))
    rc = tc.probe_backup(out=lines.append, today=_TODAY)
    text = "\n".join(lines)
    assert rc == 1, text
    assert f"daily {_D} 与主站重合代码 10 < 5000 × 0.9" in text and f"daily {_D} 行数" not in text
    assert f"备用 daily {_D}: 5000 行 (100% of 主站), 与主站重合代码 10/5000" in text
    # 代码集对不上 (回的是另一批票) 同样不行
    other = _ok(_DAILY_COLS, [[f"{300000 + i:06d}.SZ", "20261013", 1.0, 1.1, 0.9, 1.0, 100.0, 1000.0] for i in range(5000)])
    s, naps, lines = _probe_setup(monkeypatch, _probe_script(backup_daily_d=other))
    assert tc.probe_backup(out=lines.append, today=_TODAY) == 1 and "与主站重合代码 0 < 5000 × 0.9" in "\n".join(lines)


def test_probe_backup_missing_column_alone_fails(monkeypatch):
    """校验员 LOW (变异 m5 存活): 只缺一列 (其它全对) 也必须 rc 1, 结论里只有这一条。"""
    cols = [c for c in _DAILY_COLS if c != "amount"]
    rows = [r[:-1] for r in _daily_rows(4600, "20261013")]
    s, naps, lines = _probe_setup(monkeypatch, _probe_script(backup_daily_d=_ok(cols, rows)))
    rc = tc.probe_backup(out=lines.append, today=_TODAY)
    assert rc == 1
    assert lines[-1] == f"结论 (rc 1): 备用 backup.invalid 不可用 —— daily {_D} 缺列 ['amount']; 不要加键 / 加了就去掉"


def test_probe_backup_primary_zero_rows_or_wrong_date_cannot_judge(monkeypatch):
    """校验员 LOW (变异 m11 存活): 主站 daily 0 行 → rc 2, 不再往下打备用; 主站自己回包日期不对 → 也 rc 2 (那是主站的问题, 判不了备用)。"""
    s, naps, lines = _probe_setup(monkeypatch, [_CAL_SPEC, _ok(_DAILY_COLS, [])])
    rc = tc.probe_backup(out=lines.append, today=_TODAY)
    assert rc == 2 and f"主站 daily {_D} 0 行" in lines[-1] and _urls(s) == [PRIMARY, PRIMARY]
    s, naps, lines = _probe_setup(monkeypatch, [_CAL_SPEC, _ok(_DAILY_COLS, _daily_rows(5000, "20261012"))])
    rc = tc.probe_backup(out=lines.append, today=_TODAY)
    assert rc == 2 and f"主站 daily {_D} 回包日期与所问不符" in lines[-1] and "tushare_probe.py compat" in lines[-1]
    assert _urls(s) == [PRIMARY, PRIMARY]
    s, naps, lines = _probe_setup(monkeypatch, [_CAL_SPEC, _ok(_DAILY_COLS, _daily_rows(5000, "20261013")), _ok(_DAILY_COLS, [])])
    rc = tc.probe_backup(out=lines.append, today=_TODAY)
    assert rc == 2 and f"主站 daily {_P2} 0 行" in lines[-1] and _urls(s) == [PRIMARY] * 3


def test_probe_call_thread_deadline_returns_raises_and_times_out():
    """_probe_call: 自己的守护线程 + 墙钟; 正常返回值 / 异常原样带回 / 到点抛 TimeoutError 而不 terminate 任何东西。"""
    import threading
    assert tc._probe_call(lambda a, b=0: a + b, (2,), {"b": 3}, 1.0) == 5

    def boom():
        raise ValueError("x")
    with pytest.raises(ValueError):
        tc._probe_call(boom, (), {}, 1.0)
    gate = threading.Event()
    try:
        with pytest.raises(TimeoutError) as ei:
            tc._probe_call(gate.wait, (), {}, 0.02)
        assert "墙钟" in str(ei.value) and "0.02" in str(ei.value)
    finally:
        gate.set()                                              # 放被弃的线程走


def test_probe_runs_in_its_own_thread_not_through_the_shared_pool(monkeypatch):
    """校验员 LOW: 试探不得经 datasource 的共享进程池 (撞期限会 terminate 整池连带在途请求; 池满时在队列里等到超时)。
    试探经 _probe_call 在别的线程里跑; _deadline_call (池的入口) 只见到正常请求, 见不到试探。"""
    import threading
    seen = []
    real_call = tc._deadline_call

    def spy(fn, args, kwargs, dl):
        seen.append((getattr(fn, "__name__", ""), args[2], dl))
        return real_call(fn, args, kwargs, dl)

    monkeypatch.setattr(tc, "_deadline_call", spy)
    idents = []

    class ThreadSession(FakeSession):
        def post(self, url, json=None, timeout=None):      # noqa: A002
            idents.append((timeout, threading.get_ident()))
            return super().post(url, json=json, timeout=timeout)

    s, _ = _install_fo([_err("token已过期", code=2002), _ok(["a"], [[1]])])
    tc.query("daily")
    assert tc.failover_state()["active"] is True
    with tc._FO_LOCK:
        tc._FO["last_probe_at"] = tc.time.monotonic() - tc.PROBE_EVERY_SEC - 1
    tc._SESSION = ThreadSession([_cal_ok(), _ok(["a"], [[1]])])
    idents.clear()
    seen.clear()
    df = tc.query("daily", deadline_sec=77)
    assert len(df) == 1 and tc.failover_state()["active"] is False
    assert [t for t, _ in idents] == [tc.PROBE_TIMEOUT_SEC, tc.TIMEOUT_SEC]
    assert idents[0][1] != threading.get_ident() and idents[1][1] == threading.get_ident()   # 试探在别的线程, 正常请求在本线程
    assert seen == [("_post_json", tc.TIMEOUT_SEC, 77)]                                      # 池入口只见到正常请求


def test_probe_unexpected_exception_is_a_failed_probe_and_slot_is_released(monkeypatch, caplog):
    """校验员 LOW (场景 G): 试探里抛了捕获元组以外的异常 (MaybeEncodingError / MemoryError 一类) → 只算「试探失败」, 调用方的 query()
    照走备用; probing 位子释放, 到点还会再试探并回切。"""
    s, _ = _install_fo([_err("token已过期", code=2002), _ok(["a"], [[1]])])
    tc.query("daily")
    with tc._FO_LOCK:
        tc._FO["last_probe_at"] = tc.time.monotonic() - tc.PROBE_EVERY_SEC - 1

    def boom(fn, args, kwargs, dl):
        raise RuntimeError("simulated MaybeEncodingError inside probe")
    monkeypatch.setattr(tc, "_probe_call", boom)
    s = _fo_session([_ok(["a"], [[1]])])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df = tc.query("daily")
    assert len(df) == 1 and _urls(s) == [BACKUP]
    st = tc.failover_state()
    assert st["probing"] is False and st["probes"] == 1 and st["active"] is True
    lines = [r for r in caplog.records if "试探失败" in r.getMessage()]
    assert len(lines) == 1 and "试探异常 RuntimeError" in lines[0].getMessage() and TOKEN not in caplog.text
    # 位子是空的: 到点再试探, 这回正常 → 回切
    monkeypatch.undo()
    with tc._FO_LOCK:
        tc._FO["last_probe_at"] = tc.time.monotonic() - tc.PROBE_EVERY_SEC - 1
    s = _fo_session([_cal_ok(), _ok(["a"], [[1]])])
    tc.query("daily")
    assert _urls(s) == [PRIMARY, PRIMARY] and _is_probe(s.calls[0]) and tc.failover_state()["active"] is False


def test_probe_slot_released_even_when_probe_raises_baseexception(monkeypatch):
    """位子释放是 finally 兜底的: 连 BaseException (KeyboardInterrupt / SystemExit 一类) 穿过去, probing 也复位。"""
    class _Boom(BaseException):
        pass

    def boom(token, base):
        raise _Boom()
    s, _ = _install_fo([_err("token已过期", code=2002), _ok(["a"], [[1]])])
    tc.query("daily")
    with tc._FO_LOCK:
        tc._FO["last_probe_at"] = tc.time.monotonic() - tc.PROBE_EVERY_SEC - 1
    monkeypatch.setattr(tc, "_probe_primary", boom)
    with pytest.raises(_Boom):
        tc._maybe_probe_primary(TOKEN, PRIMARY, BACKUP)
    st = tc.failover_state()
    assert st["probing"] is False and st["active"] is True and tc.time.monotonic() - st["last_probe_at"] < 60
    monkeypatch.undo()
    with tc._FO_LOCK:
        tc._FO["last_probe_at"] = tc.time.monotonic() - tc.PROBE_EVERY_SEC - 1
    _fo_session([_cal_ok()])
    assert tc._maybe_probe_primary(TOKEN, PRIMARY, BACKUP) is True and tc.failover_state()["active"] is False


def test_backup_hard_deadline_timeouts_counted_separately_with_threshold_6(monkeypatch, caplog):
    """校验员 LOW: 备用侧硬期限超时单独计 (backup_timeouts, 阈值 FAILBACK_AFTER_TIMEOUTS=6, 与主站同口径): 5 次超时 + 2 次传输层失败
    都不试探 (各算各的); 第 6 次超时 → 试探主站 → 成功即回切且本次在主站重来, 回切原因写「备用连续 6 次硬期限超时」;
    备用一次 JSON 应答把两个计数都清零。"""
    s, _ = _install_fo([_err("token已过期", code=2002), _ok(["a"], [[1]])])
    tc.query("daily")
    assert tc.failover_state()["active"] is True
    real_call = tc._deadline_call

    def timeout_on_backup(fn, args, kwargs, dl):
        if args[0] == BACKUP:
            raise TimeoutError("_post_json 超过 42s 硬期限 (工作进程已终止)")
        return real_call(fn, args, kwargs, dl)

    monkeypatch.setattr(tc, "_deadline_call", timeout_on_backup)
    s = _fo_session([])
    for _ in range(5):
        with pytest.raises(tc.TushareTransport):
            tc.query("daily", retries=1, deadline_sec=42)
    st = tc.failover_state()
    assert st["backup_timeouts"] == 5 and st["backup_fails"] == 0 and st["probes"] == 0 and len(s.calls) == 0
    monkeypatch.setattr(tc, "_deadline_call", real_call)
    s = _fo_session([FakeResp({}, status=502), FakeResp({}, status=503)])
    for _ in range(2):
        with pytest.raises(tc.TushareTransport):
            tc.query("daily", retries=1)
    st = tc.failover_state()
    assert st["backup_timeouts"] == 5 and st["backup_fails"] == 2 and st["probes"] == 0 and st["active"] is True
    monkeypatch.setattr(tc, "_deadline_call", timeout_on_backup)
    s = _fo_session([_cal_ok(), _ok(["a"], [[1]])])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df = tc.query("daily", retries=1, deadline_sec=42)          # 第 6 次超时 → 试探 → 回切 → 本次在主站成功
    assert len(df) == 1 and _urls(s) == [PRIMARY, PRIMARY] and _is_probe(s.calls[0])
    st = tc.failover_state()
    assert st["active"] is False and st["returns"] == 1 and st["backup_timeouts"] == 0 and st["backup_fails"] == 0
    lines = _return_lines(caplog)
    assert len(lines) == 1 and "备用连续 6 次硬期限超时, 最后一次: daily 超过 42s 硬期限" in lines[0].getMessage()
    # 备用一次 JSON 应答 → 两个计数都清零
    monkeypatch.setattr(tc, "_deadline_call", real_call)
    s = _fo_session([_err("token已过期", code=2002), _ok(["a"], [[1]])])
    tc.query("daily")
    with tc._FO_LOCK:
        tc._FO["backup_timeouts"], tc._FO["backup_fails"] = 4, 2
    s = _fo_session([_ok(["a"], [[1]])])
    tc.query("daily")
    assert tc.failover_state()["backup_timeouts"] == 0 and tc.failover_state()["backup_fails"] == 0


def test_backup_no_permission_counts_toward_probe_and_call_redoes_on_primary_when_back(caplog):
    """校验员 MEDIUM 的「缺口」: 备用 NoPermission (40203 / unknown_api; 15000 档比主站少) 以前不触发试探, 要等 20 次 / 10 分钟。
    现在算一次备用失败: 连续 3 次 → 试探主站 → 主站回来了就回切并让**本次调用在主站重来** (主站有权限); 主站没回来照旧抛 NoPermission
    (不在备用重试, 不退避); 参数错 (BadParams) 仍是「JSON 应答清零」, 不触发试探。"""
    s, naps = _install_fo([_err("token已过期", code=2002), _ok(["a"], [[1]])])
    tc.query("daily")
    assert tc.failover_state()["active"] is True
    for n in (1, 2):
        s = _fo_session([_err("抱歉，您没有接口访问权限", code=40203)])
        with pytest.raises(tc.TushareNoPermission):
            tc.query("daily_basic")
        assert _urls(s) == [BACKUP] and tc.failover_state()["backup_fails"] == n and tc.failover_state()["probes"] == 0
    naps.clear()
    s = _fo_session([_err("[unknown_api] 接口不存在或未开放", code=404), _cal_ok(), _ok(["a"], [[1]])])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df = tc.query("daily_basic")
    assert len(df) == 1 and _urls(s) == [BACKUP, PRIMARY, PRIMARY] and _is_probe(s.calls[1]) and naps == []
    st = tc.failover_state()
    assert st["active"] is False and st["returns"] == 1 and st["backup_fails"] == 0
    lines = _return_lines(caplog)
    assert len(lines) == 1 and "备用连续 3 次失败, 最后一次: daily_basic code=404 (备用无权限或没这个接口)" in lines[0].getMessage()
    assert any("在备用无权限" in r.getMessage() and "本次在主站重来" in r.getMessage() for r in caplog.records)
    assert not any("永久性失败" in r.getMessage() for r in caplog.records if "daily_basic code=404" in r.getMessage())
    # 主站没回来: 第 3 次 → 试探 (502) → 留在备用, 抛的仍是 NoPermission (不是传输错), WARNING「两站」
    caplog.clear()
    s, naps = _install_fo([_err("token已过期", code=2002), _ok(["a"], [[1]])])
    tc.query("daily")
    with tc._FO_LOCK:
        tc._FO["backup_fails"] = 2
    s = _fo_session([_err("抱歉，您没有接口访问权限", code=40203), FakeResp({}, status=502)])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        with pytest.raises(tc.TushareNoPermission):
            tc.query("daily_basic")
    assert _urls(s) == [BACKUP, PRIMARY] and _is_probe(s.calls[1]) and naps == []
    st = tc.failover_state()
    assert st["active"] is True and st["probes"] == 1 and st["backup_fails"] == 0
    warn = [r for r in caplog.records if "试探失败" in r.getMessage()]
    assert len(warn) == 1 and warn[0].levelno == logging.WARNING and "备用连续 3 次失败" in warn[0].getMessage()
    # 参数错: JSON 应答清零, 不触发试探
    with tc._FO_LOCK:
        tc._FO["backup_fails"] = 2
    s = _fo_session([_err("必填参数 标的", code=40001)])
    with pytest.raises(tc.TushareBadParams):
        tc.query("forecast")
    assert _urls(s) == [BACKUP] and tc.failover_state()["backup_fails"] == 0 and tc.failover_state()["probes"] == 1


def test_failed_probe_resets_backup_timeouts_too(monkeypatch):
    """试探失败 (两站都不可用) 时备用的两个计数都归零重数 (变异 n10: 只清 backup_fails 不清 backup_timeouts, 下一次超时就又试探)。"""
    s, _ = _install_fo([_err("token已过期", code=2002), _ok(["a"], [[1]])])
    tc.query("daily")
    real_call = tc._deadline_call

    def timeout_on_backup(fn, args, kwargs, dl):
        if args[0] == BACKUP:
            raise TimeoutError("_post_json 超过 42s 硬期限 (工作进程已终止)")
        return real_call(fn, args, kwargs, dl)

    monkeypatch.setattr(tc, "_deadline_call", timeout_on_backup)
    with tc._FO_LOCK:
        tc._FO["backup_timeouts"] = 5
    s = _fo_session([FakeResp({}, status=502)])                  # 第 6 次超时 → 试探主站 (502) → 留在备用
    with pytest.raises(tc.TushareTransport):
        tc.query("daily", retries=1, deadline_sec=42)
    assert _urls(s) == [PRIMARY] and _is_probe(s.calls[0])
    st = tc.failover_state()
    assert st["active"] is True and st["probes"] == 1 and st["backup_timeouts"] == 0 and st["backup_fails"] == 0
    s = _fo_session([])
    with pytest.raises(tc.TushareTransport):                      # 紧接着再超时一次: 重新从 1 数起, 不再试探
        tc.query("daily", retries=1, deadline_sec=42)
    assert tc.failover_state()["backup_timeouts"] == 1 and tc.failover_state()["probes"] == 1 and len(s.calls) == 0


def test_return_clears_timeouts_counter_even_without_a_primary_success(monkeypatch):
    """校验员 LOW (变异 m1 存活): 回切必须把主站的 timeouts 也清零, 而且不能靠回切后那次主站成功顺手清 —— 回切后主站第一次就撞
    硬期限 (共享进程池连带) 时, 切换前攒下的 5 次不得凑成第 6 次再切。"""
    s, _ = _install_fo([_err("token已过期", code=2002), _ok(["a"], [[1]])])
    tc.query("daily")
    with tc._FO_LOCK:
        tc._FO["timeouts"] = 5                                  # 切换前攒下的 (切换时不清, 回切时必须清)
        tc._FO["last_probe_at"] = tc.time.monotonic() - tc.PROBE_EVERY_SEC - 1
    real_call = tc._deadline_call
    hits, mid = [], []

    def first_primary_times_out(fn, args, kwargs, dl):
        hits.append(args[0])
        if args[0] == PRIMARY and len(hits) == 1:
            raise TimeoutError("_post_json 超过 42s 硬期限 (工作进程已终止)")
        mid.append(tc.failover_state())                         # 重试那一刻的状态: 超时已计 1 次, 还没被成功清零
        return real_call(fn, args, kwargs, dl)

    monkeypatch.setattr(tc, "_deadline_call", first_primary_times_out)
    s = _fo_session([_cal_ok(), _ok(["a"], [[1]])])             # 试探 (不走 _deadline_call) → 回切 → 主站超时 1 次 → 成功
    df = tc.query("daily", retries=2, deadline_sec=42)
    assert len(df) == 1 and _urls(s) == [PRIMARY, PRIMARY] and _is_probe(s.calls[0])   # 回切不清 timeouts 的话这里是 [PRIMARY, BACKUP]
    assert len(mid) == 1 and mid[0]["timeouts"] == 1 and mid[0]["active"] is False and mid[0]["switches"] == 1
    st = tc.failover_state()
    assert st["active"] is False and st["switches"] == 1 and st["returns"] == 1 and st["timeouts"] == 0   # 成功后清零


# ---------------------------------------------------------------------------
# 分页 (2026-10-24 卡 TS-PAGINATE): 备用站 datahubco 单页上限 5000 行 (全市场按 trade_date 的 daily / adj_factor / daily_basic 恰 5000 行,
# offset=5000 再回 559 = 5559; 同日主站 daily 5562 行; limit=6000 → 400); 主站 kaixin8 一页回全市场 5,56x–5,58x 行 (PC update.log 09-29..10-23) 所以从未暴露
# —— 切到备用后不分页就静默丢 ~560 只。不联网, 无真 token。
_PG_COLS = ["ts_code", "trade_date", "close"]


def _pg_rows(n, start=0, day="20260929"):
    return [[f"{start + i:06d}.SZ", day, 1.0] for i in range(n)]


def _pg_ok(n, start=0, has_more=None, day="20260929"):
    j = {"code": 0, "msg": "", "data": {"fields": _PG_COLS, "items": _pg_rows(n, start, day)}}
    if has_more is not None:
        j["data"]["has_more"] = has_more
    return FakeResp(j)


def _page_lines(caplog):
    return [r for r in caplog.records if ": 分页 " in r.getMessage()]


def test_paginates_when_first_page_equals_site_limit(caplog):
    """恰 5000 行 → 带 offset=5000 续一页 (559 行) → 5559 行; 第一页请求体与以前逐字节相同 (不带 offset / limit); 续页同 fields 同基址;
    一行 INFO「tushare daily: 分页 2 页 5559 行 (host) trade_date=…」; 结果 .attrs["pages"] = 2。"""
    s = _install([_pg_ok(5000), _pg_ok(559, 5000)])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df = tc.query("daily", trade_date="20260929", fields="ts_code,trade_date,close")
    assert len(df) == 5559 and df.attrs.get("pages") == 2 and list(df.columns) == _PG_COLS
    assert len(s.calls) == 2
    assert s.calls[0]["json"] == {"api_name": "daily", "token": TOKEN, "params": {"trade_date": "20260929"}, "fields": "ts_code,trade_date,close"}
    assert s.calls[1]["json"]["params"] == {"trade_date": "20260929", "offset": 5000}
    assert s.calls[1]["json"]["fields"] == "ts_code,trade_date,close" and s.calls[1]["url"] == s.calls[0]["url"]
    lines = _page_lines(caplog)
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    assert "tushare daily: 分页 2 页 5559 行 (example.invalid) trade_date=20260929" in lines[0].getMessage() and TOKEN not in caplog.text
    assert df["ts_code"].is_unique and df["ts_code"].iloc[0] == "000000.SZ" and df["ts_code"].iloc[-1] == "005558.SZ"


def test_no_paging_below_limit_and_zero_change_for_small_endpoints(caplog):
    """4999 行 (不足一页) → 1 次调用, 不翻, 不打分页行, 返回的就是那一页 (attrs 没有 pages); 5559 行 (主站 kaixin8 形态: 上限其实更高) 同样不翻;
    trade_cal 之类小接口请求体里没有 offset / limit。"""
    s = _install([_pg_ok(4999)])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df = tc.query("daily", trade_date="20260929")
    assert len(df) == 4999 and len(s.calls) == 1 and not _page_lines(caplog) and "pages" not in df.attrs
    s = _install([_pg_ok(5559)])
    assert len(tc.query("daily", trade_date="20260929")) == 5559 and len(s.calls) == 1
    s = _install([_ok(["cal_date", "is_open"], [["20260929", 1]])])
    assert tc.trade_cal("2026-09-29", "2026-09-29") == ["2026-09-29"] and len(s.calls) == 1
    assert "offset" not in s.calls[0]["json"]["params"] and "limit" not in s.calls[0]["json"]["params"]
    s = _install([_ok(["a"], [])])                              # 0 行也不翻
    assert len(tc.query("daily", trade_date="20260929")) == 0 and len(s.calls) == 1


def test_has_more_from_server_overrides_row_count_heuristic():
    """回包带 has_more: False → 恰 5000 行也不翻 (提前停); has_more: True → 不足一页也续, 直到 False; 字符串 'true'/'false' 也认; 0 行即停。"""
    s = _install([_pg_ok(5000, has_more=False)])
    assert len(tc.query("daily", trade_date="20260929")) == 5000 and len(s.calls) == 1
    s = _install([_pg_ok(4000, has_more=True), _pg_ok(1000, 4000, has_more=True), _pg_ok(300, 5000, has_more=False)])
    df = tc.query("daily", trade_date="20260929")
    assert len(df) == 5300 and len(s.calls) == 3 and df.attrs["pages"] == 3
    assert [c["json"]["params"].get("offset") for c in s.calls] == [None, 4000, 5000]
    s = _install([_pg_ok(5000, has_more="false")])
    assert len(tc.query("daily", trade_date="20260929")) == 5000 and len(s.calls) == 1
    s = _install([_pg_ok(10, has_more="true"), _pg_ok(0, 10)])
    assert len(tc.query("daily", trade_date="20260929")) == 10 and len(s.calls) == 2
    assert tc._has_more({"has_more": 1}) is True and tc._has_more({"has_more": "0"}) is False
    assert tc._has_more({"has_more": "maybe"}) is None and tc._has_more({}) is None and tc._has_more(None) is None


def test_pages_are_merged_and_deduped_by_endpoint_primary_key(caplog):
    """登记了主键的接口 (adj_factor) 两页无重复 → 并集 5559 行, INFO 行不带「去重」; 没登记主键、也没有 ts_code+trade_date 的接口按整行去重
    (续页 2 行里 1 行重复: 新增恰一半, 不判行序不稳); 登记了主键但回包缺列 (daily 没要 trade_date) → 整行去重。登记了主键的接口跨页重复
    (翻页边界漂移 / 行序不稳) 现在判截断, 见 test_registered_key_overlap_across_pages_is_unstable_order_truncated (卡 TS-PAGINATE-3)。"""
    s = _install([_pg_ok(5000), _pg_ok(559, 5000)])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df = tc.query("adj_factor", trade_date="20260929")
    assert len(df) == 5559 and df["ts_code"].is_unique and df.attrs["pages"] == 2 and len(s.calls) == 2
    msg = _page_lines(caplog)[0].getMessage()
    assert "tushare adj_factor: 分页 2 页 5559 行 (example.invalid) trade_date=20260929" in msg and "去重" not in msg
    s = _install([_ok(["a", "b"], [[f"{i}", "x"] for i in range(5000)]), _ok(["a", "b"], [["4999", "x"], ["5000", "y"]])])
    df = tc.query("some_api", foo="bar")
    assert len(df) == 5001 and len(s.calls) == 2
    s = _install([_ok(["ts_code", "close"], [[f"{i:06d}.SZ", 1.0] for i in range(5000)]),
                  _ok(["ts_code", "close"], [["004999.SZ", 1.0], ["005000.SZ", 2.0]])])
    assert len(tc.query("daily", trade_date="20260929")) == 5001


def test_caller_offset_or_limit_or_paginate_false_disables_auto_paging():
    """调用方自己翻页 (传了 offset / limit —— 研究湖 lake._pages 的形态) 或 paginate=False → 恰 5000 行也不续页 (剧本只有一页, 多打一次就
    AssertionError); paginate=True 带 limit 也按本站上限翻 (续页保留 limit, 只加 offset), 传了 offset 仍不翻。"""
    for kw in ({"limit": 5000}, {"offset": 0}, {"limit": 5000, "offset": 0}, {"paginate": False}):
        s = _install([_pg_ok(5000)])
        assert len(tc.query("fina_indicator_vip", period="20260630", **kw)) == 5000, kw
        assert len(s.calls) == 1 and "offset" not in s.calls[0]["json"]["params"] or "offset" in kw
    s = _install([_pg_ok(5000), _pg_ok(10, 5000)])
    df = tc.query("daily", trade_date="20260929", limit=5000, paginate=True)
    assert len(df) == 5010
    assert [c["json"]["params"] for c in s.calls] == [{"trade_date": "20260929", "limit": 5000},
                                                       {"trade_date": "20260929", "limit": 5000, "offset": 5000}]
    s = _install([_pg_ok(5000)])
    assert len(tc.query("daily", trade_date="20260929", offset=0, paginate=True)) == 5000 and len(s.calls) == 1


def test_truncation_always_raises_tushare_truncated(caplog):
    """截断口径统一 (卡 TS-PAGINATE-2 ④; 以前「不认 / 拒 offset 返回截断表、续页传输失败才抛」两种口径): 镜像不认 offset (第二页原样回第一页) /
    拒 offset ([invalid_params]) / 续页传输失败重试耗尽 → **一律抛 TushareTruncated** (TushareError 直接子类, 不是 Transport), 带 rows / pages / reason,
    续页失败时 __cause__ 带原异常; 先打一行 WARNING「分页截断 … 已拿到 N 行 M 页」; 不返回第一页那 5000 行, 不打「分页」INFO 行, 不翻到 PAGE_MAX。"""
    s = _install([_pg_ok(5000), _pg_ok(5000)])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        with pytest.raises(tc.TushareTruncated) as ei:
            tc.query("daily", trade_date="20260929")
    e = ei.value
    assert isinstance(e, tc.TushareError) and not isinstance(e, tc.TushareTransport) and e.api == "daily"
    assert e.rows == 5000 and e.pages == 1 and "镜像不认 offset" in e.reason and "offset=5000" in e.reason and e.__cause__ is None
    assert len(s.calls) == 2 and TOKEN not in str(e) and not _page_lines(caplog)
    warn = [r for r in caplog.records if "分页截断" in r.getMessage()]
    assert len(warn) == 1 and warn[0].levelno == logging.WARNING
    assert "已拿到 5000 行 1 页" in warn[0].getMessage() and "(example.invalid) trade_date=20260929" in warn[0].getMessage()
    caplog.clear()
    s = _install([_pg_ok(5000), _err("[invalid_params] 业务参数缺失或格式不合法", code=400)])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        with pytest.raises(tc.TushareTruncated) as ei:
            tc.query("daily", trade_date="20260929")
    assert "不支持 offset 翻页" in ei.value.reason and isinstance(ei.value.__cause__, tc.TushareBadParams) and len(s.calls) == 2
    assert any("分页截断" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)
    s = _install([_pg_ok(5000), FakeResp({}, status=502), FakeResp({}, status=502), FakeResp({}, status=502)])
    naps = _no_sleep()
    with pytest.raises(tc.TushareTruncated) as ei:
        tc.query("daily", trade_date="20260929")
    assert isinstance(ei.value.__cause__, tc.TushareTransport) and "续页 offset=5000 失败" in ei.value.reason and ei.value.rows == 5000
    assert len(s.calls) == 4 and naps == [tc.BACKOFF_SEC, tc.BACKOFF_SEC * 2]
    # 跨进程 pickle 往返 (异常类约定): 属性保住
    import pickle
    e2 = pickle.loads(pickle.dumps(ei.value))
    assert isinstance(e2, tc.TushareTruncated) and (e2.rows, e2.pages, e2.reason, e2.api) == (5000, 1, ei.value.reason, "daily")


def test_exact_limit_then_zero_rows_is_not_logged_as_paging(caplog):
    """恰 5000 行而站上确实只有 5000 (续页 0 行) → 返回 5000 行, 2 次 HTTP, **不打**「分页」INFO 行 (校验员 A3: 以前打「分页 1 页 5000 行」, 与 DEPLOY
    「主站正常时不该出现分页行」冲突), 也无 WARNING; attrs["pages"] = 1。"""
    s = _install([_pg_ok(5000), _pg_ok(0, 5000)])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df = tc.query("daily", trade_date="20260929")
    assert len(df) == 5000 and len(s.calls) == 2 and df.attrs.get("pages") == 1
    assert not _page_lines(caplog) and not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_unstable_order_mirror_stops_early_as_truncated(caplog):
    """镜像不认 offset 但每次行序不同 (校验员 H1: 首版翻满 PAGE_MAX=40 页才停, 慢站 40 × 30–110s): 续页里有已出现过的主键就判「行序不稳」→ TushareTruncated,
    第 2 次 HTTP 就停 (10-25 卡 TS-PAGINATE-3; 上一版要等到某一页去重后一行新的都没有, 这个剧本第 3 次才停, 还把第二页的 559 只并进去当「已拿到」);
    续页只是第一页换了顺序 (一行新的都没有) 的, 原因写「去重后没有新行」。"""
    first, again = _pg_rows(5000), _pg_rows(5000, 559)                      # 续页 000559..005558: 4441 行与第一页重复, 559 行新的
    s = _install([FakeResp({"code": 0, "msg": "", "data": {"fields": _PG_COLS, "items": first}}),
                  FakeResp({"code": 0, "msg": "", "data": {"fields": _PG_COLS, "items": again}})])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        with pytest.raises(tc.TushareTruncated) as ei:
            tc.query("daily", trade_date="20260929")
    assert len(s.calls) == 2 < tc.PAGE_MAX and ei.value.rows == 5000 and ei.value.pages == 1
    assert "4441 行与已拿到的重复、只 559 行是新的" in ei.value.reason and "行序不稳" in ei.value.reason and "offset=5000" in ei.value.reason
    s = _install([FakeResp({"code": 0, "msg": "", "data": {"fields": _PG_COLS, "items": first}}),
                  FakeResp({"code": 0, "msg": "", "data": {"fields": _PG_COLS, "items": list(reversed(first))}})])
    with pytest.raises(tc.TushareTruncated) as ei:
        tc.query("daily", trade_date="20260929")
    assert len(s.calls) == 2 and "去重后没有新行" in ei.value.reason and "行序不稳" in ei.value.reason


def test_registered_key_overlap_across_pages_is_unstable_order_truncated(caplog):
    """校验员 10-25 T1 (LOW): 认 offset、但每次请求行序不同的镜像 (无 ORDER BY 的库) —— 第一页 = 排序 σ1 的前 5000, 续页 = 另一种排序 σ2 的 [5000:]
    (559 行里约九成已在第一页)。上一版只认「一行新的都没有」, 续页有几十行新的就静默返回并集 (5044/5562 = 90.7%, 过得了 pricestore 的 90% 守卫), 只打
    INFO「去重 N 行」; 现在 PAGE_KEYS 登记了主键的接口 (daily / adj_factor / daily_basic …) 续页里只要有一行的主键已出现过 → WARNING「分页截断 … 行序不稳」+
    TushareTruncated (口径同其它截断: 不返回截断表, rows / pages = 已拿到的)。翻页边界漂移的少量重复 (5 行) 同样判 —— 稳定的镜像不会跨页重复。
    没登记主键的接口 (按 ts_code+trade_date 或整行去重) 只在去重后新增不足续页一半时判, 少量重复照常去重、INFO 带「去重 N 行」。"""
    import random
    allc = [f"{i:06d}.SZ" for i in range(5559)]
    s2 = random.Random(11).sample(allc, len(allc))
    page1 = [[c, "20260929", 1.0] for c in allc[:5000]]
    page2 = [[c, "20260929", 1.0] for c in s2[5000:]]
    dup = sum(1 for c in s2[5000:] if c in set(allc[:5000]))
    s = _install([FakeResp({"code": 0, "msg": "", "data": {"fields": _PG_COLS, "items": page1}}),
                  FakeResp({"code": 0, "msg": "", "data": {"fields": _PG_COLS, "items": page2}})])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        with pytest.raises(tc.TushareTruncated) as ei:
            tc.query("daily", trade_date="20260929")
    e = ei.value
    assert 400 < dup < 559 and len(s.calls) == 2 and e.rows == 5000 and e.pages == 1 and e.__cause__ is None
    assert f"续页 offset=5000 的 559 行里 {dup} 行与已拿到的重复、只 {559 - dup} 行是新的" in e.reason and "行序不稳" in e.reason
    assert "登记了主键的接口不该跨页重复" in e.reason and TOKEN not in str(e)
    warn = [r for r in caplog.records if "分页截断" in r.getMessage()]
    assert len(warn) == 1 and warn[0].levelno == logging.WARNING and "已拿到 5000 行 1 页" in warn[0].getMessage()
    assert not _page_lines(caplog)
    # 翻页边界漂移 5 行 (登记了主键的 adj_factor; 上一版返回 5554 行 + INFO「去重 5 行」): 同样判
    s = _install([_pg_ok(5000), _pg_ok(559, 4995)])
    with pytest.raises(tc.TushareTruncated) as ei:
        tc.query("adj_factor", trade_date="20260929")
    assert "559 行里 5 行与已拿到的重复、只 554 行是新的" in ei.value.reason and len(s.calls) == 2 and ei.value.rows == 5000
    # 没登记主键 (stk_limit, 按 ts_code+trade_date 去重): 5 行重复 → 照常并集 + INFO「去重 5 行」; 300 行重复 (新增 259 < 559 的一半) → 判截断
    caplog.clear()
    s = _install([_pg_ok(5000), _pg_ok(559, 4995)])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df = tc.query("stk_limit", trade_date="20260929")
    assert len(df) == 5554 and df.attrs["pages"] == 2 and len(s.calls) == 2
    assert "tushare stk_limit: 分页 2 页 5554 行 (example.invalid), 去重 5 行 trade_date=20260929" in _page_lines(caplog)[0].getMessage()
    s = _install([_pg_ok(5000), _pg_ok(559, 4700)])
    with pytest.raises(tc.TushareTruncated) as ei:
        tc.query("stk_limit", trade_date="20260929")
    assert "559 行里 300 行与已拿到的重复、只 259 行是新的" in ei.value.reason and "新增不足续页一半" in ei.value.reason


def test_continuation_rows_above_limit_is_truncated_unless_server_says_has_more():
    """续页行数 > 本站上限而服务端没给 has_more → 上限配置与本站不符 (或镜像不认 offset) → TushareTruncated (停止条件补「续页行数 > 上限」);
    服务端给了 has_more 则以它为准 (官方站单页上限比配置大也能翻)。"""
    s = _install([_pg_ok(5000), _pg_ok(5600, 5000)])
    with pytest.raises(tc.TushareTruncated) as ei:
        tc.query("daily", trade_date="20260929")
    assert "> 本站单页上限 5000" in ei.value.reason and ei.value.rows == 5000 and len(s.calls) == 2
    s = _install([_pg_ok(5000, has_more=True), _pg_ok(5600, 5000, has_more=False)])
    df = tc.query("daily", trade_date="20260929")
    assert len(df) == 10600 and len(s.calls) == 2 and df.attrs["pages"] == 2


def test_page_max_truncates_runaway_paging(monkeypatch, caplog):
    """每页都不同却永远满页 → PAGE_MAX 到顶 → TushareTruncated (兜底, 正常接口到不了; 以前返回 15000 行的截断表 + WARNING)。"""
    monkeypatch.setattr(tc, "PAGE_MAX", 3)
    s = _install([_pg_ok(5000, i * 5000) for i in range(4)])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        with pytest.raises(tc.TushareTruncated) as ei:
            tc.query("daily", trade_date="20260929")
    assert len(s.calls) == 3 and ei.value.rows == 15000 and ei.value.pages == 3 and "PAGE_MAX=3" in ei.value.reason
    assert any("分页截断" in r.getMessage() and "翻到 3 页仍满页" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)


def test_page_limit_from_env_secrets_default_and_off(monkeypatch, caplog):
    """page_limit(): 环境变量 TUSHARE_PAGE_LIMIT > secrets 键 tushare_page_limit > 缺省 5000; 非整数按缺省并 WARNING; 缓存; ≤ 0 关掉自动分页;
    credentials(refresh=True) 清缓存重读; 上限 3000 时恰 3000 行翻、5000 行不翻 (多于上限 = 本站上限其实更高)。"""
    monkeypatch.setattr(tc, "_read_secrets", lambda: {"tushare_page_limit": "2500"})
    monkeypatch.setenv("TUSHARE_PAGE_LIMIT", "3000")
    tc._PAGE_LIMIT = None
    assert tc.page_limit() == 3000
    monkeypatch.delenv("TUSHARE_PAGE_LIMIT")
    assert tc.page_limit() == 3000                               # 缓存
    tc._PAGE_LIMIT = None
    assert tc.page_limit() == 2500
    monkeypatch.setattr(tc, "_read_secrets", lambda: {"tushare_page_limit": "many"})
    tc._PAGE_LIMIT = None
    with caplog.at_level(logging.WARNING, logger="ashare.tushare"):
        assert tc.page_limit() == tc.PAGE_LIMIT_DEFAULT == 5000
    assert any("tushare_page_limit 不是整数" in r.getMessage() for r in caplog.records)
    monkeypatch.setattr(tc, "_read_secrets", lambda: {})
    tc._PAGE_LIMIT = None
    assert tc.page_limit() == 5000
    s = _install([_pg_ok(3000), _pg_ok(7, 3000)])
    tc._PAGE_LIMIT = 3000
    assert len(tc.query("daily", trade_date="20260929")) == 3007 and len(s.calls) == 2
    s = _install([_pg_ok(5000)])
    tc._PAGE_LIMIT = 3000
    assert len(tc.query("daily", trade_date="20260929")) == 5000 and len(s.calls) == 1
    s = _install([_pg_ok(5000)])
    tc._PAGE_LIMIT = 0
    assert len(tc.query("daily", trade_date="20260929")) == 5000 and len(s.calls) == 1
    monkeypatch.setattr(tc, "_read_secrets", lambda: {"tushare_token": TOKEN, "tushare_base_url": PRIMARY, "tushare_page_limit": 4000})
    for k in ("TUSHARE_TOKEN", "TUSHARE_BASE_URL", "TUSHARE_BASE_URL_BACKUP"):
        monkeypatch.delenv(k, raising=False)
    tc.credentials(refresh=True)
    assert tc.page_limit() == 4000


def test_pages_pinned_to_first_host_primary_second_page_fails_whole_query_raises(monkeypatch, caplog):
    """一次 query() 的所有页钉在第一页那个站 (卡 TS-PAGINATE-2 ①, 校验员 MEDIUM): 第二页主站 502×3 → 切换计数记满、状态切到备用 (switches=1),
    但续页**不**跟到备用去拼 (以前拼成 5559 当正确行为钉住) → 整次抛 TushareTruncated (rows=5000, __cause__ Transport), 本次没有一个请求打到备用;
    每页都带同一个 deadline_sec 进硬期限执行器; 退避照旧; 下一次 query() 才从第一页起走备用。"""
    seen = []
    real_call = tc._deadline_call

    def spy(fn, args, kwargs, dl):
        seen.append((args[0], args[1]["params"].get("offset"), dl))
        return real_call(fn, args, kwargs, dl)

    monkeypatch.setattr(tc, "_deadline_call", spy)
    s, naps = _install_fo([_pg_ok(5000), FakeResp({}, status=502), FakeResp({}, status=503), FakeResp({}, status=502)])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        with pytest.raises(tc.TushareTruncated) as ei:
            tc.query("daily", trade_date="20260929", deadline_sec=42)
    assert _urls(s) == [PRIMARY] * 4 and seen == [(PRIMARY, None, 42)] + [(PRIMARY, 5000, 42)] * 3
    assert ei.value.rows == 5000 and ei.value.pages == 1 and isinstance(ei.value.__cause__, tc.TushareTransport)
    st = tc.failover_state()
    assert st["active"] is True and st["switches"] == 1 and st["fails"] == tc.FAILOVER_AFTER and len(_switch_lines(caplog)) == 1
    assert naps == [tc.BACKOFF_SEC, tc.BACKOFF_SEC * 2]
    assert any("不跨站续页" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)
    s2 = _fo_session([_pg_ok(5000), _pg_ok(559, 5000)])                  # 下一次 query(): 第一页起就在备用, 两页都在备用
    df = tc.query("daily", trade_date="20260929")
    assert len(df) == 5559 and _urls(s2) == [BACKUP, BACKUP] and df.attrs["pages"] == 2


def test_pages_pinned_on_backup_second_page_failures_probe_primary_but_never_stitch(caplog):
    """备用上同理 (卡 TS-PAGINATE-2 ①): 第一页在备用, 第二页备用 502×3 → 备用失败计数照记 → 第 3 次试探主站 (单日 trade_cal) 成功 → 回切状态 (returns=1),
    但续页不跟到主站去拼 → 整次抛 TushareTruncated; 主站在本次 query() 里只收到试探的 trade_cal, 没收到任何 daily 页; 下一次 query() 整段在主站重拉。"""
    s, naps = _install_fo([_pg_ok(5000), FakeResp({}, status=502), FakeResp({}, status=502), FakeResp({}, status=502), _cal_ok()])
    with tc._FO_LOCK:
        tc._FO.update(active=True, reason="test", switched_at=tc.time.time(), last_probe_at=tc.time.monotonic())
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        with pytest.raises(tc.TushareTruncated) as ei:
            tc.query("daily", trade_date="20260929")
    assert _urls(s) == [BACKUP] * 4 + [PRIMARY] and _is_probe(s.calls[4])
    assert not [c for c in s.calls if c["url"] == PRIMARY and c["json"]["api_name"] == "daily"]
    st = tc.failover_state()
    assert st["active"] is False and st["returns"] == 1 and len(_return_lines(caplog)) == 1 and ei.value.rows == 5000
    s2 = _fo_session([_pg_ok(5559)])
    assert len(tc.query("daily", trade_date="20260929")) == 5559 and _urls(s2) == [PRIMARY]


def test_no_probe_between_pages_on_backup_probe_happens_at_next_query(caplog):
    """校验员 E1/E2 (verify:TS-PAGINATE MEDIUM): 在备用上翻页, 两页之间恰到「每 20 次调用试探主站」的点 —— 以前续页回切后打到主站, 两站行集 / 行序不同就静默丢行
    (E1 丢 3 只且日志看着正常; E2 降序回 → 去重后只剩 5000); 现在续页不试探、不换站: 两页都在备用, 5559 行齐, 没有回切行; 试探推到下一次 query() 的第一页之前,
    那一次整段都在主站重拉。「每 20 次调用」按 query() 数, 不按页数。"""
    s, naps = _install_fo([_pg_ok(5000), _pg_ok(559, 5000), _cal_ok(), _pg_ok(5559)])
    with tc._FO_LOCK:
        tc._FO.update(active=True, reason="test", switched_at=tc.time.time(), calls_on_backup=tc.PROBE_EVERY_CALLS - 2,
                      last_probe_at=tc.time.monotonic())
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df = tc.query("daily", trade_date="20260929")
    assert len(df) == 5559 and df["ts_code"].is_unique and df.attrs["pages"] == 2 and _urls(s) == [BACKUP, BACKUP]
    st = tc.failover_state()
    assert st["active"] is True and st["calls_on_backup"] == tc.PROBE_EVERY_CALLS - 1 and not _return_lines(caplog) and st["probes"] == 0
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df2 = tc.query("daily", trade_date="20260929")                   # 第 20 次 query(): 第一页前试探 → 回切 → 整次在主站
    assert len(df2) == 5559 and _urls(s)[2:] == [PRIMARY, PRIMARY] and _is_probe(s.calls[2]) and len(_return_lines(caplog)) == 1
    assert tc.failover_state()["active"] is False


def test_direct_query_paginates_for_probe_backup():
    """--probe-backup 的备用直调同样按上限分页 (否则备用 5000/5559 = 89.9% 过不了 ≥ 90% 的门): 两页 → 5559 行, 都打给定基址, 不碰切换状态;
    调用方传了 limit 则不翻。"""
    s, _ = _install_fo([_pg_ok(5000), _pg_ok(559, 5000)])
    df = tc._direct_query(BACKUP, TOKEN, "daily", "ts_code,trade_date,close", {"trade_date": "20260929"})
    assert len(df) == 5559 and df.attrs["pages"] == 2 and _urls(s) == [BACKUP, BACKUP]
    assert s.calls[0]["json"]["params"] == {"trade_date": "20260929"} and s.calls[1]["json"]["params"] == {"trade_date": "20260929", "offset": 5000}
    assert all(c["json"]["token"] == TOKEN and c["json"]["fields"] == "ts_code,trade_date,close" for c in s.calls)
    assert tc.failover_state() == _FO_INITIAL
    s, _ = _install_fo([_pg_ok(5000)])
    assert len(tc._direct_query(BACKUP, TOKEN, "daily", "", {"trade_date": "20260929", "limit": 5000})) == 5000 and len(s.calls) == 1


def test_probe_backup_passes_when_backup_needs_two_pages_per_day(monkeypatch):
    """10-24 实况: 主站 kaixin8 一页回全市场 5559 行, 备用 datahubco 每天 5000 + 559 两页 → 8 次 HTTP, 备用 5559/5559 行 → rc 0;
    备用 daily 行带「[分页 2 页]」; 不分页的旧判法是 5000/5559 = 89.9% < 90% → rc 1。secrets 不写 tushare_page_limit = 缺省 5000。"""
    full_d, full_p = _daily_rows(5559, "20261013"), _daily_rows(5559, "20260928")
    script = [_CAL_SPEC, _ok(_DAILY_COLS, full_d), _ok(_DAILY_COLS, full_p), _ok(["cal_date", "is_open"], _CAL),
              _ok(_DAILY_COLS, full_d[:5000]), _ok(_DAILY_COLS, full_d[5000:]),
              _ok(_DAILY_COLS, full_p[:5000]), _ok(_DAILY_COLS, full_p[5000:])]
    s, naps, lines = _probe_setup(monkeypatch, script, page_limit=None)
    rc = tc.probe_backup(out=lines.append, today=_TODAY)
    text = "\n".join(lines)
    assert rc == 0, text
    assert _urls(s) == [PRIMARY] * 3 + [BACKUP] * 5 and len(s.calls) == 8
    assert [c["json"]["params"].get("offset") for c in s.calls] == [None, None, None, None, None, 5000, None, 5000]
    assert f"备用 daily {_D}: 5559 行 (100% of 主站), 与主站重合代码 5559/5559" in text and text.count("[分页 2 页]") == 2
    assert f"daily {_D} 5559/5559 行" in text and f"daily {_P2} 5559/5559 行" in text and "结论 (rc 0)" in text
    assert "硬期限执行器: 直调" in text and TOKEN not in text and naps == []
    # 对照: 备用只给第一页、第二页 0 行 (镜像认 offset 但真的只有 5000 只) → 89.9% → rc 1
    script = [_CAL_SPEC, _ok(_DAILY_COLS, full_d), _ok(_DAILY_COLS, full_p), _ok(["cal_date", "is_open"], _CAL),
              _ok(_DAILY_COLS, full_d[:5000]), _ok(_DAILY_COLS, []), _ok(_DAILY_COLS, full_p[:5000]), _ok(_DAILY_COLS, [])]
    s, naps, lines = _probe_setup(monkeypatch, script, page_limit=None)
    assert tc.probe_backup(out=lines.append, today=_TODAY) == 1 and f"daily {_D} 行数 5000 < 主站 5559 × 0.9" in "\n".join(lines)


def test_page_limit_per_host_mapping(monkeypatch, caplog):
    """页上限按 host 配置 (卡 TS-PAGINATE-2 ②): secrets 键 tushare_page_limit 可为整数 (全局) 或 {host: n} 映射 (键 = 基址的 netloc, 也认完整基址, "*" = 其余站,
    没列的站缺省 5000); 环境变量 TUSHARE_PAGE_LIMIT 同形 (整数字串或 JSON 对象), 覆盖整个 secrets 配置; 映射里非整数的站按缺省并 WARNING; JSON 坏了按缺省并 WARNING;
    分页用的上限按**第一页实际打到的站**取: 主站 6000 → 恰 6000 行翻页、恰 5000 行不翻; 切到备用 (5000) → 恰 5000 行翻页。"""
    for k in ("TUSHARE_TOKEN", "TUSHARE_BASE_URL", "TUSHARE_BASE_URL_BACKUP", "TUSHARE_PAGE_LIMIT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(tc, "_read_secrets", lambda: {"tushare_page_limit": {"primary.invalid": 6000, "backup.invalid": "5000", "x.invalid": "many"}})
    tc._PAGE_LIMIT = None
    with caplog.at_level(logging.WARNING, logger="ashare.tushare"):
        assert tc.page_limit(PRIMARY) == 6000 and tc.page_limit(BACKUP) == 5000
    assert tc.page_limit("https://other.invalid/x") == 5000 and tc.page_limit() == 5000 and tc.page_limit("https://x.invalid/api") == 5000
    assert any("tushare_page_limit['x.invalid'] 不是整数" in r.getMessage() for r in caplog.records)
    monkeypatch.setattr(tc, "_read_secrets", lambda: {"tushare_page_limit": {"*": 3000, BACKUP: 5000}})
    tc._PAGE_LIMIT = None
    assert tc.page_limit(PRIMARY) == 3000 and tc.page_limit(BACKUP) == 5000 and tc.page_limit() == 3000     # "*" 兜其余站; 完整基址也认
    monkeypatch.setenv("TUSHARE_PAGE_LIMIT", '{"PRIMARY.invalid": 7000}')
    tc._PAGE_LIMIT = None
    assert tc.page_limit(PRIMARY) == 7000 and tc.page_limit(BACKUP) == 5000                               # 环境变量覆盖整个映射 (host 不分大小写), 没列的站缺省
    monkeypatch.setenv("TUSHARE_PAGE_LIMIT", "4500")
    tc._PAGE_LIMIT = None
    assert tc.page_limit(PRIMARY) == 4500 and tc.page_limit(BACKUP) == 4500
    caplog.clear()
    monkeypatch.setenv("TUSHARE_PAGE_LIMIT", "{bad json")
    tc._PAGE_LIMIT = None
    with caplog.at_level(logging.WARNING, logger="ashare.tushare"):
        assert tc.page_limit(PRIMARY) == 5000
    assert any("不是整数也不是 {host: n} 映射" in r.getMessage() for r in caplog.records)
    monkeypatch.delenv("TUSHARE_PAGE_LIMIT")
    per_host = {"primary.invalid": 6000, "backup.invalid": 5000}
    s, _ = _install_fo([_pg_ok(6000), _pg_ok(10, 6000)])
    tc._PAGE_LIMIT = per_host
    assert len(tc.query("daily", trade_date="20260929")) == 6010 and _urls(s) == [PRIMARY, PRIMARY]
    assert s.calls[1]["json"]["params"] == {"trade_date": "20260929", "offset": 6000}
    s, _ = _install_fo([_pg_ok(5000)])
    tc._PAGE_LIMIT = per_host
    assert len(tc.query("daily", trade_date="20260929")) == 5000 and len(s.calls) == 1                     # 主站上限 6000: 5000 行不是满页
    s, _ = _install_fo([_pg_ok(5000), _pg_ok(559, 5000)])
    tc._PAGE_LIMIT = per_host
    with tc._FO_LOCK:
        tc._FO.update(active=True, reason="test", switched_at=tc.time.time(), last_probe_at=tc.time.monotonic())
    assert len(tc.query("daily", trade_date="20260929")) == 5559 and _urls(s) == [BACKUP, BACKUP]
    s, _ = _install_fo([_pg_ok(5000), _pg_ok(559, 5000)])
    tc._PAGE_LIMIT = per_host
    assert len(tc._direct_query(BACKUP, TOKEN, "daily", "", {"trade_date": "20260929"})) == 5559 and len(s.calls) == 2   # 直调按给定基址取上限
    s, _ = _install_fo([_pg_ok(5000)])
    tc._PAGE_LIMIT = per_host
    assert len(tc._direct_query(PRIMARY, TOKEN, "daily", "", {"trade_date": "20260929"})) == 5000 and len(s.calls) == 1


def test_probe_backup_truncated_backup_page_is_a_failure(monkeypatch):
    """--probe-backup: 备用第二页失败 (直调不重试) → _direct_query 抛 TushareTruncated → 该日「打不通」→ rc 1 (以前续页传输失败也抛, 现在类名与行数进输出);
    另一天两页正常仍标「[分页 2 页]」。"""
    full_d, full_p = _daily_rows(5559, "20261013"), _daily_rows(5559, "20260928")
    script = [_CAL_SPEC, _ok(_DAILY_COLS, full_d), _ok(_DAILY_COLS, full_p), _ok(["cal_date", "is_open"], _CAL),
              _ok(_DAILY_COLS, full_d[:5000]), FakeResp({}, status=502),
              _ok(_DAILY_COLS, full_p[:5000]), _ok(_DAILY_COLS, full_p[5000:])]
    s, naps, lines = _probe_setup(monkeypatch, script, page_limit=None)
    rc = tc.probe_backup(out=lines.append, today=_TODAY)
    text = "\n".join(lines)
    assert rc == 1 and len(s.calls) == 8 and naps == []
    assert f"备用 daily {_D} 失败: TushareTruncated" in text and "已拿到 5000 行 1 页" in text and f"daily {_D} 打不通或不认这把 key" in text
    assert text.count("[分页 2 页]") == 1 and f"备用 daily {_P2}: 5559 行 (100% of 主站)" in text and TOKEN not in text


# ---------------------------------------------------------------------------
# 进程池起不来 / 挂住 → 线程版硬期限 (卡 TS-PAGINATE 第 3 项; 10-24 GM 在非交互 Bash 里 --probe-backup 挂死到 300s)
def _stub_datasource(monkeypatch, runner, **attrs):
    """装一个假 ashare.datasource (sys.modules 与包属性都换 —— `from . import datasource` 走 _handle_fromlist, 包上有属性就不看 sys.modules)。"""
    import ashare as _pkg
    stub = types.ModuleType("ashare.datasource")
    stub._call_with_deadline = runner
    stub._POOL_DISABLED = False
    for k, v in attrs.items():
        setattr(stub, k, v)
    monkeypatch.setitem(sys.modules, "ashare.datasource", stub)
    monkeypatch.setattr(_pkg, "datasource", stub, raising=False)
    return stub


def _pool_lines(caplog):
    return [r for r in caplog.records if "进程池硬期限不可用" in r.getMessage()]


def test_pool_permission_error_falls_back_to_thread_deadline_once(monkeypatch, caplog):
    """执行器建池抛 PermissionError [WinError 5] (计划任务 / 非交互 Bash 的 DuplicateHandle 形态) → WARNING 一行, 本次与余下调用走线程版硬期限
    (结果正常), 执行器不再被碰, datasource._POOL_DISABLED 也被置 True; 回退后期限仍生效 (线程版到点抛 TimeoutError, 不 terminate 任何东西)。"""
    hits = []

    def boom(fn, args, kwargs, dl):
        hits.append(dl)
        raise PermissionError(5, "拒绝访问")

    stub = _stub_datasource(monkeypatch, boom)
    s = _install([_ok(["a"], [[1]]), _ok(["a"], [[2]])])
    tc.USE_PROCESS_DEADLINE = True
    try:
        with caplog.at_level(logging.INFO, logger="ashare.tushare"):
            df = tc.query("daily", trade_date="20260929", deadline_sec=42)
            df2 = tc.query("daily", trade_date="20260930", deadline_sec=42)
    finally:
        tc.USE_PROCESS_DEADLINE = False
    assert len(df) == 1 and len(df2) == 1 and len(s.calls) == 2
    assert hits == [42] and stub._POOL_DISABLED is True
    st = tc.pool_state()
    assert st["disabled"] is True and "PermissionError" in st["reason"] and TOKEN not in st["reason"]
    lines = _pool_lines(caplog)
    assert len(lines) == 1 and lines[0].levelno == logging.WARNING and "线程版硬期限" in lines[0].getMessage() and TOKEN not in caplog.text
    import threading
    gate = threading.Event()
    tc.USE_PROCESS_DEADLINE = True
    try:
        with pytest.raises(TimeoutError) as ei:
            tc._deadline_call(gate.wait, (), {}, 0.02)
        assert "0.02" in str(ei.value) and "线程版" in str(ei.value) and "硬期限" in str(ei.value)
    finally:
        gate.set()
        tc.USE_PROCESS_DEADLINE = False
    assert hits == [42]                                         # 回退后再没碰执行器


def test_pool_infra_errors_vs_business_errors(monkeypatch):
    """_is_pool_infra_error: BrokenProcessPool / _PoolUnavailable / 裸 PermissionError·BrokenPipeError·EOFError = 池的; TimeoutError / TushareError /
    requests 的异常 / 带 RemoteTraceback (工作进程里 fn 抛的) / 普通 OSError·RuntimeError = 业务的 → 原样进 query 的重试与分类, 不回退。"""
    from concurrent.futures.process import BrokenProcessPool
    from multiprocessing.pool import RemoteTraceback
    import requests
    assert tc._is_pool_infra_error(BrokenProcessPool("A child process terminated abruptly"))
    assert tc._is_pool_infra_error(PermissionError(5, "拒绝访问")) and tc._is_pool_infra_error(BrokenPipeError()) and tc._is_pool_infra_error(EOFError("Ran out of input"))

    class _PoolUnavailable(RuntimeError):
        pass
    assert tc._is_pool_infra_error(_PoolUnavailable("进程池已判定不可用"))
    assert not tc._is_pool_infra_error(TimeoutError("x 超过 42s 硬期限")) and not tc._is_pool_infra_error(tc.TushareTransport("x"))
    assert not tc._is_pool_infra_error(OSError("Connection refused")) and not tc._is_pool_infra_error(RuntimeError("业务"))
    assert not tc._is_pool_infra_error(requests.exceptions.ConnectionError("x")) and not tc._is_pool_infra_error(ValueError("x"))
    e = PermissionError(13, "fn 自己抛的")
    e.__cause__ = RemoteTraceback("...")
    assert not tc._is_pool_infra_error(e)

    def fn_error(fn, args, kwargs, dl):
        raise OSError("Connection refused")

    _stub_datasource(monkeypatch, fn_error)
    _install([])
    _no_sleep()
    tc.USE_PROCESS_DEADLINE = True
    try:
        with pytest.raises(tc.TushareTransport):
            tc.query("daily", retries=2)
    finally:
        tc.USE_PROCESS_DEADLINE = False
    assert tc.pool_state()["disabled"] is False


def test_pool_machinery_hang_trips_watchdog_and_falls_back(monkeypatch, caplog):
    """执行器自己不回来 (建池 / terminate 挂住: 10-24 形态) → 等 deadline_sec + POOL_GRACE_SEC 后判池机制挂死 → WARNING, 线程版拿到结果 (期限仍是
    deadline_sec); 之后的调用不再碰执行器 (卡住的线程被弃)。"""
    import threading
    gate = threading.Event()
    hits = []

    def hang(fn, args, kwargs, dl):
        hits.append(dl)
        gate.wait(5)

    stub = _stub_datasource(monkeypatch, hang)
    monkeypatch.setattr(tc, "POOL_GRACE_SEC", 0.05)
    s = _install([_ok(["a"], [[1]]), _ok(["a"], [[2]])])
    tc.USE_PROCESS_DEADLINE = True
    try:
        with caplog.at_level(logging.INFO, logger="ashare.tushare"):
            t0 = tc.time.monotonic()
            df = tc.query("daily", deadline_sec=0.05)
            dt = tc.time.monotonic() - t0
            df2 = tc.query("daily", deadline_sec=0.05)
    finally:
        gate.set()
        tc.USE_PROCESS_DEADLINE = False
    assert len(df) == 1 and len(df2) == 1 and len(s.calls) == 2 and hits == [0.05] and dt < 2
    st = tc.pool_state()
    assert st["disabled"] is True and "仍未从进程池机制返回" in st["reason"] and stub._POOL_DISABLED is True
    assert len(_pool_lines(caplog)) == 1


def test_pool_fallback_with_real_datasource_make_pool_permission_error(caplog):
    """真 datasource: 建池 (_make_pool) 抛 PermissionError → 它自己兜住 (WARNING「进程池不可用」, _POOL_DISABLED) 并走它的线程版, Tushare 调用结果正常;
    tushare_client 这侧不另告警 (异常没漏出来) —— 两层各管一段, 不打架。"""
    from ashare import datasource as ds
    ds._pool_reset()
    orig_make, orig_flag = ds._make_pool, ds._POOL_DISABLED
    ds._POOL_DISABLED = False
    calls = []

    def boom(ctx):
        calls.append(1)
        raise PermissionError(5, "拒绝访问")
    ds._make_pool = boom
    s = _install([_ok(["a"], [[1]]), _ok(["a"], [[2]])])
    tc.USE_PROCESS_DEADLINE = True
    try:
        with caplog.at_level(logging.INFO):
            df = tc.query("daily", trade_date="20260929")
            df2 = tc.query("daily", trade_date="20260930")
        flag_after = ds._POOL_DISABLED
    finally:
        tc.USE_PROCESS_DEADLINE = False
        ds._make_pool = orig_make
        ds._POOL_DISABLED = orig_flag
        ds._pool_reset()
    assert len(df) == 1 and len(df2) == 1 and len(s.calls) == 2 and calls == [1] and flag_after is True
    ds_warn = [r for r in caplog.records if "进程池不可用" in r.getMessage() and r.name == "ashare.datasource"]
    assert len(ds_warn) == 1 and not _pool_lines(caplog) and tc.pool_state()["disabled"] is False


def test_probe_backup_warms_the_pool_first_and_reports_fallback(monkeypatch):
    """--probe-backup 开头先热身 (_pool_ping, POOL_WARMUP_SEC): 执行器抛 PermissionError → 热身那一次就回退线程版, 6 次调用全在线程版里跑完 (rc 0),
    输出「硬期限执行器: 线程版硬期限 (进程池不可用: PermissionError…)」; 热身只碰执行器一次。执行器健康 → 「进程池硬期限就绪」。"""
    hits = []

    def boom(fn, args, kwargs, dl):
        hits.append((getattr(fn, "__name__", ""), dl))
        raise PermissionError(5, "拒绝访问")

    _stub_datasource(monkeypatch, boom)
    s, naps, lines = _probe_setup(monkeypatch, _probe_script())
    tc.USE_PROCESS_DEADLINE = True
    try:
        rc = tc.probe_backup(out=lines.append, today=_TODAY)
    finally:
        tc.USE_PROCESS_DEADLINE = False
    text = "\n".join(lines)
    assert rc == 0, text
    assert hits == [("_pool_ping", tc.POOL_WARMUP_SEC)] and len(s.calls) == 6
    assert "硬期限执行器: 线程版硬期限 (进程池不可用: PermissionError" in text and TOKEN not in text
    assert tc.pool_state()["disabled"] is True
    _stub_datasource(monkeypatch, lambda fn, a, k, dl: fn(*a, **k))
    s, naps, lines = _probe_setup(monkeypatch, _probe_script())
    tc.USE_PROCESS_DEADLINE = True
    try:
        rc = tc.probe_backup(out=lines.append, today=_TODAY)
    finally:
        tc.USE_PROCESS_DEADLINE = False
    text = "\n".join(lines)
    assert rc == 0 and "硬期限执行器: 进程池硬期限就绪 (本进程)" in text and tc.pool_state()["disabled"] is False


# 看门线程两段计时 + 二次探针 (卡 TS-PAGINATE-3, 对校验员 10-25 W1 = D3 原场景: 上一版到点只看 `_POOL is None`, 池在检查点之前刚建好、随后的调用
# 又接近期限就被判挂死)。前四条用**真** datasource 的 _pool / _call_with_deadline / _POOL / 建池锁, 只把池对象换成 _FakePool (不起进程), 时间常量按比例缩小。
class _Abandon(BaseException):
    """用例收尾专用: 被放开的「挂住的 terminate」抛它, 让被弃的工作线程安静退场 (不再往 FakeSession 发请求、不串到别的用例), 并放开建池锁。"""


class _FakeAsync:
    def __init__(self, fn, args, kwds, delay):
        self.fn, self.args, self.kwds, self.delay = fn, args, kwds or {}, delay

    def get(self, timeout=None):
        import multiprocessing as mp
        import threading
        if timeout is not None and self.delay > timeout:          # 与真 AsyncResult 一样守 timeout
            threading.Event().wait(timeout)
            raise mp.TimeoutError()
        threading.Event().wait(self.delay)
        return self.fn(*self.args, **self.kwds)


class _FakePool:
    """datasource._make_pool 换成它: ready = 建池就绪探针 (第一次 os.getpid) 多慢; ping = 之后的 os.getpid (看门线程的二次探针) 多慢; call = 业务调用多慢
    (到点在调 get 的那个线程里跑 fn, 即 _post_json → 用例的 FakeSession); hang = terminate 等这个 Event (10-24 WinError 5 形态: Pool.terminate 不回来),
    放开后抛 _Abandon; workers = 给 _pool_busy 看的工作进程 (假); built=True = 就绪探针已经做过 (直接当成建好的池用)。"""

    def __init__(self, ready=0.0, call=0.0, ping=0.0, hang=None, workers=(), built=False):
        self.ready, self.call, self.ping, self.hang = ready, call, ping, hang
        self._state, self._pool, self._cache = "RUN", list(workers), {}
        self.probed, self.pings, self.terminated = built, 0, 0

    def apply_async(self, fn, args=(), kwds=None):
        if fn is os.getpid:
            if not self.probed:
                self.probed = True
                return _FakeAsync(fn, args, kwds, self.ready)
            self.pings += 1
            return _FakeAsync(fn, args, kwds, self.ping)
        return _FakeAsync(fn, args, kwds, self.call)

    def terminate(self):
        self.terminated += 1
        if self.hang is not None:
            self.hang.wait(30)
            raise _Abandon()
        self._state = "TERMINATE"

    def join(self):
        pass


class _AliveWorker:
    def is_alive(self):
        return True


def _real_ds_fake_pool(monkeypatch, pool, probe_sec=1.0, built=False):
    """真 datasource, 只换池对象; 建池锁换一把本用例自己的 (被弃的线程持着也碍不着别的用例)。"""
    import threading
    from ashare import datasource as ds
    monkeypatch.setattr(ds, "_make_pool", lambda ctx: pool)
    monkeypatch.setattr(ds, "_POOL_DOA_WAIT_SEC", 0.0)
    monkeypatch.setattr(ds, "_POOL_PROBE_SEC", probe_sec)
    monkeypatch.setattr(ds, "_POOL", pool if built else None)
    monkeypatch.setattr(ds, "_POOL_DISABLED", False)
    monkeypatch.setattr(ds, "_POOL_LOCK", threading.RLock())
    if built:
        pool.probed = True
    return ds


def _pq(deadline_sec):
    """USE_PROCESS_DEADLINE=True 跑一次 query (retries=1) -> (df, 墙钟秒)。"""
    tc.USE_PROCESS_DEADLINE = True
    t0 = tc.time.monotonic()
    try:
        df = tc.query("daily", trade_date="20260929", retries=1, deadline_sec=deadline_sec)
    finally:
        tc.USE_PROCESS_DEADLINE = False
    return df, tc.time.monotonic() - t0


def _wait_runner_gone(timeout=5.0):
    """放开挂住的假 terminate 之后, 等被弃的工作线程 (tushare-pool-*) 退场再进下一条用例。"""
    import threading
    end = tc.time.monotonic() + timeout
    while tc.time.monotonic() < end and [x for x in threading.enumerate() if x.name.startswith("tushare-pool-") and x.is_alive()]:
        threading.Event().wait(0.02)


def test_watchdog_d3_slow_pool_build_then_near_deadline_call_is_not_hung(monkeypatch, caplog):
    """校验员 10-25 W1 = D3 原场景 (MEDIUM): 池建得慢 (就绪探针 0.4s, datasource 自己的预算 1s 内, 健康) + 随后的调用接近期限 (0.3s < 期限 0.4s)。
    上一版从调用开始统一按 期限 0.4 + 宽限 0.15 = 0.55s 计, 到点池**已建成** (0.4s 时建好) → 判池机制挂死 → 永久回退线程版 + ds._POOL_DISABLED + 同一请求
    发两次; 现在「取池」段按建池预算计、「调用」段从池到手那一刻起按期限计 → 不回退、各 1 次 HTTP、不碰 ds._POOL_DISABLED、无 WARNING、不触发二次探针。
    池已建成、上一调用慢但健康, 再来一次接近期限的调用 (0.3s) 同样不回退; 检查点时池还在建 (0.7s, 仍在预算内; 校验员 W3 对照) 也不回退。
    「取池」段的上限从**真** datasource 常量读: _POOL_DOA_WAIT_SEC + _POOL_PROBE_SEC = 2 + 90 = 92s (不写死)。"""
    from ashare import datasource as real_ds
    assert tc._pool_build_budget(real_ds) == real_ds._POOL_DOA_WAIT_SEC + real_ds._POOL_PROBE_SEC == 92.0
    pool = _FakePool(ready=0.4, call=0.3)
    ds = _real_ds_fake_pool(monkeypatch, pool, probe_sec=1.0)
    monkeypatch.setattr(tc, "POOL_GRACE_SEC", 0.15)
    s = _install([_ok(["a"], [[1]]), _ok(["a"], [[2]])])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df, dt = _pq(0.4)
        assert ds._POOL is pool                                       # 池在第一次调用的「取池」段里建好
        df2, dt2 = _pq(0.4)
    assert len(df) == 1 and len(df2) == 1 and len(s.calls) == 2       # 各 1 次 HTTP, 没有重发
    assert 0.6 <= dt < 1.5 and 0.25 <= dt2 < 1.0, (dt, dt2)
    assert tc.pool_state()["disabled"] is False and ds._POOL_DISABLED is False
    assert not _pool_lines(caplog) and not [r for r in caplog.records if "二次探针" in r.getMessage()]
    pool3 = _FakePool(ready=0.7, call=0.1)
    ds = _real_ds_fake_pool(monkeypatch, pool3, probe_sec=1.0)
    s = _install([_ok(["a"], [[3]])])
    df3, dt3 = _pq(0.4)
    assert len(df3) == 1 and len(s.calls) == 1 and 0.75 <= dt3 < 1.5 and ds._POOL is pool3
    assert tc.pool_state()["disabled"] is False and ds._POOL_DISABLED is False


def test_watchdog_waits_for_another_threads_slow_pool_build(monkeypatch, caplog):
    """「取池」段也盖住「等别的线程建池」: 别的线程 (akshare / 另一个 Tushare 调用) 先进 _pool() 建池 (0.4s, 健康), 本调用晚 0.05s 到, 在建池锁上等它建完,
    池到手后调用 0.3s (接近期限 0.4s)。上一版检查点 0.05 + 0.4 + 0.15 = 0.6s 时池已建成 → 误判挂死; 现在不回退、池只建一次、1 次 HTTP。"""
    import threading
    pool = _FakePool(ready=0.4, call=0.3)
    ds = _real_ds_fake_pool(monkeypatch, pool, probe_sec=1.0)
    made = []
    monkeypatch.setattr(ds, "_make_pool", lambda ctx: made.append(1) or pool)
    monkeypatch.setattr(tc, "POOL_GRACE_SEC", 0.15)
    s = _install([_ok(["a"], [[1]])])
    other = threading.Thread(target=ds._pool, daemon=True)
    other.start()
    threading.Event().wait(0.05)
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df, dt = _pq(0.4)
    other.join(2)
    assert len(df) == 1 and len(s.calls) == 1 and made == [1] and ds._POOL is pool and 0.55 <= dt < 1.5, dt
    assert tc.pool_state()["disabled"] is False and ds._POOL_DISABLED is False and not _pool_lines(caplog)


def test_watchdog_true_hang_in_pool_build_falls_back_after_build_budget(monkeypatch, caplog):
    """真挂死 (10-24 WinError 5 形态之一): 建池的就绪探针不回 (5s > 预算 0.3s) → datasource 去 terminate 这个池, terminate 挂住 (还持着建池锁)。
    「取池」段给足建池预算 0.3 + 宽限 0.15s 后二次探针: 池不在、建池锁仍被持着 → 挂住 → WARNING 一行, 线程版拿到结果 (1 次 HTTP), ds._POOL_DISABLED 置 True;
    reason 写明「取池」段与二次探针结论。上一版要等 期限 + 宽限 + 建池预算 (这里 0.4 + 0.15 + 0.3s) 才回退。"""
    import threading
    gate = threading.Event()
    pool = _FakePool(ready=5.0, hang=gate)
    ds = _real_ds_fake_pool(monkeypatch, pool, probe_sec=0.3)
    monkeypatch.setattr(tc, "POOL_GRACE_SEC", 0.15)
    s = _install([_ok(["a"], [[1]])])
    try:
        with caplog.at_level(logging.INFO, logger="ashare.tushare"):
            df, dt = _pq(0.4)
        st = tc.pool_state()
        assert len(df) == 1 and len(s.calls) == 1 and 0.4 <= dt < 0.85, dt
        assert st["disabled"] is True and ds._POOL_DISABLED is True and pool.terminated == 1
        assert "「取池」段超过建池预算 0.3s + 宽限 0.15s" in st["reason"] and "建池锁仍被持着" in st["reason"]
        assert "仍未从进程池机制返回" in st["reason"] and len(_pool_lines(caplog)) == 1 and TOKEN not in caplog.text
    finally:
        gate.set()
        _wait_runner_gone()


def test_watchdog_true_hang_after_call_probe_unanswered_falls_back(monkeypatch, caplog):
    """真挂死 (另两种形态, 「探针也不回 → 回退」): ① 池已建成, 调用超过硬期限 → datasource terminate 整池 (_POOL 先置 None), terminate 挂住 (不持建池锁)
    —— 「调用」段到点 (期限 0.3 + 宽限 0.15s) 二次探针: 池不在、也没有线程在建 → 回退 (上一版还要因为 _POOL is None 再白等一份建池预算, 这里 2s);
    ② 池对象还在, os.getpid 探针也不回 (0.5s > POOL_PING_SEC 0.05s), 工作进程列表为空 (不是「正忙」) → 回退, reason 写明探针未应答。"""
    import threading
    gate = threading.Event()
    pool = _FakePool(call=5.0, hang=gate)
    ds = _real_ds_fake_pool(monkeypatch, pool, probe_sec=2.0, built=True)
    monkeypatch.setattr(tc, "POOL_GRACE_SEC", 0.15)
    s = _install([_ok(["a"], [[1]])])
    try:
        with caplog.at_level(logging.INFO, logger="ashare.tushare"):
            df, dt = _pq(0.3)
        st = tc.pool_state()
        assert len(df) == 1 and len(s.calls) == 1 and 0.4 <= dt < 1.2, dt
        assert st["disabled"] is True and ds._POOL_DISABLED is True and ds._POOL is None and pool.terminated == 1
        assert "「调用」段超过硬期限 0.3s + 宽限 0.15s" in st["reason"] and "也没有线程在建" in st["reason"]
    finally:
        gate.set()
        _wait_runner_gone()
    tc._pool_fallback_reset()
    gate2 = threading.Event()
    stub = _stub_datasource(monkeypatch, lambda fn, a, k, dl: gate2.wait(5), _POOL=_FakePool(ping=0.5, built=True))
    monkeypatch.setattr(tc, "POOL_PING_SEC", 0.05)
    s = _install([_ok(["a"], [[1]])])
    try:
        df, dt = _pq(0.1)
    finally:
        gate2.set()
    st = tc.pool_state()
    assert len(df) == 1 and len(s.calls) == 1 and dt < 1.5 and st["disabled"] is True and stub._POOL_DISABLED is True
    assert "os.getpid 探针 0.05s 未应答" in st["reason"] and stub._POOL.pings == 1


def test_watchdog_probe_alive_busy_or_rebuilding_waits_one_more_round(monkeypatch, caplog):
    """「池正忙 / 慢」≠ 挂住: 「调用」段到点 (期限 0.2 + 宽限 0.05s) 时 ① os.getpid 探针应答 → 再等一轮, 结果 0.4s 到 → 不回退、1 次 HTTP; ② 探针没轮上
    (0.5s > POOL_PING_SEC 0.05s) 但工作进程都活着、任务占满 (_cache 3 > 2) → 正忙 → 同样再等一轮; ③ 池不在而建池锁有线程持着 → 正在重建 → 再等一轮
    (这一轮加建池预算 0.3s: 0.3 + 0.2 + 0.05); ④ 探针说活着、再等的一轮也用完仍不回 → 回退 (reason 带「又等了 1 轮」) —— 看门线程始终有界。"""
    import threading
    monkeypatch.setattr(tc, "POOL_GRACE_SEC", 0.05)
    monkeypatch.setattr(tc, "POOL_PING_SEC", 0.05)

    def slow(fn, a, k, dl):
        threading.Event().wait(0.4)
        return fn(*a, **k)

    def extra_rounds(caplog):
        return [r.getMessage() for r in caplog.records if "二次探针说池没挂" in r.getMessage()]

    for label, pool in (("应答", _FakePool(ping=0.0, built=True)),
                        ("正忙", _FakePool(ping=0.5, built=True, workers=[_AliveWorker(), _AliveWorker()]))):
        if label == "正忙":
            pool._cache = {0: None, 1: None, 2: None}
        caplog.clear()
        stub = _stub_datasource(monkeypatch, slow, _POOL=pool)
        s = _install([_ok(["a"], [[1]])])
        with caplog.at_level(logging.INFO, logger="ashare.tushare"):
            df, dt = _pq(0.2)
        lines = extra_rounds(caplog)
        assert len(df) == 1 and len(s.calls) == 1 and tc.pool_state()["disabled"] is False and stub._POOL_DISABLED is False, label
        assert len(lines) == 1 and label in lines[0] and "再等一轮 0.25s" in lines[0] and not _pool_lines(caplog), lines
    lk = threading.RLock()
    hold, release = threading.Event(), threading.Event()

    def holder():
        with lk:
            hold.set()
            release.wait(5)
    threading.Thread(target=holder, daemon=True).start()
    hold.wait(2)
    caplog.clear()
    try:
        stub = _stub_datasource(monkeypatch, slow, _POOL=None, _pool_lock=lambda: lk, _POOL_DOA_WAIT_SEC=0.1, _POOL_PROBE_SEC=0.2)
        s = _install([_ok(["a"], [[1]])])
        with caplog.at_level(logging.INFO, logger="ashare.tushare"):
            df, dt = _pq(0.2)
    finally:
        release.set()
    lines = extra_rounds(caplog)
    assert len(df) == 1 and len(s.calls) == 1 and tc.pool_state()["disabled"] is False
    assert len(lines) == 1 and "池正在重建" in lines[0] and "再等一轮 0.55s" in lines[0], lines
    gate = threading.Event()
    pool = _FakePool(ping=0.0, built=True)
    stub = _stub_datasource(monkeypatch, lambda fn, a, k, dl: gate.wait(5), _POOL=pool)
    s = _install([_ok(["a"], [[1]])])
    try:
        df, dt = _pq(0.2)
    finally:
        gate.set()
    st = tc.pool_state()
    assert len(df) == 1 and len(s.calls) == 1 and 0.45 <= dt < 1.5 and pool.pings == 2, dt
    assert st["disabled"] is True and "(又等了 1 轮)" in st["reason"] and "仍未从进程池机制返回" in st["reason"]


def test_watchdog_uses_a_result_that_lands_during_the_probe(monkeypatch, caplog):
    """二次探针要花时间 (生产最多 POOL_PING_SEC + 1 = 11s): 「调用」段到点 (0.1 + 0.05s) 时结果还没到, 探针 (os.getpid 0.5s 不回, 工作进程列表为空 → 不是「正忙」)
    判「不活」—— 但探针那 0.3s 里结果到了 (0.3s 时) → 直接用它: 不回退、不重发 (1 次 HTTP)、不碰 ds._POOL_DISABLED、不打 WARNING。"""
    import threading
    monkeypatch.setattr(tc, "POOL_GRACE_SEC", 0.05)
    monkeypatch.setattr(tc, "POOL_PING_SEC", 0.3)

    def lands_mid_probe(fn, a, k, dl):
        threading.Event().wait(0.3)
        return fn(*a, **k)

    stub = _stub_datasource(monkeypatch, lands_mid_probe, _POOL=_FakePool(ping=0.5, built=True))
    s = _install([_ok(["a"], [[1]])])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df, dt = _pq(0.1)
    assert len(df) == 1 and len(s.calls) == 1 and stub._POOL.pings == 1 and dt < 1.2, dt
    assert tc.pool_state()["disabled"] is False and stub._POOL_DISABLED is False and not _pool_lines(caplog)


def test_watchdog_rejudges_when_segment_advances_during_the_probe(monkeypatch, caplog):
    """二次探针判的是「已经到点的那一段」: 「取池」段到点 (建池预算 0.2 + 宽限 0.05s) 时开始探针 (池对象已在, os.getpid 0.5s 不回, POOL_PING_SEC 0.2s →
    「不活」), 探针那 0.2s 里池到手、进了「调用」段 (0.3s) —— 不按过去那段判挂住, 按新段重新计时 (期限 0.5 + 宽限 0.05s), 调用 0.3s 后结果到 →
    不回退、1 次 HTTP。没有这一步会在 0.45s 判池机制挂住、回退线程版并重发。"""
    import threading
    monkeypatch.setattr(tc, "POOL_GRACE_SEC", 0.05)
    monkeypatch.setattr(tc, "POOL_PING_SEC", 0.2)
    got_pool = threading.Event()
    stub = _stub_datasource(monkeypatch, None, _POOL=None, _POOL_DOA_WAIT_SEC=0.1, _POOL_PROBE_SEC=0.1)

    def acquire():                                  # 取池: 池对象先挂上 (探针看得到), 但要等到 0.3s 才「到手」
        stub._POOL = _FakePool(ping=0.5, built=True)
        got_pool.wait(5)
        return stub._POOL

    def call(fn, a, k, dl):
        threading.Event().wait(0.3)
        return fn(*a, **k)
    stub._pool, stub._call_with_deadline = acquire, call
    threading.Timer(0.3, got_pool.set).start()
    s = _install([_ok(["a"], [[1]])])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df, dt = _pq(0.5)
    assert len(df) == 1 and len(s.calls) == 1 and stub._POOL.pings == 1 and 0.55 <= dt < 1.5, dt
    assert tc.pool_state()["disabled"] is False and stub._POOL_DISABLED is False and not _pool_lines(caplog)


def test_pool_alive_and_busy_on_a_real_spawn_pool():
    """二次探针读的 multiprocessing.pool.Pool 内部 (_state / _pool / _cache) 在本机 Python 上对得上 (真 spawn 池 2 个工作进程, 有界, 约 3–5s):
    空闲 → os.getpid 毫秒级应答 = 活着; 两个工作进程各跑 1.5s 的任务 → 探针没轮上 (POOL_PING_SEC 缩到 0.3s) 但正忙 = 活着; terminate 之后 → 拒收 = 挂住。"""
    import multiprocessing as mp
    p = mp.get_context("spawn").Pool(processes=2)
    ds = types.SimpleNamespace(_POOL=p)
    saved = tc.POOL_PING_SEC
    try:
        p.apply_async(os.getpid).get(timeout=60)                     # 等工作进程起来
        alive, why, rebuilding = tc._pool_alive(ds, "call")
        assert alive and "应答" in why and not rebuilding, why
        tc.POOL_PING_SEC = 0.3
        busy = [p.apply_async(_REAL_SLEEP, (1.5,)) for _ in range(2)]
        alive, why, rebuilding = tc._pool_alive(ds, "call")
        assert alive and "正忙" in why and not rebuilding, why
        for r in busy:
            r.get(timeout=30)
    finally:
        tc.POOL_PING_SEC = saved
        p.terminate()
        p.join()
    alive, why, _ = tc._pool_alive(ds, "call")
    assert not alive and "未应答" in why, why


def test_truncated_is_a_failure_for_every_production_caller(monkeypatch, tmp_path):
    """TushareTruncated 走调用方**现有**的异常路径, 没有一条把它当成功 (卡 TS-PAGINATE-2 ④ 核过): market.fetch_bars_by_date 原样抛 (不吞);
    datasource.fetch_daily_basic_tushare → None (不缓存、不记「当日未出」); market.fetch_bars_bulk_tushare → 该股跳过;
    leftside_core.pricestore 按日增量: 抛出的那天不写、meta.max_trade_date 不推、之前那天已提交 (异常原样抛到调用方, 与传输失败同路 —— run_a.sh 那行
    `|| echo warn` 接住, 就绪闸门按库判)。研究湖 lake.pull_daily_basic 的 `except Exception → failed` 在 stock-core, 本仓不测 (代码核过)。"""
    import sqlite3
    import ashare.market as mk
    from ashare import datasource as ds
    from leftside_core import pricestore as ps
    from leftside_core import market as lm
    err = tc.TushareTruncated("daily: 分页截断 (x) — 续页失败; 已拿到 5000 行 1 页", "daily", rows=5000, pages=1, reason="续页失败")

    def boom(api, **kw):
        raise err

    monkeypatch.setattr(mk, "_tushare_on", lambda: True)
    monkeypatch.setattr(tc, "query", boom)
    with pytest.raises(tc.TushareTruncated):
        mk.fetch_bars_by_date("2026-09-29")
    assert mk.fetch_bars_bulk_tushare(["000001"], "2026-01-01") == {}
    monkeypatch.setattr(ds, "_ts_query", boom)
    monkeypatch.setattr(ds, "_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(ds, "_ts_daily_basic_short", {})
    saved_keys = []
    monkeypatch.setattr(ds, "_cache_save", lambda key, obj: saved_keys.append(key))
    monkeypatch.setattr(ds, "_cache_load", lambda key, max_age_h=None: None)
    assert ds.fetch_daily_basic_tushare("20260929") is None and saved_keys == [] and ds._ts_daily_basic_short == {}
    # pricestore 按日增量: 第二天抛 → 第一天已写并提交, 第二天不写, meta 不推
    days = ["2026-09-03", "2026-09-04", "2026-09-07"]
    bars = {dd: {"000001": (10.0 + i, 11.0 + i, 9.0 + i, 10.5 + i, 100.0, 1000.0),
                 "000002": (20.0 + i, 21.0 + i, 19.0 + i, 20.5 + i, 200.0, 2000.0)} for i, dd in enumerate(days)}
    facs = {dd: {"000001": 3.0, "000002": 1.0} for dd in days}
    uni = [("000001", "甲", "2020-01-01", "", "L"), ("000002", "乙", "2020-01-01", "", "L")]
    asked = []

    def by_date(dd):
        asked.append(dd)
        if dd == days[2]:
            raise err
        return bars[dd]

    d = str(tmp_path / "store")
    os.makedirs(d)
    saved_market = lm._CURRENT
    lm.set_market(lm.Market(name="ashare", dashboard_dir=d, data_dir=d, db_path=os.path.join(d, "x.db"),
                            fetch_bars_bulk=lambda codes, start: {}, fetch_index_bars=lambda s: [],
                            fetch_bars_by_date=by_date, fetch_adj_by_date=lambda dd: facs[dd],
                            trading_days=lambda a, b: [x for x in days if a <= x <= b], fetch_universe_rows=lambda: uni))
    try:
        conn = ps._conn()
        conn.executemany("INSERT OR REPLACE INTO bars_raw VALUES(?,?,?,?,?,?,?,?)", [(c, days[0], *v) for c, v in bars[days[0]].items()])
        conn.executemany("INSERT OR REPLACE INTO adj VALUES(?,?,?)", [(c, days[0], f) for c, f in facs[days[0]].items()])
        conn.executemany("INSERT OR REPLACE INTO universe(code,name,list_date,delist_date,status) VALUES(?,?,?,?,?)", uni)
        conn.commit()
        ps.materialize(None, conn)
        conn.close()
        with pytest.raises(tc.TushareTruncated):
            ps.update_daily()
        conn = sqlite3.connect(os.path.join(d, "pricestore.db"))
        got = dict(conn.execute("SELECT d, COUNT(*) FROM bars_raw GROUP BY d").fetchall())
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        conn.close()
    finally:
        lm._CURRENT = saved_market
    assert asked == [days[1], days[2]] and got == {days[0]: 2, days[1]: 2}
    assert meta.get("max_trade_date") != days[2] and meta.get("max_trade_date") != days[1]     # 异常路径不推 meta (第二天已在 bars_raw, 下次 MAX(d) 自然续)
