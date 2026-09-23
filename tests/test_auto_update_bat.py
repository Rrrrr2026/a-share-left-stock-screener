#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto_update.bat (PC 13:30 镜像任务) 的控制流契约 —— 离线, 零联网
==================================================================
病灶 (2026-09-23 卡 PC-UPDATE 校验员, 回修 B): `[2/4] watchdog.py` 之后没有任何 errorlevel 判断,
流水线两轮都失败 / 无心跳被杀不重试 (watchdog `sys.exit(1)`) 时 bat 照样走 `[3/4] copy` 把工作树里
**上一跑**的 dashboard\\*.js 拷进 docs\\ 并 `[4/4] git add docs vendor / commit / push` —— 镜像被旧价
文件冲掉 (09-09..09-23 十份 09-07 价快照就是这么上去的; 最近 10 跑 3 次异常: 09-10 首轮 -9、09-14
WinError 5、09-18 停滞被杀不重试)。闸门 `[1/4]` 只挡「库没到」, 不挡「流水线挂了」。

这里锁两层:
  ① 静态契约 (任何平台): watchdog 那行之后、第一条 copy/robocopy/git 之前必须有 rc 捕获 + 跳转;
     `:pipeline_failed` 块只写一行 ABORT 到 %LOG% 就 `exit /b 1`, 块内没有 copy/git, auto 模式不 pause,
     且它排在 `exit /b` / `goto :eof` 之后 (批处理的标签不挡顺序执行, 放错位置会被正常流程"走进去");
     闸门 `[1/4]` 的骨架 (ready %TARGET_DAY% / gate 口子 / ABORT 行 / -X utf8 / Start-Sleep) 原样;
     纯 ASCII (这台 PC 的 cmd 代码页是 GBK, 非 ASCII 字节会被当双字节读坏)。
  ② 真跑 (仅 Windows, 需要 cmd.exe + git): 把**真的 auto_update.bat** 拷进临时沙盒 (假 ashare.pricestore /
     假 watchdog.py 靠 cwd 优先被 -m 与脚本路径找到, 零联网; docs/vendor 是一个真 git 仓, origin 是本地
     bare 仓), 五条路各跑一遍:
       · ready 0 + watchdog 1        → `==== ABORT ==== pipeline failed (watchdog rc=1); nothing copied,
                                        nothing pushed`, exit 1, docs 一字不动, 没有新提交, origin 没动
       · ready 0 + watchdog.py 不存在 → 同上 (rc=2: 解释器打不开脚本也算失败, 改前照样发布)
       · ready 1,0 (等一轮) + watchdog 0 → `not ready … round 1/3, sleeping 1s` → `ready for … (rc=0)`
                                        → `[pipeline] watchdog rc=0` → docs 换新 → 提交 "auto update data"
                                        推到 origin → `==== DONE ====`, exit 0
       · ready 1 恒 (2 轮)            → 闸门 ABORT 行, watchdog 从未被调, docs/git 不动 (原卡行为不变)
       · gate 模式                    → `==== GATE-ONLY DONE ====`, exit 0, watchdog 从未被调
运行:  python -m pytest tests/test_auto_update_bat.py -q
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BAT = os.path.join(ROOT, "auto_update.bat")

ABORT_PIPE = '==== ABORT ==== pipeline failed (watchdog rc=%PIPE_RC%); nothing copied, nothing pushed'
ABORT_GATE = 'price library not ready for %TARGET_DAY% after %PSTORE_WAIT_TRIES% x %PSTORE_WAIT_SLEEP%s'


def _lines() -> list[str]:
    with open(BAT, "rb") as f:
        raw = f.read()
    return raw.decode("ascii").replace("\r\n", "\n").split("\n")


def _first(lines, pred, start=0):
    for i in range(start, len(lines)):
        if pred(lines[i]):
            return i
    return None


def _is_side_effect(line: str) -> bool:
    """copy / robocopy / git 开头的命令行 (忽略大小写与前导空白)。"""
    s = line.strip().lower()
    return s.startswith(("copy ", "robocopy ", "git ")) or bool(re.match(r'^if exist .* copy ', s))


# ---------------------------------------------------------------- ① 静态契约 (任何平台)
def test_bat_is_pure_ascii():
    """行尾不在这里管: Windows 检出是 CRLF (autocrlf), Linux 检出是 LF, 沙盒真跑前统一转成 CRLF。"""
    with open(BAT, "rb") as f:
        raw = f.read()
    bad = [b for b in raw if b >= 0x80]
    assert not bad, f"{len(bad)} 个非 ASCII 字节 (GBK 代码页下会被读坏)"
    assert b"\x00" not in raw


def test_watchdog_rc_is_checked_before_any_copy_or_git():
    lines = _lines()
    i_wd = _first(lines, lambda l: '"%PYEXE%"' in l and "PIPELINE_WATCHDOG" in l and ">>" in l)
    assert i_wd is not None, "找不到 [2/4] 的 watchdog 调用行"
    i_side = _first(lines, _is_side_effect, i_wd + 1)
    assert i_side is not None, "[3/4]/[4/4] 的 copy/git 行不见了"
    between = lines[i_wd + 1:i_side]
    assert 'set "PIPE_RC=%errorlevel%"' in [l.strip() for l in between], \
        "watchdog 之后没有立刻捕获 errorlevel (改前的病: 没人看 rc 就往下 copy/push)"
    assert 'if not "%PIPE_RC%"=="0" goto :pipeline_failed' in [l.strip() for l in between], \
        "watchdog rc != 0 没有跳到 :pipeline_failed"
    # rc 捕获必须紧跟调用 (中间任何命令都会覆盖 errorlevel)
    assert lines[i_wd + 1].strip() == 'set "PIPE_RC=%errorlevel%"'
    # 默认脚本名与测试口子
    assert 'if not defined PIPELINE_WATCHDOG set "PIPELINE_WATCHDOG=watchdog.py"' in [l.strip() for l in lines[:i_wd]]


def test_pipeline_failed_block_contract():
    lines = _lines()
    labels = [i for i, l in enumerate(lines) if l.strip().lower() == ":pipeline_failed"]
    assert len(labels) == 1, f":pipeline_failed 标签应恰好 1 个, 现 {len(labels)}"
    i = labels[0]
    # 标签之前 (跳过空行/rem) 必须是 exit /b 或 goto :eof —— 批处理的标签不挡顺序执行
    j = i - 1
    while j >= 0 and (not lines[j].strip() or lines[j].strip().lower().startswith("rem")):
        j -= 1
    prev = lines[j].strip().lower()
    assert prev.startswith("exit /b") or prev == "goto :eof", \
        f":pipeline_failed 之前一行是 {lines[j]!r}, 正常流程会顺序走进失败块"
    # 块体: 到 exit /b 1 为止
    end = _first(lines, lambda l: l.strip().lower() == "exit /b 1", i + 1)
    assert end is not None, ":pipeline_failed 块没有 exit /b 1"
    block = lines[i + 1:end + 1]
    abort = [l for l in block if ABORT_PIPE in l]
    assert len(abort) == 1 and '>> "%LOG%"' in abort[0], "ABORT 行必须写进 data\\update.log 且措辞固定 (监视器按它 grep)"
    assert not any(_is_side_effect(l) for l in block), "失败块里不许有 copy/robocopy/git"
    assert not any("goto :" in l.lower() and "pipeline_failed" not in l.lower() for l in block), "失败块不许跳回正常流程"
    pauses = [l.strip() for l in block if "pause" in l.lower()]
    assert pauses == ['if /I not "%~1"=="auto" pause'], f"auto 模式下不能 pause (计划任务无控制台会挂住): {pauses}"
    # 失败块之后不能再有会执行的命令 (只允许空行/rem/标签)
    tail = [l for l in lines[end + 1:] if l.strip() and not l.strip().lower().startswith("rem") and not l.strip().startswith(":")]
    assert not tail, f"exit /b 1 之后还有命令: {tail[:3]}"


def test_gate_contract_unchanged():
    lines = _lines()
    text = "\n".join(lines)
    assert '-m ashare.pricestore update >> "%LOG%" 2>&1' in text
    assert '-m ashare.pricestore ready %TARGET_DAY% >> "%LOG%" 2>&1' in text
    for l in lines:
        if "-m ashare.pricestore" in l:
            assert "-X utf8" in l, f"pricestore 调用缺 -X utf8 (重定向 stdout 是 GBK): {l!r}"
    assert 'if /I "%~1"=="gate" goto :gate_only' in text
    assert ABORT_GATE in text and "pipeline not run, nothing pushed" in text
    assert "Start-Sleep" in text and not re.search(r"^\s*timeout\s", text, re.M), "无控制台时 timeout 会报 input redirection 直接退出"
    assert 'if not defined PSTORE_WAIT_TRIES set "PSTORE_WAIT_TRIES=6"' in text
    assert 'if not defined PSTORE_WAIT_SLEEP set "PSTORE_WAIT_SLEEP=600"' in text


def test_step_order():
    lines = _lines()
    idx = {
        "gate_ok": _first(lines, lambda l: l.strip() == ":pstore_ok"),
        "watchdog": _first(lines, lambda l: '"%PYEXE%"' in l and "PIPELINE_WATCHDOG" in l),
        "guard": _first(lines, lambda l: 'goto :pipeline_failed' in l),
        "copy": _first(lines, lambda l: l.strip().lower().startswith("copy ")),
        "git_add": _first(lines, lambda l: l.strip().startswith("git add docs vendor")),
        "git_push": _first(lines, lambda l: l.strip().startswith("git push")),
        "done": _first(lines, lambda l: "==== DONE ====" in l),
        "failed": _first(lines, lambda l: l.strip() == ":pipeline_failed"),
    }
    assert None not in idx.values(), idx
    order = ["gate_ok", "watchdog", "guard", "copy", "git_add", "git_push", "done", "failed"]
    assert [idx[k] for k in order] == sorted(idx[k] for k in order), idx


# ---------------------------------------------------------------- ② 沙盒真跑 (Windows)
_WIN = os.name == "nt" and shutil.which("cmd.exe") is not None
_GIT = shutil.which("git")
needs_cmd = pytest.mark.skipif(not (_WIN and _GIT), reason="要 cmd.exe + git: 真跑 auto_update.bat 只能在 Windows 上")

FAKE_PRICESTORE = r'''
import os, sys
calls = os.environ["FAKE_CALLS"]
with open(calls, "a", encoding="utf-8") as f:
    f.write("pricestore " + " ".join(sys.argv[1:]) + "\n")
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
if cmd == "update":
    print("[fake pricestore] update"); sys.exit(int(os.environ.get("FAKE_UPDATE_RC", "0")))
if cmd == "ready":
    seq = [int(x) for x in os.environ.get("FAKE_READY_RC", "0").split(",")]
    with open(calls, encoding="utf-8") as f:
        n = sum(1 for l in f if l.startswith("pricestore ready"))
    rc = seq[min(n - 1, len(seq) - 1)]
    print(f"[fake pricestore] ready {sys.argv[2:]} -> rc {rc}"); sys.exit(rc)
sys.exit(99)
'''

FAKE_WATCHDOG = r'''
import os, sys
with open(os.environ["FAKE_CALLS"], "a", encoding="utf-8") as f:
    f.write("watchdog\n")
rc = int(os.environ.get("FAKE_WATCHDOG_RC", "0"))
if rc == 0:
    os.makedirs("dashboard", exist_ok=True)
    open(os.path.join("dashboard", "dashboard_data.js"), "w").write("window.__ASHARE__ = {\"run\": \"NEW\"};\n")
    open(os.path.join("dashboard", "index.html"), "w").write("<html>NEW</html>\n")
    os.makedirs(os.path.join("dashboard", "history"), exist_ok=True)
    open(os.path.join("dashboard", "history", "day_2026-09-24.json"), "w").write("{\"meta\": {\"data_date\": \"2026-09-24\"}}\n")
print(f"[fake watchdog] rc {rc}"); sys.exit(rc)
'''


def _git(cwd, *args):
    return subprocess.run([_GIT, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def _sandbox(tmp_path, watchdog=True):
    """真 bat + 假 python 模块 + 真 git 仓 (origin = 本地 bare)。返回 (沙盒目录, bare 目录, calls 文件)。
    watchdog=False: 沙盒里根本没有 watchdog.py (解释器打不开脚本 -> rc 2 的那条路)。"""
    sb = tmp_path / "repo"
    (sb / "ashare").mkdir(parents=True)
    (sb / "ashare" / "__init__.py").write_text("", encoding="utf-8")
    (sb / "ashare" / "pricestore.py").write_text(FAKE_PRICESTORE, encoding="utf-8")
    if watchdog:
        (sb / "watchdog.py").write_text(FAKE_WATCHDOG, encoding="utf-8")
    for d in ("data", "docs/history", "dashboard/history", "vendor/leftside_core"):
        (sb / d).mkdir(parents=True, exist_ok=True)
    (sb / "dashboard" / "index.html").write_text("<html>STALE</html>\n", encoding="utf-8")
    (sb / "dashboard" / "dashboard_data.js").write_text('window.__ASHARE__ = {"run": "STALE"};\n', encoding="utf-8")
    (sb / "docs" / "index.html").write_text("<html>OLD</html>\n", encoding="utf-8")
    (sb / "docs" / "dashboard_data.js").write_text('window.__ASHARE__ = {"run": "OLD"};\n', encoding="utf-8")
    (sb / "vendor" / "leftside_core" / "x.py").write_text("# vendor\n", encoding="utf-8")
    with open(BAT, "rb") as f:
        bat = f.read().decode("ascii").replace("\r\n", "\n").replace("\n", "\r\n")
    (sb / "auto_update.bat").write_bytes(bat.encode("ascii"))
    (sb / ".gitignore").write_text("data/\ndashboard/dashboard_data.js\n__pycache__/\n", encoding="utf-8")
    bare = tmp_path / "origin.git"
    assert _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare)).returncode == 0
    assert _git(sb, "init", "-q", "-b", "main").returncode == 0
    for k, v in (("user.name", "t"), ("user.email", "t@x"), ("commit.gpgsign", "false"), ("core.autocrlf", "false")):
        assert _git(sb, "config", k, v).returncode == 0
    assert _git(sb, "add", "-A").returncode == 0
    assert _git(sb, "commit", "-q", "-m", "seed").returncode == 0
    assert _git(sb, "remote", "add", "origin", str(bare)).returncode == 0
    r = _git(sb, "push", "-q", "-u", "origin", "main")
    assert r.returncode == 0, r.stderr
    return sb, bare, sb / "data" / "fake_calls.txt"


def _run(sb, calls, mode, **fake):
    env = dict(os.environ)
    for k in list(env):
        if k.startswith(("PSTORE_", "FAKE_", "PIPELINE_")) or k in ("PYTHONPATH", "PYTHONSAFEPATH", "PYTHONHOME"):
            env.pop(k)
    env.update({"FAKE_CALLS": str(calls), "PSTORE_TARGET_DAY": "20260923",
                "PSTORE_WAIT_SLEEP": "1", "PSTORE_WAIT_TRIES": "3", "PYTHONDONTWRITEBYTECODE": "1"})
    env.update({k: str(v) for k, v in fake.items()})
    p = subprocess.run(["cmd.exe", "/d", "/c", str(sb / "auto_update.bat"), mode], cwd=str(sb), env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    log = (sb / "data" / "update.log").read_text(encoding="utf-8", errors="replace")
    called = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    return p, log, called


def _head(sb):
    return _git(sb, "rev-parse", "HEAD").stdout.strip()


@needs_cmd
@pytest.mark.parametrize("how", ["watchdog exits 1", "watchdog.py missing"])
def test_pipeline_failure_copies_nothing_and_pushes_nothing(tmp_path, how):
    missing = how == "watchdog.py missing"
    sb, bare, calls = _sandbox(tmp_path, watchdog=not missing)
    head0 = _head(sb)
    fake = {} if missing else {"FAKE_WATCHDOG_RC": 1}
    want_rc = 2 if missing else 1                     # python: can't open file -> exit 2
    p, log, called = _run(sb, calls, "auto", FAKE_READY_RC=0, **fake)
    assert p.returncode == 1, (p.stdout, p.stderr, log)
    assert "[pricestore] ready for 20260923 (rc=0), continuing" in log
    assert f"==== ABORT ==== pipeline failed (watchdog rc={want_rc}); nothing copied, nothing pushed" in log, log
    assert "==== DONE ====" not in log and "[pipeline] watchdog rc=0" not in log
    assert "ABORT: pipeline failed" in p.stdout and "[3/4]" not in p.stdout and "[4/4]" not in p.stdout
    # docs 一字不动, history 没有被 /MIR, 没有提交, origin 没动
    assert (sb / "docs" / "index.html").read_text(encoding="utf-8") == "<html>OLD</html>\n"
    assert (sb / "docs" / "dashboard_data.js").read_text(encoding="utf-8") == 'window.__ASHARE__ = {"run": "OLD"};\n'
    assert not list((sb / "docs" / "history").iterdir())
    assert _head(sb) == head0
    assert _git(bare, "rev-parse", "main").stdout.strip() == head0
    assert _git(sb, "status", "--porcelain").stdout.strip() == ""
    if how == "watchdog exits 1":
        assert called == ["pricestore update", "pricestore ready 20260923", "watchdog"], called
    else:
        assert called == ["pricestore update", "pricestore ready 20260923"], called


@needs_cmd
def test_wait_one_round_then_pipeline_ok_publishes(tmp_path):
    sb, bare, calls = _sandbox(tmp_path)
    head0 = _head(sb)
    p, log, called = _run(sb, calls, "auto", FAKE_READY_RC="1,0", FAKE_WATCHDOG_RC=0)
    assert p.returncode == 0, (p.stdout, p.stderr, log)
    assert "[pricestore] not ready for 20260923 (ready rc=1, round 1/3), sleeping 1s" in log
    assert "[pricestore] ready for 20260923 (rc=0), continuing" in log
    assert "[pipeline] watchdog rc=0, copying results to docs and publishing" in log
    assert "==== DONE ====" in log and "==== ABORT ====" not in log
    assert called == ["pricestore update", "pricestore ready 20260923",
                      "pricestore update", "pricestore ready 20260923", "watchdog"], called
    # [3/4] 真拷了, [4/4] 真提交真推了
    assert (sb / "docs" / "index.html").read_text(encoding="utf-8") == "<html>NEW</html>\n"
    assert (sb / "docs" / "dashboard_data.js").read_text(encoding="utf-8") == 'window.__ASHARE__ = {"run": "NEW"};\n'
    assert (sb / "docs" / "history" / "day_2026-09-24.json").exists()
    head1 = _head(sb)
    assert head1 != head0
    assert _git(sb, "log", "-1", "--format=%s").stdout.strip() == "auto update data"
    assert _git(sb, "rev-parse", "HEAD~1").stdout.strip() == head0
    assert _git(bare, "rev-parse", "main").stdout.strip() == head1, "没有推到 origin"
    changed = _git(sb, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert set(changed) == {"docs/index.html", "docs/dashboard_data.js", "docs/history/day_2026-09-24.json"}, changed


@needs_cmd
def test_gate_timeout_never_runs_watchdog(tmp_path):
    sb, bare, calls = _sandbox(tmp_path)
    head0 = _head(sb)
    p, log, called = _run(sb, calls, "auto", FAKE_READY_RC=1, PSTORE_WAIT_TRIES=2)
    assert p.returncode == 1
    assert "==== ABORT ==== price library not ready for 20260923 after 2 x 1s (last ready rc=1); pipeline not run, nothing pushed" in log, log
    assert "watchdog" not in called and called.count("pricestore ready 20260923") == 2, called
    assert (sb / "docs" / "index.html").read_text(encoding="utf-8") == "<html>OLD</html>\n"
    assert _head(sb) == head0 and _git(bare, "rev-parse", "main").stdout.strip() == head0


@needs_cmd
def test_gate_mode_stops_after_gate(tmp_path):
    sb, bare, calls = _sandbox(tmp_path)
    head0 = _head(sb)
    p, log, called = _run(sb, calls, "gate", FAKE_READY_RC=0, FAKE_WATCHDOG_RC=1)
    assert p.returncode == 0, (p.stdout, log)
    assert "==== GATE-ONLY DONE ==== library ready for 20260923" in log
    assert called == ["pricestore update", "pricestore ready 20260923"], called
    assert (sb / "docs" / "index.html").read_text(encoding="utf-8") == "<html>OLD</html>\n"
    assert _head(sb) == head0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
