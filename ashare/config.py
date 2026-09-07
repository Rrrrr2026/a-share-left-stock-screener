#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中央配置 (Central CONFIG)
=========================
所有阈值 / 权重 / 行业数量 / 股票池开关 / token 都集中在这里。
改这里就能改变全流程的结果 (满足验收标准 #4)。

英文注释/中文注释皆可;但仪表盘与导出的 *用户可见文字* 必须为简体中文。
"""

from __future__ import annotations
import os

# 项目根目录 (this file is .../a-share-left-screener/ashare/config.py)
PKG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(PKG_DIR)

# ---- 共用核心 (leftside_core): 两个筛选器的回测/错杀/新闻标记/计划/指标等共用一份代码 ----
# 目录: 环境变量 STOCK_CORE_DIR, 否则本仓库的同级目录 ../stock-core
import sys as _sys
# 查找顺序: STOCK_CORE_DIR -> 同级 ../stock-core -> 本仓库内置副本 vendor/ (auto_update.bat 每次
# 从 stock-core 同步过来并一起提交, 所以新克隆出来的仓库不依赖本机目录也能跑)
_CORE = os.environ.get("STOCK_CORE_DIR") or os.path.join(os.path.dirname(ROOT_DIR), "stock-core")
if not os.path.isdir(os.path.join(_CORE, "leftside_core")):
    _CORE = os.path.join(ROOT_DIR, "vendor")
if os.path.isdir(os.path.join(_CORE, "leftside_core")) and _CORE not in _sys.path:
    _sys.path.insert(0, _CORE)
DATA_DIR = os.path.join(ROOT_DIR, "data")
DASHBOARD_DIR = os.path.join(ROOT_DIR, "dashboard")

os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "ashare.db")
# 仪表盘读取的数据文件 (导出为 JS, 直接 <script> 引入, 双击 HTML 即可打开, 无需服务器)
DASHBOARD_DATA_JS = os.path.join(DASHBOARD_DIR, "dashboard_data.js")


CONFIG = {
    # =====================================================================
    #  数据源 (Data sources)
    # =====================================================================
    "source": {
        "primary": "akshare",          # akshare (免费, 全市场)
        # 日线/股票池主源开关 (Tushare 适配 P1): "tushare"(现状) | "fuyao"(回滚: 腾讯/fuyao/东财一族)
        # 这一个开关同时改道四处 (免得半切半不切混口径):
        #   ① datasource.fetch_hist / fetch_long_hist —— 阶段A 直读 data/pricestore.db, 不再逐股联网
        #   ② market.fetch_bars_by_date / fetch_adj_by_date / trading_days —— 价格库按日增量
        #   ③ market.fetch_bars_bulk (逐股长历史)  ④ market.universe_codes -> stock_basic
        # **2026-09-07 P1 换库: 默认翻到 tushare** (库已换成 Tushare 单源 schema v2, 八道验收门全过)。
        # 回滚 = 把默认改回 "fuyao" 并把 data/pricestore_fuyao_bak.db 换回来 —— 两件事必须一起做:
        # v2 库配 fuyao 开关会让 update_daily 拒绝更新 (见 pricestore._update_daily_legacy 的守卫)。
        "bars": os.environ.get("ASHARE_BARS_SOURCE", "tushare"),
        "tushare_token": os.environ.get("TUSHARE_TOKEN", ""),  # 可选, 留空则只用 akshare
        "industry_classification": "东财",   # 行业分类口径: 东财(EastMoney). akshare 的 board_industry_* 即东财一级行业
        "benchmark_index": "sh000300",  # 沪深300, 用于超额收益基准
        "cache_dir": os.path.join(DATA_DIR, "cache"),
        "cache_ttl_hours": 12,          # 行情/财务数据本地缓存有效期 (小时)
        "use_cache": True,
    },

    # =====================================================================
    #  抓取行为 (Fetch behaviour) —— 限频 + 重试 + 跳过失败
    # =====================================================================
    "fetch": {
        "lookback_days": 500,           # 拉取最近多少日历日的日线 (≈330 交易日, 需 >MA250)
        "adjust": "qfq",                # 前复权
        "sleep_sec": 0.05,              # 每次接口调用之间 sleep, 防限频(并发下调小)
        "max_retries": 2,               # 单次接口失败重试次数 (akshare 内部已自带重试)
        "retry_backoff_sec": 1.0,       # 重试退避基数 (秒), 指数退避
        "timeout_sec": 30,
        # 逐只扫描的并发线程数 (网络IO密集, 提高它能成倍加速; 过高可能被限频)
        # 默认取 min(16, CPU*2); 设为具体数字可覆盖。
        "max_workers": 0,
    },

    # =====================================================================
    #  模块1 — 行业景气度 (Industry prosperity)
    # =====================================================================
    "industry": {
        "top_n": 8,                     # 入选行业数 (Top N 作为模块2的候选池)
        "use_full_market": False,       # True = 跳过行业筛选, 扫描全市场 (退路开关)
        "trend_gate_enabled": True,     # 趋势硬门槛: 行业指数需在 MA120 上方(或2%内)
        "trend_gate_tolerance_pct": 2.0,
        "ma_short": 60,
        "ma_long": 120,
        # 五大支柱权重 (sum 不必为1, 最终按加权百分位归一)
        "weights": {
            "trend":       0.25,        # 趋势
            "momentum":    0.25,        # 动量
            "breadth":     0.20,        # 广度
            "capital":     0.15,        # 资金 (无数据时权重并入趋势+动量)
            "fundamental": 0.15,        # 基本面景气
        },
        "breadth_sample": 60,           # 计算广度时, 每个行业最多抽样多少只成分股 (控制耗时)
        "momentum_excess_weight_boost": 1.0,  # m3(对沪深300超额) 的额外权重
    },

    # =====================================================================
    #  模块2 — 技术左侧扫描 (Technical left-side scan)
    #  阈值/权重沿用并扩展参考实现 a_share_left_screener.py
    # =====================================================================
    "tech": {
        # ---- 股票池过滤 ----
        # 2026-09-08: 开扫前按价格库的**点时股票池**裁候选池 (东财快照里混着 196 只早已退市的
        # 老代码和一批次新股, 它们在取数层已被判无数据跳过, 却照样计进对外的 n_scanned)。
        # 只在 CONFIG.source.bars == 'tushare' 且库存在时生效。**回滚: 这里改 False 即可**,
        # 一行关掉整段裁池, 回到东财原池 (扫描数回到 ~5180 的老口径)。
        "pool_by_store": True,
        "exclude_st": True,
        "exclude_new_days": 180,        # 上市交易日不足则视为次新, 剔除
        "min_amount_yi": 0.5,           # 近20日日均成交额下限(亿元)
        "min_price": 2.0,
        "exclude_bj": True,             # 剔除北交所(8/4/920 开头)
        # ---- 信号阈值 ----
        "channel_window": 120,          # 拟合上升通道窗口(交易日)
        "channel_band_k": 2.0,          # 下轨 = 回归线 - k*残差std
        "near_lower_pct": 4.0,          # 距下轨 <=4% 视为贴近
        "pivot_window": 10,             # 摆动低点识别窗口(左右各N根)
        "near_pivot_pct": 4.0,          # 距前低 <=4% 视为接近
        "ma_list": [60, 120, 250],
        "near_ma_pct": 3.0,             # 距均线 <=3% 视为均线支撑
        "rsi_oversold": 38.0,
        "drawdown_min": 0.18,           # 左侧前提: 距区间高至少回撤18%
        # ---- 各信号权重 ----
        "weights": {
            "channel": 1.0,
            "pivot":   1.0,
            "ma":      0.8,
            "oversold_div": 1.2,
            "drawdown": 0.6,
            "vol_confirm": 0.5,         # 支撑处量能确认(缩量企稳/放量企稳)
            # v2 新增: 支撑强度(历史触碰次数) / 趋势规整(MA250上方的回踩) / 相对强度(vs 沪深300)
            "supp_strength": 0.4, "trend_regime": 0.5, "rel_strength": 0.4,
        },
        "boll_n": 20, "boll_k": 2.0,    # 布林带下轨(额外支撑参考)
        "vol_shrink_ratio": 0.85,       # 支撑处近量/20日均量 < 此值 = 缩量企稳
        # v2 权重和 5.1→6.4 后同比例上调, 否则新信号单独就能凑过门槛
        "min_tech_score": 1.3,          # 技术分低于此值不进入候选 (后续仍做基本面)
        "detail_bars": 250,             # 详情页 K 线保留多少根
        # ---- 独立"深跌超卖抄底"桶 (与支撑型左侧互不干扰) ----
        # 刻画结构已破的深度价值/抄底标的: 深跌 + 超卖 + 逼近52周低点 (与主模型要求上升通道/贴均线/前低企稳不同)。
        "dip": {
            "drawdown_min": 0.35,       # 从近channel_window高点回撤 >= 35%
            "rsi_max": 32.0,            # RSI(14) <= 32 (真超卖)
            "pos_52w_max": 20.0,        # 处于52周区间底部 20% 以内
            "vol_spike": 1.8,           # 近量/20日均量 >= 此值 = 放量(见底确认之一)
            "weights": {"depth": 1.2, "oversold": 1.0, "nearlow": 1.0, "confirm": 0.8},
        },
        # ---- 独立"蓄势待发"桶 (🚀): 深回调后横盘收敛、贴近箱体上沿、随时可能突破 ----
        "coil": {
            "drawdown_min": 0.25,       # 箱体上沿距 250日高点 仍有 >= 25% 回撤 (大回调在先)
            "bars_since_high_min": 40,  # 250日高点距今 >= 40 根bar (回调不是刚发生的崩落)
            "consol_bars": 30,          # 横盘窗口: 近30根bar
            "range_max_pct": 16.0,      # 窗口内 (最高-最低)/中点 <= 16% 才算"横盘收敛"
            "near_high_pct": 5.0,       # 现价距箱体上沿 <= 5% 且在箱体上半部
            "squeeze_pctile": 40.0,     # 布林带宽处于近一年 40% 分位以下 = 波动被压缩
            "weights": {"tight": 1.0, "near_high": 1.0, "squeeze": 1.0, "confirm": 0.8},
        },
    },

    # =====================================================================
    #  模块4 — 技术 × 基本面 交叉打分
    # =====================================================================
    "cross": {
        # 综合分 = 技术分(标准化) * w_tech + 基本面分 * w_fund + 景气加成 * w_prosperity
        "w_tech": 0.50,
        "w_fund": 0.30,
        "w_prosperity": 0.20,
        # 基本面打分阈值 (用于 0-100 评分)
        "roe_good": 12.0,               # ROE(%) 高于此为加分
        "roe_excellent": 18.0,
        "pe_low_percentile": 30.0,      # PE 历史分位 低于此为"偏低"加分
        "pe_high_percentile": 80.0,     # 高于此为"偏高"减分
        "debt_ratio_warn": 70.0,        # 资产负债率(%) 高于此预警
        "netprofit_yoy_good": 0.0,      # 净利同比 > 0 加分
        # 结论标签阈值 (技术权重和 5.1→6.4 后按占比同步上调: 2.0/5.1≈2.5/6.4)
        "strong_left_tech": 2.5,        # 强左侧: 技术分门槛
        "strong_left_fund": 60.0,       # 强左侧: 基本面分门槛
        "strong_left_prosperity": 60.0, # 强左侧: 所属行业景气分门槛
        "fund_weak_threshold": 40.0,    # 基本面分低于此 -> "技术好但基本面弱"
    },

    # =====================================================================
    #  输出 (Output)
    # =====================================================================
    "output": {
        "final_top_n": 200,             # 仪表盘候选清单最多展示多少只
        "fund_top_n": 300,              # 仅对"技术分最高的前N只"拉基本面(限制耗时/接口压力)
        "dashboard_detail_top_n": 150,  # 详情(K线)数据为前多少只生成 (控制 JS 体积)
        "dip_top_n": 40,                # 深跌抄底桶最多并入/展示的只数 (上限, 防止灌进一堆刀)
        "coil_top_n": 40,               # 蓄势待发桶最多并入/展示的只数
        "history_days": 90,             # 历史快照保留天数 (docs/history/)
        "profile_budget_min": 45,       # 阶段C深度档案时间预算(分钟); 超时回落到最近档案
    },
}


def deep_get(d: dict, path: str, default=None):
    """按 'a.b.c' 路径取嵌套配置, 安全降级。"""
    cur = d
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur
