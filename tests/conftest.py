#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/ 公共守卫 (卡 TEST-SPEED, 2026-10-17): **用例禁真联网** —— 非 live 用例一解析外网域名就当场报错指名, 不悄悄等。

为什么有这一层: 10-13 夜 a-share 全量用例 "484 passed 耗时 6h36m" 排查下来 (本卡) 不是用例慢 —— System 日志 Kernel-Power
42/107 + Kernel-General 1 显示 PC 00:55:52→07:29:32 睡了 6h34m (时钟补 23,620,382 ms), 用例实跑约 2 分半; 但顺手量出全套
里真有两条用例在联网: test_data_date / test_ind_pe_basis 各一条经 export_data.build_payload → earnings_cal.fetch_appoint_map
打东财数据中心 (干净导出树没有 data/cache 且用例把 use_cache 关了, 每次都真拉; 10-17 实测各 40 s 上下; 东财分块慢流时
没有上界 —— requests 的 timeout=20 只管单次 socket 读, 管不住滴流)。两条已打桩; 这里再把 DNS 解析整体掐掉, 以后谁再联网
(akshare / requests / urllib 都要先 getaddrinfo) 就在第一行报 OSError, 而不是等几十秒到几小时。

规矩 (TEAM.md 协作协议补充三): 用例禁真等待 / 真联网; 真需要联网的用例标 `@pytest.mark.live`, stock-core/pytest.ini (两仓
共用) 默认 `addopts = -m "not live"` 不跑, 要跑显式 `-m live` (命令行的 -m 覆盖 ini); 本守卫对带 live 标记的用例放行真网络。

只拦 DNS (socket.getaddrinfo) 这一层, 不拦 socket 本身: 回环 (localhost / 127.0.0.1 / ::1) 放行; 进程池 / 子进程走管道不受
影响; 子进程 (cmd.exe / python -c) 里不生效 —— 子进程要联网得自己打桩。直接 connect 到字面 IP 的代码绕得过去 (标准库与
requests 都走 getaddrinfo, 实际没有这种用法)。
"""
from __future__ import annotations

import socket

import pytest

_REAL_GETADDRINFO = socket.getaddrinfo
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "", None}
#: 当前用例 nodeid + 是否 live (autouse 夹具每条用例前后维护; 收集阶段也拦, 记成 <collection>)
_STATE = {"test": "<collection>", "live": False}


def _guarded_getaddrinfo(host, *args, **kwargs):
    h = host.decode("utf-8", "replace") if isinstance(host, (bytes, bytearray)) else host
    if _STATE["live"] or h in _LOCAL_HOSTS:
        return _REAL_GETADDRINFO(host, *args, **kwargs)
    raise socket.gaierror(
        socket.EAI_NONAME,
        f"[tests/conftest.py] 用例禁真联网: {_STATE['test']} 试图解析 {h!r} —— 把取数打桩 (requests.Session / 取数函数), "
        f"真需要联网就标 @pytest.mark.live (默认 -m 'not live' 不跑)")


socket.getaddrinfo = _guarded_getaddrinfo


def pytest_configure(config):
    # stock-core/pytest.ini 已登记同名标记; 这里再登记一次是为了不带 -c 跑本目录时也不报 PytestUnknownMarkWarning
    config.addinivalue_line("markers", "live: 需要真网络的用例 (默认 -m 'not live' 不跑; 要跑显式 -m live)")


@pytest.fixture(autouse=True)
def _no_real_network(request):
    _STATE["test"] = request.node.nodeid
    _STATE["live"] = request.node.get_closest_marker("live") is not None
    try:
        yield
    finally:
        _STATE["test"], _STATE["live"] = "<between>", False
