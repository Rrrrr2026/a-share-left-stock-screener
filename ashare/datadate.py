#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
data_date — 「这批产物的行情日」的唯一定义 (2026-09-14 卡 DATA-DATE)
====================================================================
`meta.data_date` 是快照 / 看板 / 模拟盘登记 (`paper._latest_signals` 拿它当 sig_date) 共用的
"这批价格是哪一天的收盘"。09-14 之前它取的是**基准指数序列的最后一天** (run_pipeline 里
`_bench["date"].iloc[-1]`), 而阶段A 的价格早就直读价格库了 —— 两个来源、两条到达时间:

    2026-09-14 (跑批挪到 10:00 CEST = 北京 16:00 的首日):
      10:01  pricestore update  个股入库到 09-14 (5207 根), 就绪闸门 ready=True
      10:01  fetch_benchmark_close  东财/新浪指数日线还停在 09-11 (缓存实证), idx_bars 也停在 09-11
      11:14  扫描完成 -> data_date = 指数末日 = **09-11**, 快照里的价却是 09-14 收盘
      11:18  模拟盘按 data_date 登记 36 条 sig_date=09-11 (价 = 09-14 快照价) —— 前视一天
      11:19  末尾 ingest 才把 idx_bars 补到 09-14
      19:30  数据总览: STALE A股 看板数据产物 | 数据日 2026-09-11 | 应到 2026-09-14

这与 09-09 处理过的 `day_2026-08-24.json` 错标 (写成 08-21, 62 条账本信号迁移) 是同一形态。

**定义 (本模块)**: 阶段A 直读价格库时, data_date = **价格库个股末日** `MAX(bars.d)` —— 那就是
阶段A 用的价格的日期, 没有第二个候选。基准序列末日只做**交叉核对**: 不一致打 warning 点名,
以库为准。不读库 (联网模式 / 库文件不在) 时退回老口径 (基准末日, 再退 run_date)。

**自检 (防再犯)**: 无论上面怎么算, 最后过一道 `guard`:
  ① data_date < 库末日 -> log.error 点名并改成库末日 (哪天有人把定义改回指数末日, 这一行会响);
  ② data_date < 今天最后一个已收盘交易日 (北京 15:00 后当天算) -> log.error 点名 (改不了: 库本身
     旧了, 该查 `pricestore update` / 就绪闸门); 开市日历取不到时按工作日近似, 只 warning ——
     长假里"近似"会把假期当开市日, 不能用 error 吓唬值班 (09-08 复检那条"国庆连报三天假警"的教训)。

纯函数 `resolve` / `guard` / `last_closed_trading_day` 不碰任何 IO, 单测直接考; `resolve_data_date`
是 run_pipeline 用的薄封装 (读库 + 问日历 + 打日志)。
"""
from __future__ import annotations
import datetime as dt
import logging

log = logging.getLogger("ashare.datadate")

BJ_TZ = dt.timezone(dt.timedelta(hours=8))     # 北京无夏令时, 固定 UTC+8 (不依赖 tzdata)
CLOSE_HOUR = 15                                # A 股 15:00 收盘


def _iso(v) -> str | None:
    if v is None:
        return None
    s = str(v)[:10]
    return s if len(s) == 10 else None


def resolve(store_max, bench_last, run_date) -> tuple[str, list[str]]:
    """-> (data_date, notes)。notes 里每条以 'WARN ' / 'INFO ' 开头, 由调用方打日志。

    store_max  = 价格库个股末日 (阶段A 直读库时); None = 没读库 (联网模式)
    bench_last = 基准指数序列末日; None = 没拿到基准
    run_date   = 流水线跑的日子 (最后的兜底)
    """
    store_max, bench_last = _iso(store_max), _iso(bench_last)
    notes: list[str] = []
    if store_max:
        if bench_last and bench_last != store_max:
            notes.append("WARN 基准指数末日 %s ≠ 价格库个股末日 %s —— data_date 以库为准 (%s); "
                         "指数%s" % (bench_last, store_max, store_max,
                                    "还没到 (update 步没拉到当日指数, fetch_benchmark 也没拿到)"
                                    if bench_last < store_max else
                                    "比库新: 个股库落后了, 查 pricestore update / 就绪闸门"))
        else:
            notes.append("INFO data_date %s = 价格库个股末日 (基准指数末日一致)" % store_max)
        return store_max, notes
    if bench_last:
        notes.append("INFO data_date %s = 基准指数末日 (阶段A 未读价格库, 老口径)" % bench_last)
        return bench_last, notes
    notes.append("WARN data_date 退回 run_date %s (既没读库也没拿到基准指数)" % run_date)
    return str(run_date)[:10], notes


def guard(data_date, store_max, last_closed=None, exact: bool = True) -> tuple[str, list[str]]:
    """自检: -> (可能被改正的 data_date, notes)。notes 以 'ERROR ' / 'WARN ' 开头。"""
    data_date, store_max, last_closed = _iso(data_date), _iso(store_max), _iso(last_closed)
    notes: list[str] = []
    if store_max and data_date and data_date < store_max:
        notes.append("ERROR data_date %s 早于价格库个股末日 %s —— 阶段A 用的是库里 %s 的价, 标注日却更早 "
                     "(09-14 事故同形: 拿指数末日当 data_date)。已改成库末日 %s"
                     % (data_date, store_max, store_max, store_max))
        data_date = store_max
    if last_closed and data_date and data_date < last_closed:
        if exact:
            notes.append("ERROR data_date %s 早于最后一个已收盘交易日 %s (按开市日历, 北京 15:00 后当天算) "
                         "—— 今天的行情没进库就开跑了? 查 `pricestore update` 与就绪闸门; 这批快照/登记"
                         "都是旧价" % (data_date, last_closed))
        else:
            notes.append("WARN data_date %s 早于按工作日近似的最后收盘日 %s (开市日历取不到, 长假里这是"
                         "误报) —— 若今天确实开市, 同上一条 error 处置" % (data_date, last_closed))
    return data_date, notes


def last_closed_trading_day(now=None, trading_days=None) -> tuple[str, bool]:
    """北京时钟下「最后一个已收盘的交易日」-> (YYYY-MM-DD, exact)。

    15:00 之后当天算已收盘, 之前算前一天。开市日由 `trading_days(start, end)` (A 股 = Tushare
    trade_cal, 经 tushare_client 的硬期限/重试) 给出 -> exact=True; 取不到 / 抛错 / 返回空
    -> 退回"跳过周六周日"的近似, exact=False (调用方据此把 error 降成 warning)。
    """
    bj = now.astimezone(BJ_TZ) if now is not None else dt.datetime.now(BJ_TZ)
    cand = bj.date() if bj.hour >= CLOSE_HOUR else bj.date() - dt.timedelta(days=1)
    if trading_days is not None:
        try:
            days = trading_days((cand - dt.timedelta(days=30)).isoformat(), cand.isoformat())
        except Exception as e:                                 # noqa: BLE001
            log.warning("最后收盘日: 开市日历取失败 (%s), 退回工作日近似", str(e)[:120])
            days = None
        if days:
            days = sorted(_iso(d) for d in days if _iso(d) and _iso(d) <= cand.isoformat())
            if days:
                return days[-1], True
    while cand.weekday() >= 5:
        cand -= dt.timedelta(days=1)
    return cand.isoformat(), False


def _emit(notes: list[str]) -> None:
    for n in notes:
        lvl, _, msg = n.partition(" ")
        getattr(log, {"ERROR": "error", "WARN": "warning"}.get(lvl, "info"))("data_date 自检: %s" % msg
                                                                         if lvl == "ERROR" else msg)


def resolve_data_date(bench, run_date, *, now=None) -> str:
    """run_pipeline 用的薄封装: 读库末日 + 基准末日 -> resolve -> 问日历 -> guard -> 打日志。

    `bench` 是 fetch_benchmark_close() 的 DataFrame (或 None)。读库只在 `bars_from_store_on()`
    时 (与阶段A 同一个开关); 日历走 Market.trading_days (拿不到就近似, 见 last_closed_trading_day)。
    """
    from . import datasource as ds
    store_max = None
    if ds.bars_from_store_on():
        store_max = ds._store_max_date()                       # noqa: SLF001
    bench_last = None
    try:
        if bench is not None and len(bench):
            bench_last = str(bench["date"].iloc[-1])[:10]
    except Exception:                                          # noqa: BLE001
        bench_last = None
    data_date, notes = resolve(store_max, bench_last, run_date)
    fn = None
    try:
        from leftside_core.market import current
        fn = getattr(current(), "trading_days", None)
    except Exception:                                          # noqa: BLE001
        fn = None
    last_closed, exact = last_closed_trading_day(now, fn)
    data_date, gnotes = guard(data_date, store_max, last_closed, exact)
    _emit(notes + gnotes)
    log.info("data_date = %s (库末日 %s / 基准末日 %s / 最后收盘日 %s%s / run_date %s)",
             data_date, store_max, bench_last, last_closed, "" if exact else "≈", run_date)
    return data_date
