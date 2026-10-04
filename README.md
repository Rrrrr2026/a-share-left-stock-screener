# A股「左侧支撑位」筛选 + 基本面交叉 + 交互监控台

在**高景气行业**内，自动发现正回踩支撑位 / 接近前期低点的**左侧机会**，自动拉取基本面并与技术形态交叉打分，最后在一个**全中文、可交互**的监控台里一屏看全。

> ⚠️ **免责声明**：本系统仅做技术/基本面数据的自动化整理与形态筛选，**不构成任何投资建议**。“左侧买入”是在下跌中、支撑确认前进场，风险天然更高（可能继续下跌或破位）。所有标的需**人工复核**，使用者自负盈亏与风控。

---

## 0. 在线查看 (GitHub Pages)

已托管到 GitHub Pages,任何设备浏览器直接打开(公开可见):

**https://rrrrr2026.github.io/a-share-left-screener/**

> 页面上的数据是**上次发布时的快照**。要更新线上数据:本机先 `python run_pipeline.py` 生成最新结果,再**双击 `发布更新到网上.bat`**(会把 `dashboard/` 拷到 `docs/` 并 `git push`);等 1–2 分钟 Pages 自动重建,刷新网址即可。

其它查看方式见 §8(局域网共享 / 本地双击打开)。

---

## 1. 一分钟上手

```bash
# 1) 安装依赖（建议 Python 3.10+）
pip install -r requirements.txt

# 2A) 完整跑一遍（需联网拉行情，建议每个交易日收盘后 16:30 左右运行）
python run_pipeline.py

# 2B) 先看效果：离线合成演示数据（不联网）
python make_demo_data.py

# 3) 双击打开监控台
dashboard/index.html
```

跑完后所有结果写入 `data/ashare.db`（SQLite），并导出：
- `dashboard/dashboard_data.js` —— 监控台读取的数据（已内嵌，双击 HTML 即可，无需起服务器）
- `data/candidates_<日期>.csv` —— 主表一键导出（中文表头，Excel 可直接打开）

> 监控台用到 Tailwind / ECharts 的 CDN，**首次打开需联网**加载图表库。若要完全离线，可把这两个库下载到本地后改 `dashboard/index.html` 里的 `<script src>`。

---

## 2. 它做了什么（流水线）

```
[1] 行业景气筛选  →  [2] 技术左侧扫描  →  [3] 基本面拉取
      →  [4] 技术×基本面交叉打分  →  [5] 写入SQLite  →  [6] 交互监控台
```

| 模块 | 文件 | 作用 |
|---|---|---|
| 1 行业景气 | `ashare/module1_industry.py` | 五大支柱（趋势/动量/广度/资金/基本面）横截面百分位归一 → 景气总分 → 取 Top 8 行业，含 MA120 趋势硬门槛 |
| 2 技术左侧 | `ashare/module2_tech.py` | 通道下轨 / 前期低点 / 关键均线 / 超跌+MACD底背离 / 回撤前提，命中越近分越高；并算出关键支撑位、破位参考位与详情图所需的逐日序列 |
| 3 基本面 | `ashare/module3_fundamentals.py` | PE/PB（历史分位 + 行业中位对比）、ROE、EPS、营收/净利同比、毛利率、负债率、股息率 |
| 4 交叉打分 | `ashare/module4_crossscore.py` | 综合分 = 技术×0.5 + 基本面×0.3 + 景气×0.2，给出 `✅强左侧 / ⚠️技术好但基本面弱 / 🔎观察` 标签 |
| 5 持久化 | `ashare/db.py` | `industry_score / tech_scan / fundamental / final_rank / stock_detail / run_log`，按日保留历史 |
| 6 监控台 | `dashboard/index.html` | 全中文、可排序/筛选/搜索的主表 + 行业景气榜 + 个股 K线详情抽屉 + CSV导出 |

数据来源：**akshare**（免费、全 A 覆盖）。行业口径采用 **东财一级行业**（`stock_board_industry_*_em`）。所有接口都过一层**字段映射**（`ashare/datasource.py` 的 `rename_normalize`），即使某个中文字段被改名也不会让流水线崩溃。

---

## 3. 配置（改这里就能改结果）

所有阈值/权重/开关都在 `ashare/config.py` 的 `CONFIG` 里。常用项：

| 配置 | 说明 | 默认 |
|---|---|---|
| `industry.top_n` | 入选景气行业数量 | 8 |
| `industry.use_full_market` | True = 跳过行业筛选、扫全市场 | False |
| `industry.trend_gate_enabled` | 行业指数需在 MA120 上方才有资格 | True |
| `industry.weights` | 五大支柱权重（资金/基本面缺数据时自动并入其它支柱） | 0.25/0.25/0.20/0.15/0.15 |
| `tech.near_lower_pct` / `near_pivot_pct` / `near_ma_pct` | 贴近下轨/前低/均线的判定阈值 | 4 / 4 / 3 (%) |
| `tech.drawdown_min` | 左侧前提：至少回撤多少 | 0.18 |
| `tech.weights` | 各技术信号权重 | 见文件 |
| `cross.w_tech / w_fund / w_prosperity` | 综合分三项权重 | 0.5 / 0.3 / 0.2 |
| `cross.strong_left_*` | “强左侧”标签门槛 | 技术≥2.0 / 基本面≥60 / 景气≥60 |

命令行开关：
```bash
python run_pipeline.py --full-market   # 扫全市场（不做行业筛选）
python run_pipeline.py --no-cache      # 禁用本地缓存（默认 12 小时缓存）
python run_pipeline.py --demo          # 等价于 make_demo_data.py
```

### tushare（可选）
若有 tushare pro token，可设环境变量获得更稳定的财务/行业数据（当前默认仅用 akshare）：
```bash
# Windows PowerShell
$env:TUSHARE_TOKEN="你的token"
```

---

## 4. 监控台用法

- **顶部概览卡**：今日扫描数 / 命中数 / 入选景气行业 / 数据日期。
- **行业景气榜**：横向条形图，绿色=入选；鼠标悬停看 趋势/动量/广度/资金/基本面 五维分项。
- **候选股主表**：点列头排序（综合分/技术分/基本面分/距支撑%…），按行业、结论标签筛选，按代码/名称搜索；`距支撑%` 越接近 0 高亮越强；筛选状态记忆在浏览器本地。
- **个股详情**（点任意一行）：K线（前复权）叠加 MA60/120/250 + 自动绘制的**上升通道下轨** + **前低**水平线；下方 MACD / KDJ / RSI 三张副图（缩放联动）；基本面速览卡 + 估值历史分位条 + ROE 多年趋势。
- **导出CSV**：按当前筛选结果导出，中文表头，utf-8-sig（Excel 直接打开不乱码）。

---

## 5. 目录结构

```
a-share-left-screener/
├─ run_pipeline.py          # 一键运行（联网）
├─ make_demo_data.py        # 离线合成演示数据
├─ requirements.txt
├─ ashare/
│  ├─ config.py             # 中央配置 CONFIG
│  ├─ datasource.py         # akshare 封装 + 字段映射 + 重试/限频/缓存
│  ├─ indicators.py         # EMA/MACD/RSI/KDJ/通道拟合/摆动低点
│  ├─ statutil.py           # 横截面百分位 / zscore / 历史分位
│  ├─ module1_industry.py   # 行业景气
│  ├─ module2_tech.py       # 技术左侧扫描
│  ├─ module3_fundamentals.py
│  ├─ module4_crossscore.py
│  ├─ db.py                 # SQLite 持久化
│  └─ export_data.py        # 导出 dashboard_data.js + CSV
├─ dashboard/
│  ├─ index.html            # 全中文交互监控台（Tailwind + ECharts）
│  └─ dashboard_data.js     # 由导出层生成
├─ data/                    # ashare.db / 缓存 / CSV
└─ tests/test_offline.py    # 离线自测（不联网，22 项）
```

---

## 6. 自测

```bash
python tests/test_offline.py
```
用合成数据驱动真实逻辑，覆盖指标、模块1景气（打桩数据层）、模块2技术、模块4交叉打分。

---

## 7. 设计取舍 / 已知边界

- **联网**：`run_pipeline.py` 需要能访问 akshare 行情接口。无网络时用 `--demo` 看效果。
- **东财实时端点容错（重要）**：部分网络下东财 `push2` 实时端点会重置连接（报 `RemoteDisconnected`），但**历史/估值/同花顺/新浪**端点正常。系统已内置多源容错并实测可用：
  - 给所有请求注入浏览器 UA；
  - **股票池**：东财快照失败 → 自动退**新浪** `stock_zh_a_spot`；
  - **行业列表/指数/资金流**：东财失败 → 自动退**同花顺**（`stock_board_industry_name_ths` / `..._index_ths` / `..._summary_ths`）；
  - **行业指数末日守卫（2026-10-04）**：同花顺行业指数日线（年度文件）当日那根北京 ~21 点后才落地，收盘后不久跑批拿到的末日是 T-1。模块1 现在把每个行业日线的末日与 `data_date` 比：落后 → ① Tushare `ths_daily` 当日那根（与同花顺日线逐位相同） ② 同花顺行业一览当日涨跌幅补根（`close = 昨收 × (1 + 涨跌幅%)`）；都补不上 → 照旧按旧一日算，但快照 `meta.industry_asof` 留痕、看板景气榜标题旁显示「行业数据截至 X」。日志 grep `行业指数`。只关补根的回滚口子（留痕与告警关不掉）：`ASHARE_INDUSTRY_TAIL_FILL=0` 或 `touch data/industry_tail_fill.off`。
  - **个股日线**：东财被限频 → 自动退**新浪** `stock_zh_a_daily`（前复权）；这是扫描的核心数据，有了它即使东财日线被限频也能照常出结果；
  - 一旦判定东财某端点不可用，后续同类请求直接走备用源，不再反复重试拖慢整轮；
  - **限频提示**：短时间大量请求东财会触发其反爬，所有东财端点临时返回 `RemoteDisconnected`（即使你网络正常）。系统已自动切到新浪/同花顺；若仍偏慢，等几十分钟让东财冷却即可恢复。
  - **行业成分股**（东财 push2，无同花顺等价接口）若拿不到，则该网络下**自动回退到全市场扫描**，行业景气榜仍照常展示（此时 `广度` 支柱缺失、个股“所属行业”显示 `—`）。
  - 全市场回退时**个股景气分未知**，主表/详情里景气分诚实显示 `—`（不伪造中性分）；`结论标签` 仅按 技术 + 基本面 判定（`✅强左侧` = 技术到位 + 基本面扎实；`⚠️` = 技术好但基本面弱；`🔎观察` = 其它）。综合分排序里景气项用中性 50 占位，不影响相对排序。
  - 注意：全市场回退会扫描约 5000+ 只股票；逐只扫描已**多线程并发**（`CONFIG["fetch"]["max_workers"]`，默认 `min(16, CPU*2)`），实测比单线程快约 13×，全市场约 10–15 分钟可跑完，之后有缓存更快。
- **性能**：瓶颈是网络 IO（不是 CPU），所以加速靠**并发请求**而非占满 CPU。想更快可调大 `max_workers`（如 24/32），但过高可能被数据源限频；遇到大量失败就调小。
- **行业基本面支柱(E)**：聚合财务成本较高，默认不计算，其权重按比例并入其它支柱（接口已预留）。资金支柱(D)取行业主力净流入，拿不到时同样并入趋势+动量。
- **广度计算**：为控制耗时，每个行业最多抽样 `industry.breadth_sample`(默认60) 只成分股。
- **耗时**：默认只扫 Top 8 行业成分股（约数百只），每只间隔 `fetch.sleep_sec` 防限频；`--full-market` 扫全市场会明显更慢。
- **EPS 同比**：部分接口无现成字段时用净利同比近似，并以 `—` 优雅降级。
- **估值历史（PE/PB 分位）**：akshare 1.12+ 已移除 `stock_a_indicator_lg`，本系统改用东财 `stock_value_em`（一次返回 PE-TTM/PB/PE静/总市值 的逐日历史），失败再退回百度股市通 `stock_zh_valuation_baidu`。该来源不含**股息率**，故 `股息率` 显示 `—`（属 PRD 选配项，不影响核心打分）。已在 akshare **1.18.60** 上核对全部接口签名。
- 单只标的拉取/计算失败只跳过并记录，绝不中断整轮。


## 共用核心 (leftside_core)

回测 / 错杀检测 / 新闻标记 / 买卖点计划 / 指标等逻辑与 A股/美股 另一个筛选器共用，源码在 `stock-core` 仓库的 `leftside_core/`；本仓库 `vendor/leftside_core/` 是每次更新时同步进来的副本，所以克隆本仓库即可直接运行。本机若存在同级目录 `../stock-core`（或设置 `STOCK_CORE_DIR`）则优先使用它。修改共用逻辑请改 stock-core，不要直接改 vendor 副本。
