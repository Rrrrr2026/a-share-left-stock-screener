#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据源访问层 (Data access layer)
================================
封装 akshare 接口, 统一做:
  * 字段映射 (mapping layer): 接口列名是中文且偶有改名, 这里用"候选名匹配"归一为
    稳定的英文列名, 单个字段改名不会让流水线崩溃 (PRD §2 要求)。
  * 限频 + 重试 + 超时: 每次调用之间 sleep, 失败指数退避重试。
  * 本地缓存: 同一交易日内重复运行直接读缓存, 避免重复打接口。
  * 失败只跳过并记录, 绝不打断整轮 (PRD §1)。

每个 fetch_* 函数返回"规范化"后的 DataFrame (英文列名) 或 None。
"""

from __future__ import annotations
import os
import time
import pickle
import hashlib
import sqlite3
import threading
import datetime as dt
import logging

import numpy as np
import pandas as pd

from .config import CONFIG, DATA_DIR

log = logging.getLogger("ashare.datasource")

_CACHE_DIR = CONFIG["source"]["cache_dir"]
os.makedirs(_CACHE_DIR, exist_ok=True)


# ===========================================================================
#  给所有 requests.Session 注入浏览器 UA
#  (akshare 默认不带 UA, 部分东财端点会因此重置连接 RemoteDisconnected)
# ===========================================================================
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_TLS_HTTP = threading.local()


def _http():
    """线程本地长连接会话 (keep-alive)。
    2026-08-14 事故: 全市场扫描每次请求都新开socket, 关闭后在 TIME_WAIT 里躺
    ~2分钟, 连跑几轮把 Windows 临时端口耗尽 (WSAENOBUFS 10055), 浏览器等其它
    程序都连不上网。复用连接后 socket 用量从"每请求1个"降到"每线程几个"。"""
    import requests
    s = getattr(_TLS_HTTP, "sess", None)
    if s is None:
        s = requests.Session()
        try:
            from requests.adapters import HTTPAdapter
            ad = HTTPAdapter(pool_connections=8, pool_maxsize=16)
            s.mount("https://", ad)
            s.mount("http://", ad)
        except Exception:
            pass
        _TLS_HTTP.sess = s
    return s


def _install_ua_patch():
    try:
        import requests
        orig = requests.sessions.Session.__init__

        if getattr(requests.sessions.Session, "_ashare_ua_patched", False):
            return

        def patched(self, *a, **k):
            orig(self, *a, **k)
            try:
                self.headers.update({"User-Agent": _BROWSER_UA})
            except Exception:
                pass

        requests.sessions.Session.__init__ = patched
        requests.sessions.Session._ashare_ua_patched = True
    except Exception as e:  # 没装 requests 也不影响离线测试
        log.debug("UA patch skipped: %s", e)


_install_ua_patch()


# 东财 push2 实时端点一旦被重置, 置位此标志, 后续实时类请求直接走备用源(新浪/同花顺),
# 避免对每个行业都重试东财而拖慢整轮。多线程下用锁保证只翻转一次、只告警一次。
_em_realtime_down = False
_em_hist_down = False
_flag_lock = threading.Lock()

# ⚠ 同花顺(THS)专用锁。akshare 的 stock_board_*_ths 系列用 py_mini_racer(V8) 解密
# 生成 cookie; V8 **不是线程安全的**, 多线程同时初始化会让整个进程硬崩溃
# (2026-07-29 实测: Check failed: !pool->IsInitialized() -> Trace/BPT trap 5, 退出133,
#  整轮数据全丢)。模块1 用 16 线程并发跑 90 个行业, 必然踩中 -> 所有 THS 调用串行化。
# 代价: THS 那部分变串行(每行业约3s), 但换来的是不会整轮崩掉。
_ths_lock = threading.RLock()


def _mark_em_down(reason: str = ""):
    global _em_realtime_down
    with _flag_lock:
        if _em_realtime_down:
            return
        _em_realtime_down = True
    log.warning("东财实时端点疑似不可用, 后续改用备用源(新浪/同花顺)。原因: %s", str(reason)[:80])


def _mark_em_hist_down(reason: str = ""):
    global _em_hist_down
    with _flag_lock:
        if _em_hist_down:
            return
        _em_hist_down = True
    log.warning("东财日线端点疑似被限频, 后续个股日线改用新浪。原因: %s", str(reason)[:80])


def _is_conn_error(e) -> bool:
    s = type(e).__name__ + " " + str(e)
    return any(k in s for k in ("ConnectionError", "RemoteDisconnected",
                                "ConnectionReset", "ConnectTimeout", "ReadTimeout"))


# ===========================================================================
#  akshare 延迟导入 (离线测试时不强依赖)
# ===========================================================================
def _ak():
    import akshare as ak
    return ak


# ===========================================================================
#  缓存
# ===========================================================================
def _cache_key(name: str, *args) -> str:
    raw = name + "|" + "|".join(str(a) for a in args)
    h = hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]
    return f"{name}_{h}"


def _cache_load(key: str):
    if not CONFIG["source"]["use_cache"]:
        return None
    path = os.path.join(_CACHE_DIR, key + ".pkl")
    if not os.path.exists(path):
        return None
    age_h = (time.time() - os.path.getmtime(path)) / 3600.0
    if age_h > CONFIG["source"]["cache_ttl_hours"]:
        return None
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def _cache_save(key: str, obj) -> None:
    if not CONFIG["source"]["use_cache"]:
        return
    path = os.path.join(_CACHE_DIR, key + ".pkl")
    # 先写临时文件再原子改名, 避免并发写同一键 / Ctrl-C 中断产生半截损坏的 .pkl
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "wb") as f:
            pickle.dump(obj, f)
        os.replace(tmp, path)
    except Exception as e:  # 缓存失败不影响主流程
        log.debug("cache save failed %s: %s", key, e)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


# ===========================================================================
#  通用: 带重试的调用 + 字段映射
# ===========================================================================
_HARD_DEADLINE_SEC = 150     # akshare 单次调用硬期限 (墙钟)
_POOL_PROBE_SEC = 90         # 新建进程池的就绪探针上限 (spawn 启动 + 导入主模块, 正常 3-10s)
_POOL_DOA_WAIT_SEC = 2.0     # 建池后先等这么久看首批工作进程是否"一出生就死"
_POOL = None
_POOL_LOCK = None
_POOL_DISABLED = False       # True = 本进程余下调用全部改走线程版 (进程池基础设施不可用, 只告警一次)
# 父进程侧可能抛出的"基础设施"错误 (与 fn 本身的业务异常无关 —— 业务异常只会从 res.get 抛出):
# Windows 计划任务下 DuplicateHandle 拒绝访问 (PermissionError), 管道断裂 (BrokenPipeError /
# EOFError), multiprocessing 自身的 RuntimeError (spawn 未经 __main__ 守卫的 bootstrap 错误等)。
_POOL_INFRA_ERRORS = (OSError, EOFError, RuntimeError)


class _PoolUnavailable(RuntimeError):
    """进程池起不来 / 起来就死 (就绪探针失败) —— 由 _pool() 抛出, 调用方转线程版。"""


def _pool_lock():
    global _POOL_LOCK
    if _POOL_LOCK is None:
        _POOL_LOCK = threading.RLock()
    return _POOL_LOCK


def _make_pool(ctx):
    """真正建池的一行, 单独拆出便于测试注入 (如让工作进程一启动就死)。"""
    return ctx.Pool(processes=2)


def _disable_pool(where, exc):
    """进程池基础设施不可用 → 本进程余下全部走线程版硬期限; 只告警一次。"""
    global _POOL_DISABLED
    with _pool_lock():
        first = not _POOL_DISABLED
        _POOL_DISABLED = True
    if first:
        log.warning("进程池不可用 (%s: %s: %s) — 本进程余下 akshare 调用改走线程版硬期限 "
                    "(线程无法强杀, 失控分配类挂死将不再被隔离; 仅告警一次)",
                    where, type(exc).__name__, exc)
    _pool_reset()


def _pool():
    """惰性创建的独立工作进程池 (全平台统一 spawn, 与主进程多线程安全共存)。
    超时时整池 terminate 并置空, 下次调用重建。

    建池后必须做一次就绪探针 (2026-09-03..05 PC 计划任务事故): Windows 计划任务下 spawn
    出来的子进程在 bootstrap 阶段就死于 `reduction.duplicate → DuplicateHandle` 的
    PermissionError [WinError 5] —— 异常发生在**子进程**, 父进程的 Pool()/apply_async 都不
    报错, 池的 worker-handler 线程只会不断补活→再死, 任务永远不返回, 父进程要等满 150s
    硬期限才把它当"源站挂死"处理 (还会误标东财不可用)。先看首批工作进程是否一出生就死,
    再探一次 os.getpid; 拿不到结果即判定池不可用: terminate、置 _POOL_DISABLED, 抛
    _PoolUnavailable 让调用方改走线程版。"""
    global _POOL
    import multiprocessing as _mp
    with _pool_lock():
        if _POOL_DISABLED:
            raise _PoolUnavailable("进程池已判定不可用 (本进程余下走线程版)")
        if _POOL is None:
            # 统一 spawn: forkserver 在服务器 (systemd/journald 管道环境) 崩于
            # BlockingIOError EAGAIN (2026-09-03 实测, 池反复重建反复崩); spawn 起全新
            # 解释器, 与主进程多线程安全共存, 代价是每个工作进程启动多 ~3s (池常驻, 仅首次)
            ctx = _mp.get_context("spawn")
            p = _make_pool(ctx)
            try:
                first_gen = list(getattr(p, "_pool", None) or [])
                time.sleep(_POOL_DOA_WAIT_SEC)
                if first_gen and all(w.exitcode is not None for w in first_gen):
                    raise _PoolUnavailable(
                        f"首批工作进程全部立即退出 (exitcode={[w.exitcode for w in first_gen]})")
                p.apply_async(os.getpid).get(timeout=_POOL_PROBE_SEC)
            except Exception as e:          # noqa: BLE001  (含 mp.TimeoutError = 子进程起不来)
                try:
                    p.terminate()
                    p.join()
                except Exception:           # noqa: BLE001
                    pass
                _disable_pool("就绪探针", e)
                raise _PoolUnavailable(f"进程池就绪探针失败: {type(e).__name__}: {e}") from e
            _POOL = p
        return _POOL


def _pool_reset():
    global _POOL
    p = _POOL
    _POOL = None
    if p is not None:
        try:
            p.terminate()
            p.join()
        except Exception:       # noqa: BLE001
            pass


# 退出时主动收池: 不这样做, spawn 池会在解释器拆掉 io 模块之后才被 Pool.__del__ 清理,
# 每次收尾都在日志里留一条 "AttributeError: 'NoneType' object has no attribute 'BytesIO'"
# 的假 traceback (2026-09-04/07 两次被当真错误排查), 顺带解决 "leaked semaphore" 告警。
import atexit as _atexit
_atexit.register(_pool_reset)


def _call_in_thread(fn, args, kwargs, deadline_sec):
    """线程版兜底 (不可序列化的调用, 或进程池已判定不可用): 超时的线程无法强杀, 弃之为守护线程。"""
    import queue as _q
    import threading as _th
    box = _q.Queue(maxsize=1)

    def _run():
        try:
            box.put((True, fn(*args, **kwargs)))
        except Exception as e:      # noqa: BLE001
            box.put((False, e))
    _th.Thread(target=_run, daemon=True,
               name=f"ak-{getattr(fn, '__name__', 'call')}").start()
    try:
        ok, val = box.get(timeout=deadline_sec)
    except _q.Empty:
        raise TimeoutError(f"{getattr(fn, '__name__', fn)} 超过 {deadline_sec}s 硬期限 (线程版, 源站滴流/黑洞)")
    if ok:
        return val
    raise val


def _call_with_deadline(fn, args, kwargs, deadline_sec):
    """在独立工作进程里跑 fn, 主进程最多等 deadline_sec 秒, 超时 terminate 整池。

    为什么必须是进程而不是线程 (2026-09-03 两次事故):
    ① socket 超时只管"单次读多久没数据", 对端滴流钓连接时永不触发 — 一周内阶段A/阶段C/
       导出后段三次挂死 25min+ 被看门狗杀;
    ② 线程版硬期限 (同日下午) 更糟: 被弃的 stock_margin_detail_sse 线程不是在等网络,
       是在死循环分配内存, 主线程继续跑心跳正常, 它在后台把进程吃到 3.5GB 直到 OOM。
    Python 杀不死线程, 只有进程能被真正终止。不可序列化的 fn/参数退回线程版。

    进程池基础设施本身坏掉时 (2026-09-03..05 Windows 计划任务 WinError 5): 建池/提交阶段的
    父进程侧错误, 或 _pool() 的就绪探针失败, 都判定为"本进程余下不再用池" —— 告警一次、
    置 _POOL_DISABLED、退回线程版; 绝不因此把源站误标为挂死。"""
    if _POOL_DISABLED:
        return _call_in_thread(fn, args, kwargs, deadline_sec)
    import pickle
    try:
        pickle.dumps((fn, args, kwargs))
    except Exception:           # noqa: BLE001
        return _call_in_thread(fn, args, kwargs, deadline_sec)
    import multiprocessing as _mp
    try:
        res = _pool().apply_async(fn, args, kwargs)
    except _POOL_INFRA_ERRORS as e:
        # 建池/提交阶段的父进程侧基础设施错误 (fn 的业务异常不会在这里出现, 只会从 res.get 抛出)
        _disable_pool("建池/提交", e)
        return _call_in_thread(fn, args, kwargs, deadline_sec)
    try:
        return res.get(timeout=deadline_sec)
    except _mp.TimeoutError:
        _pool_reset()
        raise TimeoutError(f"{getattr(fn, '__name__', fn)} 超过 {deadline_sec}s 硬期限 (工作进程已终止)")

def call_with_retry(fn, *args, **kwargs):
    """对一个 akshare 调用做 限频sleep + 重试 + 超时容错。失败抛出最后一次异常。
    硬期限超时不重试 (源站已进入挂死态, 重试只会再白等) — 直接标记东财不可用并抛出,
    调用方走既有回退 (腾讯/新浪/缓存/回落)。"""
    f = CONFIG["fetch"]
    last_exc = None
    deadline = f.get("hard_deadline_sec", _HARD_DEADLINE_SEC)
    for attempt in range(f["max_retries"]):
        try:
            time.sleep(f["sleep_sec"])
            return _call_with_deadline(fn, args, kwargs, deadline)
        except TimeoutError as e:
            log.warning("akshare 调用挂死: %s — 标记东财不可用, 不重试", e)
            try:
                _mark_em_down(str(e))
                _mark_em_hist_down(str(e))
            except Exception:       # noqa: BLE001
                pass
            raise
        except Exception as e:  # noqa
            last_exc = e
            wait = f["retry_backoff_sec"] * (2 ** attempt)
            log.debug("retry %d/%d after error: %s (sleep %.1fs)",
                      attempt + 1, f["max_retries"], e, wait)
            time.sleep(wait)
    raise last_exc


def pick_col(df: pd.DataFrame, candidates, contains: bool = False):
    """在 df 中找到第一个匹配的列名。candidates 为候选中文/英文名列表。
    contains=True 时做子串匹配。找不到返回 None。"""
    cols = list(df.columns)
    # 1) 精确匹配
    for cand in candidates:
        if cand in cols:
            return cand
    # 2) 子串匹配
    if contains:
        for cand in candidates:
            for col in cols:
                if cand in str(col):
                    return col
    return None


def rename_normalize(df: pd.DataFrame, mapping: dict) -> pd.DataFrame:
    """mapping: {规范英文名: [候选原列名, ...]} -> 返回只含命中列、且已改名的副本。
    缺失字段不报错 (后续以 NaN/— 降级)。"""
    out = {}
    for std_name, cands in mapping.items():
        col = pick_col(df, cands, contains=True)
        if col is not None:
            out[std_name] = df[col]
    res = pd.DataFrame(out)
    return res


def _to_num(s):
    return pd.to_numeric(s, errors="coerce")


# ===========================================================================
#  1) 全A实时快照 (universe + 估值/换手/量比)  ——  stock_zh_a_spot_em
# ===========================================================================
def fetch_spot_snapshot(force: bool = False) -> pd.DataFrame | None:
    """全A快照 (股票池 + 估值/换手/量比)。优先东财, 失败退回新浪
    (东财 push2 实时端点在部分网络会被重置)。"""
    key = _cache_key("spot", dt.date.today().isoformat())
    if not force:
        c = _cache_load(key)
        if c is not None:
            return c
    # 顺序: 东财直连(多主机, 最稳) -> akshare东财 -> 新浪。
    # 直连放第一位是因为 akshare 写死的主机在本机被挡, 而其余东财主机可达。
    # 2026-08-14 事故: 限频把分页截断在 1086 行, 部分列表被当全量缓存+扫描 ->
    # 当日榜单只覆盖 1/5 市场。全A应有 5000+ 行, 低于下限视为截断源, 继续换源。
    MIN_ROWS = 4000
    df = _spot_from_em_direct()
    if df is None or len(df) < MIN_ROWS:
        log.info("东财直连快照不可用/截断(%s行), 尝试 akshare 东财 ...",
                 "0" if df is None else len(df))
        df = _spot_from_em()
    if df is None or len(df) < MIN_ROWS:
        log.info("东财快照不可用/截断, 尝试新浪快照 ...")
        df = _spot_from_sina()
    if df is None or df.empty:
        log.error("全部快照源均失败 -> 股票池为空, 本轮无法扫描。"
                  "请检查网络/代理是否放行 push2.eastmoney.com")
        return None
    df["code"] = df["code"].astype(str).str.zfill(6)
    if len(df) < MIN_ROWS:
        log.warning("所有源都只拿到截断快照(%d行): 本轮硬着头皮用, 但不缓存, 下次重取",
                    len(df))
        return df
    _cache_save(key, df)
    return df


# 东财行情主机池。akshare 写死用 17.push2, 而本机代理恰好挡住这一台 ——
# 其余主机(push2 / 1.push2 / push2delay / 82.push2)实测均可达。
# 2026-07-27 事故: akshare 东财快照失败 + 新浪快照 akshare 解析报错(list index out of
# range) -> 股票池为空 -> 扫描数 0。故自己直连 + 多主机故障转移, 不再受制于 akshare。
_EM_HOSTS = ("push2.eastmoney.com", "1.push2.eastmoney.com",
             "push2delay.eastmoney.com", "82.push2.eastmoney.com",
             "2.push2.eastmoney.com")
_em_host_ok = None          # 记住第一个成功的主机, 后续优先用它
_em_host_fails = {}         # 主机 -> 连续失败次数; 连挂多次就本轮拉黑, 不再浪费超时


def _em_get(path: str, params: dict, timeout: int = 8):
    """对东财接口做多主机轮询 + JSON 容错。全部失败返回 None。

    分页要打几十次, 若每次都从头撞坏主机, 光超时就拖垮整轮(实测 298s):
    故 ① 记住上次成功的主机并排最前; ② 连续失败 3 次的主机本轮拉黑跳过。
    """
    import requests
    global _em_host_ok
    live = [h for h in _EM_HOSTS if _em_host_fails.get(h, 0) < 3]
    if not live:                                   # 全被拉黑 -> 清零重来(网络可能恢复了)
        _em_host_fails.clear()
        live = list(_EM_HOSTS)
    if _em_host_ok in live:                        # 上次成功的主机排最前
        live.remove(_em_host_ok); live.insert(0, _em_host_ok)
    headers = {"User-Agent": _BROWSER_UA, "Referer": "https://quote.eastmoney.com/"}
    for h in live:
        try:
            r = _http().get(f"https://{h}{path}", params=params,
                            headers=headers, timeout=timeout)
            if r.status_code != 200 or not r.text.strip().startswith("{"):
                raise ValueError(f"bad body {r.status_code}")
            j = r.json()
            if not isinstance(j, dict) or j.get("data") is None:
                raise ValueError("no data")
            _em_host_ok = h
            _em_host_fails[h] = 0
            return j["data"]
        except Exception as e:
            _em_host_fails[h] = _em_host_fails.get(h, 0) + 1
            if _em_host_fails[h] == 3:
                log.info("东财主机 %s 连续失败3次, 本轮跳过", h)
            log.debug("东财主机 %s 失败: %s", h, str(e)[:60])
            continue
    return None


def _spot_from_em_direct() -> pd.DataFrame | None:
    """全A快照: 直连东财 clist 分页拉取(不经 akshare), 带主机故障转移。
    一次拿到 代码/名称/价格/量额/换手/量比/PE/PB/市值, 即股票池 + 估值字段。"""
    fields = "f12,f14,f2,f3,f5,f6,f8,f9,f10,f15,f16,f20,f21,f23,f100"  # f100=所属行业
    fs = "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048"   # 沪深主板/创业/科创/北交
    rows, pn, pz = [], 1, 200
    total = None
    while True:
        # 单页重试: 中途一次抖动就 break 会**静默截断股票池**(漏掉的股票整轮扫不到),
        # 这比慢几秒严重得多 -> 每页最多重试3次(轮换主机), 仍失败才放弃并明确告警。
        d = None
        for _try in range(3):
            d = _em_get("/api/qt/clist/get",
                        {"pn": pn, "pz": pz, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                         "fid": "f3", "fs": fs, "fields": fields})
            if d:
                break
            time.sleep(0.6 * (_try + 1))
        if not d:
            log.warning("东财快照第 %d 页三次重试仍失败, 股票池可能不完整", pn)
            break
        if total is None:
            total = d.get("total") or 0
        diff = d.get("diff") or []
        if isinstance(diff, dict):          # 个别返回是 {"0":{...}} 形式
            diff = list(diff.values())
        if not diff:
            break
        rows.extend(diff)
        if total and len(rows) >= total:
            break
        pn += 1
        if pn > 60:                          # 安全上限 (60*200=12000 只)
            break
    if not rows:
        return None
    if total and len(rows) < total * 0.95:
        log.warning("东财快照只取到 %d/%d 只(缺 %.0f%%), 本轮扫描范围偏小",
                    len(rows), total, (1 - len(rows) / total) * 100)
    df = pd.DataFrame(rows).rename(columns={
        "f12": "code", "f14": "name", "f2": "price", "f3": "pct_chg",
        "f5": "volume", "f6": "amount", "f8": "turnover", "f9": "pe_ttm",
        "f10": "volume_ratio", "f15": "high", "f16": "low",
        "f20": "total_mv", "f21": "float_mv", "f23": "pb", "f100": "industry"})
    keep = [c for c in ("code", "name", "price", "pct_chg", "volume", "amount",
                        "turnover", "pe_ttm", "volume_ratio", "high", "low",
                        "total_mv", "float_mv", "pb", "industry") if c in df.columns]
    df = df[keep].copy()
    for col in df.columns:
        if col not in ("code", "name", "industry"):
            df[col] = _to_num(df[col])       # 东财用 "-" 表示缺失 -> NaN
    df["code"] = df["code"].astype(str).str.zfill(6)
    if "industry" in df.columns:              # "-" / "" 视为无行业
        df["industry"] = df["industry"].astype(str).str.strip()
        df.loc[df["industry"].isin(["-", "", "nan", "None"]), "industry"] = None
    log.info("东财快照(直连 %s): %d 只", _em_host_ok, len(df))
    return df


def _spot_from_em() -> pd.DataFrame | None:
    if _em_realtime_down:
        return None
    try:
        raw = call_with_retry(_ak().stock_zh_a_spot_em)
    except Exception as e:
        log.warning("东财快照 stock_zh_a_spot_em 失败: %s", e)
        if _is_conn_error(e):
            _mark_em_down(e)
        return None
    df = rename_normalize(raw, {
        "code":         ["代码"],
        "name":         ["名称"],
        "price":        ["最新价"],
        "pct_chg":      ["涨跌幅"],
        "volume":       ["成交量"],
        "amount":       ["成交额"],
        "high":         ["最高"],
        "low":          ["最低"],
        "volume_ratio": ["量比"],
        "turnover":     ["换手率"],
        "pe_ttm":       ["市盈率-动态", "市盈率"],
        "pb":           ["市净率"],
        "total_mv":     ["总市值"],
        "float_mv":     ["流通市值"],
    })
    if "code" not in df.columns:
        return None
    for col in df.columns:
        if col not in ("code", "name"):
            df[col] = _to_num(df[col])
    return df


def _spot_from_sina() -> pd.DataFrame | None:
    """新浪全A快照 (较慢但端点稳定)。无量比/PE等字段时优雅缺省。"""
    try:
        raw = call_with_retry(_ak().stock_zh_a_spot)
    except Exception as e:
        log.warning("新浪快照 stock_zh_a_spot 失败: %s", e)
        return None
    df = rename_normalize(raw, {
        "code":     ["代码"],
        "name":     ["名称"],
        "price":    ["最新价"],
        "pct_chg":  ["涨跌幅"],
        "volume":   ["成交量"],
        "amount":   ["成交额"],
        "high":     ["最高"],
        "low":      ["最低"],
        "turnover": ["换手率"],
        "pe_ttm":   ["市盈率"],
        "pb":       ["市净率"],
    })
    if "code" not in df.columns:
        return None
    # 新浪代码常带 sh/sz/bj 前缀
    df["code"] = df["code"].astype(str).str.replace(r"^(sh|sz|bj)", "", regex=True)
    for col in df.columns:
        if col not in ("code", "name"):
            df[col] = _to_num(df[col])
    return df


def build_universe(spot: pd.DataFrame | None = None) -> pd.DataFrame | None:
    """从快照构造股票池 (剔除ST / 北交所), 返回 code,name。"""
    if spot is None:
        spot = fetch_spot_snapshot()
    if spot is None or spot.empty:
        return None
    df = spot[["code", "name"]].copy()
    t = CONFIG["tech"]
    if t["exclude_st"]:
        df = df[~df["name"].astype(str).str.contains("ST", case=False, na=False)]
    if t["exclude_bj"]:
        # 北交所: 8xx / 4xx, 以及 2024 年新增的 920xxx 段
        df = df[~df["code"].str.startswith(("8", "4", "920"))]
    df = df.drop_duplicates(subset=["code"])   # 去重, 避免重复代码导致命中数虚高
    return df.reset_index(drop=True)


# ===========================================================================
#  2) 个股日线 (前复权)  ——  stock_zh_a_hist
# ===========================================================================
def _sina_symbol(code: str) -> str:
    code = str(code).zfill(6)
    # 北交所(920/8/4) 必须放在 6/9 之前判断: 920 以 '9' 开头, 否则会被误判成沪市
    if code.startswith("920") or code.startswith(("8", "4")):
        return "bj" + code
    if code.startswith(("6", "9")):           # 沪市 60/68, 沪B 900
        return "sh" + code
    if code.startswith(("0", "3", "2")):      # 深市 00/30, 深B 200
        return "sz" + code
    return "sh" + code


def _finalize_hist(df: pd.DataFrame) -> pd.DataFrame | None:
    need = {"date", "open", "high", "low", "close"}
    if df is None or not need.issubset(df.columns):
        return None
    for col in ("open", "high", "low", "close", "volume", "amount", "pct_chg"):
        if col in df.columns:
            df[col] = _to_num(df[col])
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    return df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)


# ===========================================================================
#  本地价格库直读 (Tushare 适配 P1) —— 阶段A 不再逐股联网
# ===========================================================================
#  CONFIG.source.bars == "tushare" 时, 个股日线一律读 data/pricestore.db 的物化前复权表。
#  这一步才是"买 Tushare"的真正收益: 每日 5,200 次 fuyao/东财逐股请求 -> 0 次
#  (库由 run_a.sh 开头的 pricestore update 按 trade_date 拉全市场, 约 10 次调用)。
#  库里拿不到的个别代码 (次新股不足 60 根、库缺该码) 仍回落到原来的联网链, 只影响个位数只票。
_STORE_TLS = threading.local()
_store_stat = {"hit": 0, "fallback": 0, "skipped": 0}
_store_stat_lock = threading.Lock()
_store_status: dict | None = None
_store_status_lock = threading.Lock()
_store_pit: tuple | None = None          # (点时股票池 set|None, 新鲜度截止日 str|None)
_store_pit_lock = threading.Lock()

#: 阶段A 从库里取多长的历史。**刻意不是 CONFIG.fetch.lookback_days(500)**: 换库前阶段A 的主源是
#: `fuyao.hist(code, years=2.5)` —— 它无视 lookback_days, 一律给 2.5 年 (≈607 根)。module2 的
#: "前期重要低点" 走 `ind.find_pivot_lows(low, ...)` 扫**整条**序列 (只有它不是 .tail(N)),
#: 所以窗口一短枢轴集就变。09-07 换库时实测 400 只样本: 500 天窗口 vs 913 天窗口有 42/305 只
#: (13.8%) 的 tech_score 不同 (中位差 0.95 分, dip/coil 桶不受影响) —— 那是**窗口**造成的差异,
#: 不是换源造成的。换库这一步只该换数据来源, 不该顺手改扫描窗口, 所以这里对齐 fuyao 的 2.5 年。
STORE_HIST_DAYS = 913               # ≈ fuyao years=2.5 的等效自然日数

#: 库判"这只票能不能扫"的两个阈值 —— 候选池裁剪与 fetch_hist 判无数据**共用**这一份口径。
#: · MIN_BARS: module2 起步就要 60 根 (再往下 MA60/通道拟合都算不出来), 库里不足 60 根的票
#:   联网也凑不出来 (它的历史本来就这么短), 扫它只是白占分母;
#: · FRESH_TRADE_DAYS: 最后一根 bar 允许落后几个**交易日**。用于"库里有 K 线但 universe 表
#:   没有这一行"的票 (09-07 实测 3 只改过代码的: 000022/000043/300114) —— 在市证据以 K 线为准,
#:   但必须是**新鲜的** K 线, 否则刚退市的票会靠一段陈年 K 线赖在池里。
STORE_MIN_BARS = 60
STORE_FRESH_TRADE_DAYS = 10

#: 裁剪原因 -> 中文标签 (日志与留痕 json 共用)
STORE_DROP_REASON_CN = {
    "absent": "退市/不在池(库内无此码)",
    "stale": "退市/长停(有K线但已过期)",
    "too_new": f"次新<{STORE_MIN_BARS}根",
    "gap": "在池但库内无K线",
}

#: 哪些裁决**留在候选池里**。卡的规则第一句是"保留 pricestore.universe_at(今日) 内的代码",
#: 而 gap = 在池(库自己说它今天在市)但窗口内一根 K 线都没有 —— 按规则必须留下。
#: 09-08 首版把 gap 也裁了, 有两个后果: ① 与规则相反, 库说在市的票被静默移出候选池;
#: ② 阶段A 根本不会为它调 fetch_hist, 于是 fetch_hist 里那条"只有 gap 才值得回落联网"的
#: 分支对阶段A 变成死代码 —— 恰恰是"某天某批码没入库"时最需要的兜底 (安全阀只在裁后<3000
#: 才响, 少掉几十上百只不会触发)。09-07 真实候选池里 gap=0 只, 所以这一条不改变当日任何
#: 数字, 是给库出岔子那天留的。次新 (too_new) 即使在池也照裁 —— 卡的原文明确要求。
STORE_KEEP_VERDICTS = ("keep", "gap")


def _store_path() -> str:
    return os.path.join(DATA_DIR, "pricestore.db")


def bars_from_store_on() -> bool:
    """阶段A 是否直读价格库。开关与 market._bars_source 同一个 (CONFIG.source.bars)。"""
    if str(CONFIG["source"].get("bars", "fuyao") or "fuyao").lower() != "tushare":
        return False
    return os.path.exists(_store_path())


def _store_conn() -> sqlite3.Connection:
    """线程私有的**只读**连接。

    刻意不用 leftside_core.pricestore._conn(): 那是读写连接, 每次还要跑一遍建表 DDL —— 阶段A
    16 线程 × 5,200 次会平白去抢 WAL 写锁, 而别的代理这会儿正在读同一批 data/*.db。
    mode=ro + query_only 保证这里绝无可能写坏刚换上的库。
    """
    conn = getattr(_STORE_TLS, "conn", None)
    if conn is None:
        conn = sqlite3.connect(f"file:{_store_path()}?mode=ro", uri=True,
                               timeout=60, check_same_thread=False)
        conn.execute("PRAGMA query_only=1")
        _STORE_TLS.conn = conn
    return conn


def _hist_from_store(code: str, days: int, min_bars: int = 60) -> pd.DataFrame | None:
    """价格库 -> date/open/high/low/close/volume/amount (前复权, v=股, amt=元)。

    单位与 fuyao 一致 (成交量股、成交额元), 所以 module2 的 `amount/1e8 >= 0.5亿` 流动性门
    和量比计算都不用改。取不到/太短返回 None, 由调用方决定要不要回落联网。
    """
    start = (dt.date.today() - dt.timedelta(days=int(days))).isoformat()
    try:
        rows = _store_conn().execute(
            "SELECT d,o,h,l,c,v,amt FROM bars WHERE code=? AND d>=? ORDER BY d",
            (code, start)).fetchall()
    except Exception as e:                                     # noqa: BLE001
        log.debug("价格库读取 %s 失败: %s", code, str(e)[:120])
        _STORE_TLS.conn = None                                 # 连接坏了就丢掉, 下次重开
        return None
    if len(rows) < min_bars:
        return None
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close",
                                     "volume", "amount"])
    return _finalize_hist(df)


# (09-08: 原 _store_bars_count 已并入 _store_probe_one —— bar 数与最后一根日期一次查完,
#  判定统一走 store_verdict, 免得裁池与判无数据各写一份口径。)


def _store_status_map() -> dict:
    """{code: 'L'|'D'|'P'} —— 库内 universe 口径的在市状态 (Tushare stock_basic)。

    换库后**库才是股票池的权威**: 阶段A 的候选池来自东财快照, 里面混着 196 只早就退市、
    东财却还在列的老代码 (09-07 实测)。这些票库里当然没有近期 K 线, 放它们回落联网就是
    每天 200 次注定失败的逐股请求 —— 而且最后会掉到**串行加锁的新浪 V8 路径**上, 实测把
    阶段A 从 98 秒拖到 30 分钟以上, 直接威胁服务器 watchdog 的 25 分钟心跳。
    """
    global _store_status
    if _store_status is None:
        with _store_status_lock:
            if _store_status is None:
                try:
                    _store_status = {r[0]: (r[1] or "") for r in
                                     _store_conn().execute("SELECT code, status FROM universe")}
                except Exception as e:                         # noqa: BLE001
                    log.warning("价格库 universe 读取失败 (退市票将照旧回落联网): %s",
                                str(e)[:120])
                    _store_status = {}
    return _store_status


def _store_trade_day_back(n: int) -> str | None:
    """库自己的交易日历往回数 n 个交易日的日期 (含当日为第 1 个)。

    日历取 `idx_bars` (基准指数逐日收盘, d 是主键, 取末 n 行是毫秒级)。**刻意不用挂钟日期**:
    库若某天没更新 (Tushare 未就绪守卫会就地停下), 用挂钟算出来的"10 天窗口"会把整池的
    last_bar 都判成过期 —— 一次数据延迟就把候选池清空, 那是最坏的失败方式。
    """
    try:
        rows = [r[0] for r in _store_conn().execute(
            "SELECT d FROM idx_bars ORDER BY d DESC LIMIT ?", (int(n),))]
    except Exception:                                          # noqa: BLE001
        rows = []
    if len(rows) >= int(n):
        return rows[-1]
    try:                                    # idx_bars 空 (老库/没灌指数): 退回个股末日 - 宽限
        last = _store_conn().execute("SELECT MAX(d) FROM bars").fetchone()[0]
    except Exception:                                          # noqa: BLE001
        return None
    if not last:
        return None
    return (dt.date.fromisoformat(str(last)[:10]) - dt.timedelta(days=int(n) * 2)).isoformat()


def _store_pit_ctx() -> tuple:
    """(今日点时股票池 set | None, 新鲜度截止日 str | None) —— 进程内算一次。

    股票池直接用 `leftside_core.pricestore.universe_at` (未退市 + 已上市 + 首根 bar 已到),
    **不在这里重写一遍判据**: 九年重放、验收门 8 和这里必须是同一个函数, 否则口径又要分叉。
    传自己的只读连接进去, 避免 pricestore._conn() 的读写连接 + 建表 DDL 抢 WAL 锁。
    读不出来 (universe 表空 / 老库) 返回 (None, ...) = **不判**, 调用方一律按"在市"处理。
    """
    global _store_pit
    if _store_pit is None:
        with _store_pit_lock:
            if _store_pit is None:
                uni = None
                try:
                    from leftside_core import pricestore as _ps
                    codes = _ps.universe_at(dt.date.today().isoformat(), conn=_store_conn())
                    uni = set(codes) if codes else None
                except Exception as e:                         # noqa: BLE001
                    log.warning("价格库点时股票池读取失败 (不裁池, 一律按在市处理): %s",
                                str(e)[:160])
                _store_pit = (uni, _store_trade_day_back(STORE_FRESH_TRADE_DAYS))
    return _store_pit


def store_verdict(n_bars: int, last_bar: str | None, in_universe: bool,
                  fresh_after: str | None, min_bars: int = STORE_MIN_BARS) -> str:
    """库对一只票的裁决 —— **纯函数, 无 I/O, 可离线单测**; 候选池裁剪与 fetch_hist 共用。

    · keep     : 够 60 根, 且 (在点时股票池里 **或** 最后一根 bar 还新鲜) —— 可以扫;
    · stale    : 够 60 根, 但既不在池、最后一根也过期 —— 退市/长停, 别扫;
    · too_new  : 库内 1..59 根 —— 次新股, 联网也凑不出 module2 要的根数;
    · gap      : 一根都没有但在池里 —— 真缺口, **留在候选池里** (见 STORE_KEEP_VERDICTS),
                 由 fetch_hist 为它回落联网; 这是"库某天漏了一批码"时唯一的兜底;
    · absent   : 一根都没有也不在池 —— 东财快照的历史遗留退市码 (09-07 实测 196 只)。

    "在市证据以 K 线为准" 那一条 (in_universe=False 但 bar 新鲜也 keep) 是为库里有 K 线、
    universe 表却没有对应行的票留的口子 —— 09-07 实测 3 只改过代码的 (000022/000043/300114)。
    """
    if n_bars >= int(min_bars):
        if in_universe or (last_bar and fresh_after and str(last_bar) >= str(fresh_after)):
            return "keep"
        return "stale"
    if n_bars > 0:
        return "too_new"
    return "gap" if in_universe else "absent"


def _store_probe_one(code: str, days: int) -> tuple:
    """单只票: (窗口内 bar 数, 最后一根 bar 日期)。fetch_hist 的漏网路径用 (只查一只)。"""
    start = (dt.date.today() - dt.timedelta(days=int(days))).isoformat()
    try:
        r = _store_conn().execute(
            "SELECT COUNT(*), MAX(d) FROM bars WHERE code=? AND d>=?", (code, start)).fetchone()
        return (int(r[0] or 0), r[1])
    except Exception:                                          # noqa: BLE001
        return (0, None)


def _store_verdict_one(code: str, days: int) -> str:
    uni, fresh_after = _store_pit_ctx()
    n, last = _store_probe_one(code, days)
    return store_verdict(n, last, uni is None or code in uni, fresh_after)


def store_pool_meta() -> dict:
    """裁池口径的留痕 (写进 data/pool_cut/*.json, 供事后核对是按什么尺子裁的)。"""
    uni, fresh_after = _store_pit_ctx()
    return {"n_universe": (len(uni) if uni is not None else None),
            "fresh_after": fresh_after,
            "min_bars": STORE_MIN_BARS,
            "fresh_trade_days": STORE_FRESH_TRADE_DAYS}


def store_universe_filter(codes, days: int | None = None) -> tuple:
    """候选池 -> (库内在市的代码 list, {原因: [{code,n_bars,last_bar}, ...]}, 降级原因 str|None)。

    东财快照给的候选池混着早已退市的老代码与次新股 (09-07 实测 5,180 只里 196 只退市);
    它们在取数层已经被判"无数据"跳过, 但仍然计进对外的 n_scanned —— 分母不诚实。这里在
    开扫之前就按库内点时股票池裁掉, 让"扫描数"回到"今天真的扫了这么多只"。

    判据与 fetch_hist 完全同源 (`store_verdict` + `STORE_KEEP_VERDICTS`)。

    **第三个返回值是"没能裁"与"没什么可裁"的分界**, 调用方必须据此定对外口径:
    None = 过滤器真的跑完了 (哪怕一只没裁, 那也是新口径);
    非空字符串 = 库/尺子不可用, 原样放行, 这一轮的分母还是**老口径**。
    09-08 首版没有这个返回值, 两种情形都是 `(codes, {})`, 于是"库读不出来"那天一只没裁、
    n_pool_raw == n_scanned == 5180, scan_basis 却自称 'store_universe' —— 这个字段存在的
    唯一理由就是让事后对账的人分清新老口径, 那样等于恰恰在它最该说真话的那天说了假话。
    """
    codes = [str(c) for c in (codes or [])]
    win = int(days or max(int(CONFIG["fetch"]["lookback_days"]), STORE_HIST_DAYS))
    uni, fresh_after = _store_pit_ctx()
    if uni is None:
        log.warning("价格库点时股票池不可用, 候选池不裁 (%d 只原样进阶段A, 对外口径仍是老口径)",
                    len(codes))
        return (codes, {}, "价格库点时股票池不可用(universe 表空/老库/读取失败)")
    start = (dt.date.today() - dt.timedelta(days=win)).isoformat()
    probe: dict = {}
    try:
        # 一次 GROUP BY 扫窗口 (实测 0.7s / 5,310 码), 比 5,180 次逐码 COUNT 便宜一个数量级
        probe = {r[0]: (int(r[1] or 0), r[2]) for r in _store_conn().execute(
            "SELECT code, COUNT(*), MAX(d) FROM bars WHERE d>=? GROUP BY code", (start,))}
    except Exception as e:                                     # noqa: BLE001
        log.warning("价格库 bar 统计失败, 候选池不裁 (对外口径仍是老口径): %s", str(e)[:160])
        return (codes, {}, f"价格库 bar 统计失败: {str(e)[:80]}")
    keep, dropped = [], {}
    for code in codes:
        n, last = probe.get(code, (0, None))
        v = store_verdict(n, last, code in uni, fresh_after)
        if v in STORE_KEEP_VERDICTS:
            keep.append(code)
        else:
            dropped.setdefault(v, []).append(
                {"code": code, "n_bars": n, "last_bar": last,
                 "status": _store_status_map().get(code) or None})
    return (keep, dropped, None)


def backtest_prices_from_store_on() -> bool:
    """回测/模拟盘/双周取价是否直读价格库 (P2)。

    两个开关都要为真: `source.bars == "tushare"` (库是 Tushare 单源 v2, 有 bars_raw 可锚)
    + `source.backtest_prices_from_store` (本条链的独立回滚开关)。
    """
    if not bars_from_store_on():
        return False
    return bool(CONFIG["source"].get("backtest_prices_from_store", True))


def _store_max_date() -> str | None:
    try:
        return _store_conn().execute("SELECT MAX(d) FROM bars").fetchone()[0]
    except Exception:                                          # noqa: BLE001
        return None


#: 库末日允许落后 `need_date` 多少个**自然日**。比的是 need_date (调用方要重放到的最后一天)
#: 而不是 today —— 长假里 today-MAX(d) 必然 >7 天, 拿 today 当尺子会在每个国庆/春节的第一天
#: 把 2,600 只票全推回腾讯, 那正是本卡要消灭的东西。
STORE_STALE_MAX_DAYS = 7

#: 库**明说**这只票不再交易的状态 (Tushare stock_basic: L 上市 / D 退市 / P 暂停上市)。
#: 只有落进这个集合才算"联网也白跑"。**"库内查无此码" 不在此列** —— 那是"库没听说过",
#: 不是"库说它死了": universe 来自 Tushare stock_basic, 实测它对北交所 (8xx/43x/92x) 与
#: B 股 (200x/900x) 各 0 行, 把"没听说过"当"已退市"会让这些板块在回测链上永久静默无数据
#: (历史快照里已有 4 只在市 B 股候选: 深粮B/一致Ｂ/宁通信B/安道麦B)。设计文档 §3 写的也是
#: "库内无此码 -> 仅该码回落网络"; 09-08 首版实现写反了, 这里改回与文档一致。
_STORE_DEAD_STATUS = ("D", "P")


def price_series_from_store(codes: list, start: str,
                            need_date: str | None = None) -> tuple[dict, list]:
    """价格库 -> ({code: {dates, ohlc(qfq), raw_close}}, 需要回落联网的代码)。

    一次 JOIN 同时取前复权 (bars) 与原始收盘 (bars_raw): **锚定要原始价** (快照价是当天成交价,
    与 raw 同口径; qfq 的基准是库内最新一天, 快照日之后的任何除权都会让那天的 qfq 价平移),
    **收益要前复权** (跨除权只有 qfq 的涨跌幅是真涨跌幅)。两条序列逐位同索引。

    窗口内 <5 根 bar 时按 universe 分三路 (日志也分三个计数, 别混成一桶):
      · `status in ("D","P")` -> 库明说退市/暂停, **不联网** (东财候选池里的历史遗留代码,
        09-07 实测 196 只, 联网只是每天几百次注定失败的请求);
      · `status == "L"`       -> 库说在市却没数据 = 真缺口, 回落联网;
      · 库内查无此码           -> 也回落联网 (见 `_STORE_DEAD_STATUS` 注释)。
    """
    if need_date:
        mx = _store_max_date()
        if not mx:
            log.warning("回测取价: 价格库无 bars, 整批回落联网")
            return {}, list(codes)
        lag = (dt.date.fromisoformat(need_date) - dt.date.fromisoformat(mx)).days
        if lag > STORE_STALE_MAX_DAYS:
            log.warning("回测取价: 价格库末日 %s 落后目标日 %s %d 天 (>%d), 整批回落联网",
                        mx, need_date, lag, STORE_STALE_MAX_DAYS)
            return {}, list(codes)
    out, fallback, dead, unknown = {}, [], 0, 0
    status = _store_status_map()
    for code in codes:
        try:
            rows = _store_conn().execute(
                "SELECT b.d, b.o, b.h, b.l, b.c, r.c FROM bars b "
                "LEFT JOIN bars_raw r ON r.code = b.code AND r.d = b.d "
                "WHERE b.code=? AND b.d>=? ORDER BY b.d", (code, start)).fetchall()
        except Exception as e:                                 # noqa: BLE001
            log.debug("回测取价 读库 %s 失败: %s", code, str(e)[:120])
            _STORE_TLS.conn = None                             # 连接坏了就丢掉, 下次重开
            rows = []
        if len(rows) < 5:
            st = status.get(code) if status else None
            if st in _STORE_DEAD_STATUS:
                dead += 1                                      # 库明说退市/暂停: 不联网
            else:
                if st is None:
                    unknown += 1                               # 库内查无此码 (B股/北交所...)
                fallback.append(code)
            continue
        out[code] = {
            "dates": [r[0] for r in rows],
            "ohlc": np.array([r[1:5] for r in rows], dtype=float),
            "raw_close": np.array([np.nan if r[5] is None else r[5] for r in rows],
                                  dtype=float),
        }
    log.info("回测取价 读库: 命中 %d / 回落联网 %d (其中库内查无此码 %d) / 库判退市跳过 %d "
             "(共 %d)", len(out), len(fallback), unknown, dead, len(codes))
    return out, fallback


def _store_note(kind: str) -> None:
    with _store_stat_lock:
        _store_stat[kind] = _store_stat.get(kind, 0) + 1


def store_stats() -> dict:
    """阶段A 收尾时打印: 命中价格库 / 回落联网 / 按库判退市直接跳过 —— 换库当天的核验凭据。"""
    with _store_stat_lock:
        return dict(_store_stat)


def fetch_hist(code: str) -> pd.DataFrame | None:
    """个股日线(前复权)。

    CONFIG.source.bars == "tushare" -> **本地价格库直读** (零网络), 库里没有才回落下面这条链。
    回落链顺序: 同花顺(fuyao) -> 东财(akshare) -> 腾讯(直连) -> 新浪(加锁, 最后手段)。

    ⚠ 顺序不是随便排的: 阶段A 要用 16 线程扫 4000+ 只, 而 akshare 的新浪日线
    (stock_zh_a_daily) 内部用 py_mini_racer(V8) 算复权 —— V8 非线程安全, 并发下
    必崩整个进程(2026-07-29 实测 2971/4411 处 Trace/BPT trap 5)。腾讯是纯 requests、
    线程安全、640根足够 MA250, 因此排在新浪之前; 新浪只在腾讯也失败时用, 且必须加锁。
    """
    f = CONFIG["fetch"]
    store_on = bars_from_store_on()
    win = max(int(f["lookback_days"]), STORE_HIST_DAYS) if store_on else int(f["lookback_days"])
    # 缓存键含**实际**回看天数: 改了回看天数(bar数)、或在 库/联网 两条路之间来回切, 旧缓存都会
    # 自动失效 —— 否则切源当天会拿着上一条路缓存的短序列跑, 差异被静默吃掉 (09-07 换库踩过)
    key = _cache_key("hist", code, f["adjust"], win, dt.date.today().isoformat())
    c = _cache_load(key)
    if c is not None:
        return c
    df = None
    if store_on:
        df = _hist_from_store(code, win)
        if df is not None and not df.empty:
            _store_note("hit")
            _cache_save(key, df)
            return df
        # 判据与候选池裁剪**共用同一个** store_verdict: 只有 "在点时股票池里却一根 bar 都没有"
        # (gap) 才算真缺口, 值得回落联网。其余一律直接判无数据:
        #   · 退市/不在池 -> 东财快照的历史遗留代码 (09-07 实测 196 只), 联网也只是白跑;
        #   · 库里有 1..59 根 -> 这票历史本来就短 (次新股), 联网同样凑不出 module2 要的根数。
        if _store_verdict_one(code, win) != "gap":
            _store_note("skipped")
            return None
        _store_note("fallback")
        df = None
    from . import fuyao
    if fuyao.available():                      # 同花顺: 结构稳定、无V8、线程安全
        df = fuyao.hist(code, years=2.5)
    if (df is None or df.empty) and not _em_hist_down:
        df = _hist_from_em(code)
    if df is None or df.empty:
        df = _hist_from_tencent(code)          # 线程安全, 无 V8
    if df is None or df.empty:
        with _ths_lock:                        # 新浪走 py_mini_racer, 必须串行
            df = _hist_from_sina(code)
    if df is None or df.empty:
        return None
    _cache_save(key, df)
    return df


def _hist_from_em(code: str) -> pd.DataFrame | None:
    f = CONFIG["fetch"]
    end = dt.date.today()
    start = end - dt.timedelta(days=f["lookback_days"])
    try:
        raw = call_with_retry(
            _ak().stock_zh_a_hist,
            symbol=code, period="daily",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            adjust=f["adjust"],
        )
    except Exception as e:
        log.debug("东财日线 %s 失败: %s", code, e)
        if _is_conn_error(e):
            _mark_em_hist_down(e)
        return None
    if raw is None or len(raw) == 0:
        return None
    return _finalize_hist(rename_normalize(raw, {
        "date": ["日期"], "open": ["开盘"], "high": ["最高"], "low": ["最低"],
        "close": ["收盘"], "volume": ["成交量"], "amount": ["成交额"], "pct_chg": ["涨跌幅"],
    }))


def _hist_from_tencent(code: str) -> pd.DataFrame | None:
    """个股日线(前复权) —— 腾讯 fqkline, **纯 requests, 线程安全**。

    存在的理由: akshare 的新浪日线 stock_zh_a_daily 内部用 py_mini_racer(V8) 算复权,
    而阶段A 要用 16 线程扫 4000+ 只 —— V8 非线程安全, 必崩
    (2026-07-29 实测在 2971/4411 处 Trace/BPT trap 5, 整轮报废)。
    腾讯单次返回 ~640 根, 足够覆盖 MA250 所需的 ~330 根, 故作为东财之后的首选。
    """
    try:
        kl = _tencent_chunk(_tencent_symbol(code), "", "")
    except Exception as e:
        log.debug("腾讯日线 %s 失败: %s", code, e)
        return None
    if not kl:
        return None
    rows = [k[:6] for k in kl if k and len(k) >= 5]
    if len(rows) < 60:
        return None
    df = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "volume"][:len(rows[0])])
    return _finalize_hist(df)


def _hist_from_sina(code: str) -> pd.DataFrame | None:
    f = CONFIG["fetch"]
    end = dt.date.today()
    start = end - dt.timedelta(days=f["lookback_days"])
    try:
        raw = call_with_retry(
            _ak().stock_zh_a_daily,
            symbol=_sina_symbol(code), adjust=f["adjust"],
            start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"),
        )
    except Exception as e:
        log.debug("新浪日线 %s 失败: %s", code, e)
        return None
    if raw is None or len(raw) == 0:
        return None
    return _finalize_hist(rename_normalize(raw, {
        "date": ["date", "日期"], "open": ["open", "开盘"], "high": ["high", "最高"],
        "low": ["low", "最低"], "close": ["close", "收盘"],
        "volume": ["volume", "成交量"], "amount": ["amount", "成交额"],
    }))


# ===========================================================================
#  2b) 长历史日线 (盈利指引回测用, ~10年前复权)
#      东财 push2his / 新浪 / 网易 在本机均不可达 -> 用腾讯 fqkline。
#      腾讯单次最多返回 ~640 根, 但支持按日期区间查询 -> 分段翻页拼接。
# ===========================================================================
_TENCENT_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


def _tencent_symbol(code: str) -> str:
    code = str(code).zfill(6)
    if code.startswith("920") or code.startswith(("8", "4")):
        return "bj" + code
    if code.startswith(("6", "9")):
        return "sh" + code
    return "sz" + code


def _tencent_chunk(sym: str, start: str, end: str) -> list:
    """取一段日线(前复权)。返回 [[date,open,close,high,low,volume], ...]。"""
    p = {"param": f"{sym},day,{start},{end},640,qfq"}
    r = _http().get(_TENCENT_KLINE, params=p,
                    headers={"User-Agent": _BROWSER_UA}, timeout=25)
    j = r.json()
    if j.get("msg") == "param error":
        return []
    d = j.get("data") or {}
    if not isinstance(d, dict):
        return []
    sub = d.get(sym) or {}
    if not isinstance(sub, dict):
        return []
    return sub.get("qfqday") or sub.get("day") or []


# ---- 基准指数日线备源 (腾讯断连时 fetch_index_bars 用), 输出与 _tencent_chunk 同形
#      [[date, open, close, high, low, volume], ...]; 单位必须与腾讯一致: 成交量 "手"。
_EM_HIS_HOSTS = ("push2his.eastmoney.com", "19.push2his.eastmoney.com",
                 "63.push2his.eastmoney.com")


def _em_index_chunk(sym: str, start: str, end: str) -> list:
    """东财直连指数日线 (push2his kline, 不经 akshare)。2026-09-06 逐根核对 sh000300
    09-01..09-04: 价格 2 位小数、成交量 "手" 与腾讯完全一致, 可直接混入同一张 idx_bars。"""
    secid = ("1." if sym.startswith("sh") else "0.") + sym[2:]
    p = {"secid": secid, "fields1": "f1,f2,f3,f4,f5,f6",
         "fields2": "f51,f52,f53,f54,f55,f56,f57", "klt": "101", "fqt": "0",
         "beg": start.replace("-", ""), "end": end.replace("-", "")}
    last = None
    for h in _EM_HIS_HOSTS:
        try:
            r = _http().get(f"https://{h}/api/qt/stock/kline/get", params=p,
                            headers={"User-Agent": _BROWSER_UA,
                                     "Referer": "https://quote.eastmoney.com/"}, timeout=15)
            j = r.json()
            kl = ((j.get("data") or {}).get("klines")) or []
            return [k.split(",")[:6] for k in kl if k]
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"东财指数日线 {sym} 全部主机失败: {str(last)[:80]}")


def _sina_index_chunk(sym: str, start: str, end: str) -> list:
    """新浪指数日线 (akshare stock_zh_index_daily, 全量返回后按区间截取)。新浪成交量是
    "股" (2026-09-06 实测 sh000300 09-01: 21486958100 vs 腾讯/东财 214869581), 除以 100
    折成 "手" 与库内口径一致; 价格 3 位小数 (腾讯 2 位), 差异 <0.001 可忽略。"""
    df = _ak().stock_zh_index_daily(symbol=sym)
    if df is None or len(df) == 0:
        return []
    out = []
    for _, r in df.iterrows():
        d = str(r["date"])[:10]
        if start <= d <= end:
            out.append([d, r["open"], r["close"], r["high"], r["low"], float(r["volume"]) / 100.0])
    return out


def _long_hist_is_sane(df: pd.DataFrame, code: str = "") -> bool:
    """长历史数据体检。回测最怕"脏数据算出漂亮结论"(实测某些票会返回错乱行:
    单日 high/low 振幅 60%+、隔日跳变 40%+, 据此算出的 '历史涨过2142%' 是假的)。
    任何一项超阈值直接判脏 -> 上层拒绝出统计, 而不是给出可信度不明的数字。"""
    need = {"open", "high", "low", "close"}
    if df is None or len(df) < 250 or not need.issubset(df.columns):
        return False
    o, h, l, c = (df[k].astype(float) for k in ("open", "high", "low", "close"))
    n = len(df)
    bad = 0
    bad += int((h < l).sum())                                   # 最高<最低
    bad += int((h < o - 1e-9).sum()) + int((h < c - 1e-9).sum())  # 最高不封顶
    bad += int((l > o + 1e-9).sum()) + int((l > c + 1e-9).sum())  # 最低不兜底
    if bad > n * 0.01:
        log.debug("长历史 %s 脏: OHLC 关系违规 %d/%d", code, bad, n)
        return False
    # 日内振幅: A股有涨跌停, 正常 <=22%; 超 35% 的行视为错乱
    rng = ((h - l) / c.replace(0, np.nan)).abs()
    if int((rng > 0.35).sum()) > n * 0.01:
        log.debug("长历史 %s 脏: 日内振幅异常 %d/%d", code, int((rng > 0.35).sum()), n)
        return False
    # 隔日跳变: 除权/停牌复牌会有大跳, 但占比应极低; 超 25% 的跳变过多 = 拼接错位
    jump = (c / c.shift(1) - 1).abs()
    if int((jump > 0.25).sum()) > max(3, n * 0.005):
        log.debug("长历史 %s 脏: 隔日跳变异常 %d/%d", code, int((jump > 0.25).sum()), n)
        return False
    return True


def fetch_long_hist(code: str, years: int = 10) -> pd.DataFrame | None:
    """~N年前复权日线 (date/open/high/low/close/volume)。分段翻页 + 缓存。
    盈利指引的回测样本靠它; 常规扫描仍用 fetch_hist(500天) 不受影响。"""
    key = _cache_key("longhist", code, years, dt.date.today().isoformat())
    c = _cache_load(key)
    if isinstance(c, str) and c == "BAD":     # 当轮已判定脏, 不再重拉
        return None
    if c is not None:
        return c
    # 最优先本地价格库 (Tushare P1): 库本身就是单一复权基准的 10 年物化前复权, 逐段拼接的
    # 断裂问题在这里根本不存在, 也不占任何配额。
    if bars_from_store_on():
        sdf = _hist_from_store(code, int(365.25 * years) + 5, min_bars=250)
        if sdf is not None and _long_hist_is_sane(sdf, code):
            _cache_save(key, sdf)
            return sdf
    # 其次同花顺: 一次请求拿满10年前复权, 不需要分段拼接 -> 从源头消除
    # "每段复权基准不同导致断裂"的问题(腾讯那条路实测茅台47处跳变)。
    from . import fuyao
    if fuyao.available():
        fdf = fuyao.hist(code, years=years)
        if fdf is not None and _long_hist_is_sane(fdf, code):
            _cache_save(key, fdf)
            return fdf
    sym = _tencent_symbol(code)
    today = dt.date.today()
    rows, seen = [], set()
    # 每段约 2.5 年(<640根), 从最早往今天翻
    seg = 900                                  # 天/段
    cur = today - dt.timedelta(days=365 * years)
    while cur < today:
        nxt = min(cur + dt.timedelta(days=seg), today)
        try:
            kl = call_with_retry(_tencent_chunk, sym,
                                 cur.isoformat(), nxt.isoformat())
        except Exception as e:
            log.debug("腾讯长历史 %s [%s~%s] 失败: %s", code, cur, nxt, e)
            kl = []
        for k in kl:
            if not k or len(k) < 5:
                continue
            d0 = str(k[0])
            if d0 in seen:
                continue
            seen.add(d0)
            rows.append(k[:6])
        cur = nxt + dt.timedelta(days=1)
    if len(rows) < 250:                        # 不足1年 -> 视为拿不到
        return None
    df = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "volume"][:len(rows[0])])
    for col in ("open", "close", "high", "low", "volume"):
        if col in df.columns:
            df[col] = _to_num(df[col])
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df = df.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
    if not _long_hist_is_sane(df, code):
        log.info("长历史 %s 未通过数据体检, 放弃(该股不出盈利指引)", code)
        _cache_save(key, "BAD")          # 缓存坏结果, 当轮不反复重拉
        return None
    _cache_save(key, df)
    return df


# ===========================================================================
#  3) 行业列表 / 成分 / 指数历史  ——  stock_board_industry_*_em (东财一级)
# ===========================================================================
def fetch_industry_list() -> pd.DataFrame | None:
    """行业列表。优先东财, 失败退回同花顺。"""
    key = _cache_key("ind_list", dt.date.today().isoformat())
    c = _cache_load(key)
    if c is not None:
        return c
    df = _industry_list_em()
    if df is None or df.empty:
        log.info("东财行业列表不可用, 尝试同花顺 ...")
        df = _industry_list_ths()
    if df is None or df.empty:
        return None
    df = df.drop_duplicates(subset=["industry"]).reset_index(drop=True)
    df = _keep_top_level_industries(df)
    _cache_save(key, df)
    return df


# 一级行业通常 ~90-130 个。若接口返回几百个, 说明混进了二级/三级板块
# (2026-07-29 实测东财返回 486 个, 含"股份制银行Ⅲ""体育Ⅱ"这类)。
# 细分行业成分股极少, top_n 选中它们会让候选池塌成几十只 —— 必须先归一到一级。
_SUB_LEVEL_SUFFIX = ("Ⅱ", "Ⅲ", "II", "III")
_MAX_SANE_INDUSTRIES = 150


def _keep_top_level_industries(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty or len(df) <= _MAX_SANE_INDUSTRIES:
        return df
    n0 = len(df)
    mask = ~df["industry"].astype(str).str.endswith(_SUB_LEVEL_SUFFIX)
    out = df[mask].reset_index(drop=True)
    log.info("行业列表 %d 个疑似含二/三级板块, 剔除带 Ⅱ/Ⅲ 后缀的 -> %d 个", n0, len(out))
    if len(out) > _MAX_SANE_INDUSTRIES:
        # 仍然过多(东财三级板块未必都带后缀) -> 退回东财直连的一级列表(实测 100 个)
        direct = _industry_list_em_direct()
        if direct is not None and not direct.empty and len(direct) <= _MAX_SANE_INDUSTRIES:
            log.info("仍有 %d 个, 改用东财直连一级行业列表 %d 个", len(out), len(direct))
            return direct
        log.warning("行业列表仍有 %d 个(疑似细分), 候选池可能偏小; 已由管线的池子下限兜底",
                    len(out))
    return out


def _industry_list_em_direct() -> pd.DataFrame | None:
    """东财一级行业板块列表 —— 直连 clist(fs=m:90 t:2), 走主机池。
    拿到板块代码后, 成分股可用 fs=b:<board_code> 取到 -> 恢复'按行业扫描'的正常路径。"""
    d = _em_get("/api/qt/clist/get",
                {"pn": 1, "pz": 500, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                 "fid": "f3", "fs": "m:90 t:2 f:!50", "fields": "f12,f14,f3"})
    if not d:
        return None
    diff = d.get("diff") or []
    if isinstance(diff, dict):
        diff = list(diff.values())
    if not diff:
        return None
    df = pd.DataFrame(diff).rename(columns={"f12": "board_code", "f14": "industry",
                                            "f3": "pct_chg"})
    if "industry" not in df.columns:
        return None
    df.attrs["source"] = "em"
    log.info("东财行业列表(直连): %d 个", len(df))
    return df[[c for c in ("industry", "board_code", "pct_chg") if c in df.columns]]


def _industry_cons_em_direct(board_code: str) -> pd.DataFrame | None:
    """某板块成分股 —— 直连 clist(fs=b:<board_code>)。"""
    rows, pn = [], 1
    while True:
        d = _em_get("/api/qt/clist/get",
                    {"pn": pn, "pz": 200, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                     "fid": "f3", "fs": f"b:{board_code} f:!50", "fields": "f12,f14"})
        if not d:
            break
        diff = d.get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        if not diff:
            break
        rows.extend(diff)
        total = d.get("total") or 0
        if total and len(rows) >= total:
            break
        pn += 1
        if pn > 20:
            break
    if not rows:
        return None
    df = pd.DataFrame(rows).rename(columns={"f12": "code", "f14": "name"})
    if "code" not in df.columns:
        return None
    df["code"] = df["code"].astype(str).str.zfill(6)
    return df[["code", "name"]]


def _industry_list_em() -> pd.DataFrame | None:
    # 注: 这里**故意不用** _industry_list_em_direct()。东财行业指数K线(push2his)在本机
    # 不可达, 而模块1 需要每个行业的K线; 同花顺又不认东财独有的行业名(实测"文字媒体"
    # "零食"均 FAIL) -> 若把列表换成东财口径, 景气榜会掉一大批行业。
    # 行业列表保持"同花顺列表+同花顺K线"的一致口径; 个股行业归属另由快照 f100 提供。
    if _em_realtime_down:
        return None
    try:
        raw = call_with_retry(_ak().stock_board_industry_name_em)
    except Exception as e:
        log.warning("东财行业列表失败: %s", e)
        if _is_conn_error(e):
            _mark_em_down(e)
        return None
    df = rename_normalize(raw, {
        "industry":  ["板块名称", "行业名称", "名称"],
        "board_code": ["板块代码", "代码"],
        "pct_chg":   ["涨跌幅"],
    })
    df.attrs["source"] = "em"
    return df if "industry" in df.columns else None


def _industry_list_ths() -> pd.DataFrame | None:
    try:
        with _ths_lock:                      # V8 非线程安全, 见 _ths_lock 说明
            raw = call_with_retry(_ak().stock_board_industry_name_ths)
    except Exception as e:
        log.warning("同花顺行业列表失败: %s", e)
        return None
    df = rename_normalize(raw, {
        "industry":  ["name", "板块名称", "名称"],
        "board_code": ["code", "板块代码", "代码"],
    })
    df.attrs["source"] = "ths"
    return df if "industry" in df.columns else None


_board_map_cache = None
_board_map_lock = threading.Lock()


def _board_code_of(industry: str) -> str | None:
    """行业名 -> 东财板块代码(BKxxxx)。

    ⚠ 这里**绝不能**调 fetch_industry_list(): 该函数在东财失败时会回退到同花顺,
    而 akshare 的同花顺接口用 py_mini_racer(V8) 解密。本函数是在模块1的16线程池
    **内部**被调用的, 多线程同时初始化 V8 会直接把进程打崩
    (2026-07-29 事故: Check failed: !pool->IsInitialized() -> Trace/BPT trap, 退出133)。
    故只用东财直连列表(纯 requests, 线程安全), 且用锁保证只拉一次。
    """
    global _board_map_cache
    if _board_map_cache is None:
        with _board_map_lock:
            if _board_map_cache is None:          # 双检锁: 只有第一个线程去拉
                m = {}
                try:
                    lst = _industry_list_em_direct()
                    if lst is not None and not lst.empty and "board_code" in lst.columns:
                        m = dict(zip(lst["industry"].astype(str),
                                     lst["board_code"].astype(str)))
                except Exception as e:
                    log.debug("板块代码表获取失败: %s", e)
                _board_map_cache = m              # 失败也写空表, 避免每次重试
    return (_board_map_cache or {}).get(str(industry))


def fetch_industry_cons(industry: str) -> pd.DataFrame | None:
    key = _cache_key("ind_cons", industry, dt.date.today().isoformat())
    c = _cache_load(key)
    if c is not None:
        return c
    # 先走直连(akshare 默认主机 17.push2 在本机被挡, 其余主机可达)
    bc = _board_code_of(industry)
    if bc:
        df = _industry_cons_em_direct(bc)
        if df is not None and not df.empty:
            _cache_save(key, df)
            return df
    if _em_realtime_down:    # 直连也失败 -> 上层回退全市场
        return None
    try:
        raw = call_with_retry(_ak().stock_board_industry_cons_em, symbol=industry)
    except Exception as e:
        log.debug("fetch_industry_cons %s failed: %s", industry, e)
        if _is_conn_error(e):
            _mark_em_down(e)
        return None
    df = rename_normalize(raw, {
        "code": ["代码"],
        "name": ["名称"],
    })
    if "code" not in df.columns:
        return None
    df["code"] = df["code"].astype(str).str.zfill(6)
    _cache_save(key, df)
    return df


def fetch_industry_hist(industry: str) -> pd.DataFrame | None:
    """行业指数日线。优先东财, 失败退回同花顺行业指数。"""
    key = _cache_key("ind_hist", industry, CONFIG["fetch"]["lookback_days"],
                     dt.date.today().isoformat())
    c = _cache_load(key)
    if c is not None:
        return c
    df = _industry_hist_em(industry)
    if df is None or df.empty:
        df = _industry_hist_ths(industry)
    if df is None or df.empty:
        return None
    _cache_save(key, df)
    return df


def _normalize_idx_hist(df: pd.DataFrame) -> pd.DataFrame | None:
    if "close" not in df.columns or "date" not in df.columns:
        return None
    for col in ("open", "high", "low", "close", "amount"):
        if col in df.columns:
            df[col] = _to_num(df[col])
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    return df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)


def _industry_hist_em(industry: str) -> pd.DataFrame | None:
    if _em_realtime_down:
        return None
    end = dt.date.today()
    start = end - dt.timedelta(days=CONFIG["fetch"]["lookback_days"])
    try:
        raw = call_with_retry(
            _ak().stock_board_industry_hist_em,
            symbol=industry,
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            period="日k", adjust="",
        )
    except Exception as e:
        log.debug("东财行业指数 %s 失败: %s", industry, e)
        if _is_conn_error(e):
            _mark_em_down(e)
        return None
    if raw is None or len(raw) == 0:
        return None
    return _normalize_idx_hist(rename_normalize(raw, {
        "date": ["日期"], "open": ["开盘"], "high": ["最高"],
        "low": ["最低"], "close": ["收盘"], "amount": ["成交额"],
    }))


def _industry_hist_ths(industry: str) -> pd.DataFrame | None:
    end = dt.date.today()
    start = end - dt.timedelta(days=CONFIG["fetch"]["lookback_days"])
    try:
        with _ths_lock:                      # V8 非线程安全, 见 _ths_lock 说明
          raw = call_with_retry(
            _ak().stock_board_industry_index_ths,
            symbol=industry,
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
    except Exception as e:
        log.debug("同花顺行业指数 %s 失败: %s", industry, e)
        return None
    if raw is None or len(raw) == 0:
        return None
    return _normalize_idx_hist(rename_normalize(raw, {
        "date": ["日期"], "open": ["开盘价", "开盘"], "high": ["最高价", "最高"],
        "low": ["最低价", "最低"], "close": ["收盘价", "收盘"], "amount": ["成交额"],
    }))


# ===========================================================================
#  4) 基准指数 (沪深300) 历史收盘  ——  stock_zh_index_daily_em
# ===========================================================================
def fetch_benchmark_close() -> pd.DataFrame | None:
    sym = CONFIG["source"]["benchmark_index"]
    key = _cache_key("bench", sym, dt.date.today().isoformat())
    c = _cache_load(key)
    if c is not None:
        return c
    raw = None
    for fn, kw in (
        (lambda: _ak().stock_zh_index_daily_em(symbol=sym), {}),
        (lambda: _ak().stock_zh_index_daily(symbol=sym), {}),
    ):
        try:
            raw = call_with_retry(fn)
            if raw is not None and len(raw):
                break
        except Exception as e:
            log.debug("benchmark fetch attempt failed: %s", e)
            raw = None
    if raw is None or len(raw) == 0:
        return None
    # 除 date/close 外顺带保留 OHLCV (若源有): 消费方只读 date/close, 多列无害;
    # ingest_cache_to_pricestore 用它把当日指数补进 idx_bars (2026-09-06 A 指数双源)
    df = rename_normalize(raw, {
        "date":  ["date", "日期"],
        "close": ["close", "收盘"],
        "open": ["open", "开盘"], "high": ["high", "最高"], "low": ["low", "最低"],
        "volume": ["volume", "成交量"],
    })
    if "close" not in df.columns:
        return None
    for col in ("close", "open", "high", "low", "volume"):
        if col in df.columns:
            df[col] = _to_num(df[col])
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    df = df.sort_values("date").reset_index(drop=True)
    _cache_save(key, df)
    return df


# ===========================================================================
#  5) 个股估值历史 (PE/PB 分位)
#     注意: akshare 1.12+ 已移除 stock_a_indicator_lg, 改用东财 stock_value_em
#     (一次返回 PE-TTM/PB/PE静/市销/总市值 的逐日历史); 失败再退回百度股市通。
# ===========================================================================
def fetch_valuation_hist(code: str) -> pd.DataFrame | None:
    key = _cache_key("val", code, dt.date.today().isoformat())
    c = _cache_load(key)
    if c is not None:
        return c
    df = _valuation_from_value_em(code)
    if df is None or df.empty:
        df = _valuation_from_baidu(code)
    if df is None or df.empty:
        return None
    _cache_save(key, df)
    return df


def _valuation_from_value_em(code: str) -> pd.DataFrame | None:
    """东财估值分析: 一次拿到 PE-TTM/PB 等的逐日序列。"""
    try:
        raw = call_with_retry(_ak().stock_value_em, symbol=code)
    except Exception as e:
        log.debug("stock_value_em %s failed: %s", code, e)
        return None
    if raw is None or len(raw) == 0:
        return None
    df = rename_normalize(raw, {
        "date":     ["数据日期", "trade_date", "日期"],
        "pe_ttm":   ["PE(TTM)", "市盈率(TTM)"],
        "pe":       ["PE(静)", "市盈率(静)"],
        "pb":       ["市净率"],
        "ps_ttm":   ["市销率"],
        "total_mv": ["总市值"],
    })
    if "date" not in df.columns:
        return None
    for col in df.columns:
        if col != "date":
            df[col] = _to_num(df[col])
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    return df.sort_values("date").reset_index(drop=True)


def _valuation_from_baidu(code: str) -> pd.DataFrame | None:
    """百度股市通: 每个指标一次调用, 取近五年, 按日期合并。"""
    out = None
    for indicator, std in (("市盈率(TTM)", "pe_ttm"), ("市净率", "pb")):
        try:
            raw = call_with_retry(_ak().stock_zh_valuation_baidu,
                                  symbol=code, indicator=indicator, period="近五年")
        except Exception as e:
            log.debug("baidu valuation %s %s failed: %s", code, indicator, e)
            continue
        if raw is None or len(raw) == 0:
            continue
        d = rename_normalize(raw, {"date": ["date", "日期"], std: ["value"]})
        if "date" not in d.columns or std not in d.columns:
            continue
        d["date"] = pd.to_datetime(d["date"], errors="coerce")
        d[std] = _to_num(d[std])
        out = d if out is None else out.merge(d, on="date", how="outer")
    if out is None:
        return None
    return out.sort_values("date").reset_index(drop=True)


# ===========================================================================
#  6) 财务指标 (ROE/EPS/增长/负债)  ——  stock_financial_analysis_indicator
# ===========================================================================
def fetch_financial_indicator(code: str) -> pd.DataFrame | None:
    key = _cache_key("fin", code, dt.date.today().isoformat())
    c = _cache_load(key)
    if c is not None:
        return c
    start_year = str(dt.date.today().year - 5)
    raw = None
    for kwargs in ({"symbol": code, "start_year": start_year},
                   {"symbol": code}):
        try:
            raw = call_with_retry(_ak().stock_financial_analysis_indicator, **kwargs)
            if raw is not None and len(raw):
                break
        except Exception as e:
            log.debug("fetch_financial_indicator %s failed (%s): %s", code, kwargs, e)
            raw = None
    if raw is None or len(raw) == 0:
        return None
    df = rename_normalize(raw, {
        "date":         ["日期"],
        "roe":          ["净资产收益率(%)", "净资产收益率"],
        "eps":          ["摊薄每股收益(元)", "加权每股收益(元)", "每股收益"],
        "revenue_yoy":  ["主营业务收入增长率(%)", "营业收入增长率", "主营业务收入增长率"],
        "netprofit_yoy": ["净利润增长率(%)", "净利润增长率"],
        "gross_margin": ["销售毛利率(%)", "销售毛利率"],
        "debt_ratio":   ["资产负债率(%)", "资产负债率"],
    })
    for col in df.columns:
        if col != "date":
            df[col] = _to_num(df[col])
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.sort_values("date").reset_index(drop=True)
    _cache_save(key, df)
    return df


# ===========================================================================
#  7) 个股基础信息 (所属行业/上市时间/市值)  ——  stock_individual_info_em
# ===========================================================================
def fetch_basic_info(code: str) -> dict | None:
    key = _cache_key("info", code, dt.date.today().isoformat())
    c = _cache_load(key)
    if c is not None:
        return c
    try:
        raw = call_with_retry(_ak().stock_individual_info_em, symbol=code)
    except Exception as e:
        log.debug("fetch_basic_info %s failed: %s", code, e)
        return None
    if raw is None or len(raw) == 0:
        return None
    # 该接口返回两列: item / value
    try:
        d = dict(zip(raw.iloc[:, 0].astype(str), raw.iloc[:, 1]))
    except Exception:
        return None
    info = {
        "industry": d.get("行业"),
        "name": d.get("股票简称"),
        "list_date": d.get("上市时间"),
        "total_mv": d.get("总市值"),
        "float_mv": d.get("流通市值"),
    }
    _cache_save(key, info)
    return info


# ===========================================================================
#  7b) 个股所属行业 (全市场回退时补全 '所属行业' 列)
#      东财个股信息(stock_individual_info_em)在本机被墙 -> 用雪球个股基础信息,
#      其 affiliate_industry.ind_name 走 HTTPS(443) 可达; 东财兜底(多不可用)。
# ===========================================================================
def _xq_symbol(code: str) -> str:
    code = str(code).zfill(6)
    if code.startswith("920") or code.startswith(("8", "4")):   # 北交所
        return "BJ" + code
    if code.startswith(("6", "9")):                             # 沪市
        return "SH" + code
    return "SZ" + code                                          # 深市 00/30/2


# akshare 内置的 xq_a_token 是硬编码的, 会过期 (过期后接口返回的 JSON 里没有 'data',
# 表现为每只都查不到行业)。这里进程内自取一枚新鲜 token (访问雪球首页拿 cookie), 失败
# 则回落到 akshare 内置值。只取一次, 多线程共用。
_xq_token = None
_xq_token_lock = threading.Lock()


def _get_xq_token() -> str | None:
    global _xq_token
    if _xq_token is not None:
        return _xq_token or None
    with _xq_token_lock:
        if _xq_token is not None:
            return _xq_token or None
        tok = ""
        try:
            import requests
            s = requests.Session()
            s.headers.update({"User-Agent": _BROWSER_UA})
            s.get("https://xueqiu.com/", timeout=CONFIG["fetch"]["timeout_sec"])
            tok = s.cookies.get("xq_a_token") or ""
            if tok:
                log.info("雪球 token 已自动刷新")
        except Exception as e:
            log.debug("雪球 token 获取失败, 用 akshare 内置值: %s", e)
        _xq_token = tok
        return tok or None


def _stock_industry_from_xq(code: str) -> str | None:
    try:
        raw = call_with_retry(_ak().stock_individual_basic_info_xq,
                              symbol=_xq_symbol(code), token=_get_xq_token())
    except Exception as e:
        log.debug("雪球个股信息 %s 失败: %s", code, e)
        return None
    if raw is None or len(raw) == 0:
        return None
    try:
        d = dict(zip(raw.iloc[:, 0].astype(str), raw.iloc[:, 1]))
    except Exception:
        return None
    ind = d.get("affiliate_industry")
    if isinstance(ind, str):
        import ast
        try:
            ind = ast.literal_eval(ind)
        except Exception:
            ind = None
    if isinstance(ind, dict):
        name = ind.get("ind_name")
        return str(name) if name else None
    return None


def fetch_hist_long(code: str, years: int = 10) -> pd.DataFrame | None:
    """个股**长历史**日线(前复权), 供模块5做"历次深跌后表现"的历史类比统计。

    与 fetch_hist 分开: 主扫描只需 500 天, 这里要 ~10 年才够找到多次深跌事件。
    走新浪 stock_zh_a_daily(本机可达且线程安全; 东财日线在本机被限)。按日缓存。
    """
    key = _cache_key("hist_long", code, years, dt.date.today().isoformat())
    c = _cache_load(key)
    if c is not None:
        return c
    if bars_from_store_on():                   # Tushare P1: 库里就有 10 年, 不必再走新浪 V8
        sdf = _hist_from_store(code, int(years * 365.25) + 5, min_bars=250)
        if sdf is not None and not sdf.empty:
            _cache_save(key, sdf)
            return sdf
    end = dt.date.today()
    start = end - dt.timedelta(days=int(years * 365.25))
    try:
        raw = call_with_retry(
            _ak().stock_zh_a_daily, symbol=_sina_symbol(code), adjust="qfq",
            start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))
    except Exception as e:
        log.debug("长历史日线 %s 失败: %s", code, e)
        return None
    if raw is None or len(raw) == 0:
        return None
    df = _finalize_hist(rename_normalize(raw, {
        "date": ["date", "日期"], "open": ["open", "开盘"], "high": ["high", "最高"],
        "low": ["low", "最低"], "close": ["close", "收盘"], "volume": ["volume", "成交量"],
    }))
    if df is None or df.empty:
        return None
    _cache_save(key, df)
    return df


_ind_map = None
_ind_map_lock = threading.Lock()


def fetch_industry_map() -> dict:
    """全市场 {code: 所属行业} —— 一次批量请求拿完 (东财口径, 如 '半导体'/'银行Ⅱ')。

    关键: 走 **push2delay** 主机。本机 push2/push2his 被墙(见项目笔记), 只有 delay 可达;
    它的 clist 接口带 f100(所属行业) 字段, 5500+ 只一次返回, ~2s, 纯 JSON 线程安全
    —— 远优于逐只查(雪球有WAF; 巨潮 stock_profile_cninfo 用 py_mini_racer, 多线程会
    让进程硬崩)。拿不到则返回空 dict, 上层再逐只降级。
    """
    global _ind_map
    if _ind_map is not None:
        return _ind_map
    with _ind_map_lock:
        if _ind_map is not None:
            return _ind_map
        key = _cache_key("ind_map", dt.date.today().isoformat())
        c = _cache_load(key)
        if c:
            _ind_map = c
            return _ind_map
        m = {}
        used_host = None
        # 主机故障转移: 本机对各 push2 主机的可达性会变(代理时好时坏), 逐个试。
        # 2026-07-27 实测 push2.eastmoney.com 可达而 push2delay SSL 报错 —— 反过来的
        # 情况以前也出现过, 所以这里不写死, 谁通用谁。
        HOSTS = ("push2.eastmoney.com", "82.push2.eastmoney.com",
                 "push2delay.eastmoney.com", "push2his.eastmoney.com")
        for host in HOSTS:
            m = {}
            try:
                import requests
                sess = requests.Session()
                sess.headers.update({"User-Agent": _BROWSER_UA,
                                     "Referer": "https://quote.eastmoney.com/"})
                url = f"https://{host}/api/qt/clist/get"
                page, total, PZ = 1, None, 100      # 服务端每页上限 100, 必须分页
                while page <= 80:                   # 上限兜底, 正常 ~59 页
                    r = sess.get(url, params={
                        "pn": page, "pz": PZ, "po": 0, "np": 1, "fltt": 2, "invt": 2,
                        "fid": "f12", "fs": "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23",
                        "fields": "f12,f14,f100"},
                        timeout=CONFIG["fetch"]["timeout_sec"])
                    d = ((r.json() or {}).get("data") or {})
                    rows = d.get("diff") or []
                    if isinstance(rows, dict):      # 某些主机返回 {"0":{...}} 形式
                        rows = list(rows.values())
                    if total is None:
                        total = d.get("total") or 0
                    if not rows:
                        break
                    for it in rows:
                        code = str(it.get("f12") or "").zfill(6)
                        ind = str(it.get("f100") or "").strip()
                        if code and ind and ind not in ("-", "—"):
                            m[code] = ind
                    if total and page * PZ >= total:
                        break
                    page += 1
            except Exception as e:  # noqa: BLE001
                log.debug("批量行业映射失败(%s): %s", host, e)
                continue
            if m:
                used_host = host
                break
        if m:
            log.info("批量行业映射: %d 只 (东财 %s)", len(m), used_host)
            _cache_save(key, m)
        else:
            log.warning("批量行业映射获取失败(所有东财主机不可达), 将逐只降级")
        _ind_map = m
        return _ind_map


# 巨潮 stock_profile_cninfo 内部走 py_mini_racer(V8) 解密, **多线程并发会让进程直接
# abort**(libmini_racer address_pool_manager Check failed) —— 与阶段C同源的坑。
# 用全局锁把它串行化; 它只作为批量映射之外的兜底, 调用量很小。
_cninfo_lock = threading.Lock()


def _stock_industry_from_cninfo(code: str) -> str | None:
    """巨潮个股概况的『所属行业』(证监会口径, 如 '酒、饮料和精制茶制造业')。
    全A覆盖、在本机可达, 作为雪球(有WAF)与东财(被墙)之外的主力兜底。"""
    try:
        with _cninfo_lock:                     # 串行, 防 V8 并发崩进程
            raw = call_with_retry(_ak().stock_profile_cninfo, symbol=str(code).zfill(6))
    except Exception as e:
        log.debug("巨潮个股概况 %s 失败: %s", code, e)
        return None
    if raw is None or len(raw) == 0 or "所属行业" not in raw.columns:
        return None
    try:
        v = raw.iloc[0]["所属行业"]
    except Exception:
        return None
    v = None if v is None else str(v).strip()
    return v or None


_stk_ind_miss = set()          # 本轮已查且没结果的代码 (只在进程内, 不落盘)
_stk_ind_miss_lock = threading.Lock()


def fetch_stock_industry(code: str) -> str | None:
    """个股所属行业名。优先雪球(走443可达), 东财兜底。

    只把"查到的结果"写进磁盘缓存。查不到的仅记在进程内集合里(同轮不重复空查),
    **不落盘** —— 否则数据源临时挂掉(如雪球 token 过期)会把空结果缓存一整天,
    当天即便修好了也全是 '—'(2026-07-27 踩过这个坑)。
    """
    code = str(code).zfill(6)
    m = fetch_industry_map()                      # 0) 批量映射: 一次请求覆盖全市场, 最优先
    if m.get(code):
        return m[code]
    key = _cache_key("stk_ind", code, dt.date.today().isoformat())
    c = _cache_load(key)
    if c:                        # 只认非空的缓存
        return c
    with _stk_ind_miss_lock:
        if code in _stk_ind_miss:
            return None
    name = _stock_industry_from_xq(code)          # 1) 雪球(名称贴近THS口径, 但有WAF)
    if not name:
        name = _stock_industry_from_cninfo(code)  # 2) 巨潮(证监会口径, 串行)
    if not name:
        info = fetch_basic_info(code)             # 3) 东财兜底(本机多不可用)
        name = (info or {}).get("industry")
    if name:
        _cache_save(key, name)
    else:
        with _stk_ind_miss_lock:
            _stk_ind_miss.add(code)
    return name or None


# ===========================================================================
#  8) 行业资金流 (主力净流入, 近5日)  ——  stock_sector_fund_flow_rank
#     (可选数据源; 拿不到则上层把"资金"支柱权重并入趋势+动量)
# ===========================================================================
def fetch_industry_fund_flow() -> pd.DataFrame | None:
    """行业资金净流入。优先东财资金流排名, 失败退回同花顺行业摘要(净流入)。"""
    key = _cache_key("ind_flow", dt.date.today().isoformat())
    c = _cache_load(key)
    if c is not None:
        return c
    df = _fund_flow_em()
    if df is None or df.empty:
        df = _fund_flow_ths()
    if df is None or df.empty:
        return None
    _cache_save(key, df)
    return df


def _fund_flow_em() -> pd.DataFrame | None:
    if _em_realtime_down:
        return None
    raw = None
    for kwargs in ({"indicator": "5日", "sector_type": "行业资金流"},
                   {"indicator": "今日", "sector_type": "行业资金流"}):
        try:
            raw = call_with_retry(_ak().stock_sector_fund_flow_rank, **kwargs)
            if raw is not None and len(raw):
                break
        except Exception as e:
            log.debug("东财行业资金流失败 (%s): %s", kwargs, e)
            if _is_conn_error(e):
                _mark_em_down(e)
            raw = None
    if raw is None or len(raw) == 0:
        return None
    df = rename_normalize(raw, {
        "industry":  ["名称", "板块名称"],
        "net_inflow": ["5日主力净流入-净额", "今日主力净流入-净额",
                       "主力净流入-净额", "主力净流入"],
    })
    if "industry" not in df.columns or "net_inflow" not in df.columns:
        return None
    df["net_inflow"] = _to_num(df["net_inflow"])
    return df


def _fund_flow_ths() -> pd.DataFrame | None:
    try:
        with _ths_lock:                      # V8 非线程安全, 见 _ths_lock 说明
            raw = call_with_retry(_ak().stock_board_industry_summary_ths)
    except Exception as e:
        log.debug("同花顺行业摘要失败: %s", e)
        return None
    if raw is None or len(raw) == 0:
        return None
    df = rename_normalize(raw, {
        "industry": ["板块", "名称", "板块名称"],
        "net_inflow": ["净流入", "净额", "主力净流入"],
    })
    if "industry" not in df.columns or "net_inflow" not in df.columns:
        return None
    df["net_inflow"] = _to_num(df["net_inflow"])
    return df


# ===========================================================================
#  6b) 全市场季度业绩 (归母净利/营收, 累计口径) —— stock_yjbb_em 按报告期批量
#      12个报告期 = 3年, 一次全市场; 供"近四季归母/营收同比×4"与"增长持续性"。
# ===========================================================================
import threading as _th

_YJBB_LOCK = _th.Lock()
_YJBB_MAP: dict | None = None


def _report_periods(n: int = 12) -> list:
    """最近 n 个财报报告期 (YYYYMMDD, 新→旧)。含当前正在披露的期。"""
    today = dt.date.today()
    periods = []
    y, q_ends = today.year, [(3, 31), (6, 30), (9, 30), (12, 31)]
    cand = []
    for yy in range(y - 6, y + 1):      # 往回6年: 优质股筛选要"4个完整年度同比"(需5个年报)
        for (m, d) in q_ends:
            cand.append(dt.date(yy, m, d))
    cand = [c for c in cand if c <= today]
    cand.sort(reverse=True)
    return [f"{c:%Y%m%d}" for c in cand[:n]]


def fetch_profit_reports(n_periods: int = 12) -> dict:
    """批量抓最近 n 个报告期的业绩报表(东财, 每期一次调用覆盖全市场)。
    返回 {code: {"periods": [...升序 'YYYY-MM-DD'], "ni_cum": [...], "rev_cum": [...]}}
    ni = 归母净利润(累计, 元), rev = 营业总收入(累计, 元)。整体按天缓存。"""
    key = _cache_key("yjbb_bulk", n_periods, dt.date.today().isoformat())
    c = _cache_load(key)
    if c is not None:
        return c if isinstance(c, dict) else {}
    out: dict = {}
    n_failed = 0
    for p in _report_periods(n_periods):
        try:
            raw = call_with_retry(_ak().stock_yjbb_em, date=p)
        except Exception as e:
            log.warning("yjbb 报告期 %s 抓取失败: %s", p, e)
            n_failed += 1
            continue
        if raw is None or len(raw) == 0:
            continue
        log.info("业绩报表 %s: %d 行", p, len(raw))
        pd_date = f"{p[:4]}-{p[4:6]}-{p[6:]}"
        for _, r in raw.iterrows():
            code = str(r.get("股票代码", "")).strip()
            if not code:
                continue
            d = out.setdefault(code, {})
            ni = r.get("净利润-净利润")
            rev = r.get("营业总收入-营业总收入")
            roe = r.get("净资产收益率")

            def _f(v):
                return None if v is None or (isinstance(v, float) and np.isnan(v)) else float(v)

            d[pd_date] = (_f(ni), _f(rev), _f(roe))
        time.sleep(0.3)
    result = {}
    for code, dd in out.items():
        periods = sorted(dd)
        result[code] = {
            "periods": periods,
            "ni_cum": [dd[p][0] for p in periods],
            "rev_cum": [dd[p][1] for p in periods],
            "roe_cum": [dd[p][2] for p in periods],
        }
    # 有报告期抓取失败时不缓存: 缺期会让单季拆解/TTM出现空洞,
    # 宁可下次重试, 也不能把不完整的批量数据钉一整天
    if result and n_failed == 0:
        _cache_save(key, result)
    return result


def _parse_cn_amount(s) -> float | None:
    """'272.43亿'/'5,230.50万'/'-3.2亿' -> 元; 解析失败返回 None。"""
    if s is None:
        return None
    t = str(s).replace(",", "").strip()
    if not t or t in ("--", "False", "None", "nan"):
        return None
    mult = 1.0
    if t.endswith("亿"):
        mult, t = 1e8, t[:-1]
    elif t.endswith("万"):
        mult, t = 1e4, t[:-1]
    try:
        return float(t) * mult
    except Exception:
        return None


def fetch_single_q_ths(code: str) -> dict:
    """同花顺利润表(按单季度): 官方口径的单季 归母净利润/营业总收入 —
    直接是披露的单季数, 不需要累计差分, 且经重述调整、与市面软件一致。
    返回 {"periods": [...升序], "ni_parent_q": [...元], "rev_q": [...元]}; 失败 {}。
    注: THS 接口用 py_mini_racer 算 cookie, 多线程会崩 -> 只能单线程调用。"""
    key = _cache_key("thsq", code, dt.date.today().isoformat())
    c = _cache_load(key)
    if c is not None:
        return c if isinstance(c, dict) else {}
    out = {}
    try:
        raw = call_with_retry(_ak().stock_financial_benefit_ths,
                              symbol=code, indicator="按单季度")
        if raw is not None and len(raw):
            rows = []
            for _, r in raw.iterrows():
                p = str(r.get("报告期", ""))[:10]
                ni = _parse_cn_amount(r.get("*归属于母公司所有者的净利润"))
                rev = _parse_cn_amount(r.get("*营业总收入"))
                if len(p) == 10 and (ni is not None or rev is not None):
                    rows.append((p, ni, rev))
            rows.sort(key=lambda t: t[0])
            rows = rows[-16:]           # 近16个单季足够 (同比×4 + TTM×2年)
            if rows:
                out = {"periods": [p for (p, _, _) in rows],
                       "ni_parent_q": [n for (_, n, _) in rows],
                       "rev_q": [v for (_, _, v) in rows]}
    except Exception as e:
        log.debug("fetch_single_q_ths %s 失败: %s", code, e)
        out = {}
    if out:
        _cache_save(key, out)
    return out


def prefetch_quarterly_reports():
    """在基本面阶段前显式预热 (避免并发首调用打爆接口)。"""
    global _YJBB_MAP
    with _YJBB_LOCK:
        if _YJBB_MAP is None:
            _YJBB_MAP = fetch_profit_reports()
    return len(_YJBB_MAP or {})


_PREV_Q_MONTH = {"06": "03", "09": "06", "12": "09"}


def _single_quarters(periods: list, cums: list) -> list:
    """A股财报为年内累计值: 单季 = 本期累计 - 上期累计(必须是同年"相邻"季);
    Q1 = 累计本身。缺季时不得跨季相减(否则把两三个季的和当单季), 记 None。"""
    out = []
    for i, (p, v) in enumerate(zip(periods, cums)):
        if v is None:
            out.append(None)
            continue
        if p[5:7] == "03":
            out.append(v)
        elif (i > 0 and periods[i - 1][:4] == p[:4]
              and periods[i - 1][5:7] == _PREV_Q_MONTH.get(p[5:7])
              and cums[i - 1] is not None):
            out.append(v - cums[i - 1])
        else:
            out.append(None)
    return out


def get_quarterly_series(code: str) -> dict:
    """单只股票的季度序列 (单季口径):
    首选 同花顺按单季度官方数 (fetch_single_q_ths, 精确、经重述调整);
    兜底 东财业绩表累计差分 (缺季有防护但精度略逊)。
    返回 {"periods": [...], "ni_q": [...], "rev_q": [...],
          "ni_cum": [...], "rev_cum": [...], "fy_ni": [...], "fy_dates": [...], "src": "ths"|"yjbb"}"""
    global _YJBB_MAP
    if _YJBB_MAP is None:
        prefetch_quarterly_reports()
    d = (_YJBB_MAP or {}).get(code)
    ths = fetch_single_q_ths(code) if _THS_OK.get(code, False) else {}   # 默认False: 未预取的不碰THS(线程安全)
    if ths.get("periods"):
        periods = ths["periods"]
        ni_q, rev_q = ths["ni_parent_q"], ths["rev_q"]
        cum_by = {p: v for p, v in zip(d["periods"], d["ni_cum"])} if d else {}
        rev_cum_by = {p: v for p, v in zip(d["periods"], d["rev_cum"])} if d else {}
        # 年报归母 (年度趋势): 优先东财累计年报; THS只有单季则按年求和(4季齐才算)
        fy_dates, fy_ni = [], []
        if d:
            for p, v in zip(d["periods"], d["ni_cum"]):
                if p[5:7] == "12" and v is not None:
                    fy_dates.append(p)
                    fy_ni.append(v)
        else:
            by_year = {}
            for p, v in zip(periods, ni_q):
                if v is not None:
                    by_year.setdefault(p[:4], []).append(v)
            for y in sorted(by_year):
                if len(by_year[y]) == 4:
                    fy_dates.append(f"{y}-12-31")
                    fy_ni.append(sum(by_year[y]))
        return {"periods": periods, "ni_q": ni_q, "rev_q": rev_q,
                "ni_cum": [cum_by.get(p) for p in periods],
                "rev_cum": [rev_cum_by.get(p) for p in periods],
                "fy_dates": fy_dates, "fy_ni": fy_ni, "src": "ths"}
    if not d:
        return {}
    periods = d["periods"]
    ni_q = _single_quarters(periods, d["ni_cum"])
    rev_q = _single_quarters(periods, d["rev_cum"])
    fy = [(p, v) for p, v in zip(periods, d["ni_cum"]) if p[5:7] == "12" and v is not None]
    return {"periods": periods, "ni_q": ni_q, "rev_q": rev_q,
            "ni_cum": d["ni_cum"], "rev_cum": d["rev_cum"],
            "fy_dates": [p for (p, _) in fy], "fy_ni": [v for (_, v) in fy], "src": "yjbb"}


# THS 预取标记: 只有预取过(单线程)的代码才在并发阶段用 THS 缓存, 未预取的直接走兜底,
# 避免并发线程触发 THS 首次网络调用 (py_mini_racer 多线程会硬崩)
_THS_OK: dict = {}


def prefetch_single_q_ths(codes: list, budget_sec: int = 600) -> int:
    """单线程串行预取 THS 单季数据 (带时间预算); 命中当日缓存的秒回。
    返回成功只数。并发阶段 get_quarterly_series 只读缓存, 不再打网络。"""
    t0 = time.time()
    n = 0
    for code in codes:
        if time.time() - t0 > budget_sec:
            log.warning("THS单季预取超预算, 已取 %d/%d, 其余走业绩表差分兜底", n, len(codes))
            break
        try:
            ok = bool(fetch_single_q_ths(code).get("periods"))
        except Exception:
            ok = False
        _THS_OK[code] = ok
        if ok:
            n += 1
    for code in codes:
        _THS_OK.setdefault(code, False)
    return n

