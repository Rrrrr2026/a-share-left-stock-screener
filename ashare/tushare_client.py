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
    (单日, 单次, socket 20s / 墙钟 30s), 应答 code 0 即回切并打一行 INFO「tushare 基址回切: 备用 <host> → 主站 <host> (原因: …)」;
    备用连续 FAILBACK_AFTER=3 次失败 (传输层 / 不认这把 key / **无权限或没这个接口**) 或连续 FAILBACK_AFTER_TIMEOUTS=6 次硬期限
    超时 (单独计, 与主站同口径) 也立即试探主站, 成功即回切并在主站重来一轮 —— 坏备用不会把主站的一次抖动放大成整轮停摆,
    15000 档缺的接口在主站回来后也能在主站补上; 两站都不可用就留在备用 (再满 3 次再试)。一次 query() 最多两轮
    (MAX_ROUNDS_PER_CALL), 防两站都抖时乒乓。**试探在自己的线程里跑, 不经 datasource 的共享进程池** (2026-10-17 卡
    TS-FALLBACK-3: 走池的试探撞 30s 期限会 terminate 整池, 连带在途的备用请求与 akshare 请求; 池被慢请求占满时试探还会在队列里
    等到超时而主站根本没收到请求 —— 校验员真进程池复现); 试探里抛了什么都只算「试探失败」, probing 位子在 finally 里释放。
  · **不计入切换的**: 业务错误 (无权限 40203 / unknown_api / 参数 / 超频) —— 它们是 JSON 应答, 反而清零主站计数; 网关 HTTP 429
    限流 —— 按超频线性退避重试, 不计也不清零。15000 档把权限错误当故障会循环切换, 所以主站只有传输层才算 (备用上的无权限只触发
    「试探主站」, 不触发切换; 主站回不来照旧把 NoPermission 抛给调用方)。
  · **没配备用键**: 不计数、不切换、不试探, 错误分类顺序 (超频 → 参数 → 权限 → 传输) 与异常类型同 7b92bb6 版 (用例逐字节钉住);
    token 失效文案只在配了备用时才单独分出来 (TushareBadToken) 用于立即切换。**有无备用都生效的两处刻意改动** (2026-10-12):
    网关 HTTP 429 从「传输错误 2s/4s 退避抛 Transport」改成「超频 12s/24s 退避抛 RateLimited」; 「[unknown_api]」「[invalid_params]」
    及含「未开放」「参数缺失」的文案一次就抛 (NoPermission / BadParams), 不再白重试 3 次 —— 这两个子串比 kaixin8 实测的两种形态宽,
    是有意的 (都是永久性条件)。
  · **最坏墙钟**: 没备用 3×120 + 退避 2+4 ≈ 366s; 配了备用, 一次调用最多两轮 + 最多两次主站试探 (各 ≤ 30s):
    2×366 + 2×30 ≈ 792s ≈ 13 分钟 —— 有 TimeoutStartSec 的调用方按这个数收紧 retries / deadline_sec (见 trade_cal)。
  · **真验收**: `python -X utf8 -m ashare.tushare_client --probe-backup` (6 次调用: 主站 trade_cal + 两个开市日的 daily 走生产路径,
    备用站同三项直调绕开切换状态; 备用回包的 trade_date 必须**全部等于所问日** —— 生产按所问日期写价格库、不看回包日期, 无视或
    错配 trade_date 的备用站切过去就会静默写错 K 线和复权因子, 光数行数拦不住 (10-17 校验员实证); 第二个日子是已知的过去开市日,
    专抓「一律回最新一天」的镜像; 两天的代码集与主站重合 ≥ 90% 且行数 ≥ 主站 × 0.9), 退出码 0 可用 / 1 不可用 / 2 判不了;
    加键之前用环境变量 TUSHARE_BASE_URL_BACKUP 临时给备用地址先验 —— 主站健康时请求永远到不了备用站, 光跑 `pricestore ready`
    什么都验不出 (10-12 校验员实证)。
  进程池里跑的 `_post_json` 只拿主进程选好的基址当参数, 切换状态只在主进程 (加锁) 判定。

分页 (2026-10-24 卡 TS-PAGINATE): 备用站 datahubco **单页最多回 5000 行** (GM 10-24 用候选 secrets 跑 compat: 全市场按 trade_date 的 daily /
adj_factor / daily_basic 都恰 5000 行; 加测 daily offset=5000 → 559 行 (合计 5559 = 全市场), limit=6000 → 400「query limit exceeds the maximum」,
daily_basic offset=5000 → 559 行); 主站 kaixin8 单页能回 5,2xx 行所以从未暴露 —— 一旦切到备用, 不分页就静默丢 ~560 只。`query()` 对回包行数
**恰等于本站单页上限** (`page_limit()`: secrets 键 `tushare_page_limit` / 环境变量 TUSHARE_PAGE_LIMIT / 缺省 PAGE_LIMIT_DEFAULT=5000; ≤ 0 关掉)
的调用自动带 offset 续拉, 直到不足一页 / 0 行 / 服务端 has_more=False (回包里有这个字段就听它的), 各页合并后按该接口主键去重 (PAGE_KEYS; 没登记
的接口有 ts_code+trade_date 就按它, 否则整行去重); 每一页都是一次完整的 `_query_page` (限频桶 / 硬期限 / 重试 / 切换计数一样算), 多于一页时多打
一行 INFO「tushare <api>: 分页 N 页 M 行 (<host>)」。行数 < 上限不翻页 —— 对本来就 ≤ 5000 行的接口零改动 (第一页的请求体与以前逐字节相同,
不带 offset / limit)。调用方自己翻页的 (传了 offset / limit, 如 stock-core research/lake.py 的 `_pages`) 不自动翻; `paginate=False` 强关,
`paginate=True` 带 limit 也按本站上限翻。镜像不认 offset (原样回上一页) → 停, WARNING 一行; 续页被拒参数 (BadParams) → WARNING 并返回已拿到
的页。`--probe-backup` 的备用直调 (`_direct_query`) 同样分页, 否则备用 5000/5559 = 89.9% 永远过不了 ≥ 90% 的门。

进程池回退 (同卡): `_deadline_call` 把 datasource 的进程级硬期限跑在一个看门线程里, 最多等 deadline_sec + POOL_GRACE_SEC —— 池机制自己挂住
(10-24 GM 在非交互 Bash 里跑 --probe-backup: spawn 报 PermissionError [WinError 5] 后 Pool.terminate 不回来, 挂到外层 300s 超时) 或从池机制
抛出基础设施异常 (PermissionError / BrokenPipeError / EOFError / BrokenProcessPool / datasource._PoolUnavailable; 工作进程里 fn 自己抛的异常带
RemoteTraceback, 不算) → WARNING 一行, 本进程余下 Tushare 调用永久改走线程版硬期限 (`_thread_call`, 期限仍生效), 并把 datasource._POOL_DISABLED
置 True (与 09-05 datasource「基础设施异常一次即永久回退线程版」同口径, 让 akshare 那侧也别再碰可能已挂住的池)。`--probe-backup` 开头先用一次
`_pool_ping` (POOL_WARMUP_SEC) 把这条路走通, 池坏了在那里就回退, 后面 6 次不再各等一轮。

本模块提供:
    available()                      -> 是否配了 token
    page_limit()                     -> 本站单页上限 (自动分页阈值: secrets tushare_page_limit / 环境变量 / 缺省 5000)
    credentials()                    -> (token, base_url, backup_base_url); 没配备用则第三项为 ""
    failover_state() / failover_reset() -> 备用基址切换状态 (诊断 / 用例用)
    probe_backup()                   -> 备用站真验收 (命令行 --probe-backup), 退出码 0/1/2
    pool_state()                     -> 进程池回退状态 (诊断 / 用例用)
    query(api, **params)             -> pandas DataFrame (空结果也带列名; 恰满一页自动分页, 见「分页」)
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
FAILBACK_AFTER = 3          # 备用连续这么多次失败 (传输层 / 不认 key / 无权限) → 试探主站一次, 成功即回切 (坏备用不把主站一次抖动放大成整轮停摆)
FAILBACK_AFTER_TIMEOUTS = 6 # 备用连续这么多次进程级硬期限超时才试探 (单独计, 与主站同口径: 共享进程池被别人的超时重置会连带在途的备用请求)
PROBE_EVERY_CALLS = 20      # 在备用上每这么多次调用试探主站一次 (轻量 trade_cal)
PROBE_EVERY_SEC = 600       # …或每这么多秒 (先到先试)
PROBE_TIMEOUT_SEC = 20      # 试探那次的 socket 超时 (requests timeout; 滴流时它不触发, 靠下面的墙钟)
PROBE_DEADLINE_SEC = 30     # 试探那次的墙钟 (线程版 _probe_call: 到点就不等了, 不 terminate 任何东西, 不碰共享进程池)
PROBE_SECOND_DAY_BACK = 5   # --probe-backup 的第二个核对日: 最近开市日再往前这么多个开市日 (已知的过去开市日, 抓「一律回最新一天」的镜像)
MAX_ROUNDS_PER_CALL = 2     # 一次 query() 最多在两个基址上各来一轮 (切换/回切后重来一轮; 再切就不重来, 防两站都抖时乒乓)
TOKEN_ERROR_CODES = (2002, 40101)   # token 过期/无效类错误码 → 立即切备用 (10-08 旧镜像就是 code=2002 token已过期)
PAGE_LIMIT_DEFAULT = 5000   # 「本站单页上限」缺省 (备用站 datahubco 10-24 实测; 主站 kaixin8 单页能回 5,2xx 行, 恰 5000 行那天多打一页 0 行也无害);
                            # secrets 键 tushare_page_limit / 环境变量 TUSHARE_PAGE_LIMIT 可改, ≤ 0 = 关掉自动分页 (见 page_limit)
PAGE_MAX = 40               # 自动分页最多翻这么多页 (40 × 5000 = 20 万行; 全市场按日端点 ≤ 6 千行, 单股全史 ≤ 6 千行), 防镜像每页都不同却翻不完
PAGE_KEYS = {               # 合并去重的主键 (列齐才用); 没登记的接口: 有 ts_code + trade_date 就按它, 否则整行去重
    "daily": ("ts_code", "trade_date"), "adj_factor": ("ts_code", "trade_date"), "daily_basic": ("ts_code", "trade_date"),
    "stk_factor": ("ts_code", "trade_date"), "stk_factor_pro": ("ts_code", "trade_date"), "moneyflow": ("ts_code", "trade_date"),
    "ths_daily": ("ts_code", "trade_date"), "index_daily": ("ts_code", "trade_date"), "sw_daily": ("ts_code", "trade_date"),
    "stock_basic": ("ts_code",), "trade_cal": ("exchange", "cal_date"),
}
POOL_GRACE_SEC = 30         # 进程级硬期限之外再给池机制这么久 (建池 / 就绪探针 / terminate): 还没回来就判「池机制挂死」, 回退线程版 (10-24 非交互 Bash 的 WinError 5 形态)
POOL_WARMUP_SEC = 20        # --probe-backup 开头的进程池热身 (_pool_ping) 期限: 健康的 spawn 池 5–8s 就绪 (2s DOA 等待 + 起解释器 + 导入); 超过它 + POOL_GRACE_SEC 即回退线程版

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
    refresh=True 重读 secrets 并把基址切换状态复位 (新凭据从主站重新开始), 单页上限缓存 (page_limit) 也清掉重读。"""
    global _CRED, _PAGE_LIMIT
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
            _PAGE_LIMIT = None      # 新凭据可能换了站, 单页上限重读
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


_PAGE_LIMIT = None          # page_limit() 的缓存 (环境变量 / secrets / 缺省); credentials(refresh=True) 清掉重读


def page_limit() -> int:
    """本站单页上限 (行): 环境变量 TUSHARE_PAGE_LIMIT > secrets 键 tushare_page_limit > PAGE_LIMIT_DEFAULT; ≤ 0 = 关掉自动分页。
    回包行数**恰等于**它才续页 (见 _page_more) —— 所以这个数必须是**本站真实的**单页上限: 写大了 (站只给 5000 你写 6000) 等于关掉分页, 静默少拉;
    写小了 (站能给 5,2xx 你写 5000) 只是恰好 5000 行那天多打一页 0 行。主备两站共用一个数 (两站上限不同时按较小的写: 主站多打一页无害)。"""
    global _PAGE_LIMIT
    if _PAGE_LIMIT is None:
        raw = os.environ.get("TUSHARE_PAGE_LIMIT")
        if raw is None or str(raw).strip() == "":
            raw = _read_secrets().get("tushare_page_limit")
        try:
            v = PAGE_LIMIT_DEFAULT if raw is None or str(raw).strip() == "" else int(str(raw).strip())
        except (TypeError, ValueError):
            log.warning("tushare_page_limit 不是整数 (%r), 按缺省 %d", raw, PAGE_LIMIT_DEFAULT)
            v = PAGE_LIMIT_DEFAULT
        _PAGE_LIMIT = v
    return _PAGE_LIMIT


# ---------------------------------------------------------------- 备用基址切换 (状态只在主进程, 加锁)
_FO_LOCK = threading.Lock()
#: 初始状态 (failover_reset 回到这里; 用例也拿它比):
#:   active 在备用上 / fails 主站连续传输层失败 / timeouts 主站连续硬期限超时 (单独计) / reason+switched_at 上次切换
#:   backup_fails 备用连续失败 (传输层 / 不认 key / 无权限) / backup_timeouts 备用连续硬期限超时 (单独计) /
#:   calls_on_backup 切换 (或上次试探) 以来在备用上的调用数 / last_probe_at 上次试探 (monotonic)
#:   probing 有线程正在试探 / probes·switches·returns 累计次数 (诊断)
_FO_INITIAL = {"active": False, "fails": 0, "timeouts": 0, "reason": "", "switched_at": None,
               "backup_fails": 0, "backup_timeouts": 0, "calls_on_backup": 0, "last_probe_at": None, "probing": False,
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
    _FO.update(active=True, reason=reason, switched_at=time.time(), backup_fails=0, backup_timeouts=0, calls_on_backup=0,
               last_probe_at=time.monotonic())
    _FO["switches"] += 1
    log.info("tushare 基址切换: 主站 %s → 备用 %s (原因: %s)", _host(base), _host(backup), reason)


def _return_locked(base: str, backup: str, reason: str) -> None:
    """调用方持 _FO_LOCK。回到主站: 两边计数全部清零 (主站从头算), 打一行 INFO。"""
    on_backup = time.time() - (_FO["switched_at"] or time.time())
    n = _FO["calls_on_backup"]
    _FO.update(active=False, reason="", switched_at=None, fails=0, timeouts=0, backup_fails=0, backup_timeouts=0,
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
    """备用答了 JSON (code 0, 或 token 失效 / 无权限以外的业务错误码): 备用的两个连续计数清零 (不影响「每 20 次 / 10 分钟试探主站」)。"""
    with _FO_LOCK:
        _FO["backup_fails"] = 0
        _FO["backup_timeouts"] = 0


def _thread_call(fn, args, kwargs, deadline_sec, timeout_msg: str):
    """线程版墙钟期限: 在自己的守护线程里跑 fn, 主线程最多等 deadline_sec 秒, 到点抛 TimeoutError(timeout_msg) (被弃的线程只占一个 socket,
    自己读完或 socket 超时后退出; 线程杀不死, 所以这是兜底不是主路 —— 主路是 datasource 的进程池)。试探 (_probe_call) 与进程池不可用时的
    回退 (_deadline_call) 共用。"""
    import queue as _q
    box = _q.Queue(maxsize=1)

    def _run():
        try:
            box.put((True, fn(*args, **kwargs)))
        except BaseException as e:      # noqa: BLE001  (全部带回主线程, 由调用方分类)
            box.put((False, e))
    threading.Thread(target=_run, daemon=True, name=f"tushare-{getattr(fn, '__name__', 'call')}").start()
    try:
        ok, val = box.get(timeout=deadline_sec)
    except _q.Empty:
        raise TimeoutError(timeout_msg) from None
    if ok:
        return val
    raise val


def _probe_call(fn, args, kwargs, deadline_sec):
    """试探专用的墙钟期限 (线程版, 见 _thread_call)。**刻意不走 datasource 的共享进程池**: 池是 akshare 与 Tushare 共用的 2 个工作进程,
    走池的试探 ① 撞期限会 _pool_reset() terminate 整池, 在途的健康备用请求与 akshare 请求一起被记成超时 (备用侧还会因此连着
    触发下一次试探); ② 池被两条慢请求占满时试探在队列里排队, 排队时间也算在期限内, 主站根本没收到请求就被判「试探失败」
    (2026-10-17 校验员真进程池 E2/E3/E4 复现)。试探是一次单日 trade_cal 的小 POST, 线程版够用。"""
    return _thread_call(fn, args, kwargs, deadline_sec, f"trade_cal 超过 {deadline_sec}s 墙钟 (试探线程已弃)")


def _probe_primary(token: str, base: str) -> tuple:
    """对主站打一次轻量 trade_cal (单日, 单次, PROBE_TIMEOUT_SEC socket / PROBE_DEADLINE_SEC 墙钟, 不重试, 不经共享进程池)
    -> (ok, 说明)。ok 只认 code 0 —— 业务错误码 (比如 token 仍失效) 不算恢复。**抛出的任何 Exception 都算试探失败**
    (不让调用方的 query() 因为一次试探里的意外炸掉, 位子也不会卡住); 说明里不含 token。"""
    day = time.strftime("%Y%m%d")
    payload = {"api_name": "trade_cal", "token": token,
               "params": {"exchange": "SSE", "start_date": day, "end_date": day}, "fields": "cal_date,is_open"}
    _BUCKET.acquire()
    t0 = time.time()
    try:
        j = _probe_call(_post_json, (base, payload, PROBE_TIMEOUT_SEC), {}, PROBE_DEADLINE_SEC)
    except TimeoutError:
        return False, f"trade_cal 超过 {PROBE_DEADLINE_SEC}s 墙钟"
    except (TushareError, OSError, ValueError) as e:
        return False, f"trade_cal {_redact(str(e), token)[:80]}"
    except Exception as e:      # noqa: BLE001  (MaybeEncodingError / MemoryError 一类: 也只是「这次没探通」)
        return False, f"trade_cal 试探异常 {type(e).__name__}: {_redact(str(e), token)[:60]}"
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
        _FO["backup_timeouts"] = 0
        if not _FO["active"]:
            return False
        log.log(level, "tushare 主站 %s 试探失败 (%s): %s; 继续用备用 %s", _host(base), why, detail, _host(backup))
        return False


def _run_probe(token: str, base: str, backup: str, why: str, level: int = logging.INFO) -> bool:
    """占到 probing 位子的那个线程调用: 试探 → 收尾。**位子一定释放**: _probe_primary 把 Exception 都兜成「试探失败」,
    这里再用 finally 兜 BaseException (KeyboardInterrupt / SystemExit 一类) —— 否则 probing 卡成 True, 本进程再也不试探、
    永不回切 (2026-10-17 校验员场景 G: 试探里抛 RuntimeError 后拨钟 +5000s 仍直走备用)。"""
    done = False
    try:
        ok, detail = _probe_primary(token, base)
        r = _finish_probe(ok, detail, base, backup, why, level=level)
        done = True
        return r
    finally:
        if not done:
            with _FO_LOCK:
                _FO["probing"] = False
                _FO["last_probe_at"] = time.monotonic()


def _note_backup_failure(token: str, base: str, backup: str, reason: str, timeout: bool = False) -> bool:
    """备用上一次失败。两个连续计数**各算各的** (与主站同口径): timeout=True 是进程级硬期限超时 (backup_timeouts, 阈值
    FAILBACK_AFTER_TIMEOUTS=6 —— 共享进程池被别的调用的超时 terminate 时, 在途的备用请求也会被记成超时, 备用其实没坏);
    其余 (传输层 / 备用不认这把 key 的 2002·40101 / 备用无权限或没这个接口) 计 backup_fails, 阈值 FAILBACK_AFTER=3。
    满了就立即试探主站一次: 成功即回切并返回 True (本次调用随即在主站重来一轮 —— 坏备用不把主站的一次抖动放大成整轮停摆,
    备用缺的接口也能在主站补上); 失败 = 两站都不可用, 留在备用 (再满再试)。"""
    with _FO_LOCK:
        if not _FO["active"]:
            return False
        key, limit, what = (("backup_timeouts", FAILBACK_AFTER_TIMEOUTS, "硬期限超时") if timeout
                            else ("backup_fails", FAILBACK_AFTER, "失败"))
        _FO[key] += 1
        if _FO[key] < limit or not _begin_probe_locked():
            return False
        why = f"备用连续 {_FO[key]} 次{what}, 最后一次: {reason}"
    return _run_probe(token, base, backup, why, level=logging.WARNING)


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
    return _run_probe(token, base, backup, why)


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


_TC_POOL = {"disabled": False, "reason": ""}   # 本进程内 Tushare 这侧「进程池不可用, 余下走线程版」的开关 (一次即永久, 与 datasource._POOL_DISABLED 同口径)


def _pool_ping() -> int:
    """最便宜的池内调用 (--probe-backup 热身用): 模块级、可 pickle, 在工作进程里返回它自己的 pid。"""
    return os.getpid()


def pool_state() -> dict:
    """诊断用: Tushare 侧进程池回退状态的副本 (disabled / reason), 不含凭据。"""
    return dict(_TC_POOL)


def _pool_fallback_reset() -> None:
    """用例用: 回到「先试进程池」。"""
    _TC_POOL.update(disabled=False, reason="")


def _is_pool_infra_error(e: BaseException) -> bool:
    """从 datasource 执行器抛出来的异常是不是**池机制自己**的 (建池 / 提交 / 管道), 而不是 fn 在工作进程里抛的业务异常。
    工作进程里抛的异常经 multiprocessing 带回时 __cause__ 是 RemoteTraceback —— 那是 fn 的, 原样抛给调用方 (requests 的连接错也在此列);
    TimeoutError (硬期限) 与 TushareError 永远是业务的。余下: concurrent.futures 的 BrokenExecutor (含 BrokenProcessPool) / datasource 的
    _PoolUnavailable 一定是池的; PermissionError (WinError 5 DuplicateHandle) / BrokenPipeError / EOFError (管道断) 只要不是 requests/urllib3
    抛的也算池的 (requests 把 socket 错误都包成自己的 ConnectionError, 不会裸抛这三种); 其它 OSError / RuntimeError 看抛出点是否在
    multiprocessing / concurrent.futures 里。"""
    if isinstance(e, (TimeoutError, TushareError)):
        return False
    try:
        from multiprocessing.pool import RemoteTraceback
        if isinstance(e.__cause__, RemoteTraceback):
            return False
    except Exception:           # noqa: BLE001
        pass
    try:
        from concurrent.futures import BrokenExecutor
        if isinstance(e, BrokenExecutor):
            return True
    except Exception:           # noqa: BLE001
        pass
    if type(e).__name__ == "_PoolUnavailable":
        return True
    if not isinstance(e, (OSError, EOFError, RuntimeError)):
        return False
    if (type(e).__module__ or "").split(".")[0] in ("requests", "urllib3", "ssl", "http", "socket"):
        return False
    if isinstance(e, (PermissionError, BrokenPipeError, EOFError)):
        return True
    tb = e.__traceback__
    while tb is not None:
        fname = (tb.tb_frame.f_code.co_filename or "").replace("\\", "/")
        if "/multiprocessing/" in fname or "/concurrent/futures/" in fname:
            return True
        tb = tb.tb_next
    return False


def _disable_pool_side(ds, reason: str) -> None:
    """池机制不可用 → 本进程余下 Tushare 调用改走线程版硬期限, WARNING 只打一次; 同时把 datasource._POOL_DISABLED 置 True (不经它的
    _disable_pool: 那条路会去 terminate 可能已经挂住的池)。reason 由调用方抹过 token。"""
    first = not _TC_POOL["disabled"]
    _TC_POOL.update(disabled=True, reason=reason)
    if not first:
        return
    log.warning("tushare 进程池硬期限不可用 (%s) — 本进程余下 Tushare 调用改走线程版硬期限 (期限仍生效; 线程无法强杀, 失控线程不再被隔离; 只告警一次)", reason)
    try:
        if ds is not None and getattr(ds, "_POOL_DISABLED", None) is False:
            ds._POOL_DISABLED = True
    except Exception:           # noqa: BLE001
        pass


def _deadline_call(fn, args, kwargs, deadline_sec):
    """把一次调用交给 `ashare.datasource` 的**进程级**墙钟期限跑, 外面再套一层看门线程 (2026-10-24 卡 TS-PAGINATE)。

    懒导入是为了避开 datasource -> market -> tushare_client 的循环导入; 脱离仓库单跑 (或 datasource 导不进来) 时退化为直调, 不让基础设施问题
    变成功能缺失。优先用 `_call_with_deadline` 而不是公开的 `call_with_retry`: 后者在超时路径上会顺手把**东财**标记为不可用 —— Tushare 挂了
    不该冤枉东财 (它还是兜底源)。
    看门线程: datasource 的执行器在自己的线程里跑, 本线程最多等 deadline_sec + POOL_GRACE_SEC —— 正常情况它在 deadline_sec 内要么返回要么抛
    TimeoutError (硬期限), 多等的 30s 只给池机制 (建池 / 就绪探针 / terminate); 到点还没回来 = 池机制挂死 (10-24 非交互 Bash: spawn 子进程
    PermissionError [WinError 5] 后 Pool.terminate 等一个死掉的 handler 线程, 永不返回), 或它抛出池机制自己的异常 (_is_pool_infra_error) →
    `_disable_pool_side` 一次, 本次与余下调用改走 `_thread_call` (期限仍是 deadline_sec)。fn 自己的异常与硬期限 TimeoutError 原样抛。"""
    if not USE_PROCESS_DEADLINE:
        return fn(*args, **kwargs)
    name = getattr(fn, "__name__", "call")
    thread_msg = f"{name} 超过 {deadline_sec}s 硬期限 (线程版, 源站滴流/黑洞; 线程已弃)"
    if _TC_POOL["disabled"]:
        return _thread_call(fn, args, kwargs, deadline_sec, thread_msg)
    try:
        from . import datasource as ds
    except Exception as e:      # noqa: BLE001
        log.debug("datasource 不可用 (%s), 硬期限降级为直调", type(e).__name__)
        return fn(*args, **kwargs)
    runner = getattr(ds, "_call_with_deadline", None)
    if runner is None:          # 私有入口若被重构掉, 退回公开入口 (仍是进程级期限)
        def runner(f, a, k, _dl):
            return ds.call_with_retry(f, *a, **k)
    import queue as _q
    box = _q.Queue(maxsize=1)

    def _run():
        try:
            box.put((True, runner(fn, args, kwargs, deadline_sec)))
        except BaseException as e:      # noqa: BLE001
            box.put((False, e))
    threading.Thread(target=_run, daemon=True, name=f"tushare-pool-{name}").start()
    tok = args[1].get("token", "") if len(args) > 1 and isinstance(args[1], dict) else ""
    try:
        ok, val = box.get(timeout=float(deadline_sec) + POOL_GRACE_SEC)
    except _q.Empty:
        _disable_pool_side(ds, f"{name} 超过硬期限 {deadline_sec}s + {POOL_GRACE_SEC}s 仍未从进程池机制返回 (建池 / 就绪探针 / terminate 挂住)")
        return _thread_call(fn, args, kwargs, deadline_sec, thread_msg)
    if ok:
        return val
    if _is_pool_infra_error(val):
        _disable_pool_side(ds, f"{type(val).__name__}: {_redact(str(val), tok)[:100]}")
        return _thread_call(fn, args, kwargs, deadline_sec, thread_msg)
    raise val


# ---------------------------------------------------------------- 主入口 (分页壳) + 单页调用
def _has_more(d):
    """回包 data 里的 has_more (官方 Tushare 新版有这个字段; 镜像不一定给) -> True / False / None (没给或看不懂)。"""
    v = d.get("has_more") if isinstance(d, dict) else None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(v)
    if isinstance(v, str) and v.strip().lower() in ("true", "false", "1", "0"):
        return v.strip().lower() in ("true", "1")
    return None


def _page_more(n_rows: int, has_more, limit: int) -> bool:
    """这一页之后还翻不翻: 服务端给了 has_more 就听它的 (True 翻 / False 停; 0 行不翻); 没给 → 行数**恰等于**本站单页上限才翻 (不足一页 = 全部;
    多于上限 = 本站上限其实更高, 回的已是全部, 不翻 —— 主站 kaixin8 回 5,2xx 行就是这种)。"""
    if n_rows <= 0 or limit <= 0:
        return False
    if has_more is not None:
        return bool(has_more)
    return n_rows == limit


def _dedupe(api: str, df: pd.DataFrame) -> tuple:
    """各页合并后按该接口主键去重 -> (df, 去掉的行数)。PAGE_KEYS 登记的接口按登记的列 (列齐才用), 否则有 ts_code+trade_date 就按它, 否则整行。"""
    cols = set(map(str, df.columns))
    keys = PAGE_KEYS.get(api)
    if keys and set(keys) <= cols:
        subset = list(keys)
    elif {"ts_code", "trade_date"} <= cols:
        subset = ["ts_code", "trade_date"]
    else:
        subset = None
    try:
        out = df.drop_duplicates(subset=subset, keep="first").reset_index(drop=True)
    except TypeError:           # 不可哈希的单元格 (Tushare 回包都是标量, 以防万一): 不去重
        return df, 0
    return out, len(df) - len(out)


def _paginate(api: str, p: dict, fetch, limit: int) -> pd.DataFrame:
    """通用分页: fetch(params) -> (df, has_more, host) 打一页。第一页按原参数 (不带 offset / limit —— 对 ≤ 上限的接口零改动), 之后每页只多
    一个 offset = 已拿到的行数, 直到 _page_more 说停 / 0 行 / 镜像不认 offset (原样回上一页) / PAGE_MAX; 合并去重, 结果 .attrs["pages"] = 页数,
    多于一页时打一行 INFO「tushare <api>: 分页 N 页 M 行 (<host>)」。续页被拒参数 (BadParams: 镜像不支持 offset) → WARNING 并返回已拿到的页;
    续页传输失败重试耗尽 → 整次调用抛 (宁可失败也不返回被截断的全市场表)。"""
    df, more, host = fetch(dict(p))
    if not _page_more(len(df), more, limit):
        return df
    pages, offset, stop = [df], len(df), ""
    while True:
        try:
            d2, more2, host = fetch(dict(p, offset=offset))
        except TushareBadParams as e:
            stop = f"offset={offset} 被拒 ({str(e)[:80]}), 镜像不支持 offset 翻页, 已拿到的 {offset} 行可能不是全部"
            break
        k = len(d2)
        if k == 0:
            break
        if k == len(pages[-1]) and d2.equals(pages[-1]):
            stop = f"offset={offset} 回包与上一页相同的 {k} 行, 镜像不认 offset"
            break
        pages.append(d2)
        offset += k
        if not _page_more(k, more2, limit):
            break
        if len(pages) >= PAGE_MAX:
            stop = f"翻到 {len(pages)} 页仍满页, 截断"
            break
    out = pd.concat(pages, ignore_index=True)
    out, dropped = _dedupe(api, out)
    try:
        out.attrs["pages"] = len(pages)
    except Exception:           # noqa: BLE001
        pass
    tail = "" if not p else " " + ",".join(f"{k}={v}" for k, v in p.items())[:80]
    if stop:
        log.warning("tushare %s: %s, 停止翻页 (%s)%s", api, stop, host, tail)
    log.info("tushare %s: 分页 %d 页 %d 行 (%s)%s%s", api, len(pages), len(out), host, f", 去重 {dropped} 行" if dropped else "", tail)
    return out


def query(api: str, fields: str = "", retries: int = MAX_RETRIES,
          deadline_sec: float = DEADLINE_SEC, paginate=None, **params) -> pd.DataFrame:
    """调用一个 Tushare 端点 -> DataFrame (无数据时返回带列名的空表)。

    params 里值为 None 的键会被丢掉 (方便可选参数直接传)。
    抛 TushareUnavailable (没 token) / TushareNoPermission / TushareBadParams (都不重试) /
    TushareRateLimited (含网关 HTTP 429; 线性退避后仍超频) / TushareTransport (重试耗尽)。
    每一页经 `_query_page` (限频 / 硬期限 / 重试 / 备用基址切换, 见其 docstring); 分页规则见模块 docstring「分页」:
    paginate=None (缺省) = 调用方没传 offset / limit 才自动翻 (研究湖 lake._pages 自己按 limit/offset 翻, 这里不插手); False = 不翻;
    True = 带 limit 也按本站上限 (`page_limit()`) 翻 (传了 offset 仍不翻)。行数 < 上限 (或 page_limit() ≤ 0) 时返回的就是那一页, 与以前完全一样。
    """
    token, base, backup = credentials()
    if not token:
        raise TushareUnavailable(f"{api}: 未配置 tushare_token (data/secrets.json)", api)
    p = {k: v for k, v in params.items() if v is not None}
    auto = paginate is not False and "offset" not in p and (paginate is True or "limit" not in p)
    limit = page_limit() if auto else 0

    def fetch(q):
        return _query_page(api, fields, retries, deadline_sec, q)
    if limit <= 0:
        return fetch(p)[0]
    return _paginate(api, p, fetch, limit)


def _query_page(api: str, fields: str, retries: int, deadline_sec: float, p: dict) -> tuple:
    """一页 (一次逻辑调用, 含重试与切换) -> (DataFrame, has_more, host)。query() 的每一页都走这里; p 已去掉 None 值。
    配了备用基址时 (模块 docstring「备用基址」): 开头先看要不要试探主站 (在备用上每 20 次调用 / 10 分钟一次); 每次请求前由
    主进程按切换状态选基址 (`_active_base`); 主站连续 FAILOVER_AFTER 次传输层失败 / FAILOVER_AFTER_TIMEOUTS 次硬期限超时 /
    token 失效类错误 → 切备用; 备用连续 FAILBACK_AFTER 次失败 (传输层 / 不认 key / 无权限) 或 FAILBACK_AFTER_TIMEOUTS 次硬期限超时
    且主站试探成功 → 回主站; 切换或回切后**本次调用在新基址上重新来一轮** (完整 retries 预算, 切换瞬间不退避), 但一次调用最多
    MAX_ROUNDS_PER_CALL=2 轮。没配备用键时与 7b92bb6 版一样 (除 429 与 unknown_api / invalid_params 两处刻意改动, 见模块 docstring)。
    """
    token, base, backup = credentials()
    if not token:
        raise TushareUnavailable(f"{api}: 未配置 tushare_token (data/secrets.json)", api)
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
                restart = _note_backup_failure(token, base, backup, f"{api} 超过 {deadline_sec}s 硬期限", timeout=True)
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
                return df, _has_more(d), _host(use_base)
            err = _classify(api, code, _redact(j.get("msg"), token), with_backup=with_backup)
            if role == "backup" and not isinstance(err, (TushareBadToken, TushareNoPermission)):
                _note_backup_ok()           # 备用答了 JSON (参数错 / 超频 / 其它业务错误码也算) → 备用两个连续计数清零;
                                            # 「不认这把 key」与「无权限 / 没这个接口」算备用失败 (下面)
            if isinstance(err, (TushareNoPermission, TushareBadParams)):
                if role == "backup" and isinstance(err, TushareNoPermission):
                    # 备用无权限 / 没这个接口 (15000 档比主站少): 在备用上重试没用, 但算一次备用失败 —— 连续满 3 次就试探主站,
                    # 主站回来了本次调用就在主站重来 (主站有权限); 回不来才把 NoPermission 抛给调用方 (卡 TS-FALLBACK-3)
                    last = err
                    restart = _note_backup_failure(token, base, backup, f"{api} code={code} (备用无权限或没这个接口)")
                    if restart:
                        log.warning("tushare %s 在备用无权限 (%s), 主站已回来, 本次在主站重来: %s", api, _host(use_base), err)
                if not restart:
                    log.warning("tushare %s 永久性失败, 不重试: %s", api, err)
                    raise err
            else:
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


# ---------------------------------------------------------------- 命令行: 备用站真验收 (卡 TS-FALLBACK-2; 10-17 TS-FALLBACK-3 加日期核对)
PROBE_DAILY_FIELDS = "ts_code,trade_date,open,high,low,close,vol,amount"   # 与 ashare.market.fetch_bars_by_date 同字
PROBE_DAILY_MIN_RATIO = 0.9     # 备用 daily 行数与「与主站重合的代码数」都要 ≥ 主站的这个比例才算可用 (与 pricestore 未就绪守卫 0.9 同一个数)


def _direct_query(url: str, token: str, api: str, fields: str, params: dict) -> pd.DataFrame:
    """绕开切换状态, 对指定基址打 (每页单次, 不重试; 走同一个进程级硬期限) -> DataFrame。业务错误按 _classify 抛。
    与 query() 一样按本站单页上限自动分页 (调用方传了 offset / limit 则不翻): 备用 datahubco 单页 5000 行, 不分页的话 --probe-backup 的
    「备用行数 ≥ 主站 × 0.9」永远过不了 (5000/5559 = 89.9%)。"""
    def fetch(q):
        payload = {"api_name": api, "token": token, "params": dict(q), "fields": fields}
        j = _deadline_call(_post_json, (url, payload, TIMEOUT_SEC), {}, DEADLINE_SEC)
        code = j.get("code") if isinstance(j, dict) else None
        if code != 0:
            raise _classify(api, code, _redact(j.get("msg") if isinstance(j, dict) else j, token))
        d = j.get("data") or {}
        cols = list(d.get("fields") or [])
        return pd.DataFrame(d.get("items") or [], columns=cols or None), _has_more(d), _host(url)
    p = dict(params)
    limit = page_limit() if "offset" not in p and "limit" not in p else 0
    if limit <= 0:
        return fetch(p)[0]
    return _paginate(api, p, fetch, limit)


def _daily_dates_match(df, day) -> tuple:
    """daily 回包里 trade_date 列是否**全部**等于所问的 day -> (ok, 不符的日期样例)。
    生产 (ashare.market._rows_from_daily / fetch_adj_by_date) 按 ts_code 取值、不看 trade_date, 把回包按**所问日期**写进价格库 ——
    无视或错配 trade_date 的备用站 (kaixin8 已实证会无视参数: 单日 is_open、index_member_all 的 l1_code) 切过去之后, 断更后补跑
    旧日会静默写错 K 线和复权因子; 光数行数拦不住 (10-17 校验员 J1: 问 20261016 回 20261015 的 5000 行照样 rc 0)。
    没有 trade_date 列时返回 ok (那是「缺列」, 由列检查单独判, 不重复报)。"""
    if df is None or "trade_date" not in df.columns:
        return True, []
    want = _ymd(day)
    bad = sorted({_ymd(d) for d in df["trade_date"]} - {want})
    return not bad, bad[:3]


def _codes(df) -> set:
    return set(map(str, df["ts_code"])) if df is not None and "ts_code" in df.columns else set()


def _pages_note(df) -> str:
    """回包是分页拼出来的就标「[分页 N 页]」(query / _direct_query 在结果 .attrs["pages"] 上记页数; 单页没有这个键)。"""
    try:
        n = int((getattr(df, "attrs", None) or {}).get("pages", 1))
    except Exception:           # noqa: BLE001
        n = 1
    return f" [分页 {n} 页]" if n > 1 else ""


def _warm_pool() -> str:
    """--probe-backup 开头: 先拿最便宜的 _pool_ping 把进程级硬期限这条路走通一次 -> 一句说明 (走的是进程池还是线程版)。池机制挂住 / 起不来
    (10-24 非交互 Bash 的 WinError 5 形态) 时在这里就按 _deadline_call 的规则回退线程版, 后面 6 次调用不再各等一轮 (最坏 POOL_WARMUP_SEC +
    POOL_GRACE_SEC = 50s 而不是挂死)。不打 token。"""
    if not USE_PROCESS_DEADLINE:
        return "直调 (USE_PROCESS_DEADLINE=False)"
    t0 = time.time()
    try:
        pid = _deadline_call(_pool_ping, (), {}, POOL_WARMUP_SEC)
    except Exception as e:          # noqa: BLE001
        return f"热身失败 {type(e).__name__}: {str(e)[:80]} —— 后面各次调用自行回退"
    st = pool_state()
    if st["disabled"]:
        return f"线程版硬期限 (进程池不可用: {st['reason'][:100]}) {time.time() - t0:.1f}s"
    where = "工作进程" if pid != os.getpid() else "本进程"
    return f"进程池硬期限就绪 ({where}) {time.time() - t0:.1f}s"


def probe_backup(out=print, today=None) -> int:
    """`python -X utf8 -m ashare.tushare_client --probe-backup`: 备用站**真验收**, 共 6 次调用 (备用按本站单页上限分页时 HTTP 次数多于 6,
    备用 daily 行标「[分页 N 页]」; 开头另有一次不联网的进程池热身 `_warm_pool`, 池坏了在那里就回退线程版) ——
    ① 主站 trade_cal (近 3 周 + 后 1 周) 与两个开市日的 daily (窗口内最近一个开市日 D, 以及 D 往前 PROBE_SECOND_DAY_BACK 个开市日
    的 P) 走生产路径 query() (经切换机制, 每步核 failover_state()['active'] 必须是 False = 主站正常时没切);
    ② 备用站同三项**直调** (绕开切换状态, 不重试) —— 主站健康时生产请求永远到不了备用, 所以备用地址写错 / 不认这把 key 只有直调
    才看得出。判定 (每个日子各判): 备用开市日列表与主站一致; 备用 daily 行数 ≥ 主站 × PROBE_DAILY_MIN_RATIO; 与主站重合的代码数
    ≥ 主站代码数 × PROBE_DAILY_MIN_RATIO (重复行凑数的回包过不了); 带齐生产要的列; **回包 trade_date 全部等于所问日** (无视或
    错配 trade_date 的备用站, 切过去生产会按所问日期写错价格库; 第二个日子 P 专抓「一律回最新一天」的镜像 —— 它在 D 上与正确
    镜像完全一样)。退出码 0 = 备用可用 / 1 = 备用不可用 / 2 = 判不了 (没 token / 没配备用 / 主站本身失败或回包日期不对 / 验收时
    就切了)。只打 host、行数、代码数、耗时; token 永不打印 (异常文本也先抹)。
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
    out(f"主站 {_host(base)} / 备用 {_host(backup)}; 日历窗口 {start}..{end}; 共 6 次调用 (主站 3 走生产路径, 备用 3 直调)")
    out(f"硬期限执行器: {_warm_pool()}")
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
    if len(past) < 2:
        out("判不了 (rc 2): 主站日历窗口内过去开市日不足 2 天, 换个时间再验")
        return 2
    day = past[-1]
    day2 = past[-1 - PROBE_SECOND_DAY_BACK] if len(past) > PROBE_SECOND_DAY_BACK else past[0]
    prim = {}
    for d in (day, day2):
        t0 = time.time()
        try:
            df = query("daily", trade_date=_ymd(d), fields=PROBE_DAILY_FIELDS)
        except Exception as e:          # noqa: BLE001
            out(f"判不了 (rc 2): 主站 {_host(base)} daily {d} 失败: {type(e).__name__}: {_redact(str(e), token)[:120]}")
            return 2
        if failover_state()["active"]:
            out(f"判不了 (rc 2): 验收期间主站就切到了备用 ({failover_state()['reason']})")
            return 2
        n = len(df)
        ok_date, wrong = _daily_dates_match(df, d)
        out(f"主站 daily {d}: {n} 行, {len(_codes(df))} 个代码 {time.time() - t0:.1f}s" + _pages_note(df)
            + ("" if ok_date else f" —— 回包日期 {wrong} 与所问不符"))
        if n == 0:
            out(f"判不了 (rc 2): 主站 daily {d} 0 行 (镜像当日未入库?), 换个时间再验")
            return 2
        if not ok_date:
            out(f"判不了 (rc 2): 主站 daily {d} 回包日期与所问不符 —— 主站自己无视 trade_date? 先用 tools/tushare_probe.py compat 查主站")
            return 2
        prim[d] = df
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
    nb = {}
    for d in (day, day2):
        t0 = time.time()
        try:
            df_b = _direct_query(backup, token, "daily", PROBE_DAILY_FIELDS, {"trade_date": _ymd(d)})
        except Exception as e:          # noqa: BLE001
            out(f"备用 daily {d} 失败: {type(e).__name__}: {_redact(str(e), token)[:120]}")
            bad.append(f"daily {d} 打不通或不认这把 key")
            continue
        df_p = prim[d]
        n_p, n_b = len(df_p), len(df_b)
        nb[d] = n_b
        codes_p = _codes(df_p)
        common = len(_codes(df_b) & codes_p)
        missing = sorted(set(PROBE_DAILY_FIELDS.split(",")) - set(map(str, df_b.columns)))
        ok_date, wrong = _daily_dates_match(df_b, d)
        notes = ([f"缺列 {missing}"] if missing else []) + ([] if ok_date else [f"回包日期 {wrong} 与所问 {_ymd(d)} 不符"])
        out(f"备用 daily {d}: {n_b} 行 ({n_b / n_p:.0%} of 主站), 与主站重合代码 {common}/{len(codes_p)} {time.time() - t0:.1f}s" + _pages_note(df_b)
            + (" —— " + "; ".join(notes) if notes else ""))
        if n_b < PROBE_DAILY_MIN_RATIO * n_p:
            bad.append(f"daily {d} 行数 {n_b} < 主站 {n_p} × {PROBE_DAILY_MIN_RATIO:g}")
        if common < PROBE_DAILY_MIN_RATIO * len(codes_p):
            bad.append(f"daily {d} 与主站重合代码 {common} < {len(codes_p)} × {PROBE_DAILY_MIN_RATIO:g}")
        if missing:
            bad.append(f"daily {d} 缺列 {missing}")
        if not ok_date:
            bad.append(f"daily {d} 回包日期与所问不符 (备用无视或错配 trade_date, 生产会按所问日期写错价格库)")
    if bad:
        out(f"结论 (rc 1): 备用 {_host(backup)} 不可用 —— " + "; ".join(bad) + "; 不要加键 / 加了就去掉")
        return 1
    out(f"结论 (rc 0): 备用 {_host(backup)} 可用 —— 日历一致 {len(days_p)} 天, daily {day} {nb[day]}/{len(prim[day])} 行, "
        f"daily {day2} {nb[day2]}/{len(prim[day2])} 行 (两天回包日期都对, 代码重合 ≥ {PROBE_DAILY_MIN_RATIO:.0%}); "
        f"主站 {_host(base)} 正常, 切换状态 active=False")
    return 0


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -X utf8 -m ashare.tushare_client",
                                 description="Tushare 客户端自检 (只打 host / 行数 / 耗时; token 永不打印)")
    ap.add_argument("--probe-backup", action="store_true",
                    help="备用基址真验收: 6 次调用 (主站 trade_cal + 两个开市日的 daily 走生产路径, 备用同三项直调; 核日历 / 行数 / "
                         "代码重合 / 回包日期); 退出码 0 可用 / 1 不可用 / 2 判不了")
    ap.add_argument("--state", action="store_true", help="打印本进程的切换状态与进程池回退状态 (不联网)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if a.probe_backup:
        return probe_backup()
    if a.state:
        print(failover_state())
        print(pool_state())
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
