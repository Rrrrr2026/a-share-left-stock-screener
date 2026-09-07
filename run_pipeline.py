#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键运行 (One-command pipeline)
===============================
    python run_pipeline.py              # 完整跑: 行业景气 -> 技术扫描 -> 基本面 -> 交叉打分 -> 入库 -> 导出仪表盘
    python run_pipeline.py --full-market  # 跳过行业筛选, 扫描全市场
    python run_pipeline.py --demo       # 不联网: 用合成数据填库 + 导出, 便于先看仪表盘
    python run_pipeline.py --no-cache   # 不使用本地缓存
跑完后双击打开 dashboard/index.html。
"""
from __future__ import annotations
import os
import sys
import time
import socket
import argparse
import logging
import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed

# 防卡死: 给所有网络请求设默认超时。akshare/requests 若不显式传 timeout, 单个卡住的
# 连接会让阶段C(单线程)无限期挂起(历史上曾卡在 12/200)。30s 足够正常返回, 卡住则抛错被 _safe 捕获。
socket.setdefaulttimeout(30)

from ashare.config import CONFIG
from ashare.config import DATA_DIR
from ashare import db
from ashare import datasource as ds
from ashare import module1_industry as m1
from ashare import module2_tech as m2
from ashare import module3_fundamentals as m3
from ashare import module4_crossscore as m4
from ashare import module6_profile as m6
from ashare import tradeplan as tp
from ashare import export_data as ex

# Windows 控制台默认 GBK, 输出中文/emoji 会报 UnicodeEncodeError; 统一切到 UTF-8
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("ashare.run")


def _tqdm():
    try:
        from tqdm import tqdm
        return tqdm
    except Exception:
        def _f(x, **k):
            return x
        return _f


def run(full_market: bool, use_cache: bool):
    # 全局socket兜底超时: 任何库(akshare内部等)没设超时的阻塞读, 60秒后抛异常
    # 走重试, 而不是永远挂死。2026-08-13/17/18/19 连续四天 13:30 任务卡死在
    # 某个无超时的网络读上, 4小时被调度器杀掉、留下孤儿进程, 当天不出榜。
    socket.setdefaulttimeout(60)
    # 心跳 (watchdog.py 据此判断是否卡死): 只有"有进展"才更新 —— 进展 = 各阶段循环每完成
    # 一只 (下方 _prog) 或 任意一条日志; 纯存活不算进展, 否则主线程卡在网络读上时心跳照样跳。
    # 心跳带 watchdog 下发的令牌, 孤儿/手动进程写的心跳不会冒充被监控的子进程。
    import threading as _th
    _hb = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "heartbeat.txt")
    _hb_token = os.environ.get("LS_HB_TOKEN", "manual")
    _prog = {"n": 0}

    class _ProgressHandler(logging.Handler):
        def emit(self, record):
            _prog["n"] += 1
    logging.getLogger().addHandler(_ProgressHandler())

    def _beat():
        last = -1
        while True:
            if _prog["n"] != last:
                last = _prog["n"]
                try:
                    with open(_hb, "w", encoding="utf-8") as _f:
                        _f.write(f"{_hb_token}|{dt.datetime.now().isoformat()}|{last}")
                except Exception:
                    pass
            time.sleep(60)
    _th.Thread(target=_beat, daemon=True).start()
    tqdm = _tqdm()
    run_date = dt.date.today().isoformat()
    started = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    CONFIG["source"]["use_cache"] = use_cache
    if full_market:
        CONFIG["industry"]["use_full_market"] = True

    db.init_db()
    db.clear_run(run_date)   # 干净快照: 清掉今天的旧结果(含演示数据)

    # ---------------- 模块1: 行业景气 ----------------
    log.info("模块1: 计算行业景气度 ...")
    ind_df = m1.compute_industry_scores(
        progress_cb=lambda i, n, name: (i % 5 == 0) and log.info("  行业 %d/%d %s", i, n, name))
    if ind_df is not None and not ind_df.empty:
        db.save_industry_scores(run_date, ind_df)
    prosperity_map = {}
    ind_to_codes = {}
    selected_inds = []
    if ind_df is not None and not ind_df.empty:
        prosperity_map = dict(zip(ind_df["industry"], ind_df["prosperity_score"]))
        selected_inds = list(ind_df[ind_df["selected"]]["industry"])
    log.info("模块1: 入选行业 %s", selected_inds)

    # ---------------- 候选股票池 ----------------
    spot = ds.fetch_spot_snapshot()
    spot_map = {}
    if spot is not None and not spot.empty:
        spot_map = {r["code"]: r.to_dict() for _, r in spot.iterrows()}

    def _full_market_universe():
        uni = ds.build_universe(spot)
        rows = []
        if uni is not None:
            thr = CONFIG["tech"]["min_amount_yi"] * 1e8
            minp = CONFIG["tech"]["min_price"]
            for _, r in uni.iterrows():
                code, name = r["code"], r["name"]
                sp = spot_map.get(code, {})
                price = sp.get("price")
                if price is not None and price == price and price < minp:
                    continue   # 低价股预筛, 避免无谓拉取日线
                amt = sp.get("amount")
                if amt is not None and amt == amt and 0 < amt < thr * 0.3:
                    continue   # 明显流动性不足预筛
                rows.append((code, name, None))
        log.info("候选池: 全市场(预筛后) %d 只", len(rows))
        return rows

    universe = []   # list of (code, name, industry)
    # v2: 扫描面扩大 — 扫"全部行业"的成分股(带行业归属), 景气作为打分/标签而非硬性预筛
    # (与美股版一致: 全市场扫, 高景气只是加成)。原"仅入选行业"模式已被覆盖。
    all_inds = (list(ind_df["industry"]) if (ind_df is not None and not ind_df.empty)
                else list(selected_inds))
    if CONFIG["industry"]["use_full_market"] or not all_inds:
        universe = _full_market_universe()
    else:
        seen = set()
        for ind_name in all_inds:
            cons = ds.fetch_industry_cons(ind_name)
            if cons is None:
                continue
            ind_to_codes[ind_name] = list(cons["code"])
            for _, r in cons.iterrows():
                code = r["code"]
                if code in seen:
                    continue
                # 基础过滤: ST / 北交所
                name = r.get("name") or (spot_map.get(code, {}).get("name"))
                if CONFIG["tech"]["exclude_st"] and name and "ST" in str(name).upper():
                    continue
                if CONFIG["tech"]["exclude_bj"] and str(code).startswith(("8", "4", "920")):
                    continue
                seen.add(code)
                universe.append((code, name, ind_name))
        log.info("候选池: 全行业成分股 %d 只 (行业数 %d)", len(universe), len(ind_to_codes))
        # 行业成分接口大面积失败会让扫描面悄悄缩水: 覆盖过低时并入全市场池补齐。
        # 全A正常 ~5200 只; 2026-08-14 限频事故只拿到 1086 只、恰好躲过旧阈值 1000 ->
        # 阈值提到 3000, 任何明显缩水都并入全市场池
        if 0 < len(universe) < 3000:
            log.warning("行业成分覆盖偏低(%d只), 并入全市场池补齐 ...", len(universe))
            have = {c for (c, _, _) in universe}
            for (c, n, i) in _full_market_universe():
                if c not in have:
                    universe.append((c, n, i))
        # 成分股全部获取失败(东财实时端点被重置)时, 回退到全市场扫描, 保证流程不空跑
        if len(universe) == 0:
            log.warning("行业成分股获取失败(东财push2被限, 无可用备用成分接口), 回退到全市场扫描。"
                        "行业景气榜仍展示; 但个股缺行业归属, '所属行业/景气加成/行业PE对比'将显示 '—'。")
            universe = _full_market_universe()

    # 行业 PE 中位 (用于基本面对比)
    industry_pe_median = m3.compute_industry_pe_median(spot, ind_to_codes) if ind_to_codes else {}

    # 市场地位 (垄断力代理): 东财行业内 总市值排名/份额。
    # 全量来自快照(行业+总市值都在里面, 零额外请求) — 成分股接口挂掉也不影响
    dom_map = {}
    if spot is not None and not spot.empty and {"industry", "total_mv"} <= set(spot.columns):
        _s = spot[["code", "industry", "total_mv"]].dropna()
        _s = _s[(_s["industry"].astype(str) != "") & (_s["total_mv"] > 0)]
        for ind_name, g in _s.groupby("industry"):
            g = g.sort_values("total_mv", ascending=False).reset_index(drop=True)
            total = float(g["total_mv"].sum())
            for i, r in g.iterrows():
                share = round(float(r["total_mv"]) / total * 100.0, 1) if total > 0 else None
                dom_map[r["code"]] = {"rank": int(i) + 1, "n": int(len(g)), "share": share}
        log.info("市场地位分组: %d 个行业, 覆盖 %d 只", _s["industry"].nunique(), len(dom_map))

    # ---------------- 模块2: 技术扫描 (并发, 阶段A) ----------------
    # 网络IO密集 -> 线程池并发; 只做技术打分, 便宜且快。
    workers = CONFIG["fetch"]["max_workers"] or min(16, (os.cpu_count() or 4) * 2)

    _bench = ds.fetch_benchmark_close()
    if _bench is not None and not _bench.empty:
        # 日期作索引 -> beta() 按日期交集对齐
        bench_close = _bench.set_index(_bench["date"].astype(str))["close"]
    else:
        bench_close = None

    def _scan_stock(code, name, industry):
        h = ds.fetch_hist(code)
        if h is None:
            return None
        rec, detail = m2.scan_one(code, name, h, spot_map.get(code), bench_close=bench_close)
        if rec is None:
            return None
        # 支撑分达标 OR 深跌抄底桶 OR 蓄势待发桶, 三者其一即保留
        if (rec["tech_score"] < CONFIG["tech"]["min_tech_score"]
                and not rec.get("dip") and not rec.get("coil")):
            return None
        rec["industry"] = industry
        return (rec, detail)

    log.info("阶段A 技术扫描: %d 只, 并发 %d 线程 ...", len(universe), workers)
    hits = []
    n_scanned = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_scan_stock, c, n, i) for (c, n, i) in universe]
        for fut in tqdm(as_completed(futures), total=len(futures)):
            _prog["n"] += 1
            n_scanned += 1
            try:
                r = fut.result()
            except Exception as e:
                log.debug("扫描失败: %s", e)
                continue
            if r:
                hits.append(r)
    if ds.bars_from_store_on():
        _st = ds.store_stats()
        log.info("阶段A 取数来源: 本地价格库 %d 只 / 库判退市直接跳过 %d 只 / 回落联网 %d 只 "
                 "(Tushare P1: 回落数应是个位数的次新股)",
                 _st.get("hit", 0), _st.get("skipped", 0), _st.get("fallback", 0))
    log.info("技术命中 %d 只", len(hits))

    # ---------------- 模块3-4: 仅对技术分最高的前N只拉基本面 (阶段B) ----------------
    # 技术分降序; 同分时按代码升序, 保证跨次运行结果确定(否则受线程完成顺序影响)
    hits.sort(key=lambda rd: (-rd[0]["tech_score"], rd[0]["code"]))
    top_hits = hits[:CONFIG["output"]["fund_top_n"]]
    # 并入"深跌抄底"桶: 支撑分排不进 top_hits、但深跌达标的, 按 dip_score 取前 dip_top_n 只补进来。
    # 先剔除已在 top_hits 的再切片(与 export 过滤顺序一致), 让深跌超卖股也能进 final_rank(带 🪸 标签)。
    _seen = {rd[0]["code"] for rd in top_hits}
    dip_pool = sorted([rd for rd in hits if rd[0].get("dip")],
                      key=lambda rd: -rd[0].get("dip_score", 0.0))
    dip_new = [rd for rd in dip_pool if rd[0]["code"] not in _seen][:CONFIG["output"].get("dip_top_n", 40)]
    for rd in dip_new:
        top_hits.append(rd)
        _seen.add(rd[0]["code"])
    log.info("深跌抄底桶: 命中 %d 只, 并入候选 %d 只", len(dip_pool), len(dip_new))
    # 并入"蓄势待发"桶 (与 dip 同构, 排除 dip 重叠与展示过滤同口径)
    coil_pool = sorted([rd for rd in hits if rd[0].get("coil") and not rd[0].get("dip")],
                       key=lambda rd: -rd[0].get("coil_score", 0.0))
    coil_new = [rd for rd in coil_pool if rd[0]["code"] not in _seen][:CONFIG["output"].get("coil_top_n", 40)]
    for rd in coil_new:
        top_hits.append(rd)
        _seen.add(rd[0]["code"])
    log.info("蓄势待发桶: 命中 %d 只, 并入候选 %d 只", len(coil_pool), len(coil_new))
    log.info("阶段B 基本面+交叉打分: 取技术分最高的 %d 只(含深跌/蓄势) ...", len(top_hits))

    # 预热全市场季度业绩批量缓存 (近四季归母/营收同比×4 的数据源), 避免并发首调用
    n_qr = ds.prefetch_quarterly_reports()
    log.info("季度业绩批量缓存: 覆盖 %d 只", n_qr)
    # THS官方单季数预取 (单线程, py_mini_racer不能进线程池; 预算10分钟, 超时走差分兜底)
    try:
        n_ths = ds.prefetch_single_q_ths([rd[0]["code"] for rd in top_hits], budget_sec=600)
        log.info("THS单季官方数预取: %d/%d 只", n_ths, len(top_hits))
    except Exception as e:
        log.warning("THS单季预取失败(全部走业绩表差分兜底): %s", e)

    def _fund_stock(rd):
        rec, detail = rd
        industry = rec.get("industry")
        if not industry:
            # 全市场回退时个股无行业归属: 快照的东财行业列免费全覆盖, 没有再逐只补
            industry = (spot_map.get(rec["code"]) or {}).get("industry")
            if not industry:
                try:
                    industry = ds.fetch_stock_industry(rec["code"])
                except Exception:
                    industry = None
            rec["industry"] = industry
        f = m3.pull_fundamentals(
            rec["code"], industry=industry,
            industry_pe_median=industry_pe_median.get(industry) if industry else None,
            spot_row=spot_map.get(rec["code"]))
        # 市场地位 (行业内市值排名/份额)
        d = dom_map.get(rec["code"])
        if d:
            crown = "👑" if (d["rank"] == 1 and (d["share"] or 0) >= 15) else ""
            share_txt = f" · {d['share']}%" if d["share"] is not None else ""
            f["dominance_disp"] = f"{crown}#{d['rank']}/{d['n']}{share_txt}"
            f["dom_rank"], f["dom_n"], f["dom_share"] = d["rank"], d["n"], d["share"]
        fr = m4.cross_score(rec, f, prosperity_map.get(industry) if industry else None)
        return (rec, detail, f, fr)

    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_fund_stock, rd) for rd in top_hits]
        for fut in tqdm(as_completed(futures), total=len(futures)):
            _prog["n"] += 1
            try:
                results.append(fut.result())
            except Exception as e:
                log.debug("基本面失败: %s", e)
                continue

    # 按综合分排序后落库(同分按代码升序, 结果确定); 详情(K线)只存前 N 只以控制 JS 体积
    results.sort(key=lambda x: (-(x[3]["final_score"] if x[3].get("final_score") is not None else -1),
                                x[0]["code"]))
    detail_n = CONFIG["output"]["dashboard_detail_top_n"]
    show_n = CONFIG["output"].get("final_top_n") or len(results)
    final_records = [x[3] for x in results]
    # export 浮现集合 = 前 show_n 名 + 落榜的 dip/coil 按各自分数补足 (与 export 过滤同口径)
    dip_tail = sorted([fr for fr in final_records[show_n:] if fr.get("dip")],
                      key=lambda fr: -(fr.get("dip_score") or 0.0))[:CONFIG["output"].get("dip_top_n", 40)]
    coil_tail = sorted([fr for fr in final_records[show_n:]
                        if fr.get("coil") and not fr.get("dip")],
                       key=lambda fr: -(fr.get("coil_score") or 0.0))[:CONFIG["output"].get("coil_top_n", 40)]
    # 深跌全池留痕: 恐慌日命中远超top40, 公布的桶胜率实为前40名 — 全量落盘供分位验证
    try:
        import json as _json
        _dp_dir = os.path.join(DATA_DIR, "dip_pool")
        os.makedirs(_dp_dir, exist_ok=True)
        _dp = [{"code": x.get("code"), "dip_score": x.get("dip_score"),
                "drawdown_pct": x.get("drawdown_pct"), "tag": x.get("tag"), "rank": _i + 1}
               for _i, x in enumerate(sorted([x for x in final_records if x.get("dip")],
                                             key=lambda x: -(x.get("dip_score") or 0.0)))]
        with open(os.path.join(_dp_dir, f"{run_date}.json"), "w", encoding="utf-8") as _f:
            _json.dump(_dp, _f, ensure_ascii=False)
    except Exception:
        pass

    shown_extra = ({fr["code"] for fr in final_records[:show_n] if fr.get("dip") or fr.get("coil")}
                   | {fr["code"] for fr in dip_tail} | {fr["code"] for fr in coil_tail})
    for idx, (rec, detail, f, fr) in enumerate(results):
        db.save_tech(run_date, [rec])
        db.save_fundamental(run_date, rec["code"], f)
        db.save_final(run_date, [fr])
        if (idx < detail_n or rec["code"] in shown_extra) and detail:
            db.save_detail(run_date, rec["code"], detail)

    # ---------------- 阶段C1: 买卖点建议 (Trade Plan) ----------------
    # 历史数据走当日缓存(fetch_hist 命中即秒回); coil 股自动走"突破型"剧本。
    # ⚠️ 技术好但基本面弱: 两市完整窗口与M1九年皆为负期望 -> 停发买点 (标签与展示
    # 保留; 回测影子事件照常构建)。复活门(数据化, 杜绝手工翻案): 影子样本
    # n_resolved>=12 且 win10_post>=max(0.55, 全池+3pp) 且 avg_ret>0 时自动恢复发放。
    def _weak_tag_allowed() -> bool:
        try:
            import json as _json
            with open(os.path.join(DATA_DIR, "backtest_result.json"), encoding="utf-8") as _f:
                _agg = (_json.load(_f) or {}).get("agg") or {}
            _s = (_agg.get("by_tag") or {}).get("⚠️ 技术好但基本面弱") or {}
            _p0 = _agg.get("p0") or 0.0
            return ((_s.get("n_resolved") or 0) >= 12
                    and (_s.get("win10_post") or 0.0) >= max(0.55, _p0 + 0.03)
                    and (_s.get("avg_ret") or 0.0) > 0)
        except Exception:
            return False
    _weak_ok = _weak_tag_allowed()
    if not _weak_ok:
        log.info("⚠️标签复活门未达标: 本轮不为该标签生成买卖点 (影子统计继续)")
    plan_targets = [fr for fr in (final_records[:show_n] + dip_tail + coil_tail)
                    if _weak_ok or "基本面弱" not in (fr.get("tag") or "")]
    tech_by_code = {rec["code"]: rec for (rec, _, _, _) in results}
    log.info("阶段C1 买卖点回测: %d 只 ...", len(plan_targets))
    plan_stats = {}
    for fr in tqdm(plan_targets):
        try:
            h = ds.fetch_hist(fr["code"])
            plan_stats[fr["code"]] = tp.compute_event_stats(h) if h is not None else None
        except Exception as e:
            log.debug("买卖点回测 %s 失败: %s", fr["code"], e)
            plan_stats[fr["code"]] = None
    prior = tp.pool_prior([s for s in plan_stats.values() if s])
    log.info("  事件池: 全池 %d 次事件 (先验)", prior.get("n", 0))
    n_plans = 0
    for fr in plan_targets:
        rec = tech_by_code.get(fr["code"])
        if not rec:
            continue
        try:
            plan = tp.build_trade_plan(rec, plan_stats.get(fr["code"]), prior)
            if plan:
                db.save_trade_plan(run_date, fr["code"], plan)
                n_plans += 1
        except Exception as e:
            log.debug("买卖点生成 %s 失败: %s", fr["code"], e)
    log.info("  买卖点建议: %d 只已生成", n_plans)

    # ---------------- 模块6: 个股深度档案 (阶段C) ----------------
    # 仅对最终展示的候选生成: 简介/主营构成/营收增速/现金流+漏洞/风险/新闻/两融/龙虎榜/大宗
    # 注: akshare 部分东财接口用 py_mini_racer(V8) 解密, 多线程会崩 -> 单线程串行。
    # 深度档案只为可操作标签生成 (用户指定: 仅 强左侧 + 蓄势待发) —
    # 观察/基本面弱 占榜单大头但很少被点开, 砍掉后阶段C耗时降 ~2/3, 限频压力大减
    _prof_pool = final_records[:show_n] + dip_tail + coil_tail
    prof_targets = [fr for fr in _prof_pool
                    if ("强左侧" in (fr.get("tag") or "")) or ("蓄势待发" in (fr.get("tag") or ""))]
    log.info("深度档案范围: 强左侧+蓄势待发 %d 只 (榜单共 %d)", len(prof_targets), len(_prof_pool))
    # 时间预算: 东财F10被限频时单只档案可能要几分钟, 无预算会让整轮永远跑不完、
    # 计划任务被1/4小时上限杀掉 → 网站断更(2026-07/08 两度发生的根因)。
    # 预算内尽量拉新档案; 超时/失败的股票回落到库里最近一天的档案(公司简介/年报数据变化很慢)。
    _budget_sec = CONFIG["output"].get("profile_budget_min", 45) * 60
    _t0 = time.time()
    log.info("阶段C 深度档案: %d 只 (主营/现金流/新闻/两融/大宗) 单线程, 预算 %d 分钟 ...",
             len(prof_targets), _budget_sec // 60)
    _done_codes = set()

    # 单只硬期限: pull_profile 内部可能出现任何超时都覆盖不到的阻塞 (对端滴流钓连接等,
    # 2026-08-31 阶段C 首只挂死25分钟, 看门狗只能杀全程 → 当日榜单没发布)。
    # 放守护线程里等 180s; 超时即判定源站不可用, 直接结束阶段C走回落 —— 不逐只重试:
    # 东财F10走 V8 解密非线程安全, 弃掉的卡死线程若复活会与新调用并发, 必须避免。
    def _pull_with_deadline(code, sector, deadline_sec=180):
        import queue as _q
        import threading as _th
        box = _q.Queue(maxsize=1)
        def _run():
            try:
                box.put((True, m6.pull_profile(code, sector=sector)))
            except Exception as e:      # noqa: BLE001
                box.put((False, e))
        _th.Thread(target=_run, daemon=True, name=f"prof-{code}").start()
        try:
            ok, val = box.get(timeout=deadline_sec)
        except _q.Empty:
            raise TimeoutError(f"pull_profile {code} 超过 {deadline_sec}s 硬期限")
        if ok:
            return val
        raise val

    for fr in tqdm(prof_targets):
        if time.time() - _t0 > _budget_sec:
            log.warning("深度档案超出时间预算, 已拉 %d/%d, 其余回落到最近档案",
                        len(_done_codes), len(prof_targets))
            break
        try:
            p = _pull_with_deadline(fr["code"], fr.get("industry"))
            db.save_profile(run_date, fr["code"], p)
            if p.get("summary") or (p.get("revenue") or {}).get("years"):
                _done_codes.add(fr["code"])
        except TimeoutError as e:
            log.warning("深度档案挂死: %s — 判定源站不可用, 提前结束阶段C, 其余回落", e)
            break
        except Exception as e:
            log.debug("深度档案失败 %s: %s", fr["code"], e)
        # 心跳: 阶段C成功时不打日志, 曾致 25min 零日志被看门狗误杀 (服务器单只10-13s
        # ×182只=36min > 25min 呆滞线; PC 网络快从未触发) — 2026-08-31 第三跑 130/182 冤死
        _prog["n"] += 1
    # 回落: 没拉到(或全空)的股票, 用库里最近一个run_date的档案顶上
    _miss = [fr["code"] for fr in prof_targets if fr["code"] not in _done_codes]
    n_fb = db.backfill_profiles_from_latest(run_date, _miss) if _miss else 0
    log.info("深度档案: 新拉 %d, 回落补齐 %d, 缺口 %d",
             len(_done_codes), n_fb, len(_miss) - n_fb)

    data_date = str(_bench["date"].iloc[-1]) if (_bench is not None and not _bench.empty) else run_date
    finished = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.log_run(run_date, started, finished, n_scanned, len(final_records),
               selected_inds, "ok", data_date=data_date)
    log.info("扫描完成: 扫描 %d, 命中 %d", n_scanned, len(final_records))

    # ---------------- 导出仪表盘 ----------------
    ex.write_dashboard_js(run_date)
    ex.write_csv(run_date)
    ex.write_history_snapshot(run_date)
    try:
        ex.write_watch_js()
        ex.write_starmap_js()
    except Exception as e:
        log.warning("watch_data 导出失败: %s", e)
    try:
        from ashare import backtest as bt
        bt.run_backtest()
    except Exception as e:
        log.warning("信号回测失败(不影响榜单与发布): %s", e, exc_info=True)
    try:
        from ashare import quality as ql
        ql.build_quality()
    except Exception as e:
        log.warning("优质榜构建失败(不影响榜单与发布): %s", e, exc_info=True)
    try:
        from ashare import paper
        paper.update_portfolio()
    except Exception as e:
        log.warning("自动模拟组合更新失败(不影响榜单与发布): %s", e, exc_info=True)
    try:
        from ashare import biweekly
        biweekly.update()
    except Exception as e:
        log.warning("双周组合更新失败(不影响榜单与发布): %s", e, exc_info=True)
    log.info("✅ 全部完成。请双击打开 dashboard/index.html")


def main():
    ap = argparse.ArgumentParser(description="A股左侧支撑位筛选 + 监控")
    ap.add_argument("--full-market", action="store_true", help="跳过行业筛选, 扫描全市场")
    ap.add_argument("--demo", action="store_true", help="离线合成数据演示 (不联网)")
    ap.add_argument("--no-cache", action="store_true", help="禁用本地缓存")
    args = ap.parse_args()

    if args.demo:
        from make_demo_data import build_demo
        build_demo()
        return

    t0 = time.time()
    try:
        run(full_market=args.full_market, use_cache=not args.no_cache)
    except KeyboardInterrupt:
        log.warning("用户中断")
        sys.exit(1)
    log.info("耗时 %.1f 秒", time.time() - t0)


if __name__ == "__main__":
    main()
