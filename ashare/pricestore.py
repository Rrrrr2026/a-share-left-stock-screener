#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pricestore — 共用核心 leftside_core.pricestore 的本仓库入口。"""
from . import market as _market          # noqa: F401
import leftside_core.pricestore as _core

globals().update({k: v for k, v in vars(_core).items() if not k.startswith("__")})


if __name__ == "__main__":
    import logging
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # 2026-09-07 换库后: 不带参数不再默认 backfill —— backfill 走逐股"抓取日"基准的 qfq 路径,
    # 在 Tushare v2 库上会被核心守卫拒写, 但让人手滑就能碰到守卫本身就是脚坑。run_a.sh 用的是
    # `-m ashare.pricestore update` (按 trade_date 全市场增量), 手工也请显式给命令。
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in ("update", "backfill", "coverage", "ready"):
        print("用法: python -m ashare.pricestore update|backfill|coverage|ready [YYYYMMDD]"
              "  (生产日更 = update; ready 的退出码 0=已到 1=还没到 2=判不了)")
        raise SystemExit(2)
    if cmd == "ready":
        # run_a.sh v2 的等待循环用它判"当天行情进库了没有"。**只打一行 + 退出码**, 不打
        # coverage —— 循环里每 10 分钟问一次, 日志要短。
        v = ready_for(sys.argv[2] if len(sys.argv) > 2 else None)
        print(f"ready={v['ready']} {v['reason']}")
        raise SystemExit(v["code"])
    if cmd == "update":
        print(update_daily())
    elif cmd == "backfill":
        print(backfill())
    print(coverage())
