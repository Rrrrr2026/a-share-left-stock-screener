#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tushare(兼容镜像) **全目录**只读探针  ——  数据湖设计前置调研
=============================================================
`tools/tushare_probe.py` 只覆盖 P0/P1 要用的 ~30 个端点; 老板买了 15,000 积分档并要求
"把 tushare 上面能拉的数据全拉过来", 于是本脚本把 **股票 + 指数** 的整个 Pro 目录扫一遍,
逐端点给出 有权限/无权限/参数不合法/失败, 以及 行数 · 耗时 · 字段名 —— 这是
`stock-core/design/data_lake_design.md` 的事实底座。

铁律 (与本仓库其它 Tushare 代码一致):
  · token 只经 `ashare/tushare_client.py` 从 `data/secrets.json` 读, **永不打印/入库/进 git**
  · **只读**: 不落任何生产库, 结果只写 `data/tushare_probe_full.json` (data/ 已 gitignore)
  · **轻探**: 每端点 **一次** 最小调用, 默认 `limit=10`; 默认限频 60 次/分 + 每次 0.6s 间隔,
    因为 P1 价格库重建常与本脚本同机同 token 并跑, 探针不许把重建挤下去
  · 分钟线只探 **一只股票 · 一天**; 全市场按日的端点只在"行数即容量估算依据"时才不限行
    (FULL 标记, 共 ~10 个, 都是单页返回)

用法:
    python tools/tushare_probe_full.py                     # 全量 (~130 端点, 5-10 分钟)
    python tools/tushare_probe_full.py --domain 财务,参考   # 只探某几域
    python tools/tushare_probe_full.py --only vip          # 名字含 vip 的
    python tools/tushare_probe_full.py --gap 1.2 --rate 40 # 更客气 (重建正忙时)
    python tools/tushare_probe_full.py --list              # 只列清单不联网
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from ashare import tushare_client as tsc  # noqa: E402

OUT_JSON = os.path.join(ROOT, "data", "tushare_probe_full.json")

# ---------------------------------------------------------------- 探针清单
# (域, 名称, api, params, flags)
#   flags: "FULL" = 不加 limit (行数本身就是容量估算依据, 且该端点单页返回)
#          "SLOW" = 已知慢 (放宽硬期限)
#          "MIN"  = 分钟线, 只探一股一天
# 占位符 {LAST}=最近已收盘交易日 {LAST_DT}=同一天的 YYYY-MM-DD 形式 {D10}=十日前 {PERIOD}=最近完整报告期

TESTS = [
    # ---------------- 1. 基础 ----------------
    ("基础", "stock_basic 在市", "stock_basic", dict(list_status="L"), ""),
    ("基础", "stock_basic 退市", "stock_basic", dict(list_status="D"), ""),
    ("基础", "stock_basic 暂停", "stock_basic", dict(list_status="P"), ""),
    ("基础", "trade_cal", "trade_cal", dict(exchange="SSE", start_date="{D10}", end_date="{LAST}"), "FULL"),
    ("基础", "namechange", "namechange", dict(ts_code="000001.SZ"), ""),
    ("基础", "hs_const 沪股通", "hs_const", dict(hs_type="SH"), ""),
    ("基础", "stock_company", "stock_company", dict(ts_code="600519.SH"), ""),
    ("基础", "stk_managers", "stk_managers", dict(ts_code="600519.SH"), ""),
    ("基础", "stk_rewards", "stk_rewards", dict(ts_code="600519.SH"), ""),
    ("基础", "new_share 新股", "new_share", dict(start_date="20260101", end_date="{LAST}"), ""),
    ("基础", "bak_basic 备用基础", "bak_basic", dict(trade_date="{LAST}"), ""),
    ("基础", "stk_premarket 盘前", "stk_premarket", dict(trade_date="{LAST}"), ""),

    # ---------------- 2. 行情 ----------------
    ("行情", "daily 单股10日", "daily", dict(ts_code="000001.SZ", start_date="{D10}", end_date="{LAST}"), ""),
    ("行情", "weekly 周线", "weekly", dict(ts_code="000001.SZ", start_date="20260101", end_date="{LAST}"), ""),
    ("行情", "monthly 月线", "monthly", dict(ts_code="000001.SZ", start_date="20250101", end_date="{LAST}"), ""),
    ("行情", "adj_factor 单股", "adj_factor", dict(ts_code="000001.SZ", start_date="{D10}", end_date="{LAST}"), ""),
    ("行情", "daily_basic 全市场一日", "daily_basic", dict(trade_date="{LAST}"), "FULL"),
    ("行情", "moneyflow 个股资金流", "moneyflow", dict(trade_date="{LAST}"), "FULL"),
    ("行情", "moneyflow_dc 东财资金流", "moneyflow_dc", dict(trade_date="{LAST}"), "FULL"),
    ("行情", "moneyflow_ths 同花顺资金流", "moneyflow_ths", dict(trade_date="{LAST}"), ""),
    ("行情", "moneyflow_mkt_dc 大盘资金", "moneyflow_mkt_dc", dict(trade_date="{LAST}"), ""),
    ("行情", "moneyflow_hsgt 沪深港通", "moneyflow_hsgt", dict(start_date="{D10}", end_date="{LAST}"), ""),
    ("行情", "hsgt_top10 陆股通十大", "hsgt_top10", dict(trade_date="{LAST}"), ""),
    ("行情", "ggt_daily 港股通", "ggt_daily", dict(start_date="{D10}", end_date="{LAST}"), ""),
    ("行情", "ggt_top10 港股通十大", "ggt_top10", dict(trade_date="{LAST}"), ""),
    ("行情", "stk_limit 涨跌停价", "stk_limit", dict(trade_date="{LAST}"), "FULL"),
    ("行情", "suspend_d 停复牌", "suspend_d", dict(trade_date="{LAST}"), "FULL"),
    ("行情", "bak_daily 备用行情", "bak_daily", dict(trade_date="{LAST}"), ""),
    ("行情", "stk_factor 技术因子", "stk_factor", dict(ts_code="000001.SZ", start_date="{D10}", end_date="{LAST}"), ""),
    ("行情", "stk_factor_pro 因子加强", "stk_factor_pro", dict(ts_code="000001.SZ", start_date="{D10}", end_date="{LAST}"), "SLOW"),
    ("行情", "cyq_perf 每日筹码", "cyq_perf", dict(trade_date="{LAST}"), "FULL"),
    ("行情", "cyq_chips 筹码分布", "cyq_chips", dict(ts_code="000001.SZ", trade_date="{LAST}"), ""),
    ("行情", "stk_nineturn 神奇九转", "stk_nineturn", dict(ts_code="000001.SZ", start_date="{D10}", end_date="{LAST}"), ""),
    ("行情", "stk_surv 机构调研", "stk_surv", dict(trade_date="{LAST}"), ""),
    ("行情", "stk_mins 1分钟(一股一天)", "stk_mins",
     dict(ts_code="000001.SZ", freq="1min", start_date="{LAST_DT} 09:30:00", end_date="{LAST_DT} 15:00:00"), "MIN FULL"),
    ("行情", "stk_mins 5分钟(同股同天)", "stk_mins",
     dict(ts_code="000001.SZ", freq="5min", start_date="{LAST_DT} 09:30:00", end_date="{LAST_DT} 15:00:00"), "MIN FULL"),
    ("行情", "rt_k 实时K(仅探)", "rt_k", dict(ts_code="000001.SZ"), ""),

    # ---------------- 3. 指数 / 行业 ----------------
    ("指数", "index_basic 指数列表", "index_basic", dict(market="SSE"), ""),
    ("指数", "index_daily 沪深300", "index_daily", dict(ts_code="000300.SH", start_date="{D10}", end_date="{LAST}"), ""),
    ("指数", "index_weekly", "index_weekly", dict(ts_code="000300.SH", start_date="20260101", end_date="{LAST}"), ""),
    ("指数", "index_monthly", "index_monthly", dict(ts_code="000300.SH", start_date="20250101", end_date="{LAST}"), ""),
    ("指数", "index_weight 成分权重", "index_weight", dict(index_code="000300.SH", start_date="20260801", end_date="{LAST}"), ""),
    ("指数", "index_dailybasic 指数估值", "index_dailybasic", dict(trade_date="{LAST}"), "FULL"),
    ("指数", "index_classify 申万分类", "index_classify", dict(level="L1", src="SW2021"), "FULL"),
    ("指数", "index_member_all 申万成分", "index_member_all", dict(l1_code="801010.SI"), ""),
    ("指数", "sw_daily 申万行情", "sw_daily", dict(ts_code="801010.SI", start_date="{D10}", end_date="{LAST}"), ""),
    ("指数", "ths_index 同花顺板块", "ths_index", dict(exchange="A", type="N"), "FULL"),
    ("指数", "ths_member 同花顺成分", "ths_member", dict(ts_code="885800.TI"), ""),
    ("指数", "ths_daily 同花顺行情", "ths_daily", dict(ts_code="885800.TI", start_date="{D10}", end_date="{LAST}"), ""),
    ("指数", "ths_hot 同花顺热榜", "ths_hot", dict(trade_date="{LAST}"), ""),
    ("指数", "dc_index 东财板块", "dc_index", dict(trade_date="{LAST}"), "FULL"),
    ("指数", "dc_member 东财成分", "dc_member", dict(trade_date="{LAST}", ts_code="BK1184.DC"), ""),
    ("指数", "dc_daily 东财板块行情", "dc_daily", dict(ts_code="BK1184.DC", start_date="{D10}", end_date="{LAST}"), ""),
    ("指数", "dc_hot 东财热榜", "dc_hot", dict(trade_date="{LAST}"), ""),
    ("指数", "ci_daily 中信行业", "ci_daily", dict(trade_date="{LAST}"), ""),
    ("指数", "idx_factor_pro 指数因子", "idx_factor_pro", dict(ts_code="000300.SH", start_date="{D10}", end_date="{LAST}"), "SLOW"),
    ("指数", "moneyflow_ind_dc 行业资金", "moneyflow_ind_dc", dict(trade_date="{LAST}"), "FULL"),
    ("指数", "moneyflow_ind_ths 行业资金", "moneyflow_ind_ths", dict(trade_date="{LAST}"), ""),
    ("指数", "moneyflow_cnt_ths 概念资金", "moneyflow_cnt_ths", dict(trade_date="{LAST}"), ""),

    # ---------------- 4. 财务 ----------------
    ("财务", "income 单股", "income", dict(ts_code="600519.SH", start_date="20240101"), ""),
    ("财务", "income_vip 按期", "income_vip", dict(period="{PERIOD}"), ""),
    ("财务", "balancesheet 单股", "balancesheet", dict(ts_code="600519.SH", start_date="20240101"), ""),
    ("财务", "balancesheet_vip 按期", "balancesheet_vip", dict(period="{PERIOD}"), ""),
    ("财务", "cashflow 单股", "cashflow", dict(ts_code="600519.SH", start_date="20240101"), ""),
    ("财务", "cashflow_vip 按期", "cashflow_vip", dict(period="{PERIOD}"), ""),
    ("财务", "fina_indicator 单股", "fina_indicator", dict(ts_code="600519.SH", start_date="20240101"), ""),
    ("财务", "fina_indicator_vip 按期", "fina_indicator_vip", dict(period="{PERIOD}"), "SLOW"),
    ("财务", "forecast 单股", "forecast", dict(ts_code="600519.SH"), ""),
    ("财务", "forecast 按期", "forecast", dict(period="{PERIOD}"), ""),
    ("财务", "forecast_vip 按期", "forecast_vip", dict(period="{PERIOD}"), ""),
    ("财务", "express 单股", "express", dict(ts_code="600519.SH"), ""),
    ("财务", "express_vip 按期", "express_vip", dict(period="{PERIOD}"), ""),
    ("财务", "dividend 分红", "dividend", dict(ts_code="600519.SH"), ""),
    ("财务", "fina_audit 审计", "fina_audit", dict(ts_code="600519.SH"), ""),
    ("财务", "fina_mainbz 主营", "fina_mainbz", dict(ts_code="600519.SH", period="20241231"), ""),
    ("财务", "fina_mainbz_vip 按期", "fina_mainbz_vip", dict(period="20241231"), ""),
    ("财务", "disclosure_date 预约披露", "disclosure_date", dict(end_date="{PERIOD}"), ""),

    # ---------------- 5. 参考 / 股东 ----------------
    ("参考", "top10_holders 十大股东", "top10_holders", dict(ts_code="600519.SH", period="20241231"), ""),
    ("参考", "top10_floatholders 十大流通", "top10_floatholders", dict(ts_code="600519.SH", period="20241231"), ""),
    ("参考", "stk_holdernumber 股东户数", "stk_holdernumber", dict(ts_code="600519.SH"), ""),
    ("参考", "stk_holdertrade 股东增减持", "stk_holdertrade", dict(ann_date="{LAST}"), "FULL"),
    ("参考", "pledge_stat 质押统计", "pledge_stat", dict(ts_code="600519.SH"), ""),
    ("参考", "pledge_detail 质押明细", "pledge_detail", dict(ts_code="002456.SZ"), ""),
    ("参考", "repurchase 回购", "repurchase", dict(start_date="20260101", end_date="{LAST}"), ""),
    ("参考", "share_float 限售解禁", "share_float", dict(ann_date="{LAST}"), ""),
    ("参考", "concept 概念列表", "concept", dict(src="ts"), "FULL"),   # 该镜像 50101 无此端点, 概念改走 ths_index/dc_index
    ("参考", "concept_detail 概念成分", "concept_detail", dict(id="TS0"), ""),
    ("参考", "report_rc 卖方预测", "report_rc", dict(report_date="{LAST}"), ""),
    ("参考", "broker_recommend 券商金股", "broker_recommend", dict(month="202608"), ""),
    ("参考", "stk_account 投资者账户", "stk_account", dict(start_date="20250101", end_date="{LAST}"), ""),
    ("参考", "hk_hold 沪深股通持股", "hk_hold", dict(trade_date="{LAST}"), "FULL"),
    ("参考", "ccass_hold 中央结算", "ccass_hold", dict(trade_date="{LAST}"), ""),

    # ---------------- 6. 特色 / 资金 ----------------
    ("特色", "margin 两融汇总", "margin", dict(trade_date="{LAST}"), ""),
    ("特色", "margin_detail 两融明细", "margin_detail", dict(trade_date="{LAST}"), "FULL"),
    ("特色", "margin_secs 标的证券", "margin_secs", dict(trade_date="{LAST}"), ""),
    ("特色", "top_list 龙虎榜", "top_list", dict(trade_date="{LAST}"), "FULL"),
    ("特色", "top_inst 龙虎榜机构", "top_inst", dict(trade_date="{LAST}"), "FULL"),
    ("特色", "block_trade 大宗", "block_trade", dict(trade_date="{LAST}"), "FULL"),
    ("特色", "limit_list_d 涨跌停板", "limit_list_d", dict(trade_date="{LAST}"), "FULL"),
    ("特色", "limit_list_ths 同花顺涨停", "limit_list_ths", dict(trade_date="{LAST}"), ""),
    ("特色", "limit_step 连板天梯", "limit_step", dict(trade_date="{LAST}"), ""),
    ("特色", "limit_cpt_list 涨停最强板块", "limit_cpt_list", dict(trade_date="{LAST}"), ""),
    ("特色", "kpl_list 开盘啦榜单", "kpl_list", dict(trade_date="{LAST}", tag="涨停"), ""),
    ("特色", "kpl_concept 开盘啦题材", "kpl_concept", dict(trade_date="{LAST}"), ""),
    ("特色", "kpl_concept_cons 题材成分", "kpl_concept_cons", dict(trade_date="{LAST}"), ""),
    ("特色", "hm_list 游资名录", "hm_list", dict(), ""),
    ("特色", "hm_detail 游资明细", "hm_detail", dict(trade_date="{LAST}"), ""),
    ("特色", "stk_auction_c 集合竞价", "stk_auction_c", dict(trade_date="{LAST}"), ""),
    ("特色", "stk_auction_o 开盘竞价", "stk_auction_o", dict(trade_date="{LAST}"), ""),

    # ---------------- 7. 文本 ----------------
    ("文本", "news 新闻快讯", "news", dict(src="sina", start_date="{LAST_DT} 09:00:00", end_date="{LAST_DT} 10:00:00"), ""),
    ("文本", "major_news 长篇新闻", "major_news", dict(start_date="{LAST_DT} 09:00:00", end_date="{LAST_DT} 12:00:00"), ""),
    ("文本", "cctv_news 新闻联播", "cctv_news", dict(date="{LAST}"), ""),
    ("文本", "anns_d 公告", "anns_d", dict(ann_date="{LAST}"), ""),
    ("文本", "irm_qa_sz 互动易", "irm_qa_sz", dict(trade_date="{LAST}"), ""),

    # ---------------- 8. 宏观 (可选) ----------------
    ("宏观", "shibor", "shibor", dict(start_date="{D10}", end_date="{LAST}"), ""),
    ("宏观", "shibor_lpr", "shibor_lpr", dict(start_date="20260101", end_date="{LAST}"), ""),
    ("宏观", "cn_gdp", "cn_gdp", dict(start_q="2025Q1"), ""),
    ("宏观", "cn_cpi", "cn_cpi", dict(start_m="202501"), ""),
    ("宏观", "cn_ppi", "cn_ppi", dict(start_m="202501"), ""),
    ("宏观", "cn_m 货币供应", "cn_m", dict(start_m="202501"), ""),
    ("宏观", "cn_pmi", "cn_pmi", dict(start_m="202501"), ""),
    ("宏观", "cn_sf 社融", "cn_sf", dict(start_m="202501"), ""),
    ("宏观", "us_tycr 美债收益率", "us_tycr", dict(start_date="{D10}", end_date="{LAST}"), ""),

    # ---------------- 9. 港美股 (可选) ----------------
    ("港美", "hk_basic 港股列表", "hk_basic", dict(list_status="L"), ""),
    ("港美", "hk_daily 港股日线", "hk_daily", dict(ts_code="00700.HK", start_date="{D10}", end_date="{LAST}"), ""),
    ("港美", "hk_tradecal 港股日历", "hk_tradecal", dict(start_date="{D10}", end_date="{LAST}"), ""),
    ("港美", "hk_mins 港股分钟", "hk_mins", dict(ts_code="00700.HK", freq="1min",
                                            start_date="{LAST_DT} 09:30:00", end_date="{LAST_DT} 10:00:00"), "MIN"),
    ("港美", "us_basic 美股列表", "us_basic", dict(), ""),
    ("港美", "us_daily 美股日线", "us_daily", dict(ts_code="AAPL", start_date="{D10}", end_date="{LAST}"), ""),
    ("港美", "us_tradecal 美股日历", "us_tradecal", dict(start_date="{D10}", end_date="{LAST}"), ""),
    ("港美", "us_daily_adj 美股复权", "us_daily_adj", dict(ts_code="AAPL", start_date="{D10}", end_date="{LAST}"), ""),
]


# ---------------------------------------------------------------- 运行
def resolve_dates(last_override: str = "", period_override: str = "") -> dict:
    """最近已收盘交易日 / 十日前 / 最近完整报告期。全部走北京日历, 不做时区换算。

    `--last` 可回拨基准日: T+1 才发布的数据集 (两融/沪深股通持股/结算持仓) 用当日探会得空表,
    看着像"没权限", 其实只是还没出。判"有没有" 一律用回拨 3-5 个交易日的日期复探。
    """
    import datetime as dt
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
    start = (today - dt.timedelta(days=40)).strftime("%Y%m%d")
    days = []
    try:
        days = tsc.trade_cal(start, today.strftime("%Y%m%d"))
    except Exception as e:                                  # noqa: BLE001
        print(f"trade_cal 失败 ({type(e).__name__}), 用兜底日期", file=sys.stderr)
    days = [d for d in days if d < today.isoformat()]       # 只用**已收盘**的日子
    last = days[-1].replace("-", "") if days else (today - dt.timedelta(days=3)).strftime("%Y%m%d")
    d10 = days[-11].replace("-", "") if len(days) >= 11 else start
    # 最近**已过法定截止**的完整报告期 (半年报 8/31, 三季报 10/31, 年报 4/30)
    y, m = today.year, today.month
    period = f"{y}0630" if m >= 9 else (f"{y}0331" if m >= 5 else f"{y - 1}0930")
    if m >= 11:
        period = f"{y}0930"
    if last_override:
        last = last_override.replace("-", "")
        idx = [i for i, d in enumerate(days) if d.replace("-", "") == last]
        d10 = days[max(0, idx[0] - 10)].replace("-", "") if idx else d10
    return {"LAST": last, "D10": d10, "PERIOD": period_override or period,
            "LAST_DT": f"{last[:4]}-{last[4:6]}-{last[6:8]}"}


def fill(params: dict, ctx: dict) -> dict:
    out = {}
    for k, v in params.items():
        s = str(v)
        for key, val in ctx.items():
            s = s.replace("{" + key + "}", val)
        out[k] = s
    return out


def probe(api: str, params: dict, flags: str, limit: int):
    """一次最小调用 -> (状态, 行数, 秒, 备注/字段)。不重试, 分类即结论。"""
    p = dict(params)
    if "FULL" not in flags and limit:
        p.setdefault("limit", str(limit))
    deadline = 90.0 if "SLOW" in flags else 45.0
    t0 = time.time()
    try:
        df = tsc.query(api, retries=1, deadline_sec=deadline, **p)
    except tsc.TushareNoPermission as e:
        return "NOPERM", 0, round(time.time() - t0, 1), str(e)[len(api) + 2:][:70]
    except tsc.TushareBadParams as e:
        return "BADPARAM", 0, round(time.time() - t0, 1), str(e)[len(api) + 2:][:70]
    except tsc.TushareRateLimited as e:
        return "RATE", 0, round(time.time() - t0, 1), str(e)[len(api) + 2:][:70]
    except tsc.TushareUnavailable as e:
        return "NOTOKEN", 0, round(time.time() - t0, 1), str(e)[:70]
    except tsc.TushareTransport as e:
        # 50101 "请指定正确的接口名" = 该镜像根本没有这个端点 (与"有端点但没权限"是两回事);
        # 403 "您暂时无法使用该接口" = 有端点无权限。两者都不该被当成网络抖动。
        code = getattr(e, "code", None)
        kind = "NOAPI" if code == 50101 else ("NOPERM" if code == 403 else "FAIL")
        return kind, 0, round(time.time() - t0, 1), str(e)[len(api) + 2:][:70]
    except Exception as e:                                  # noqa: BLE001
        return "FAIL", 0, round(time.time() - t0, 1), f"{type(e).__name__}: {str(e)[:60]}"
    sec = round(time.time() - t0, 1)
    cols = list(df.columns)
    status = "OK" if len(df) else "EMPTY"
    return status, len(df), sec, ",".join(cols[:8]) + (f" …+{len(cols) - 8}" if len(cols) > 8 else "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", default="", help="逗号分隔, 只探这些域 (基础/行情/指数/财务/参考/特色/文本/宏观/港美)")
    ap.add_argument("--only", default="", help="名称或 api 含该子串才探")
    ap.add_argument("--gap", type=float, default=0.6, help="每次调用之间的静默秒数 (默认 0.6)")
    ap.add_argument("--rate", type=float, default=60.0, help="本进程限频 次/分 (默认 60, 给并跑的重建让路)")
    ap.add_argument("--limit", type=int, default=10, help="非 FULL 端点的 limit (默认 10)")
    ap.add_argument("--list", action="store_true", help="只打印清单, 不联网")
    ap.add_argument("--last", default="", help="基准交易日回拨 (T+1 数据集复探用, 如 20260901)")
    ap.add_argument("--period", default="", help="报告期覆盖 (如 20250630)")
    ap.add_argument("--out", default=OUT_JSON)
    args = ap.parse_args()

    tests = TESTS
    if args.domain:
        want = {d.strip() for d in args.domain.split(",") if d.strip()}
        tests = [t for t in tests if t[0] in want]
    if args.only:
        k = args.only.lower()
        tests = [t for t in tests if k in t[1].lower() or k in t[2].lower()]

    if args.list:
        for dom, name, api, params, flags in tests:
            print(f"{dom:<5}{name:<26}{api:<22}{flags}")
        print(f"\n共 {len(tests)} 个端点")
        return 0

    if not tsc.available():
        print("data/secrets.json 里没有 tushare_token —— 无法探针", file=sys.stderr)
        return 2
    tsc._BUCKET = tsc.TokenBucket(args.rate)                # 探针要比生产客气

    ctx = resolve_dates(args.last, args.period)
    print(f"基准: 最近已收盘交易日 {ctx['LAST']} · 十日前 {ctx['D10']} · 报告期 {ctx['PERIOD']}")
    print(f"策略: 每端点 1 次调用, 非 FULL 加 limit={args.limit}, 限频 {args.rate}/min + 间隔 {args.gap}s\n")
    print(f"{'域':<4}{'端点':<28}{'结果':<9}{'行数':>7}{'秒':>7}  字段/说明")
    print("-" * 118)

    rows, t_start = [], time.time()
    for i, (dom, name, api, params, flags) in enumerate(tests):
        p = fill(params, ctx)
        status, n, sec, info = probe(api, p, flags, args.limit)
        mark = "" if "FULL" in flags else "≤lim"
        print(f"{dom:<4}{name:<28}{status:<9}{n:>7}{sec:>6}s  {info}")
        rows.append({"domain": dom, "name": name, "api": api, "params": p, "flags": flags,
                     "status": status, "rows": n, "capped": mark == "≤lim", "sec": sec, "info": info})
        if i < len(tests) - 1:
            time.sleep(args.gap)

    ok = [r for r in rows if r["status"] == "OK"]
    empty = [r for r in rows if r["status"] == "EMPTY"]
    noperm = [r for r in rows if r["status"] == "NOPERM"]
    noapi = [r for r in rows if r["status"] == "NOAPI"]
    bad = [r for r in rows if r["status"] == "BADPARAM"]
    other = [r for r in rows if r["status"] in ("FAIL", "RATE", "NOTOKEN")]
    print("-" * 118)
    print(f"合计 {len(rows)} 个端点, 用时 {round(time.time() - t_start)}s: OK {len(ok)} / 空表 {len(empty)} / "
          f"无权限 {len(noperm)} / 无此端点 {len(noapi)} / 参数不合法 {len(bad)} / 其它失败 {len(other)}")
    print("  注: 空表 ≠ 无权限 —— T+1 才发布的数据集用当日探必空, 请 --last 回拨 3-5 个交易日复探")
    for label, group in (("无权限", noperm), ("无此端点", noapi), ("参数不合法", bad),
                         ("空表(待复探)", empty), ("其它失败", other)):
        if group:
            print(f"  {label}: " + ", ".join(f"{r['api']}" for r in group))

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"probed_at": time.strftime("%Y-%m-%d %H:%M:%S"), "context": ctx,
                   "limit": args.limit, "results": rows}, f, ensure_ascii=False, indent=1)
    print(f"明细 -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
