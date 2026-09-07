#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tushare 客户端离线自测 (不联网, 打桩 requests.Session)。

覆盖: 正常返回成表 / 请求体结构 / 错误文本分类 (超频文本里也含"权限" -> 必须判超频) /
权限与必填参数不重试 / 传输错误退避重试 / 令牌桶限频 / ts_code 与日期归一 /
index_daily 升序 / trade_cal 转 ISO / 硬期限走 datasource 的**进程级**执行器。
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
    slept = []
    real_sleep = tc.time.sleep
    tc.time.sleep = lambda s: slept.append(s)
    try:
        b.acquire()                        # 令牌不足 -> 至少睡一次 (打桩后不真等)
    except Exception:                      # noqa: BLE001
        pass
    finally:
        tc.time.sleep = real_sleep
    check("空桶时会等待", bool(slept))
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
    saved = sys.modules.get("ashare.datasource")
    sys.modules["ashare.datasource"] = stub
    try:
        df = tc.query("daily", trade_date="20260904", deadline_sec=42)
    finally:
        if saved is not None:
            sys.modules["ashare.datasource"] = saved
        else:
            sys.modules.pop("ashare.datasource", None)
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
