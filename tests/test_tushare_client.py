#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tushare 客户端离线自测 (不联网, 打桩 requests.Session)。

覆盖: 正常返回成表 / 请求体结构 / 错误文本分类 (超频文本里也含"权限" -> 必须判超频) /
权限与必填参数不重试 / 传输错误退避重试 / 令牌桶限频 / ts_code 与日期归一 /
index_daily 升序 / trade_cal 转 ISO / 硬期限走 datasource 的**进程级**执行器 /
备用基址自动切换 (文末 pytest 风格: 主站 3 次传输层失败 / token 失效立即切 / 业务错误不切 /
备用也坏按原逻辑抛 / 没配备用键一切照旧 / 基址由主进程选好传给 _post_json; 不写任何 token)。
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
# 备用基址自动切换 (2026-10-12 卡 TS-FALLBACK)。pytest 风格 (monkeypatch / caplog), 不联网, 不出现任何真 token。
# 背景: 10-10 换到镜像 tl.kaixin8.top 后老板另给了备用站 (同一 key, 15000 积分档); 旧镜像的 token 10-08 起 code=2002。
import logging
import pytest

PRIMARY = "https://primary.invalid/tushare/sdk"
BACKUP = "https://backup.invalid/api/tushare"
_FO_INITIAL = {"active": False, "fails": 0, "reason": "", "switched_at": None}


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


def test_failover_after_three_transport_failures_on_primary(caplog):
    """主站连续 3 次 5xx → 切备用; 触发切换的那次调用在备用上重来并成功 (不退避); 之后的调用直接走备用。
    journal 只有一行 INFO, 只写 host (不写完整地址, 更不写 token)。"""
    s, naps = _install_fo([FakeResp({}, status=502), FakeResp({}, status=503), FakeResp({}, status=502),
                           _ok(["cal_date", "is_open"], [["20261012", 1]])])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        df = tc.query("trade_cal", exchange="SSE")
    assert len(df) == 1
    assert _urls(s) == [PRIMARY, PRIMARY, PRIMARY, BACKUP]
    assert naps == [tc.BACKOFF_SEC, tc.BACKOFF_SEC * 2]        # 主站两次退避; 切换瞬间不退避
    assert s.calls[3]["json"]["token"] == TOKEN                 # 备用站用同一个 key
    st = tc.failover_state()
    assert st["active"] is True and "连续 3 次传输层失败" in st["reason"] and TOKEN not in st["reason"]
    lines = _switch_lines(caplog)
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    msg = lines[0].getMessage()
    assert "tushare 基址切换: 主站 primary.invalid → 备用 backup.invalid (原因: " in msg
    assert "/tushare/sdk" not in msg and "/api/tushare" not in msg and TOKEN not in caplog.text
    # 本进程余下调用全部走备用, 主站不再碰, 也不再打切换行
    s2 = _fo_session([_ok(["a"], [[1]])])
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        tc.query("daily", trade_date="20261012")
    assert _urls(s2) == [BACKUP] and len(_switch_lines(caplog)) == 1


def test_failover_counter_spans_calls_and_resets_on_any_primary_reply(monkeypatch):
    """连续计数跨调用累计 (retries=1 的多次调用也能触发); 四种传输层失败都算 (5xx / 连接错 / 非 JSON / 硬期限超时);
    主站任何一次 JSON 应答 (哪怕是超频错误码) 都把计数清零 —— 要的是「连续」。"""
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
    # 硬期限超时 (进程级执行器抛 TimeoutError) 也是传输层失败
    real_call = tc._deadline_call

    def timeout_on_primary(fn, args, kwargs, dl):
        if args[0] == PRIMARY:
            raise TimeoutError("_post_json 超过 42s 硬期限 (工作进程已终止)")
        return real_call(fn, args, kwargs, dl)

    monkeypatch.setattr(tc, "_deadline_call", timeout_on_primary)
    _fo_session([])
    with pytest.raises(tc.TushareTransport):
        tc.query("daily", retries=1, deadline_sec=42)
    assert tc.failover_state()["fails"] == 2 and tc.failover_state()["active"] is False
    s = _fo_session([_ok(["a"], [[1]])])                        # 第 3 次 → 切换, 同一次调用在备用上成功
    df = tc.query("daily", retries=1, deadline_sec=42)
    assert len(df) == 1 and _urls(s) == [BACKUP]
    st = tc.failover_state()
    assert st["active"] is True and "硬期限" in st["reason"]


@pytest.mark.parametrize("code,msg", [(2002, "token已过期"), (40101, "token无效"), ("2002", "token已过期"),
                                      (2002, "认证失败"), (40101, "Unauthorized"),       # 只看码, 文案没有 token 字样
                                      (-1, "抱歉，您输入的TOKEN无效，请确认后重试。")])   # 只看文案, 码不在表里
def test_token_error_on_primary_switches_immediately(code, msg, caplog):
    """主站返回 token 失效类错误 (code 2002 / 40101, 字符串码也认, 或文案 token+无效/过期) → 不等 3 次、不退避, 立即切备用。"""
    s, naps = _install_fo([_err(msg, code=code), _ok(["cal_date", "is_open"], [["20261012", 1]])])
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


def test_backup_failure_raises_like_before_and_never_flaps_back():
    """备用也坏 → 按原逻辑 (备用上一轮完整重试 + 退避) 抛 TushareTransport; 之后不回切主站;
    备用上的权限错误也按原逻辑不重试、不再切。"""
    s, naps = _install_fo([FakeResp({}, status=502)] * 6)
    with pytest.raises(tc.TushareTransport):
        tc.query("daily")
    assert _urls(s) == [PRIMARY] * 3 + [BACKUP] * 3
    assert naps == [tc.BACKOFF_SEC, tc.BACKOFF_SEC * 2, tc.BACKOFF_SEC, tc.BACKOFF_SEC * 2]
    assert tc.failover_state()["active"] is True
    s = _fo_session([FakeResp({}, status=502)] * 3)
    with pytest.raises(tc.TushareTransport):
        tc.query("daily")
    assert _urls(s) == [BACKUP] * 3
    s = _fo_session([_err("抱歉，您没有接口访问权限", code=40203)])
    with pytest.raises(tc.TushareNoPermission):
        tc.query("daily")
    assert _urls(s) == [BACKUP]
    s = _fo_session([_err("token已过期", code=2002)] * 3)       # 备用上的 token 失效: 原逻辑 (传输层重试后抛)
    with pytest.raises(tc.TushareTransport):
        tc.query("daily")
    assert _urls(s) == [BACKUP] * 3


@pytest.mark.parametrize("backup", ["", None, PRIMARY])
def test_without_backup_key_nothing_changes(backup, caplog):
    """没配 tushare_base_url_backup (空 / 缺 / 与主站相同) → 不计数、不切换、不打切换行, 退避与异常与以前一样;
    2002 没备用时仍按传输层错误重试 3 次后抛 (类型仍是 TushareTransport, 其子类 TushareBadToken)。"""
    s, naps = _install_fo([FakeResp({}, status=502)] * 3, backup=backup)
    with caplog.at_level(logging.INFO, logger="ashare.tushare"):
        with pytest.raises(tc.TushareTransport):
            tc.query("daily")
        assert _urls(s) == [PRIMARY] * 3 and naps == [tc.BACKOFF_SEC, tc.BACKOFF_SEC * 2]
        assert tc.failover_state() == _FO_INITIAL
        s = _fo_session([_err("token已过期", code=2002)] * 3)
        with pytest.raises(tc.TushareTransport) as ei:
            tc.query("trade_cal")
    assert isinstance(ei.value, tc.TushareBadToken) and _urls(s) == [PRIMARY] * 3
    assert tc.failover_state() == _FO_INITIAL and not _switch_lines(caplog)
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
    assert st["active"] is True and st["fails"] == tc.FAILOVER_AFTER
    assert tc._active_base(PRIMARY, BACKUP) == (BACKUP, False)
    assert tc._active_base(PRIMARY, "") == (PRIMARY, False)     # 没配备用: 状态无关紧要, 永远主站
    tc._CRED = (TOKEN, "https://example.invalid/")


def test_classify_token_errors():
    """token 失效类的分类: code 2002 / 40101 (含字符串码) 或文案 token+无效/过期 → TushareBadToken (仍是 Transport 子类);
    其它文案的分类不变。"""
    e = tc._classify("trade_cal", 2002, "token已过期")
    assert isinstance(e, tc.TushareBadToken) and isinstance(e, tc.TushareTransport) and e.code == 2002
    assert isinstance(tc._classify("daily", "40101", "invalid token"), tc.TushareBadToken)
    assert isinstance(tc._classify("daily", 2002, "认证失败"), tc.TushareBadToken)          # 只凭码
    assert isinstance(tc._classify("daily", "40101", "Unauthorized"), tc.TushareBadToken)   # 只凭码 (字符串)
    assert type(tc._classify("daily", 2001, "认证失败")) is tc.TushareTransport               # 码不在表里、文案无 token → 普通传输错
    assert isinstance(tc._classify("daily", -1, "抱歉，您输入的TOKEN无效，请确认后重试。"), tc.TushareBadToken)
    assert type(tc._classify("daily", -1, "系统内部错误")) is tc.TushareTransport
    assert isinstance(tc._classify("daily", -1, "抱歉，您没有接口访问权限，请确认积分"), tc.TushareNoPermission)
    assert isinstance(tc._classify("daily", -1, "抱歉，您每分钟最多访问该接口500次，权限的具体详情访问：…"),
                      tc.TushareRateLimited)
    assert isinstance(tc._classify("daily", 40203, "token 字样但只是说明: 请检查权限"), tc.TushareNoPermission)
