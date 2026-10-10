# -*- coding: utf-8 -*-
"""换 Tushare token (老板本人跑; token 永不进屏幕、日志、命令行参数、git)。

用法 (PowerShell 或 Git Bash, 在仓库根目录):
    python -X utf8 tools/set_tushare_token.py             # 校验 → 写本机 data/secrets.json → 同步服务器 → 服务器就绪探针
    python -X utf8 tools/set_tushare_token.py --no-server # 只改本机
选项: --secrets PATH (默认 data/secrets.json) / --no-validate / --no-server /
      --host root@178.104.49.234 / --key ~/.ssh/hetzner_ed25519 /
      --server-secrets /srv/stock/a-share-left-stock-screener/data/secrets.json

流程: ① getpass 隐式读入 token (不回显); ② 用 trade_cal 校验 (code=0 且有行才算有效; 失败只打印 code 与文案, 不改文件);
③ 原地更新 secrets.json 的 tushare_token 键 (其它键原样; 临时文件 + os.replace); ④ 把 token 经 ssh 的 stdin 送到服务器,
服务器端只改同一个键 (属主、600 权限不变); ⑤ 服务器上以 stock 身份跑 `ashare.pricestore ready <最近交易日>` 做就绪探针。
2026-10-08 起 trade_cal 报 code=2002 token已过期 (服务器与 PC 同时), A 股流水线停在 09-30 —— 本工具就是为这种换 token 写的。
"""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import os
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SECRETS = os.path.join(ROOT, "data", "secrets.json")
DEFAULT_HOST = "root@178.104.49.234"
DEFAULT_KEY = os.path.join(os.path.expanduser("~"), ".ssh", "hetzner_ed25519")
DEFAULT_SERVER_SECRETS = "/srv/stock/a-share-left-stock-screener/data/secrets.json"
DEFAULT_BASE = "https://api.tushare.pro/"

# 服务器端只改一个键; token 从 stdin 读, 不进命令行 (ps 可见) 也不进日志
SERVER_PY = (
    "import json,os,sys,pwd;p=sys.argv[1];tok=sys.stdin.read().strip();"
    "assert len(tok)>=20 and tok.split()==[tok],'bad token';d=json.load(open(p,encoding='utf-8'));"
    "old=d.get('tushare_token','');d['tushare_token']=tok;st=os.stat(p);t=p+'.new';"
    "fd=os.open(t,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600);"
    "os.write(fd,(json.dumps(d,ensure_ascii=False,indent=2)+'\\n').encode('utf-8'));os.close(fd);"
    "os.chown(t,st.st_uid,st.st_gid);os.chmod(t,0o600);os.replace(t,p);"
    "print('server: secrets.json 已更新 (键 tushare_token, 新长度 %d, 旧长度 %d; 属主 %s, 权限 600)'"
    "%(len(tok),len(old),pwd.getpwuid(st.st_uid).pw_name))"
)


def validate(token: str, base: str) -> tuple[bool, str, str | None]:
    """返回 (有效?, 文案, 最近一个已开市日 YYYYMMDD 或 None)。文案里永远没有 token。"""
    today = dt.date.today()
    body = json.dumps({
        "api_name": "trade_cal", "token": token,
        "params": {"exchange": "SSE",
                   "start_date": (today - dt.timedelta(days=20)).strftime("%Y%m%d"),
                   "end_date": today.strftime("%Y%m%d")},
        "fields": "cal_date,is_open",
    }).encode("utf-8")
    req = urllib.request.Request(base, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # 网络错误: 只报类型, 不泄露正文
        return False, f"校验请求失败: {type(e).__name__}", None
    code = d.get("code")
    if code != 0:
        return False, f"Tushare 拒绝: code={code} {str(d.get('msg', ''))[:80]}", None
    data = d.get("data") or {}
    fields, items = data.get("fields") or [], data.get("items") or []
    last_open = None
    if items and "cal_date" in fields and "is_open" in fields:
        ci, oi = fields.index("cal_date"), fields.index("is_open")
        opens = sorted(str(row[ci]) for row in items if str(row[oi]) == "1" and str(row[ci]) <= today.strftime("%Y%m%d"))
        last_open = opens[-1] if opens else None
    return bool(items), f"有效: trade_cal 返回 {len(items)} 行 (最近 20 天), 最近开市日 {last_open}", last_open


def write_local(path: str, token: str) -> str:
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    old = d.get("tushare_token", "")
    d["tushare_token"] = token
    tmp = path + ".new"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return f"本机 {os.path.relpath(path, ROOT)} 已更新 (新长度 {len(token)}, 旧长度 {len(old)}; 其它 {len(d) - 1} 个键原样)"


def push_server(token: str, host: str, key: str, server_secrets: str) -> str:
    cmd = ["ssh", "-i", key, "-o", "ConnectTimeout=20", host, "python3", "-c", json.dumps(SERVER_PY), server_secrets]
    try:
        r = subprocess.run(cmd, input=token + "\n", capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return "服务器更新超时 (90s), 服务器文件未动"
    if r.returncode != 0:
        return f"服务器更新失败 rc={r.returncode}: {(r.stderr or r.stdout).strip()[:200]}"
    return r.stdout.strip()


def server_probe(host: str, key: str, day: str | None) -> str:
    day = day or (dt.date.today() - dt.timedelta(days=1)).strftime("%Y%m%d")
    probe = (f"cd /srv/stock/a-share-left-stock-screener && /srv/stock/venv/bin/python -m ashare.pricestore ready {day} 2>&1"
             " | tail -n 3; echo rc=${PIPESTATUS[0]}")
    cmd = ["ssh", "-i", key, "-o", "ConnectTimeout=20", host, "su", "-s", "/bin/bash", "stock", "-c", json.dumps(probe)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return "服务器就绪探针超时 (180s)"
    lines = (r.stdout or r.stderr).strip().splitlines()[-4:]
    return f"服务器就绪探针 (ready {day}; 退出码 0=已到 1=还没到 2=判不了, 换 token 后不该再是 2):\n  " + "\n  ".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--secrets", default=DEFAULT_SECRETS)
    ap.add_argument("--no-validate", action="store_true")
    ap.add_argument("--no-server", action="store_true")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--key", default=DEFAULT_KEY)
    ap.add_argument("--server-secrets", default=DEFAULT_SERVER_SECRETS)
    a = ap.parse_args()

    if not os.path.exists(a.secrets):
        print(f"找不到 {a.secrets}", file=sys.stderr)
        return 2
    with open(a.secrets, encoding="utf-8") as f:
        base = json.load(f).get("tushare_base_url") or DEFAULT_BASE

    if sys.stdin.isatty():  # 真终端 (PowerShell / cmd): 隐式输入。Git Bash 的 mintty 不是 Windows 控制台, 请用 PowerShell 跑
        token = getpass.getpass("粘贴新的 Tushare token 后回车 (不回显): ").strip()
    else:  # 管道/重定向: 读一行 (用于测试或 `echo <token> |`, 不回显)
        token = sys.stdin.readline().strip()
    if len(token) < 20 or token.split() != [token]:
        print("token 形状不对 (太短或含空白), 未改任何文件", file=sys.stderr)
        return 2

    last_open = None
    if not a.no_validate:
        ok, msg, last_open = validate(token, base)
        print(msg)
        if not ok:
            print("未改任何文件 (--no-validate 可跳过校验)", file=sys.stderr)
            return 1

    print(write_local(a.secrets, token))

    if a.no_server:
        print("按要求未同步服务器 (服务器要另跑一次: 去掉 --no-server)")
        return 0
    print(push_server(token, a.host, a.key, a.server_secrets))
    print(server_probe(a.host, a.key, last_open))
    print("下一步: 服务器 `systemctl start stock-a.service` (约 1 小时, 补齐缺的 K 线并发布); "
          "PC 跑 auto_update.bat auto 或等下个 13:30 任务。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
