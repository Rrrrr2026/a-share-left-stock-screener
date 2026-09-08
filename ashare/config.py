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


#: 候选池按库裁 (CONFIG.tech.pool_by_store) 的**可按下去的**回滚口子 -> (是否开, 被谁关的)。
#: 服务器上 run_a.sh 在跑流水线之前会 `git reset -q --hard origin/main`, 而 config.py 是被
#: 跟踪文件 —— 值班的人登录服务器把下面的 True 改成 False, 那行改动会在下一次 stock-a 启动的
#: 头几秒被 reset 抹掉, 日志里既不报错也不提示, **他会以为关掉了其实没关**。09-03「静默跑旧码」
#: 与 09-07「优质榜静默失败」都是同一种失败形态, 所以这个开关必须留一条服务器上真按得下去的路:
#:   ① `sudo systemctl edit stock-a` 加 Environment=ASHARE_POOL_BY_STORE=0 (stock-a.service
#:      现在没有任何 Environment=, 也没有 .env 文件, 所以只有这一条环境变量的路);
#:   ② 或 `touch /srv/stock/a-share-left-stock-screener/data/pool_by_store.off` —— data/ 不在
#:      git 里, reset 抹不掉, 且 stock 用户自己就能按, 不需要 root;
#:   ③ 或在 PC 上改下面的默认值再 commit+push (需要等下一轮或手动重跑)。
#: 优先级 环境变量 > 停机文件 > 默认值; 无论被哪一层关掉, run_pipeline 都会打一行
#: "候选池按库裁: 已关闭 (被谁关的)" —— 不允许静默关闭。
#:
#: **环境变量只认 6 个值**: 关 = 0 / false / off, 开 = 1 / true / on (大小写不敏感)。
#: 别的值 (`yes` `no` `Off ` 带空格的、拼错的) 一律**不当数**, 返回第三个元素 warn,
#: 由 run_pipeline 在裁池那一步 log.warning 出来, 然后按下面的默认路径 (停机文件 → 默认值 开)
#: 走。09-08 首版还认 yes/no, 于是"值班的人写了个自以为有效的值"与"写对了"在日志里
#: 长得一模一样 —— 这正是 09-03「静默跑旧码」/ 09-07「优质榜静默失败」的同一形态:
#: **按下去没反应, 却没有任何一行字说它没反应**。现在写错值一定会响, 且响的时候明说
#: "按默认(开)处理, 要关请用 0/false/off 或 touch data/pool_by_store.off"。
def _pool_by_store_switch(default: bool = True) -> tuple:
    """-> (是否开, 被谁关的, 环境变量写错时的告警文本)。三个元素都可离线单测。"""
    raw = os.environ.get("ASHARE_POOL_BY_STORE")
    v = (raw or "").strip().lower()
    warn = ""
    if v in ("0", "false", "off"):
        return False, f"环境变量 ASHARE_POOL_BY_STORE={v}", ""
    if v in ("1", "true", "on"):
        return True, "", ""
    if v:                       # 写了东西, 但不是这 6 个值之一 -> 不当数, 但必须响
        warn = (f"环境变量 ASHARE_POOL_BY_STORE={raw!r} 不是可识别的值 "
                f"(只认 0/1/true/false/on/off, 大小写不敏感), 本轮**忽略它**按默认处理; "
                f"要关裁池请用 ASHARE_POOL_BY_STORE=0 或 touch {DATA_DIR}/pool_by_store.off")
    off_file = os.path.join(DATA_DIR, "pool_by_store.off")
    if os.path.exists(off_file):
        return False, f"停机文件 {off_file}", warn
    return bool(default), "", warn


_POOL_BY_STORE, _POOL_BY_STORE_OFF_BY, _POOL_BY_STORE_WARN = _pool_by_store_switch(True)


#: **回测/模拟盘/双周取价读库 (P2) 的代码默认值 —— 就是这一行。**
#: 2026-09-09 GM 翻开关时**只改这一个字符** ("0" -> "1"), 外加
#: `tests/test_backtest_anchor.py::EXPECTED_DEFAULT` 那一行的期望值。别的地方一个字都不用动。
#: 单独拎出来是因为 09-08 之前它埋在 `os.environ.get(..., "0")` 的第二个参数里 —— 要翻开关
#: 得在一坨注释中间找那个字符串字面量, 而且没有任何一处能让单测指着它说"默认值就该是这个"。
DEFAULT_BACKTEST_STORE = "0"


#: 回测取价读库的**三层开关**, 与上面 `_pool_by_store_switch` 同一套路 (故意长得一样:
#: 值班的人只需要记住一种口径)。优先级 环境变量 > 停机文件 > 代码默认值。
#:   ① `sudo systemctl edit stock-a` 加 `Environment=ASHARE_BACKTEST_PRICES_FROM_STORE=0`;
#:   ② `touch <repo>/data/backtest_store.off` (data/ 不在 git 里, run_a.sh 的
#:      `git reset --hard` 抹不掉; stock 用户自己就能按, 不需要 root);
#:   ③ PC 上改 DEFAULT_BACKTEST_STORE 再 commit+push。
#: **光在服务器上改这个文件是按不下去的** —— run_a.sh 每次启动前 `git reset -q --hard
#: origin/main`, 手改会在下一次 stock-a 起来的头几秒被静默丢弃 (与 pool_by_store 同因)。
#: **环境变量只认 6 个值**: 关 = 0/false/off, 开 = 1/true/on (大小写不敏感)。别的值一律
#: **不当数**, 返回第三个元素 warn 由 run_pipeline log.warning 出来, 然后按默认路径走 ——
#: "按下去没反应却没有任何一行字说它没反应" 是本队记了三次的失败形态, 这里不再重复。
#: 无论最后是哪一层定的, run_pipeline 在回测/模拟盘/双周之前都会打一行
#: "回测取价: 读库 开/关 (被谁定的)", 不存在静默切换。
def _backtest_store_switch(default: str = DEFAULT_BACKTEST_STORE) -> tuple:
    """-> (是否读库, 被谁定的, 环境变量写错时的告警文本)。三个元素都可离线单测。"""
    raw = os.environ.get("ASHARE_BACKTEST_PRICES_FROM_STORE")
    v = (raw or "").strip().lower()
    warn = ""
    if v in ("0", "false", "off"):
        return False, f"环境变量 ASHARE_BACKTEST_PRICES_FROM_STORE={v}", ""
    if v in ("1", "true", "on"):
        return True, f"环境变量 ASHARE_BACKTEST_PRICES_FROM_STORE={v}", ""
    if v:                       # 写了东西, 但不是这 6 个值之一 -> 不当数, 但必须响
        warn = (f"环境变量 ASHARE_BACKTEST_PRICES_FROM_STORE={raw!r} 不是可识别的值 "
                f"(只认 0/1/true/false/on/off, 大小写不敏感), 本轮**忽略它**按默认处理; "
                f"要关回测读库请用 ASHARE_BACKTEST_PRICES_FROM_STORE=0 或 "
                f"touch {DATA_DIR}/backtest_store.off")
    off_file = os.path.join(DATA_DIR, "backtest_store.off")
    if os.path.exists(off_file):
        return False, f"停机文件 {off_file}", warn
    return str(default).strip().lower() in ("1", "true", "on"), "", warn


_BT_STORE, _BT_STORE_OFF_BY, _BT_STORE_WARN = _backtest_store_switch()
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
        # 回测/模拟盘/双周的取价 (fetch_price_series) 是否也读价格库 (P2, 2026-09-07)。
        # 与 "bars" 是**两个**开关: 这条链的锚定口径 (find_anchor 改用原始价) 与阶段A 无关,
        # 出问题要能单独关掉而不必把整个 bars 源退回 fuyao。关掉 = 退回逐股腾讯前复权。
        #
        # **默认 0 (关) —— 但默认值本身已经搬到模块顶上的 `DEFAULT_BACKTEST_STORE` 那一行,
        # 并配了 `_backtest_store_switch()` 三层开关 (环境变量 > 停机文件 > 这个默认值)。**
        # 2026-09-09 GM 翻开关 = 改那一个字符 + tests 里 EXPECTED_DEFAULT 那一行, 别处不动。
        #
        # GM 当天定的开关裁决规则是三条同时满足才翻开: (a) anchor 门 pass (b) 重放里
        # "新口径更差" = 0 笔 (c) pool 级最差 |Δ| <= 0.5pp。修正两份错标快照的 meta、把演示
        # 种子快照排除出回放样本之后全量重跑 research/validate_backtest_anchor.py, 实测:
        #   (a) pass  —— raw exact 99.97% (7,286/7,288), 干净样本占比 100% (下限 80%)
        #   (b) **1 笔** —— 600061 XD国投资 2026-06-30: 那天它除权, 快照存的"昨收"是**除权后的**
        #       06-30 收盘 6.40, 既不等于 raw 收盘 6.55 也不是当天成交价, raw 锚定退到 06-26
        #       (near 1.72%) 反而错了一格; qfq 锚定因为基准恰好没再变而蒙对。
        #   (c) **0.9pp** —— win10 52.3% -> 53.2% (参与判定的分段最差 1.6pp: ☑️次强左侧 n=757)
        #
        # **2026-09-08 卡 R3-4: 在生产同款样本 (只读 scp 下来的服务器 35 份快照, 比 PC 多 5 份、
        # 含两组周末重跑的重复 as_of) 上复算, 三个数逐条重现**
        # (`research/validate_backtest_anchor.py --history-dir <服务器副本>`):
        #   (a) pass —— raw exact **99.98%** (8,619/8,621), 干净样本占比 100%; 且服务器那两份
        #       快照的 meta 仍是错值, 修正表 SNAPSHOT_DATA_DATE_FIX 在它们身上**真的生效了**
        #       (FIXED_DATA_DATE 两条: 07-01→06-30, 08-21→08-24), 证明修正走代码这条路到得了生产。
        #   (b) **1 笔**, 仍是 600061 @ 06-30 (来源 day_2026-07-01.json), 根因在快照不在开关。
        #   (c) **0.9pp** (win10 52.3%→53.2%), 参与判定的分段最差 **1.6pp** (☑️次强左侧 n=757/754)。
        #   成交日变化 **95 笔**归因: 修复(旧口径锚错) 64 / 两边都exact但锚到不同bar 30 /
        #   两边exact且同一根bar 0 / **新口径更差 1** / 两边都没命中 0 / 来自错标快照 0。
        # **GM 裁决: 这 0.9pp 按「修正」读, 不按「回归」读** —— 95 笔里 94 笔 (64+30) 是把入场日
        # 从"早 1~6 根 bar"拨回快照价真正来自的那根, 只有 1 笔更差, 而那 1 笔的根因是快照在除权日
        # 存了**除权后的昨收** (生成侧问题, 见 export_data 的 XD 修法), 不是取价/锚定口径的问题。
        # 开关本身没有被这三个数否掉; **今天 (09-08) 仍然保持关的唯一理由是排期**: 14:00 那轮
        # 已经同时上了「换库 + 裁池 + 日更分块」三个变量, 不叠第四个。09-09 再翻。
        #
        # **旧注释里"102 笔里 98 笔是修复, 2 笔是快照自己存了前复权价"这句归因已被推翻**, 别再引用:
        # 那 102 笔里有 44 笔的价来自两份 meta.data_date 与内容不符的快照 (07-01 41 / 08-24 3),
        # 两个口径都在错误的日子上比价, 谁"命中"都不算修复。快照 meta 按价格证据修正后重算,
        # 成交日变化 95 笔 = 旧口径锚错(修复) 64 / 两边都exact但锚到不同bar 30 / 新口径更差 1;
        # "两边 exact 且同一根 bar" 这一桶首版被并进了"修复"里 (名不副实), 现在单列, 实测 0 笔。
        #
        # **2026-09-08 回修补记 —— 两条"别拿它当翻开理由"的更正**:
        #   · 模拟盘账本里有 62 条信号的 sig_date 被写成 2026-08-21 而价格是 08-24 收盘 (它们
        #     是从那份错标快照登记的, 账本只在注册那一刻写 sig_date, 事后改快照 meta 追不回)。
        #     逐笔用 find_anchor 复算: **换成 raw 锚定后 62/62 仍然锚不到真实的 08-24 那根 bar**
        #     —— find_anchor 只从 sig_date 那根往前找 6 根, 真实 bar 在 idx0+1, 够不着。
        #     与首版报的两个数交叉核对 (两个数都复现了): 52 条 scale 偏离 >0.25% 里 **43 条**
        #     属于这批错标信号 (病因是快照标注日, 开关修不了); 5 条 qfq/raw 锚到不同 bar 里
        #     **1 条** (603379) 属于这批 —— 它换 raw 也只锚到 08-21, 仍是错的。**剩下 4 条**
        #     (000408 / 603558 / 301162 / 002587, qfq 早锚 1~4 根) 才是这个开关真能修的。
        #     所以首版那句"52/5 是支持翻开开关的独立证据"要按 4 条算, 不是 52 或 5 条。
        #   · 上面 (a)(b)(c) 三个数是在 **PC 的 30 份快照**样本上量的; 生产 (服务器) 跑的是
        #     35 份 —— 多 5 份, 且含两组周末重跑造成的重复 as_of (08-28 / 09-04, 净贡献 2 笔
        #     事件)。真要按这三个数拍板, 应在生产同款样本上再复跑一次。
        #
        # 其余三项验证依旧干净: 抽样 26 笔快照价与 raw 锚定bar 逐值相等、10日收益与手算
        # raw×因子比一致到 1e-6; 2,457 只读库 0.195 秒; pytest 全绿。
        # 开启/关闭方式见 `_backtest_store_switch()` 上面那段 (三层, 与裁池开关同口径)。
        # 详见 stock-core/design/backtest_price_from_store.md。
        "backtest_prices_from_store": _BT_STORE,
        # 被哪一层定的 (环境变量 / 停机文件), 空串 = 用的代码默认值。只用于日志, 不允许静默切换。
        "backtest_prices_from_store_off_by": _BT_STORE_OFF_BY,
        # 环境变量写了个不认识的值时的告警文本 (空串 = 没问题)。由 run_pipeline 在回测那一步
        # log.warning 打出来 —— config 在 basicConfig 之前就 import 完了, 在这里直接 log 会走
        # lastResort 只落到 stderr, 进不了 journal 的正文 (与 pool_by_store_switch_warn 同因)。
        "backtest_prices_from_store_switch_warn": _BT_STORE_WARN,
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
        # 只在 CONFIG.source.bars == 'tushare' 且库存在时生效。
        # **回滚**: 服务器上 `systemctl edit stock-a` 加 Environment=ASHARE_POOL_BY_STORE=0,
        # 或 `touch data/pool_by_store.off`; PC 上才是改这个默认值 (再 commit+push)。
        # 只在服务器上改下面这一行是**按不下去**的 —— run_a.sh 每次 git reset --hard 会抹掉它。
        # 判定与提示语见上面 _pool_by_store_switch()。
        "pool_by_store": _POOL_BY_STORE,
        # 被谁关的 (环境变量 / 停机文件), 空串 = 没被关。只用于日志, 不允许静默关闭。
        "pool_by_store_off_by": _POOL_BY_STORE_OFF_BY,
        # 环境变量写了个不认识的值时的告警文本 (空串 = 没问题)。由 run_pipeline 在裁池
        # 那一步 log.warning 打出来 —— config 在 basicConfig 之前就 import 完了, 在这里
        # 直接 log 会走 lastResort 只落到 stderr, 进不了 journal 的正文。
        "pool_by_store_switch_warn": _POOL_BY_STORE_WARN,
        "exclude_st": True,
        "exclude_new_days": 180,        # 上市交易日不足则视为次新, 剔除
        "min_amount_yi": 0.5,           # 近20日日均成交额下限(亿元)
        "min_price": 2.0,
        "exclude_bj": True,             # 剔除北交所(8/4/920 开头)
        # ---- 策略射程 (GM 决定 2026-09-08) ----
        # **本策略射程不含 B 股 (沪B 900xxx / 深B 200xxx)**, 与 exclude_bj 并排。
        # 理由 (口径决定, 不是 bug): ① 价格库 (Tushare stock_basic 镜像) 对 B 股**零覆盖** ——
        # 09-08 复核: universe 5,551 行里 B 股 0 行, bars 里 B 股 0 根, 单为它补一路行情不值;
        # ② B 股是外币 (港币/美元) 计价、流动性极低的存量板块, 与本策略的成交额门槛/仓位
        # 口径根本对不上。
        # **B 股到底有多少只: 本仓拿不到权威数, 别在这里写一个准数。** 东财全A快照
        # (ds.fetch_spot_snapshot) 对 B 股恒 0 行 (09-07/09-04/09-03/09-02/08-28 逐日实测都是 0,
        # 北交所倒是每天 350+ 只), 价格库也 0 行; 唯一见过 B 股的入口是东财**行业成分表**,
        # data/cache 里累计出现过 **60 只 distinct B 股代码** —— 这是**下界**, 不是"在市共 60 只"。
        # ⚠ 09-08 首版这里写的"在市 B 股只剩 7 只"是**错的** (09-08 复检推翻): 那个 7 是空跑当天
        # 恰好抓到的 **9/90 个行业成分表**里的 B 股数, 随当天行业接口拿到几个行业漂 (同法实测
        # 09-04=2 / 09-03=4 / 08-28=19), 既不是全市场数, 也**不能**拿来预测裁池后分母会少几只。
        # 射程决定本身不依赖这个数 (①②任一条都足够), 但写进档案的数字必须说真话。
        # 写成**显式规则**而不是让"库里没有"顺手把它们裁掉: 前者是策略边界 (可复查、可推翻),
        # 后者是数据缺口冒充策略决定 —— 09-07 的教训就是"看着正常的静默"最贵。
        # 生效点在**候选池构建阶段**的两条路 (datasource.build_universe 全市场路 +
        # run_pipeline 的行业成分路), 所以 B 股既不进候选池、也不进裁池的 uncovered 统计。
        # 实情提醒: 全市场那条路在真实数据上是**空转** (东财快照里根本没有 B 股), B 股只会
        # 从行业成分那条路进来 —— 两条路都设是为了口径一致, 不是因为两条路都真的会命中。
        "exclude_b_share": True,
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
