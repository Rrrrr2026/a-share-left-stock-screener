#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto_update.bat 的数据湖步 (卡 LAKE-1, 2026-09-26) —— 离线, 零联网
=================================================================
[1/4] 闸门放行 (`:pstore_ok`) 之后、`gate` 分支之前, 多一段**非致命**的
`"%PYEXE%" -X utf8 "..\\stock-core\\research\\lake.py" update --budget 900 >> "%LOG%"`:
  · 只在 ..\\stock-core\\research\\lake.py 存在时跑 (沙盒里没有这个路径 → 原有五条路的 called 序列一个字不变);
  · 退出码非 0 只往 data\\update.log 写一行 `[lake] update exited with a non-zero rc`, 不 goto、不 exit,
    闸门结果 / 流水线 / docs / git 全不受影响;
  · gate 模式也跑它 (所以 `auto_update.bat gate` 就是它的实机测试)。
这里锁: ① 静态契约 (任何平台) ② 沙盒真跑 (Windows): 假 lake.py 退出 1 / 0, gate 与 auto 两种模式。
运行: python -m pytest tests/test_auto_update_lake_step.py -q
"""
from __future__ import annotations

import re

import pytest

from test_auto_update_bat import _first, _is_side_effect, _lines, _run, _sandbox, needs_cmd

LAKE_CALL = '"%PYEXE%" -X utf8 "..\\stock-core\\research\\lake.py" update --budget 900 >> "%LOG%" 2>&1'
LAKE_GUARD = 'if exist "..\\stock-core\\research\\lake.py" ('
LAKE_WARN = '[lake] update exited with a non-zero rc'
CMD_WORD = re.compile(r"\b(goto|exit)\b")            # 命令词; 措辞里的 exited 不算

FAKE_LAKE = r'''
import os, sys
with open(os.environ["FAKE_CALLS"], "a", encoding="utf-8") as f:
    f.write("lake " + " ".join(sys.argv[1:]) + "\n")
rc = int(os.environ.get("FAKE_LAKE_RC", "0"))
print(f"[fake lake] rc {rc}"); sys.exit(rc)
'''


# ---------------------------------------------------------------- ① 静态契约
def test_lake_step_sits_between_gate_ok_and_gate_branch_and_is_non_fatal():
    lines = _lines()
    i_ok = _first(lines, lambda l: l.strip() == ":pstore_ok")
    i_gate = _first(lines, lambda l: l.strip() == 'if /I "%~1"=="gate" goto :gate_only')
    i_guard = _first(lines, lambda l: l.strip() == LAKE_GUARD)
    i_call = _first(lines, lambda l: l.strip() == LAKE_CALL)
    assert None not in (i_ok, i_gate, i_guard, i_call), (i_ok, i_gate, i_guard, i_call)
    assert i_ok < i_guard < i_call < i_gate, "湖步必须在闸门放行之后、gate 分支之前 (gate 模式也要跑到它)"
    block = lines[i_guard:i_gate]
    close = _first(block, lambda l: l.strip() == ")")
    assert close is not None and close == len(block) - 1, "if exist ( ... ) 块要在 gate 分支前闭合"
    body = [l.strip() for l in block[1:close]]
    assert body == [LAKE_CALL.strip(), f'if errorlevel 1 echo {LAKE_WARN} - non-fatal, see the lines above >> "%LOG%"'], body
    assert not any(CMD_WORD.search(l.lower()) for l in body), "非致命: 块内不许 goto/exit 命令"
    assert not any("(" in l or ")" in l for l in body), "括号块体内不许再有括号: echo 文本里的 ) 会提前闭合 if exist ( 块 (首版就是这样静默不跑的)"
    assert not any(_is_side_effect(l) for l in block), "湖步块里不许有 copy/robocopy/git"
    assert "%errorlevel%" not in "\n".join(block), "括号块内 %errorlevel% 会在解析期展开, 只能用 if errorlevel"
    assert "-X utf8" in LAKE_CALL and "--budget" in LAKE_CALL


def test_lake_step_does_not_touch_the_other_contracts():
    """原有契约的锚点行都还在, 且 [2/4] watchdog 之后的第一条副作用行不是湖步。"""
    text = "\n".join(_lines())
    assert '-m ashare.pricestore ready %TARGET_DAY% >> "%LOG%" 2>&1' in text
    assert text.count(":pstore_ok") == 2 and text.count(":gate_only") == 2
    assert not re.search(r"^\s*timeout\s", text, re.M)


# ---------------------------------------------------------------- ② 沙盒真跑 (Windows)
def _with_fake_lake(tmp_path):
    sb, bare, calls = _sandbox(tmp_path)
    core = tmp_path / "stock-core" / "research"
    core.mkdir(parents=True)
    (core / "lake.py").write_text(FAKE_LAKE, encoding="utf-8")
    return sb, bare, calls


@needs_cmd
@pytest.mark.parametrize("lake_rc", [1, 0])
def test_gate_mode_runs_lake_update_and_ignores_its_rc(tmp_path, lake_rc):
    sb, bare, calls = _with_fake_lake(tmp_path)
    p, log, called = _run(sb, calls, "gate", FAKE_READY_RC=0, FAKE_LAKE_RC=lake_rc)
    assert p.returncode == 0, (p.stdout, log)
    assert "==== GATE-ONLY DONE ==== library ready for 20260923" in log
    assert called == ["pricestore update", "pricestore ready 20260923", "lake update --budget 900"], called
    assert (LAKE_WARN in log) == (lake_rc != 0), log
    assert "[fake lake] rc" in log, "湖步的 stdout 要进 data\\update.log"
    assert (sb / "docs" / "index.html").read_text(encoding="utf-8") == "<html>OLD</html>\n"


@needs_cmd
def test_auto_mode_lake_failure_does_not_change_pipeline_guard(tmp_path):
    sb, bare, calls = _with_fake_lake(tmp_path)
    p, log, called = _run(sb, calls, "auto", FAKE_READY_RC=0, FAKE_LAKE_RC=1, FAKE_WATCHDOG_RC=1)
    assert p.returncode == 1
    assert called == ["pricestore update", "pricestore ready 20260923", "lake update --budget 900", "watchdog"], called
    assert LAKE_WARN in log and "==== ABORT ==== pipeline failed (watchdog rc=1); nothing copied, nothing pushed" in log
    assert (sb / "docs" / "index.html").read_text(encoding="utf-8") == "<html>OLD</html>\n"


@needs_cmd
def test_without_stock_core_the_lake_step_is_skipped(tmp_path):
    sb, bare, calls = _sandbox(tmp_path)
    p, log, called = _run(sb, calls, "gate", FAKE_READY_RC=0)
    assert p.returncode == 0 and called == ["pricestore update", "pricestore ready 20260923"], called
    assert "lake" not in log.lower()
