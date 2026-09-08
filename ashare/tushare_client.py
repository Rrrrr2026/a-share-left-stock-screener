#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tushare Pro (及协议兼容镜像) 原生 HTTP 客户端  ——  P0
======================================================
为什么不用 tushare SDK: ① SDK 钉死 pandas 版本, 本仓库两市共用一套依赖, 不接受它挑版本;
② SDK 自带的重试/限频看不见摸不着, 而本仓库的铁律是 **任何第三方网络调用必须有进程级墙钟期限**
(2026-09-03 三次挂死 + 一次线程版 OOM 的教训, 见 CHRONICLE)。所以直接 POST JSON:

    POST <base>  {"api_name": ..., "token": ..., "params": {...}, "fields": "..."}
    -> {"code": 0, "msg": "", "data": {"fields": [...], "items": [[...], ...]}}

token 与基址只从 `data/secrets.json` 的 `tushare_token` / `tushare_base_url` 读 (环境变量
TUSHARE_TOKEN / TUSHARE_BASE_URL 可覆盖, 用于临时切官方站)。**token 永不进日志/异常文本/git**。
基址走配置是因为老板买的是协议兼容镜像 (design/tushare_adapter_design.md §5), 可随时切回
api.tushare.pro。

本模块提供:
    available()                      -> 是否配了 token
    query(api, **params)             -> pandas DataFrame (空结果也带列名)
    index_daily(ts_code, start, end) -> 指数日线 DataFrame (升序)
    trade_cal(start, end)            -> 开市日列表 ["YYYY-MM-DD", ...]
    to_ts_code("sh000300")           -> "000300.SH"

设计要点:
  · 令牌桶限频 (默认 150 次/分钟, 低于镜像实测上限, 多线程共享)
  · 瞬时错误 (网络/5xx/非 JSON/超频/硬期限) 退避重试; **权限不足 / 必填参数缺失 不重试**
    (镜像的错误文本: "抱歉，您没有接口访问权限…" / "必填参数 标的" / "每分钟最多访问该接口N次";
     注意超频文本里也含"权限"二字, 所以分类顺序是 超频 -> 参数 -> 权限)
  · 每次 HTTP 经 `ashare.datasource` 的**进程级**硬期限执行 (懒导入, 避免与 datasource 循环)
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

import pandas as pd

log = logging.getLogger("ashare.tushare")

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(PKG_DIR)
SECRETS_PATH = os.path.join(ROOT_DIR, "data", "secrets.json")

DEFAULT_BASE = "https://api.tushare.pro/"
RATE_PER_MIN = 150          # 令牌桶: 每分钟请求数上限 (镜像行情类实测 >200/min, 留余量)
TIMEOUT_SEC = 60            # 单次 socket 超时 (滴流时它不触发 -> 靠下面的硬期限)
DEADLINE_SEC = 120          # 进程级墙钟硬期限 (fina_indicator_vip 实测 21.9s, stk_factor_pro 13.5s)
MAX_RETRIES = 3
BACKOFF_SEC = 2.0           # 瞬时错误退避基数 (指数退避)
BACKOFF_RATE_SEC = 12.0     # 超频退避基数 (线性退避)

#: 测试用: 置 False 后在本进程内直调 (进程池里 mock 的 session 不生效)。生产恒为 True。
USE_PROCESS_DEADLINE = True


# ---------------------------------------------------------------- 异常
class TushareError(RuntimeError):
    """基类。构造签名保持 (message, api, code) 且 message 必给 —— 异常要能被
    multiprocessing 从工作进程 pickle 回主进程。"""

    def __init__(self, message: str, api: str = "", code=None):
        super().__init__(message)
        self.api = api
        self.code = code


class TushareUnavailable(TushareError):
    """没有配 token (或 secrets.json 缺失) —— 调用方应回退到旧数据源。"""


class TushareTransport(TushareError):
    """网络/HTTP/JSON 解析层错误 —— 可重试。"""


class TushareRateLimited(TushareError):
    """超频 ("每分钟最多访问该接口N次") —— 退避后重试。"""


class TushareNoPermission(TushareError):
    """积分/权限不足 —— 重试无意义, 直接抛给调用方去回退。"""


class TushareBadParams(TushareError):
    """必填参数缺失/参数错误 (镜像对 forecast/express 要求带 ts_code) —— 不重试。"""


_RATE_HINTS = ("每分钟最多访问", "每小时最多访问", "抽取失败", "频率", "too many", "rate limit")
_PARAM_HINTS = ("必填参数", "参数错误", "缺少参数", "不能为空", "invalid param")
_PERM_HINTS = ("没有接口访问权限", "权限", "积分", "permission", "未开通")


def _classify(api: str, code, msg: str) -> TushareError:
    """按镜像返回的错误文本判断该不该重试。顺序要紧: 超频文本里也含"权限"二字。"""
    m = str(msg or "")
    low = m.lower()
    if any(h in m or h in low for h in _RATE_HINTS):
        return TushareRateLimited(f"{api}: 超频 ({m[:120]})", api, code)
    if any(h in m or h in low for h in _PARAM_HINTS):
        return TushareBadParams(f"{api}: 参数不合法 ({m[:120]})", api, code)
    if any(h in m or h in low for h in _PERM_HINTS):
        return TushareNoPermission(f"{api}: 无权限/积分不足 ({m[:120]})", api, code)
    return TushareTransport(f"{api}: code={code} {m[:120]}", api, code)


# ---------------------------------------------------------------- 凭据
_CRED = None
_CRED_LOCK = threading.Lock()


def _read_secrets() -> dict:
    try:
        with open(SECRETS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:      # noqa: BLE001  (损坏的 secrets 不该炸掉整条流水线)
        log.warning("secrets.json 读取失败: %s", type(e).__name__)
        return {}


def credentials(refresh: bool = False):
    """-> (token, base_url)。token 只在进程内存里流转, 永不打印。"""
    global _CRED
    if _CRED is None or refresh:
        with _CRED_LOCK:
            s = _read_secrets()
            token = (os.environ.get("TUSHARE_TOKEN") or s.get("tushare_token") or "").strip()
            base = (os.environ.get("TUSHARE_BASE_URL") or s.get("tushare_base_url")
                    or DEFAULT_BASE).strip()
            _CRED = (token, base or DEFAULT_BASE)
    return _CRED


def available() -> bool:
    """是否配了 token (调用方据此决定走 Tushare 还是旧源)。"""
    try:
        return bool(credentials()[0])
    except Exception:           # noqa: BLE001
        return False


# ---------------------------------------------------------------- 限频
class TokenBucket:
    """线程安全令牌桶: 平滑到 rate_per_min, 允许短时突发到桶容量。"""

    def __init__(self, rate_per_min: float):
        self.capacity = float(rate_per_min)
        self.tokens = float(rate_per_min)
        self.rate = float(rate_per_min) / 60.0
        self.ts = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self, n: float = 1.0) -> float:
        """取 n 个令牌, 不够就睡到够。返回累计等待秒数。"""
        waited = 0.0
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.ts) * self.rate)
                self.ts = now
                if self.tokens >= n:
                    self.tokens -= n
                    return waited
                wait = (n - self.tokens) / self.rate
            wait = min(max(wait, 0.01), 5.0)
            time.sleep(wait)
            waited += wait


_BUCKET = TokenBucket(RATE_PER_MIN)


# ---------------------------------------------------------------- HTTP
_SESSION = None
_SESSION_LOCK = threading.Lock()


def _session():
    """进程内复用一个 requests.Session (连接复用)。工作进程各有各的。"""
    global _SESSION
    if _SESSION is None:
        with _SESSION_LOCK:
            if _SESSION is None:
                import requests
                s = requests.Session()
                s.headers.update({"Content-Type": "application/json",
                                  "Accept": "application/json"})
                _SESSION = s
    return _SESSION


def _post_json(base: str, payload: dict, timeout: float) -> dict:
    """一次 POST 并返回解析后的 JSON。

    **必须是模块级函数**: 它要被 pickle 到工作进程里执行 (进程级硬期限)。
    这里只判传输层, 业务错误码留给主进程分类 (自定义异常跨进程 pickle 越简单越好)。
    """
    r = _session().post(base, json=payload, timeout=timeout)
    if r.status_code != 200:
        raise TushareTransport(f"HTTP {r.status_code}", "http", r.status_code)
    try:
        return r.json()
    except Exception as e:      # noqa: BLE001  (网关返回 HTML 错误页)
        raise TushareTransport(f"响应非 JSON: {type(e).__name__} {r.text[:80]!r}", "http") from e


def _deadline_call(fn, args, kwargs, deadline_sec):
    """把一次调用交给 `ashare.datasource` 的**进程级**墙钟期限跑。

    懒导入是为了避开 datasource -> market -> tushare_client 的循环导入; 脱离仓库单跑
    (或 datasource 导不进来) 时退化为直调, 不让基础设施问题变成功能缺失。
    优先用 `_call_with_deadline` 而不是公开的 `call_with_retry`: 后者在超时路径上会
    顺手把**东财**标记为不可用 —— Tushare 挂了不该冤枉东财 (它还是兜底源)。
    """
    if not USE_PROCESS_DEADLINE:
        return fn(*args, **kwargs)
    try:
        from . import datasource as ds
    except Exception as e:      # noqa: BLE001
        log.debug("datasource 不可用 (%s), 硬期限降级为直调", type(e).__name__)
        return fn(*args, **kwargs)
    runner = getattr(ds, "_call_with_deadline", None)
    if runner is None:          # 私有入口若被重构掉, 退回公开入口 (仍是进程级期限)
        return ds.call_with_retry(fn, *args, **kwargs)
    return runner(fn, args, kwargs, deadline_sec)


# ---------------------------------------------------------------- 主入口
def query(api: str, fields: str = "", retries: int = MAX_RETRIES,
          deadline_sec: float = DEADLINE_SEC, **params) -> pd.DataFrame:
    """调用一个 Tushare 端点 -> DataFrame (无数据时返回带列名的空表)。

    params 里值为 None 的键会被丢掉 (方便可选参数直接传)。
    抛 TushareUnavailable (没 token) / TushareNoPermission / TushareBadParams (都不重试) /
    TushareRateLimited / TushareTransport (重试耗尽)。
    """
    token, base = credentials()
    if not token:
        raise TushareUnavailable(f"{api}: 未配置 tushare_token (data/secrets.json)", api)
    p = {k: v for k, v in params.items() if v is not None}
    payload = {"api_name": api, "token": token, "params": p, "fields": fields or ""}
    last: Exception | None = None
    for attempt in range(1, max(1, retries) + 1):
        waited = _BUCKET.acquire()
        if waited > 1.0:
            log.debug("tushare 限频等待 %.1fs (%s)", waited, api)
        t0 = time.time()
        try:
            j = _deadline_call(_post_json, (base, payload, TIMEOUT_SEC), {}, deadline_sec)
        except TimeoutError as e:                       # 进程级硬期限 (源站滴流/黑洞)
            last = TushareTransport(f"{api}: 超过 {deadline_sec}s 硬期限 ({e})", api)
            log.warning("tushare %s 硬期限超时 %d/%d", api, attempt, retries)
        except (TushareTransport, OSError, ValueError) as e:   # requests 异常继承 OSError
            last = e if isinstance(e, TushareError) else TushareTransport(f"{api}: {e}", api)
            log.warning("tushare %s 传输失败 %d/%d: %s", api, attempt, retries, str(e)[:120])
        else:
            code = j.get("code")
            if code == 0:
                d = j.get("data") or {}
                cols = list(d.get("fields") or [])
                items = d.get("items") or []
                df = pd.DataFrame(items, columns=cols or None)
                log.info("tushare %s: %d 行 %.1fs%s", api, len(df), time.time() - t0,
                         "" if not p else " " + ",".join(f"{k}={v}" for k, v in p.items())[:80])
                return df
            err = _classify(api, code, j.get("msg"))
            if isinstance(err, (TushareNoPermission, TushareBadParams)):
                log.warning("tushare %s 永久性失败, 不重试: %s", api, err)
                raise err
            last = err
            log.warning("tushare %s 失败 %d/%d: %s", api, attempt, retries, err)
        if attempt < max(1, retries):
            if isinstance(last, TushareRateLimited):
                nap = BACKOFF_RATE_SEC * attempt
            else:
                nap = BACKOFF_SEC * (2 ** (attempt - 1))
            time.sleep(nap)
    raise last if last is not None else TushareTransport(f"{api}: 未知失败", api)


# ---------------------------------------------------------------- helpers
def _ymd(d) -> str:
    """'2026-09-04' / date / '20260904' -> '20260904'。禁止任何时区换算 (Tushare 日期是北京日历)。"""
    if d is None:
        return ""
    s = str(d)[:10] if not isinstance(d, str) else str(d)
    return s.replace("-", "")[:8]


def to_ts_code(sym: str) -> str:
    """本仓库代码/前端符号 -> Tushare ts_code。
    'sh000300'/'000300'/'000300.SH' -> '000300.SH'; 'sz399006' -> '399006.SZ'。
    纯 6 位无前缀时按 A 股/指数惯例: 6/9 开头 -> SH, 其余 -> SZ (000300 为沪深300, 特判)。"""
    s = str(sym or "").strip()
    if not s:
        return ""
    if "." in s:
        return s.upper()
    low = s.lower()
    if low.startswith(("sh", "sz", "bj")):
        return f"{low[2:]}.{low[:2].upper()}"
    if low.startswith("csi") or low.startswith("hs"):
        low = low[3:] if low.startswith("csi") else low[2:]
    digits = "".join(ch for ch in low if ch.isdigit())
    if len(digits) != 6:
        return s.upper()
    if digits in ("000300", "000905", "000852", "000001", "000016"):
        return f"{digits}.SH"                    # 主要宽基指数都在上交所
    return f"{digits}.SH" if digits[0] in "69" else f"{digits}.SZ"


def index_daily(ts_code: str, start, end=None, **kw) -> pd.DataFrame:
    """指数日线 -> DataFrame(trade_date, open, high, low, close, vol, amount, ...) **升序**。
    ts_code 可传 'sh000300' 之类, 内部转换。vol 单位为**手**, amount 为**千元** (Tushare 口径)。"""
    df = query("index_daily", ts_code=to_ts_code(ts_code),
               start_date=_ymd(start), end_date=_ymd(end) or None, **kw)
    if len(df) and "trade_date" in df.columns:
        df = df.sort_values("trade_date").reset_index(drop=True)
    return df


def trade_cal(start, end, exchange: str = "SSE", is_open: str = "1",
              retries: int = MAX_RETRIES, deadline_sec: float = DEADLINE_SEC) -> list:
    """开市日历 -> ['2026-09-04', ...] 升序。**任何参数下都只返开市日**: is_open=None 时服务端
    会给全部日历日, 但下面随即只留 is_open==1 —— 拿不到 is_open=0 的日子 (2026-09-08 卡 GATE-FIX
    核实并改正这句, 首版写的"is_open=None 则返回全部日历日"与代码不符; 就绪闸门的覆盖自检因此
    只能"往 target 之后问开市日", 见 leftside_core.pricestore._calendar_days)。

    `retries` / `deadline_sec` 透传给 `query`, 默认就是全局那对常量 (3 次 × 120s 硬期限,
    最坏 3×120 + 退避 2+4 ≈ 366s)。**跑在 systemd 定时器里、又有 TimeoutStartSec 的调用方
    必须自己收紧这两个值** —— 366s 已经单独超过 stock-tsprobe 的 5min 启动超时, 镜像滴流
    那天会让单元被 systemd 判 timeout/failed (2026-09-08 卡 DATA-B 返工据此加的这两个参数)。
    """
    df = query("trade_cal", fields="cal_date,is_open", exchange=exchange,
               retries=retries, deadline_sec=deadline_sec,
               start_date=_ymd(start), end_date=_ymd(end), is_open=is_open)
    if not len(df) or "cal_date" not in df.columns:
        return []
    if is_open is None and "is_open" in df.columns:
        df = df[pd.to_numeric(df["is_open"], errors="coerce") == 1]
    out = sorted({f"{str(d)[:4]}-{str(d)[4:6]}-{str(d)[6:8]}" for d in df["cal_date"]})
    return out
