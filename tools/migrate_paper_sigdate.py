#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模拟盘账本 sig_date 迁移 (2026-09-08 卡 R3-4) —— **默认 dry-run, 一个字节都不写**。

## 病灶

`dashboard/history/day_2026-08-24.json` 的 `meta.data_date` 曾被写成 `2026-08-21`
(周一跑却贴了上周五; 275 条候选里 271 条命中 08-24 的原始收盘, 命中 08-21 的 0 条)。
模拟盘 `leftside_core.paper._register` **只在注册那一刻**把当时的 `data_date` 抄进
`sig_date` 和 `id`, 之后再也不回头看快照 —— 所以事后把快照 meta 改对 (或上
`SNAPSHOT_DATA_DATE_FIX` 修正表) **追不回账本里已经写死的那批**。

后果不是"差三天"这么轻: `_simulate_signal` 用 `sig_date` 定 `idx0`, 再用
`find_anchor` 从 idx0 往前找 **6 根**。真实的 bar 在 idx0+1 (08-24 在 08-21 之后),
**往前找永远够不着**, 于是这批信号一律锚在 08-21 那根上, 相当于提前一个交易日入场,
scale 也跟着偏 —— 09-08 复核实测: 换成 raw 锚定后这批仍然 62/62 锚不到真实 bar,
也就是说"回测取价读库"那个开关**修不了它**, 只能迁移账本。

## 判定 (与卡上预登记的规则逐字一致)

一条 `sig_date == 2026-08-21` 的信号要被改判为 2026-08-24, 必须**同时**满足:
  ① 它的 `cand.price` **逐值等于** `day_2026-08-24.json` 里同一 code 的 price;
  ② 且**不等于** `day_2026-08-21.json` 里同一 code 的 price
     (该 code 根本不在 08-21 那份里 = 也算"不等于", 但会在对账表里单独标出来)。
两个条件都用**精确相等** (float 逐值), 不设容差 —— 这里判的是"这条记录是从哪份文件
抄来的", 不是"两个价差不多"; 一设容差就会把 08-21 与 08-24 收盘恰好接近的票也卷进来。
`cat == "quality"` 的信号没有快照价 (`cand` 只有 code/name), **一律不动**, 单独列出。

## 用法

    python -X utf8 tools/migrate_paper_sigdate.py                 # dry-run (默认)
    python -X utf8 tools/migrate_paper_sigdate.py --out r.json    # 顺便把对账表落盘
    python -X utf8 tools/migrate_paper_sigdate.py --apply         # 真写 (先自动备份)

`--apply` 才写账本, 且写之前一定先把原件复制到
`data/backups/paper_portfolio.<ts>.json`, 写的时候走**临时文件 + os.replace** 原子替换
(半个 JSON 比没有 JSON 更糟)。回滚命令由脚本自己打出来, 不用现编。
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

WRONG_DATE = "2026-08-21"          # 账本里写着的 (= 快照 meta 当时的错值)
RIGHT_DATE = "2026-08-24"          # 价格证明的真实数据日


def _load(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def snapshot_prices(hist_dir: str, day: str) -> dict:
    """day_<day>.json -> {code: price}。文件缺失返回空 dict (调用方会报出来)。"""
    p = os.path.join(hist_dir, "day_%s.json" % day)
    if not os.path.exists(p):
        return {}
    out = {}
    for c in (_load(p).get("candidates") or []):
        code, px = c.get("code"), c.get("price")
        if code and px:
            try:
                out[code] = float(px)
            except (TypeError, ValueError):
                continue
    return out


def classify(state: dict, px_wrong: dict, px_right: dict) -> dict:
    """-> {"move": [...], "keep": [...], "quality": [...], "id_clash": [...]}。纯函数, 可离线单测。"""
    existing_ids = {s.get("id") for s in state.get("signals") or []}
    res: dict = {"move": [], "keep": [], "quality": [], "id_clash": []}
    for i, s in enumerate(state.get("signals") or []):
        if s.get("sig_date") != WRONG_DATE:
            continue
        code = s.get("code")
        cand = s.get("cand") or {}
        px = cand.get("price")
        row = {"i": i, "id": s.get("id"), "cat": s.get("cat"), "code": code,
               "name": s.get("name"), "sig_date_old": s.get("sig_date"),
               "price": None if px is None else float(px),
               "px_2026_08_21": px_wrong.get(code), "px_2026_08_24": px_right.get(code)}
        if s.get("cat") == "quality" or not px:
            row["verdict"] = "no_price"          # 优质榜信号没有快照价, 判不了 -> 不动
            res["quality"].append(row)
            continue
        px = float(px)
        hit_right = code in px_right and px_right[code] == px
        hit_wrong = code in px_wrong and px_wrong[code] == px
        if hit_right and not hit_wrong:
            new_id = "%s:%s:%s" % (s.get("cat"), code, RIGHT_DATE)
            row["verdict"] = "move"
            row["sig_date_new"] = RIGHT_DATE
            row["id_new"] = new_id
            row["only_in_0824"] = code not in px_wrong
            if new_id in existing_ids:
                # 同 (cat, code, 08-24) 已经有一条 —— 改过去会撞 id, 交给人看, 不自动合并
                row["verdict"] = "id_clash"
                res["id_clash"].append(row)
            else:
                res["move"].append(row)
        else:
            row["verdict"] = ("both_match" if (hit_right and hit_wrong)
                              else ("only_0821" if hit_wrong else "no_match"))
            res["keep"].append(row)
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", default=ROOT)
    ap.add_argument("--ledger", default=None, help="默认 <repo>/data/paper_portfolio.json")
    ap.add_argument("--history-dir", default=None, help="默认 <repo>/dashboard/history")
    ap.add_argument("--out", default=None, help="把逐条对账表写成 JSON (只读产物, 不是账本)")
    ap.add_argument("--apply", action="store_true", help="真写账本 (默认只 dry-run)")
    args = ap.parse_args()

    ledger = args.ledger or os.path.join(args.repo, "data", "paper_portfolio.json")
    hist = args.history_dir or os.path.join(args.repo, "dashboard", "history")
    if not os.path.exists(ledger):
        print("账本不存在: %s" % ledger)
        return 2
    px_wrong, px_right = snapshot_prices(hist, WRONG_DATE), snapshot_prices(hist, RIGHT_DATE)
    print("账本   : %s" % ledger)
    print("快照目录: %s" % hist)
    print("参照价 : day_%s.json %d 条 / day_%s.json %d 条"
          % (WRONG_DATE, len(px_wrong), RIGHT_DATE, len(px_right)))
    if not px_right:
        print("**day_%s.json 读不到候选价, 无法判定, 退出**" % RIGHT_DATE)
        return 2

    state = _load(ledger)
    n_all = len(state.get("signals") or [])
    res = classify(state, px_wrong, px_right)
    n_wrong_day = sum(len(res[k]) for k in ("move", "keep", "quality", "id_clash"))
    print("\n账本共 %d 条信号, 其中 sig_date=%s 的 %d 条" % (n_all, WRONG_DATE, n_wrong_day))
    print("  改判 %s -> %s : **%d 条**" % (WRONG_DATE, RIGHT_DATE, len(res["move"])))
    print("  保持不动         : %d 条 (%s)" % (
        len(res["keep"]), ", ".join(
            "%s=%d" % (v, sum(1 for r in res["keep"] if r["verdict"] == v))
            for v in ("both_match", "only_0821", "no_match"))))
    print("  优质榜无快照价    : %d 条 (不判)" % len(res["quality"]))
    print("  id 会撞车 (待人工) : %d 条" % len(res["id_clash"]))

    print("\n--- 逐条对账 (改判的 %d 条) ---" % len(res["move"]))
    print("%-4s %-9s %-8s %-6s %-9s %-9s %-9s %s" % (
        "#", "cat", "code", "价", "08-21价", "08-24价", "新sig_date", "新 id"))
    for n, r in enumerate(res["move"], 1):
        print("%-4d %-9s %-8s %-6s %-9s %-9s %-9s %s" % (
            n, r["cat"], r["code"], r["price"],
            "—" if r["px_2026_08_21"] is None else r["px_2026_08_21"],
            r["px_2026_08_24"], r["sig_date_new"], r["id_new"]))
    if res["id_clash"]:
        print("\n--- id 撞车 (**不自动改**, 需要人拍) ---")
        for r in res["id_clash"]:
            print("    %s -> %s 已存在" % (r["id"], r["id_new"]))

    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = os.path.join(args.repo, "data", "backups", "paper_portfolio.%s.json" % ts)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"ledger": ledger, "history_dir": hist, "n_signals": n_all,
                       "wrong_date": WRONG_DATE, "right_date": RIGHT_DATE,
                       "applied": bool(args.apply), "backup": bak if args.apply else None,
                       "counts": {k: len(v) for k, v in res.items()},
                       **res}, f, ensure_ascii=False, indent=1)
        print("\n对账表写入 %s" % args.out)

    if not args.apply:
        print("\n**dry-run: 账本一个字节都没动。** 要真写请加 --apply")
        print("  --apply 会先备份到 : %s" % bak)
        print("  回滚 (PowerShell)  : Copy-Item -Force '%s' '%s'" % (bak, ledger))
        print("  回滚 (bash)        : cp -f '%s' '%s'" % (bak, ledger))
        return 0

    if not res["move"]:
        print("\n没有需要改判的记录, 不写。")
        return 0
    os.makedirs(os.path.dirname(bak), exist_ok=True)
    shutil.copy2(ledger, bak)
    for r in res["move"]:
        s = state["signals"][r["i"]]
        s["sig_date"] = RIGHT_DATE
        s["id"] = r["id_new"]
        s["sig_date_migrated_from"] = WRONG_DATE          # 留痕: 这条被改过, 谁都查得到
    tmp = ledger + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, ledger)
    print("\n已改判 %d 条; 备份 %s" % (len(res["move"]), bak))
    print("回滚 (PowerShell): Copy-Item -Force '%s' '%s'" % (bak, ledger))
    print("回滚 (bash)      : cp -f '%s' '%s'" % (bak, ledger))
    return 0


if __name__ == "__main__":
    sys.exit(main())
