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
api.tushare.pro。镜像站的基址是**完整 POST 地址** (2026-10-10 起 tl.kaixin8.top 带 /tushare/sdk 路径)。

备用基址 (2026-10-12 卡 TS-FALLBACK, 同日第二轮 TS-FALLBACK-2 加回切): 可选键 `tushare_base_url_backup` (环境变量
TUSHARE_BASE_URL_BACKUP 可覆盖) 配第二个站 (同一 key; 老板给的 datahubco 只有 15000 积分档, 权限可能少于主站)。
  · **切过去**: 主站连续 FAILOVER_AFTER=3 次传输层失败 (HTTP 非 200 且非 429 / 非 JSON / 连接错或 socket 超时; 跨调用、
    跨线程累计, 主站任何一次 JSON 应答即清零) 或连续 FAILOVER_AFTER_TIMEOUTS=6 次**进程级硬期限超时** (单独计, 阈值
    更高: 共享进程池被别的调用的超时 terminate 时, 在途的主站请求会一起被记成超时, 一次池重置顶多几条, 不够 6)
    或返回 token 失效类错误 (code 2002 / 40101, 或文案 token+无效/过期) → 切到备用, 触发切换的那次调用在备用上重新
    来一轮完整重试; journal 一行 INFO「tushare 基址切换: 主站 <host> → 备用 <host> (原因: …)」(只打 host)。
  · **切回来**: 在备用上每 PROBE_EVERY_CALLS=20 次调用或 PROBE_EVERY_SEC=600 秒 (先到先试) 对主站打一次轻量 trade_cal
    (单日, 单次, 30s 硬期限), 应答 code 0 即回切并打一行 INFO「tushare 基址回切: 备用 <host> → 主站 <host> (原因: …)」;
    备用连续 FAILBACK_AFTER=3 次失败也立即试探主站, 成功即回切并在主站重来一轮 —— 坏备用不会把主站的一次抖动放大成
    整轮停摆; 两站都不可用就留在备用 (再满 3 次再试)。一次 query() 最多两轮 (MAX_ROUNDS_PER_CALL), 防两站都抖时乒乓。
  · **不计入切换的**: 业务错误 (无权限 40203 / unknown_api / 参数 / 超频) —— 它们是 JSON 应答, 反而清零; 网关 HTTP 429
    限流 —— 按超频线性退避重试, 不计也不清零。15000 档把权限错误当故障会循环切换, 所以只有传输层才算。
  · **没配备用键一切照旧**: 不计数、不切换、不试探, 错误分类顺序 (超频 → 参数 → 权限 → 传输) 与异常类型同 7b92bb6 版
    (用例逐字节钉住); token 失效文案只在配了备用时才单独分出来 (TushareBadToken) 用于立即切换。
  · **最坏墙钟**: 没备用 3×120 + 退避 2+4 ≈ 366s; 配了备用, 一次调用最多两轮 + 最多两次主站试探 (各 ≤ 30s):
    2×366 + 2×30 ≈ 792s ≈ 13 分钟 —— 有 TimeoutStartSec 的调用方按这个数收紧 retries / deadline_sec (见 trade_cal)。
  · **真验收**: `python -X utf8 -m ashare.tushare_client --probe-backup` (4 次调用: 主站 trade_cal + daily 走生产路径,
    备用站同两项直调绕开切换状态), 退出码 0 可用 / 1 不可用 / 2 判不了; 加键之前用环境变量 TUSHARE_BASE_URL_BACKUP 临时给
    备用地址先验 —— 主站健康时请求永远到不了备用站, 光跑 `pricestore ready` 什么都验不出 (10-12 校验员实证)。
  进程池里跑的 `_post_json` 只拿主进程选好的基址当参数, 切换状态只在主进程 (加锁) 判定。

本模块提供:
    available()                      -> 是否配了 token
    credentials()                    -> (token, base_url, backup_base_url); 没配备用则第三项为 ""
    failover_state() / failover_reset() -> 备用基址切换状态 (诊断 / 用例用)
    probe_backup()                   -> 备用站真验收 (命令行 --probe-backup), 退出码 0/1/2
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
FAILOVER_AFTER = 3          # 主站连续这么多次传输层失败 (HTTP 非 200 非 429 / 非 JSON / 连接错) → 切到备用基址 (配了备用才计)
FAILOVER_AFTER_TIMEOUTS = 6 # 主站连续这么多次进程级硬期限超时才切 (单独计, 阈值更高: 共享进程池被别人的超时重置会连带在途请求)
FAILBACK_AFTER = 3          # 备用连续这么多次失败 → 试探主站一次, 成功即回切 (坏备用不把主站一次抖动放大成整轮停摆)
PROBE_EVERY_CALLS = 20      # 在备用上每这么多次调用试探主站一次 (轻量 trade_cal)
PROBE_EVERY_SEC = 600       # …或每这么多秒 (先到先试)
PROBE_TIMEOUT_SEC = 20      # 试探那次的 socket 超时 (小于硬期限: 尽量让 socket 先超时, 不去 terminate 共享进程池)
PROBE_DEADLINE_SEC = 30     # 试探那次的进程级硬期限 (单次, 不重试)
MAX_ROUNDS_PER_CALL = 2     # 一次 query() 最多在两个基址上各来一轮 (切换/回切后重来一轮; 再切就不重来, 防两站都抖时乒乓)
TOKEN_ERROR_CODES = (2002, 40101)   # token 过期/无效类错误码 → 立即切备用 (10-08 旧镜像就是 code=2002 token已过期)

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


class TushareBadToken(TushareTransport):
    """token 过期/无效 (code 2002 / 40101, 或文案 token+无效/过期)。**只在配了备用基址时**才被 `_classify` 分出来
    (没配备用时判错顺序与 7b92bb6 版一字不差, 这类文案照旧落到 超频/参数/权限/传输 四类里); 仍是 TushareTransport 的
    子类, 同一基址上的重试语义不变, 只多一件事 —— 在主站遇到它就立即切备用 (各镜像各自校验 key)。"""


class TushareRateLimited(TushareError):
    """超频 ("每分钟最多访问该接口N次", 或网关层 HTTP 429) —— 线性退避后重试; 不计入备用基址切换。"""


class TushareNoPermission(TushareError):
    """积分/权限不足 —— 重试无意义, 直接抛给调用方去回退。"""


class TushareBadParams(TushareError):
    """必填参数缺失/参数错误 (镜像对 forecast/express 要求带 ts_code) —— 不重试。"""


_RATE_HINTS = ("每分钟最多访问", "每小时最多访问", "抽取失败", "频率", "too many", "rate limit")
# 「[invalid_params] 业务参数缺失或格式不合法」(kaixin8 code 400): 'invalid param' 带空格对不上下划线形态, 10-13 卡 TS-PROBE 实测
_PARAM_HINTS = ("必填参数", "参数错误", "缺少参数", "不能为空", "invalid param", "invalid_params", "参数缺失")
# 「[unknown_api] 接口不存在或未开放」(kaixin8 code 404, 研究湖的 fina_indicator_vip / income_vip): 以前被当传输错误白重试 3 次
_PERM_HINTS = ("没有接口访问权限", "权限", "积分", "permission", "未开通", "unknown_api", "未开放")
_TOKEN_HINTS = ("无效", "过期", "invalid", "expired")     # 与 "token" 同时出现才算 (镜像各家文案不一)


def _is_token_error(code, low: str) -> bool:
    """token 失效类: 错误码 2002 / 40101 (字符串形态也认), 或文案同时含 token 与 无效/过期。"""
    try:
        if int(code) in TOKEN_ERROR_CODES:
            return True
    except (TypeError, ValueError):
        pass
    return "token" in low and any(h in low for h in _TOKEN_HINTS)


def _classify(api: str, code, msg: str, with_backup: bool = False) -> TushareError:
    """按镜像返回的错误码/文本判断该不该重试。

    顺序要紧: 超频文本里也含"权限"二字, 所以是 超频 -> 参数 -> 权限 -> 传输 —— 这个顺序和四种异常类型与 7b92bb6 版
    (没有备用基址机制的最后一版) 完全一样, 用例逐字节钉住。**只有 with_backup=True (配了备用基址) 时**才在最前面多判一步
    token 失效 (code 2002 / 40101 或文案 token+无效/过期 → TushareBadToken, 用于立即切备用): 没配备用时「token无效, 没有
    接口访问权限」照旧是 NoPermission 一次就抛, 不会变成重试 3 次 (10-12 校验员抓的「没配备用键一切照旧」说过头)。
    新认两种 kaixin8 文案 (10-13 卡 TS-PROBE): [unknown_api] → NoPermission, [invalid_params] → BadParams, 都不再重试。
    """
    m = str(msg or "")
    low = m.lower()
    if with_backup and _is_token_error(code, low):
        return TushareBadToken(f"{api}: token 过期/无效 code={code} ({m[:120]})", api, code)
    if any(h in m or h in low for h in _RATE_HINTS):
        return TushareRateLimited(f"{api}: 超频 ({m[:120]})", api, code)
    if any(h in m or h in low for h in _PARAM_HINTS):
        return TushareBadParams(f"{api}: 参数不合法 ({m[:120]})", api, code)
    if any(h in m or h in low for h in _PERM_HINTS):
        return TushareNoPermission(f"{api}: 无权限/积分不足 ({m[:120]})", api, code)
    return TushareTransport(f"{api}: code={code} {m[:120]}", api, code)


def _redact(text, token: str = "") -> str:
    """把 token 从任何要进日志/异常/屏幕的文本里抹掉 (镜像的 msg / 网关错误页偶尔会回显请求体)。先抹再截断。"""
    s = "" if text is None else str(text)
    if token and token in s:
        s = s.replace(token, "<token>")
    return s


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
    """-> (token, base_url, backup_base_url)。token 只在进程内存里流转, 永不打印。
    backup_base_url 没配就是 "" (secrets 键 tushare_base_url_backup / 环境变量 TUSHARE_BASE_URL_BACKUP);
    refresh=True 重读 secrets 并把基址切换状态复位 (新凭据从主站重新开始)。"""
    global _CRED
    if _CRED is None or refresh:
        with _CRED_LOCK:
            s = _read_secrets()
            token = (os.environ.get("TUSHARE_TOKEN") or s.get("tushare_token") or "").strip()
            base = (os.environ.get("TUSHARE_BASE_URL") or s.get("tushare_base_url")
                    or DEFAULT_BASE).strip()
            backup = (os.environ.get("TUSHARE_BASE_URL_BACKUP") or s.get("tushare_base_url_backup")
                      or "").strip()
            _CRED = (token, base or DEFAULT_BASE, backup)
        if refresh:
            failover_reset()
    c = _CRED
    if len(c) == 2:             # 旧形态 (用例直接塞 (token, base)): 等价于没配备用
        c = (c[0], c[1], "")
    return c


def available() -> bool:
    """是否配了 token (调用方据此决定走 Tushare 还是旧源)。"""
    try:
        return bool(credentials()[0])
    except Exception:           # noqa: BLE001
        return False


# ---------------------------------------------------------------- 备用基址切换 (状态只在主进程, 加锁)
_FO_LOCK = threading.Lock()
#: 初始状态 (failover_reset 回到这里; 用例也拿它比):
#:   active 在备用上 / fails 主站连续传输层失败 / timeouts 主站连续硬期限超时 (单独计) / reason+switched_at 上次切换
#:   backup_fails 备用连续失败 / calls_on_backup 切换 (或上次试探) 以来在备用上的调用数 / last_probe_at 上次试探 (monotonic)
#:   probing 有线程正在试探 / probes·switches·returns 累计次数 (诊断)
_FO_INITIAL = {"active": False, "fails": 0, "timeouts": 0, "reason": "", "switched_at": None,
               "backup_fails": 0, "calls_on_backup": 0, "last_probe_at": None, "probing": False,
               "probes": 0, "switches": 0, "returns": 0}
_FO = dict(_FO_INITIAL)


def _host(url) -> str:
    """只取主机名: 日志/异常文本里基址只以 host 出现 (完整地址虽不含 token, 也没必要进 journal)。"""
    try:
        from urllib.parse import urlsplit
        return urlsplit(str(url)).netloc or str(url)
    except Exception:           # noqa: BLE001
        return str(url)


def _has_backup(base, backup) -> bool:
    return bool(backup) and backup != base


def _active_base(base: str, backup) -> tuple:
    """主进程判定本次请求打哪个基址 -> (base_url, role)。
    role None = 没配备用 (什么都不计, 行为与没有这套机制时完全一样); "primary" = 打主站且配了备用 (失败要计数、JSON 应答
    要清零、token 失效要切); "backup" = 已切到备用 (失败计 backup_fails, 连续 FAILBACK_AFTER 次就试探主站)。"""
    if not _has_backup(base, backup):
        return base, None
    with _FO_LOCK:
        if _FO["active"]:
            return backup, "backup"
    return base, "primary"


def _switch_locked(base: str, backup: str, reason: str) -> None:
    """调用方持 _FO_LOCK。只打 host; token 不在这里也不在 reason 里 (reason 来自抹过 token 的异常文本/错误码)。"""
    _FO.update(active=True, reason=reason, switched_at=time.time(), backup_fails=0, calls_on_backup=0,
               last_probe_at=time.monotonic())
    _FO["switches"] += 1
    log.info("tushare 基址切换: 主站 %s → 备用 %s (原因: %s)", _host(base), _host(backup), reason)


def _return_locked(base: str, backup: str, reason: str) -> None:
    """调用方持 _FO_LOCK。回到主站: 两边计数全部清零 (主站从头算), 打一行 INFO。"""
    on_backup = time.time() - (_FO["switched_at"] or time.time())
    n = _FO["calls_on_backup"]
    _FO.update(active=False, reason="", switched_at=None, fails=0, timeouts=0, backup_fails=0,
               calls_on_backup=0, last_probe_at=None)
    _FO["returns"] += 1
    log.info("tushare 基址回切: 备用 %s → 主站 %s (原因: %s; 在备用 %d 次调用 / %.0f 秒)",
             _host(backup), _host(base), reason, n, on_backup)


def _note_primary_transport_failure(base: str, backup: str, reason: str) -> bool:
    """主站一次传输层失败 (HTTP 非 200 非 429 / 非 JSON / 连接错或 socket 超时)。连续满 FAILOVER_AFTER 次 → 切换并
    返回 True (本次调用随即在备用上重来一轮); 已经切过 (本线程或别的线程) → False, 不再计数。"""
    with _FO_LOCK:
        if _FO["active"]:
            return False
        _FO["fails"] += 1
        if _FO["fails"] < FAILOVER_AFTER:
            return False
        _switch_locked(base, backup, f"主站连续 {_FO['fails']} 次传输层失败, 最后一次: {reason}")
        return True


def _note_primary_timeout(base: str, backup: str, reason: str) -> bool:
    """主站一次进程级硬期限超时。**单独计, 阈值更高** (FAILOVER_AFTER_TIMEOUTS): datasource 的 spawn 池是 akshare 与
    Tushare 共用的, 任何调用撞硬期限都会 terminate 整池, 在途的主站请求到自己的期限后也被记成超时 —— 主站其实没坏
    (10-12 校验员用真进程池复现)。一次池重置顶多连带几条, 不够 6; 真滴流的主站每次 120s, 6 次 = 两次调用后也会切。"""
    with _FO_LOCK:
        if _FO["active"]:
            return False
        _FO["timeouts"] += 1
        if _FO["timeouts"] < FAILOVER_AFTER_TIMEOUTS:
            return False
        _switch_locked(base, backup, f"主站连续 {_FO['timeouts']} 次硬期限超时, 最后一次: {reason}")
        return True


def _note_primary_token_error(base: str, backup: str, reason: str) -> bool:
    """主站返回 token 失效类错误 → 立即切换 (同一 key 在备用站可能仍有效: 各镜像各自校验)。"""
    with _FO_LOCK:
        if _FO["active"]:
            return False
        _switch_locked(base, backup, f"主站返回 token 失效类错误: {reason}")
        return True


def _note_primary_ok() -> None:
    """主站答了 JSON (code 0 或业务错误码都算): 传输层正常, 两个连续计数都清零。"""
    with _FO_LOCK:
        _FO["fails"] = 0
        _FO["timeouts"] = 0


def _note_backup_ok() -> None:
    """备用答了 JSON (code 0 或 token 失效以外的业务错误码): 备用连续失败计数清零 (不影响「每 20 次 / 10 分钟试探主站」)。"""
    with _FO_LOCK:
        _FO["backup_fails"] = 0


def _probe_primary(token: str, base: str) -> tuple:
    """对主站打一次轻量 trade_cal (单日, 单次, PROBE_TIMEOUT_SEC socket / PROBE_DEADLINE_SEC 硬期限, 不重试)
    -> (ok, 说明)。ok 只认 code 0 —— 业务错误码 (比如 token 仍失效) 不算恢复。说明里不含 token。"""
    day = time.strftime("%Y%m%d")
    payload = {"api_name": "trade_cal", "token": token,
               "params": {"exchange": "SSE", "start_date": day, "end_date": day}, "fields": "cal_date,is_open"}
    _BUCKET.acquire()
    t0 = time.time()
    try:
        j = _deadline_call(_post_json, (base, payload, PROBE_TIMEOUT_SEC), {}, PROBE_DEADLINE_SEC)
    except TimeoutError:
        return False, f"trade_cal 超过 {PROBE_DEADLINE_SEC}s 硬期限"
    except (TushareError, OSError, ValueError) as e:
        return False, f"trade_cal {_redact(str(e), token)[:80]}"
    code = j.get("code") if isinstance(j, dict) else None
    if code == 0:
        return True, f"trade_cal {day} 应答正常 {time.time() - t0:.1f}s"
    msg = j.get("msg") if isinstance(j, dict) else j
    return False, f"trade_cal code={code} {_redact(msg, token)[:60]}"


def _begin_probe_locked() -> bool:
    """调用方持 _FO_LOCK: 没人在试探就占住位子 (同一时刻只一个线程试探主站, 其余线程照走备用)。"""
    if _FO["probing"]:
        return False
    _FO["probing"] = True
    return True


def _finish_probe(ok: bool, detail: str, base: str, backup: str, why: str, level: int = logging.INFO) -> bool:
    """试探结束: 记时、放位子、计数归零; 成功即回切 (返回 True), 失败打一行留在备用。"""
    with _FO_LOCK:
        _FO["probing"] = False
        _FO["probes"] += 1
        _FO["last_probe_at"] = time.monotonic()
        if ok and _FO["active"]:
            _return_locked(base, backup, f"{why}, 主站试探成功 ({detail})")     # 里面把两边计数全部清零
            return True
        _FO["calls_on_backup"] = 0
        _FO["backup_fails"] = 0
        if not _FO["active"]:
            return False
        log.log(level, "tushare 主站 %s 试探失败 (%s): %s; 继续用备用 %s", _host(base), why, detail, _host(backup))
        return False


def _note_backup_failure(token: str, base: str, backup: str, reason: str) -> bool:
    """备用上一次失败 (传输层 / 硬期限 / 备用不认这把 key 的 token 失效类错误都算)。连续满 FAILBACK_AFTER 次 → 立即试探主站一次: 成功即回切并返回 True
    (本次调用随即在主站重来一轮 —— 坏备用不把主站的一次抖动放大成整轮停摆); 失败 = 两站都不可用, 留在备用
    (再满 FAILBACK_AFTER 次再试)。"""
    with _FO_LOCK:
        if not _FO["active"]:
            return False
        _FO["backup_fails"] += 1
        if _FO["backup_fails"] < FAILBACK_AFTER or not _begin_probe_locked():
            return False
        why = f"备用连续 {_FO['backup_fails']} 次失败, 最后一次: {reason}"
    ok, detail = _probe_primary(token, base)
    return _finish_probe(ok, detail, base, backup, why, level=logging.WARNING)


def _maybe_probe_primary(token: str, base: str, backup: str) -> bool:
    """每次 query() 开头 (配了备用才调): 在备用上每 PROBE_EVERY_CALLS 次调用或 PROBE_EVERY_SEC 秒试探主站一次
    (先到先试), 成功即回切 (本次调用直接打主站)。不在备用 / 别的线程正在试探 → 什么都不做。"""
    with _FO_LOCK:
        if not _FO["active"]:
            return False
        _FO["calls_on_backup"] += 1
        since = time.monotonic() - (_FO["last_probe_at"] or 0.0)
        due_calls = _FO["calls_on_backup"] >= PROBE_EVERY_CALLS
        if not (due_calls or since >= PROBE_EVERY_SEC) or not _begin_probe_locked():
            return False
        why = f"在备用 {_FO['calls_on_backup']} 次调用" if due_calls else f"在备用 {since:.0f} 秒"
    ok, detail = _probe_primary(token, base)
    return _finish_probe(ok, detail, base, backup, why)


def failover_state() -> dict:
    """诊断用: 切换状态的副本 (见 _FO_INITIAL 的键), 不含任何凭据。"""
    with _FO_LOCK:
        return dict(_FO)


def failover_reset() -> None:
    """回到主站、全部计数清零 (用例与 credentials(refresh=True) 用)。生产里的回切走试探 (_maybe_probe_primary /
    _note_backup_failure); 每天的跑批都是新进程, 新进程从主站开始。"""
    with _FO_LOCK:
        _FO.clear()
        _FO.update(_FO_INITIAL)


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
    `base` 是主进程按切换状态选好的基址 (主站或备用), 当参数传进来; 工作进程 (spawn 出来的模块副本) 的
    切换状态永远是初始值, 这里不看也不改它。
    """
    r = _session().post(base, json=payload, timeout=timeout)
    if r.status_code == 429:    # 网关限流: 不是故障 —— 主进程按超频线性退避重试, 不计入备用基址切换
        raise TushareRateLimited("HTTP 429 限流", "http", 429)
    if r.status_code != 200:
        raise TushareTransport(f"HTTP {r.status_code}", "http", r.status_code)
    try:
        return r.json()
    except Exception as e:      # noqa: BLE001  (网关返回 HTML 错误页; 错误页可能回显请求体, 先抹 token 再截断)
        body = _redact(r.text, (payload or {}).get("token", ""))[:80]
        raise TushareTransport(f"响应非 JSON: {type(e).__name__} {body!r}", "http") from e


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
    TushareRateLimited (含网关 HTTP 429; 线性退避后仍超频) / TushareTransport (重试耗尽)。
    配了备用基址时 (模块 docstring「备用基址」): 开头先看要不要试探主站 (在备用上每 20 次调用 / 10 分钟一次); 每次请求前由
    主进程按切换状态选基址 (`_active_base`); 主站连续 FAILOVER_AFTER 次传输层失败 / FAILOVER_AFTER_TIMEOUTS 次硬期限超时 /
    token 失效类错误 → 切备用; 备用连续 FAILBACK_AFTER 次失败且主站试探成功 → 回主站; 切换或回切后**本次调用在新基址上
    重新来一轮** (完整 retries 预算, 切换瞬间不退避), 但一次调用最多 MAX_ROUNDS_PER_CALL=2 轮。没配备用键时与 7b92bb6 版一样。
    """
    token, base, backup = credentials()
    if not token:
        raise TushareUnavailable(f"{api}: 未配置 tushare_token (data/secrets.json)", api)
    p = {k: v for k, v in params.items() if v is not None}
    payload = {"api_name": api, "token": token, "params": p, "fields": fields or ""}
    with_backup = _has_backup(base, backup)
    if with_backup:
        _maybe_probe_primary(token, base, backup)       # 在备用上: 到点就试探主站, 成功即回切
    last: Exception | None = None
    budget = max(1, retries)
    attempt = 0
    rounds = 1
    while attempt < budget:
        attempt += 1
        use_base, role = _active_base(base, backup)     # 主进程判定; 工作进程只拿结果当参数
        waited = _BUCKET.acquire()
        if waited > 1.0:
            log.debug("tushare 限频等待 %.1fs (%s)", waited, api)
        t0 = time.time()
        restart = False
        try:
            j = _deadline_call(_post_json, (use_base, payload, TIMEOUT_SEC), {}, deadline_sec)
        except TimeoutError as e:                       # 进程级硬期限 (源站滴流/黑洞, 或共享进程池被别人的超时重置)
            last = TushareTransport(f"{api}: 超过 {deadline_sec}s 硬期限 ({e})", api)
            log.warning("tushare %s 硬期限超时 %d/%d (%s)", api, attempt, budget, _host(use_base))
            if role == "primary":
                restart = _note_primary_timeout(base, backup, f"{api} 超过 {deadline_sec}s 硬期限")
            elif role == "backup":
                restart = _note_backup_failure(token, base, backup, f"{api} 超过 {deadline_sec}s 硬期限")
        except TushareRateLimited as e:                 # 网关 HTTP 429: 限流不是故障, 线性退避重试; 不计入切换、不清零
            last = TushareRateLimited(f"{api}: {e}", api, 429)
            log.warning("tushare %s 限流 %d/%d (%s): %s", api, attempt, budget, _host(use_base), str(e)[:120])
        except (TushareTransport, OSError, ValueError) as e:   # requests 异常 (含 socket 超时) 继承 OSError
            text = _redact(str(e), token)
            last = e if isinstance(e, TushareError) else TushareTransport(f"{api}: {text}", api)
            log.warning("tushare %s 传输失败 %d/%d (%s): %s", api, attempt, budget, _host(use_base), text[:120])
            if role == "primary":
                restart = _note_primary_transport_failure(base, backup, f"{api} {text[:80]}")
            elif role == "backup":
                restart = _note_backup_failure(token, base, backup, f"{api} {text[:80]}")
        else:
            if role == "primary":
                _note_primary_ok()          # 主站答了 JSON = 传输层正常, 连续失败清零 (业务错误码也算答了)
            code = j.get("code")
            if code == 0:
                if role == "backup":
                    _note_backup_ok()
                d = j.get("data") or {}
                cols = list(d.get("fields") or [])
                items = d.get("items") or []
                df = pd.DataFrame(items, columns=cols or None)
                log.info("tushare %s: %d 行 %.1fs%s", api, len(df), time.time() - t0,
                         "" if not p else " " + ",".join(f"{k}={v}" for k, v in p.items())[:80])
                return df
            err = _classify(api, code, _redact(j.get("msg"), token), with_backup=with_backup)
            if role == "backup" and not isinstance(err, TushareBadToken):
                _note_backup_ok()           # 备用答了 JSON (业务错误码也算) → 备用连续失败清零; 但「不认这把 key」算备用失败
            if isinstance(err, (TushareNoPermission, TushareBadParams)):
                log.warning("tushare %s 永久性失败, 不重试: %s", api, err)
                raise err
            last = err
            log.warning("tushare %s 失败 %d/%d (%s): %s", api, attempt, budget, _host(use_base), err)
            if isinstance(err, TushareBadToken):
                if role == "primary":
                    restart = _note_primary_token_error(base, backup, f"{api} code={code}")
                elif role == "backup":
                    restart = _note_backup_failure(token, base, backup, f"{api} code={code} (备用不认这把 key)")
        if restart:
            if rounds < MAX_ROUNDS_PER_CALL:
                rounds += 1
                attempt = 0             # 刚切换/回切: 本次调用在新基址上重新来一轮, 不退避
                continue
            break                       # 已经两轮: 不再重来 (状态已改, 下一次调用会用新基址)
        if attempt < budget:
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
    最坏 3×120 + 退避 2+4 ≈ 366s)。**配了备用基址时最坏翻倍再加试探** (2026-10-12 卡 TS-FALLBACK-2):
    切换/回切后在另一个基址重来一轮, 一次调用最多两轮, 外加最多两次主站试探 (各 ≤ PROBE_DEADLINE_SEC=30s):
    2×366 + 2×30 ≈ 792s ≈ 13 分钟。**跑在 systemd 定时器里、又有 TimeoutStartSec 的调用方
    必须自己收紧这两个值** —— 366s 已经单独超过 stock-tsprobe 的 5min 启动超时, 镜像滴流
    那天会让单元被 systemd 判 timeout/failed (2026-09-08 卡 DATA-B 返工据此加的这两个参数);
    retries=1 时一次调用最坏 ≈ 2×120 + 2×30 = 300s。
    """
    df = query("trade_cal", fields="cal_date,is_open", exchange=exchange,
               retries=retries, deadline_sec=deadline_sec,
               start_date=_ymd(start), end_date=_ymd(end), is_open=is_open)
    return _open_days(df)


def _open_days(df) -> list:
    """trade_cal 回包 -> 开市日 ISO 升序列表 (trade_cal 与 --probe-backup 共用)。"""
    if df is None or not len(df) or "cal_date" not in df.columns:
        return []
    if "is_open" in df.columns:
        # 任何参数下都在本地再过滤一遍: 2026-10-10 换到镜像 tl.kaixin8.top 后发现它对 start==end 的
        # 单日查询不套 is_open=1 (返回 is_open=0 的那一行), 区间查询才套 —— 客户端只数行数就把周六
        # 10-10 当成开市日, 就绪闸门等了 60 分钟拒发 10-09 产物。服务端过滤只当优化, 不当保证。
        df = df[pd.to_numeric(df["is_open"], errors="coerce") == 1]
    out = sorted({f"{str(d)[:4]}-{str(d)[4:6]}-{str(d)[6:8]}" for d in df["cal_date"]})
    return out


# ---------------------------------------------------------------- 命令行: 备用站真验收 (卡 TS-FALLBACK-2)
PROBE_DAILY_FIELDS = "ts_code,trade_date,open,high,low,close,vol,amount"   # 与 ashare.market.fetch_bars_by_date 同字
PROBE_DAILY_MIN_RATIO = 0.9     # 备用 daily 行数 ≥ 主站的这个比例才算可用 (与 pricestore 未就绪守卫 0.9 同一个数)


def _direct_query(url: str, token: str, api: str, fields: str, params: dict) -> pd.DataFrame:
    """绕开切换状态, 对指定基址打一次 (单次, 不重试; 走同一个进程级硬期限) -> DataFrame。业务错误按 _classify 抛。"""
    payload = {"api_name": api, "token": token, "params": dict(params), "fields": fields}
    j = _deadline_call(_post_json, (url, payload, TIMEOUT_SEC), {}, DEADLINE_SEC)
    code = j.get("code") if isinstance(j, dict) else None
    if code != 0:
        raise _classify(api, code, _redact(j.get("msg") if isinstance(j, dict) else j, token))
    d = j.get("data") or {}
    cols = list(d.get("fields") or [])
    return pd.DataFrame(d.get("items") or [], columns=cols or None)


def probe_backup(out=print, today=None) -> int:
    """`python -X utf8 -m ashare.tushare_client --probe-backup`: 备用站**真验收**, 共 4 次调用 ——
    ① 主站 trade_cal (近 3 周 + 后 1 周) 与 daily (窗口内最近一个开市日) 走生产路径 query() (经切换机制, 完了核
    failover_state()['active'] 必须是 False = 主站正常时没切); ② 备用站同两项**直调** (绕开切换状态, 不重试) ——
    主站健康时生产请求永远到不了备用, 所以备用地址写错 / 不认这把 key 只有直调才看得出。
    判定: 备用开市日列表与主站一致, 且备用 daily 行数 ≥ 主站 × PROBE_DAILY_MIN_RATIO 并带齐生产要的列。
    退出码 0 = 备用可用 / 1 = 备用不可用 (打不通 / 不认 key / 日历或行数不符) / 2 = 判不了 (没 token / 没配备用 /
    主站本身失败或验收时就切了)。只打 host、行数、耗时; token 永不打印 (异常文本也先抹)。
    加键之前先验: PowerShell `$env:TUSHARE_BASE_URL_BACKUP='<备用完整 POST 地址>'` 再跑本命令 (credentials 认环境变量)。"""
    import datetime as _dt
    token, base, backup = credentials(refresh=True)
    if not token:
        out("判不了 (rc 2): 没配 tushare_token (data/secrets.json)")
        return 2
    if not _has_backup(base, backup):
        out("判不了 (rc 2): 没配备用基址 —— secrets.json 键 tushare_base_url_backup, 或临时 "
            "TUSHARE_BASE_URL_BACKUP=<备用完整 POST 地址> (与主站不同)")
        return 2
    today = today or _dt.date.today()
    start, end = today - _dt.timedelta(days=21), today + _dt.timedelta(days=7)
    out(f"主站 {_host(base)} / 备用 {_host(backup)}; 日历窗口 {start}..{end}; 共 4 次调用 (主站 2 走生产路径, 备用 2 直调)")
    t0 = time.time()
    try:
        days_p = trade_cal(start, end)
    except Exception as e:          # noqa: BLE001
        out(f"判不了 (rc 2): 主站 {_host(base)} trade_cal 失败: {type(e).__name__}: {_redact(str(e), token)[:120]}")
        return 2
    st = failover_state()
    if st["active"]:
        out(f"判不了 (rc 2): 验收期间主站就切到了备用 ({st['reason']}) —— 先看主站, 再验备用")
        return 2
    out(f"主站 trade_cal: 开市日 {len(days_p)} 天 {time.time() - t0:.1f}s; 切换状态 active={st['active']}")
    if not days_p:
        out("判不了 (rc 2): 主站日历窗口内 0 个开市日")
        return 2
    past = [d for d in days_p if d <= today.isoformat()]
    day = past[-1] if past else days_p[0]
    t0 = time.time()
    try:
        df_p = query("daily", trade_date=_ymd(day), fields=PROBE_DAILY_FIELDS)
    except Exception as e:          # noqa: BLE001
        out(f"判不了 (rc 2): 主站 {_host(base)} daily {day} 失败: {type(e).__name__}: {_redact(str(e), token)[:120]}")
        return 2
    if failover_state()["active"]:
        out(f"判不了 (rc 2): 验收期间主站就切到了备用 ({failover_state()['reason']})")
        return 2
    n_p = len(df_p)
    out(f"主站 daily {day}: {n_p} 行 {time.time() - t0:.1f}s")
    if n_p == 0:
        out(f"判不了 (rc 2): 主站 daily {day} 0 行 (镜像当日未入库?), 换个时间再验")
        return 2
    bad = []
    t0 = time.time()
    try:
        days_b = _open_days(_direct_query(backup, token, "trade_cal", "cal_date,is_open",
                                          {"exchange": "SSE", "start_date": _ymd(start), "end_date": _ymd(end),
                                           "is_open": "1"}))
        same = days_b == days_p
        out(f"备用 trade_cal: 开市日 {len(days_b)} 天 {time.time() - t0:.1f}s"
            + (" (与主站一致)" if same else f" —— 与主站不一致 (主站 {len(days_p)} 天: {sorted(set(days_p) ^ set(days_b))[:6]})"))
        if not same:
            bad.append("日历与主站不一致")
    except Exception as e:          # noqa: BLE001
        out(f"备用 trade_cal 失败: {type(e).__name__}: {_redact(str(e), token)[:120]}")
        bad.append("trade_cal 打不通或不认这把 key")
    n_b = 0
    t0 = time.time()
    try:
        df_b = _direct_query(backup, token, "daily", PROBE_DAILY_FIELDS, {"trade_date": _ymd(day)})
        n_b = len(df_b)
        missing = sorted(set(PROBE_DAILY_FIELDS.split(",")) - set(map(str, df_b.columns)))
        out(f"备用 daily {day}: {n_b} 行 ({n_b / n_p:.0%} of 主站) {time.time() - t0:.1f}s"
            + (f" —— 缺列 {missing}" if missing else ""))
        if n_b < PROBE_DAILY_MIN_RATIO * n_p:
            bad.append(f"daily 行数 {n_b} < 主站 {n_p} × {PROBE_DAILY_MIN_RATIO:g}")
        if missing:
            bad.append(f"daily 缺列 {missing}")
    except Exception as e:          # noqa: BLE001
        out(f"备用 daily 失败: {type(e).__name__}: {_redact(str(e), token)[:120]}")
        bad.append("daily 打不通或不认这把 key")
    if bad:
        out(f"结论 (rc 1): 备用 {_host(backup)} 不可用 —— " + "; ".join(bad) + "; 不要加键 / 加了就去掉")
        return 1
    out(f"结论 (rc 0): 备用 {_host(backup)} 可用 —— 日历一致 {len(days_p)} 天, daily {day} {n_b}/{n_p} 行; "
        f"主站 {_host(base)} 正常, 切换状态 active=False")
    return 0


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -X utf8 -m ashare.tushare_client",
                                 description="Tushare 客户端自检 (只打 host / 行数 / 耗时; token 永不打印)")
    ap.add_argument("--probe-backup", action="store_true",
                    help="备用基址真验收: 4 次调用 (主站 trade_cal+daily 走生产路径, 备用同两项直调); 退出码 0 可用 / 1 不可用 / 2 判不了")
    ap.add_argument("--state", action="store_true", help="打印本进程的切换状态 (不联网)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if a.probe_backup:
        return probe_backup()
    if a.state:
        print(failover_state())
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    import importlib
    import sys
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:           # noqa: BLE001
            pass
    # 用包里那份模块跑 (不是 __main__ 这份副本): datasource/market 与进程池看到的是同一个模块实例, 切换状态只有一份
    sys.exit(importlib.import_module("ashare.tushare_client").main())
