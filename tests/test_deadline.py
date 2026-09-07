#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
硬期限调用链离线自测 (不联网): ashare.datasource._call_with_deadline 及其进程池/线程版兜底。

覆盖:
  · 线程版兜底: 不可序列化的 fn 走线程; 超时抛 TimeoutError(线程版)
  · 进程池正常路径 (真 spawn): 在工作进程执行 (pid != 主进程); 超时 terminate 整池并重建
  · 工作进程里的业务异常原样透传, 不会被当成"基础设施错误"而禁池
  · 基础设施错误 → 只告警一次 + 置 _POOL_DISABLED + 余下全走线程版:
      - 建池阶段父进程侧 PermissionError (Windows 计划任务 DuplicateHandle WinError 5 的父进程形态)
      - 提交阶段 EOFError (管道断裂)
      - 子进程一启动就死 (WinError 5 的真实形态: 异常在子进程, 父进程只看到任务永不返回)
运行:  python tests/test_deadline.py     或     python -m pytest tests/test_deadline.py -q
"""
from __future__ import annotations
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ashare import datasource as ds  # noqa: E402

MAIN_PID = os.getpid()
_MARK = "进程池不可用"


class _Counter(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.hits = 0

    def emit(self, record):
        if _MARK in record.getMessage():
            self.hits += 1


def _fresh():
    """每个用例前把模块状态归零: 关掉现有池、清禁用标记。"""
    ds._pool_reset()
    ds._POOL_DISABLED = False


def _with_counter():
    c = _Counter()
    ds.log.addHandler(c)
    return c


def _drop(c):
    ds.log.removeHandler(c)


# ---------------------------------------------------------------------------
def test_thread_fallback_unpicklable():
    _fresh()
    orig = ds._pool
    ds._pool = lambda: (_ for _ in ()).throw(AssertionError("不可序列化的调用不该碰进程池"))
    try:
        assert ds._call_with_deadline(lambda: 42, (), {}, 5) == 42
    finally:
        ds._pool = orig
    assert ds._POOL_DISABLED is False


def test_thread_fallback_timeout():
    _fresh()
    t0 = time.time()
    try:
        ds._call_with_deadline(lambda: time.sleep(5), (), {}, 0.5)
        raise AssertionError("应当超时")
    except TimeoutError as e:
        assert "线程版" in str(e)
    assert time.time() - t0 < 4


def test_pool_path_real_spawn():
    _fresh()
    pid = ds._call_with_deadline(os.getpid, (), {}, 60)
    assert isinstance(pid, int) and pid != MAIN_PID, "应在独立工作进程里执行"
    assert ds._POOL is not None and ds._POOL_DISABLED is False
    # 池常驻: 第二次复用, 不重建
    p1 = ds._POOL
    assert ds._call_with_deadline(os.getpid, (), {}, 60) != MAIN_PID
    assert ds._POOL is p1
    # 超时 → TimeoutError(工作进程已终止) + 整池 terminate 置空
    t0 = time.time()
    try:
        ds._call_with_deadline(time.sleep, (10,), {}, 1)
        raise AssertionError("应当超时")
    except TimeoutError as e:
        assert "工作进程已终止" in str(e)
    assert time.time() - t0 < 8
    assert ds._POOL is None and ds._POOL_DISABLED is False
    # 之后自动重建
    assert ds._call_with_deadline(os.getpid, (), {}, 60) != MAIN_PID
    _fresh()


def test_business_exception_passthrough_keeps_pool():
    """fn 在工作进程里抛的 OSError 是业务异常 (如网络错误), 必须原样透传, 不能触发禁池。"""
    _fresh()
    c = _with_counter()
    try:
        try:
            ds._call_with_deadline(os.stat, (os.path.join(os.sep, "__no_such_dir__", "x"),), {}, 60)
            raise AssertionError("应当抛 FileNotFoundError")
        except FileNotFoundError:
            pass
        assert ds._POOL_DISABLED is False and ds._POOL is not None
        assert c.hits == 0
    finally:
        _drop(c)
        _fresh()


def test_pool_create_permission_error_disables_once():
    _fresh()
    calls = []

    def boom(ctx):
        calls.append(1)
        raise PermissionError(13, "拒绝访问")
    orig = ds._make_pool
    ds._make_pool = boom
    c = _with_counter()
    try:
        for _ in range(3):
            assert ds._call_with_deadline(os.getpid, (), {}, 5) == MAIN_PID  # 线程版: 同进程
        assert ds._POOL_DISABLED is True
        assert len(calls) == 1, "禁用后不得再尝试建池"
        assert c.hits == 1, f"应只告警一次, 实际 {c.hits}"
        # 禁用态下不可序列化/可序列化都走线程, 且不再碰池
        assert ds._call_with_deadline(lambda: "ok", (), {}, 5) == "ok"
        assert len(calls) == 1
    finally:
        _drop(c)
        ds._make_pool = orig
        _fresh()


def test_pool_submit_eof_error_disables():
    _fresh()

    class _DeadPool:
        def apply_async(self, *a, **k):
            raise EOFError("Ran out of input")

        def terminate(self):
            pass

        def join(self):
            pass
    orig = ds._pool
    ds._pool = lambda: _DeadPool()
    c = _with_counter()
    try:
        assert ds._call_with_deadline(os.getpid, (), {}, 5) == MAIN_PID
        assert ds._POOL_DISABLED is True and c.hits == 1
        assert ds._call_with_deadline(os.getpid, (), {}, 5) == MAIN_PID
        assert c.hits == 1
    finally:
        _drop(c)
        ds._pool = orig
        _fresh()


def test_probe_detects_workers_dying_at_start():
    """模拟 Windows 计划任务 WinError 5 的真实形态: 子进程 bootstrap 即死, 父进程无异常。"""
    _fresh()
    orig_make, orig_probe = ds._make_pool, ds._POOL_PROBE_SEC
    ds._make_pool = lambda ctx: ctx.Pool(processes=2, initializer=os._exit, initargs=(1,))
    ds._POOL_PROBE_SEC = 8
    c = _with_counter()
    t0 = time.time()
    try:
        assert ds._call_with_deadline(os.getpid, (), {}, 30) == MAIN_PID   # 退回线程版
        dt = time.time() - t0
        assert ds._POOL_DISABLED is True and ds._POOL is None
        assert c.hits == 1, f"应只告警一次, 实际 {c.hits}"
        assert dt < ds._POOL_PROBE_SEC + 15, f"判定太慢: {dt:.1f}s"
        assert ds._call_with_deadline(os.getpid, (), {}, 5) == MAIN_PID
        assert c.hits == 1
    finally:
        _drop(c)
        ds._make_pool, ds._POOL_PROBE_SEC = orig_make, orig_probe
        _fresh()


def test_call_with_retry_uses_fallback_when_disabled():
    """call_with_retry 全链: 禁池态下正常返回值, 不会把它误当成源站挂死。"""
    _fresh()
    ds._POOL_DISABLED = True
    orig = ds._pool
    ds._pool = lambda: (_ for _ in ()).throw(AssertionError("禁用态不该碰池"))
    try:
        assert ds.call_with_retry(os.getpid) == MAIN_PID
    finally:
        ds._pool = orig
        _fresh()


TESTS = [test_thread_fallback_unpicklable, test_thread_fallback_timeout, test_pool_path_real_spawn,
         test_business_exception_passthrough_keeps_pool, test_pool_create_permission_error_disables_once,
         test_pool_submit_eof_error_disables, test_probe_detects_workers_dying_at_start,
         test_call_with_retry_uses_fallback_when_disabled]

if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    fails = 0
    for fn in TESTS:
        t0 = time.time()
        try:
            fn()
            print(f"  ✓ {fn.__name__} ({time.time() - t0:.1f}s)")
        except Exception as e:      # noqa: BLE001
            fails += 1
            print(f"  ✗ FAIL: {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n结果: {len(TESTS) - fails} 通过, {fails} 失败")
    sys.exit(1 if fails else 0)
