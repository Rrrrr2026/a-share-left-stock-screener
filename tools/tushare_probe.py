# -*- coding: utf-8 -*-
"""
Tushare (协议兼容镜像) 只读探针 —— 两个子命令
=============================================
    python -X utf8 tools/tushare_probe.py                    # p0: 2026-09 P0 端点清单 (历史用法, 行为不变)
    python -X utf8 tools/tushare_probe.py compat             # 镜像兼容性探针 (卡 TS-PROBE / TS-PROBE-2, **换 key / 换镜像后必跑**)
    python -X utf8 tools/tushare_probe.py compat --expect    # 只列「代码期望字段 + 各消费方期限」, 不联网 (0 次调用)

compat 为什么要有 (2026-10-10 教训, CHRONICLE「GM 记 10-10 12:55」): 换到镜像 tl.kaixin8.top 后, 它对
start==end 的**单日** trade_cal 不套 is_open=1 过滤 (区间查询才套), 客户端数到 1 行就把周六 10-10 当开市日,
就绪闸门等了 60 分钟拒发 10-09 产物 (a-share f1de80f 起 `tushare_client.trade_cal` 一律本地过滤)。旧镜像没这毛病,
所以从未暴露 —— 换镜像 = 换了一个「协议兼容」但细节行为未知的实现, 必须先探再切。本子命令把流水线真正依赖的
那几种调用形态各打一次, 只核**形状** (行数 / 字段集 / is_open 语义 / 回包日期 / 延迟 / HTTP 状态), 最后给一张
PASS/WARN/FAIL 汇总表和**两档结论**。

两档结论 (卡 TS-PROBE-2): 每个检查项带「消费方」—— 生产 (a-share `ashare/*.py`, 流水线) / 数据总览 (stock-core
`research/data_catalog.py`, 服务器 19:30) / 研究湖 (stock-core `research/lake.py`, PC data/lake)。生产 + 数据总览 =
**生产档**, 研究湖 = **研究湖档**, 两档各自判「可用 / 不可用 (有 FAIL) / 未跑完 (有 SKIP)」。**退出码只看生产档**:
0 = 生产档可用 (PASS 或只有 WARN), 1 = 生产档不可用或没跑完, 2 = 判不了 (没 token / 参数错)。研究湖档不可用照样
打出来, 但不改退出码 —— 只换 token (HANDBOOK 流程 A) 时不能因为研究湖 FAIL 去还原 .bak。

日历真值**不取自被测镜像** (卡 TS-PROBE-2): 用本文件内置的 A 股休市表 `A_CLOSED` (国办放假通知 + 上交所休市通知,
来源与维护点见表头) + 周末。镜像 trade_cal 把法定假日标成开市、或把开市日标成休市 = FAIL (前者让生产 `trading_days`
把假日当开市日: `_update_daily_by_date` 那天拉到 0 行就判「当日未就绪」就地停下, 之后天天从这天重试, 价格库永久停更);
假日一例、开市日 D、按日那几次问的日子 P 都从内置表取, 与镜像无关。表没覆盖的年份 (国办通知还没出) 只核周末并
WARN, 提醒补表。

探什么 (默认 26 次调用, 财务不足一页时各多 1 次续页; `--max-calls` 只能 1..30 —— 硬上限 30, 超了拒跑 (退出码 2);
用到上限就不再发, 剩下的判 SKIP = 未跑完):
  ① trade_cal: 区间 [今天-200 天, 今天+CAL_FWD_DAYS] 带/不带 is_open 各一次 —— 不带的那次逐自然日对照内置休市表
     (逐日一行、周末必 0、假日必 0、开市日必 1), 带的那次把镜像的**原样回包**喂给客户端 `tushare_client.trade_cal`
     回放 (打桩 query, 不联网), 回放结果必须等于内置表的开市日; 单日 周六 / 周日 / 假日 (内置表里今天及之前最近的
     工作日休市日) / 开市日 D (内置表里今天之前最后一个开市日) 各带/不带 is_open 一次 —— 核返回行数与 is_open 值并回放;
  ② daily / adj_factor 按 trade_date (全市场一天) 与按 ts_code 区间 (000001.SZ 截至 D 的最近 20 个开市日): 按日问的是
     **已知过去的开市日 P** (内置表里上个月最后一个开市的周二, 且至少在 PAST_DAY_MIN_AGE 天前), 不是最近开市日 ——
     「无视 trade_date、一律回最新一天」的镜像在最近开市日上与正确镜像分不出来。回包里只要有一行 trade_date ≠ P 就
     FAIL (生产 `market._rows_from_daily` / `fetch_adj_by_date` 不看 trade_date, 会把别日的行情/因子当成 P 写进价格库);
     再核行数 (过 `market._rows_from_daily` / keep_a_code 后与 stock_basic L 的 A 股只数比, 门槛 = 生产未就绪守卫的
     ready_ratio)、字段集、因子是否覆盖每根日线;
  ③ daily_basic 按 P (日期不符同样 FAIL: 生产估值兜底 `fetch_valuation_tushare` 也不看 trade_date) / stock_basic (L、D、
     P 各一次) / index_daily (区间截至 D + 单日 P; 指数不停牌, 区间缺开市日 = FAIL) / ths_daily 按 P (生产只认所问那天:
     别日的行 WARN, 当天的 .TI 一行没有 = FAIL) / fina_indicator_vip + income_vip (最近已过法定披露截止的一期,
     limit = 湖的翻页步长 FIN_PAGE; 不足一页就补一次 offset 续页, 分清「镜像单页上限 < FIN_PAGE」与「该期就这么多」;
     上限截断时看湖自己兜不兜得住: lake.py 有 PAGE_CONFIRM_MIN (stock-core 7df1e67 起) 且上限 ≥ 它 → 湖会续页确认、
     按上限接着翻, 只判 WARN; 否则湖「不足一页即停」会静默少拉 = FAIL) / 研究湖每月重拉的申万三件 index_classify (L1) /
     index_member_all (一个 L1, is_new=Y, limit = MEMBER_PAGE; 核回包是否真按 l1_code / is_new 过滤) / sw_daily (申万行业指数, 同样不停牌): 字段集与行数是否与
     代码期望一致; 按区间查的端点 (指数 / 个股 / 申万日线) 回包里任何一行落在内置表的休市日上 = FAIL (那是假数据);
  ④ 每次调用: HTTP 状态 / code / 行数 / 列数 / 延迟 (工作进程里量的纯 HTTP 时间) / 字节。延迟按**每档自己最严的
     消费方期限**判 (> 期限 = FAIL, > 一半 = WARN) —— 例: trade_cal 的生产档期限是数据总览子进程的 10 s, 研究湖档是
     120 s, 同一次 7.5 s 的调用在生产档 WARN、在研究湖档 PASS;
  ⑤ 汇总表 + 两档结论 + 一行总结论。

「期望字段」与「期限」从代码里收集, **不手抄** (`collect_expectations`, 纯 AST, 不 import 被扫的模块):
  · 扫 a-share `ashare/*.py` 全部 (生产: tushare_client / datasource / market (= pricestore 的取数钩子) / datadate …)、
    stock-core `research/lake.py` (研究湖) 与 `research/data_catalog.py` (数据总览; 找法同 ashare.config:
    STOCK_CORE_DIR → 同级 ../stock-core)。模块级字符串常量若是一段导入了 tushare_client 的 Python 源码 (子进程脚本,
    如 data_catalog 的 `_XCAL_CHILD`), 当源码再解析一遍 (行号折回原文件)。
  · 认的调用形态: `query("api", fields=…)` / `_ts_query(…)` / `self.query(…)` / `self._pages(…)` 第一个参数是 api 名,
    `tsc.trade_cal(…)` / `tsc.index_daily(…)` 是 tushare_client 的薄封装 (字段取封装内部那处); 客户端模块的别名从
    import 语句里认 (`from ashare import tushare_client as t` 之后 `t.trade_cal(…)` 也算)。
  · fields 参数能静态求值就算: 字符串字面量、模块常量 (TS_DAILY_BASIC_FIELDS)、`",".join(列表常量)`、
    函数里的局部变量 (含外层函数的, 如湖 pull_daily_basic 里嵌套 fetch 用的 fields)。
  · 期限: 站点的 `deadline_sec=` (没写 = 客户端 DEADLINE_SEC); 同一作用域里显式改了 `<客户端>.TIMEOUT_SEC = N`
    (socket 超时) 的再取 min —— 数据总览子进程就是 TIMEOUT_SEC=10 + deadline_sec=10 (子进程里不走进程级期限, 卡它的
    是这个 socket 超时与父进程 20 s 墙钟)。客户端**默认**的 socket 超时 (TIMEOUT_SEC 60 s) 不另算: 探针自己的请求用的
    就是同一个 socket 超时, 首字节超过它这次调用在探针里同样失败。每个消费方取自己站点里最严的那个。
  · 列引用: 同一函数里对**这次调用结果那个变量**做的 `need <= set(df.columns)` 守卫集合与 `"x" in df.columns`
    判断 (按变量名 + 行号就近归属, 所以 fetch_bars_bulk_tushare 里 `"adj_factor" in fa.columns` 只算 adj_factor 的,
    不会冤枉 daily)。没给 fields 的调用 (index_daily) 走镜像默认字段集, 期望 = 这些列引用。
  · 湖的财务两表字段是运行时从镜像发现、缓存在 lake.db meta 的 (`Lake._fin_fields`), 代码里只有 FIN_TABLES 的
    主键/文本/日期列: 能只读打开 lake.db 就用 meta 里那份完整字段表 (湖下次真会请求的就是它, 缺列会被 `_records`
    **静默写成 NULL**), 打不开就退回 FIN_TABLES 列。
  以后有人在代码里加一处 Tushare 调用或一个字段, 探针的期望自动跟着变; 代码在用而本探针不探的端点打在「未覆盖」行。

铁律: 只读 (不写任何库; lake.db 以 `mode=ro` 只读 meta / pull_log 两张小表; 结果只在 --json 时落一个文件);
token 只在请求体里, **永不打印/落盘** —— 镜像回包里出现的 token (JSON 回包的 msg, 或网关错误页这种非 JSON 回包) 一律
**先抹后截**: 整串换成 <token>, 再把 token 任何 ≥ TOKEN_FRAG_MIN 字的片段换成 <token…> (`scrub_token`; 非 JSON 回包在
工作进程里截 120 字之前就抹), 地址只打主机名; 每次 HTTP 经 `tushare_client._deadline_call` 走 datasource 的**进程级**
墙钟硬期限 (与生产同一个执行器); 默认凭据就是生产客户端 `tushare_client.credentials()` 的那一份 (data/secrets.json,
环境变量 TUSHARE_TOKEN / TUSHARE_BASE_URL 可覆盖), `--secrets PATH` 可指向另一份 secrets.json (只读其 tushare_token /
tushare_base_url 两键)。探针不经 query, 不会触发 TS-FALLBACK 的切备用。
**不在模块顶层动 stdout / 读 secrets / 联网**: 硬期限的 spawn 子进程会按路径重导入本文件 (见 tushare_ready_probe.py 同款说明)。
"""
from __future__ import annotations

import argparse
import ast
import collections
import datetime as dt
import glob
import json
import os
import re
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

BJ = dt.timezone(dt.timedelta(hours=8))
DEFAULT_BASE = "https://api.tushare.pro/"

MAX_CALLS = 30              # 卡 TS-PROBE: 一次探针的调用总数**硬上限** (默认计划 26 次, 续页/重试也算); --max-calls 只能 1..30
GAP_SEC = 1.0               # 两次调用之间的静默秒数 (trade_cal 连打 10 次, 别把镜像的分钟频控打满)
BUDGET_SEC = 900.0          # 整个探针的墙钟预算; 用完不再发调用 (剩下的判 SKIP)
DEADLINE_MARGIN_SEC = 30.0  # 探针每次调用的硬期限 = 该端点各消费方里**最宽**的期限 + 这点余量 (量得出实际耗时, 再按各档期限判)
SLOW_FRAC = 0.5             # 延迟超过期限的这个比例 -> WARN
LOOKBACK_DAYS = 200         # 区间日历往回看的自然日 (盖住最近两三个长假; 真值是内置休市表, 不靠镜像)
RANGE_OPEN_DAYS = 20        # 按 ts_code 区间取最近多少个开市日
RANGE_CODE = "000001.SZ"    # 按 ts_code 区间的样本股 (平安银行, 极少停牌)
SW_L1_FALLBACK = "801010.SI"  # 申万分类没拿到时, 成员/日线那两次用的 L1 (农林牧渔)
ROUND_CAPS = (1000, 2000, 3000, 4000, 5000, 6000, 8000, 9000, 10000)   # 行数恰为整数 -> 疑似单页上限截断
RATE_BACKOFF_SEC = 12.0     # 超频后重试前等多久 (= tushare_client.BACKOFF_RATE_SEC)
PAST_DAY_MIN_AGE = 7        # 按日问的 P 至少在这么多自然日之前 (与最近开市日拉开, 测得出「无视 trade_date」的镜像)
TOKEN_FRAG_MIN = 8          # token 的片段 >= 这么长也抹 (网关/镜像只回显了一截时)
RAW_HEAD_SCAN = 65536       # 非 JSON 回包: 先在前这么多字里抹 token, 再截前 120 字

LEVELS = ("PASS", "WARN", "FAIL", "SKIP")
_RANK = {lv: i for i, lv in enumerate(LEVELS)}


def _worst(*levels: str) -> str:
    return max((lv for lv in levels if lv), key=lambda lv: _RANK[lv], default="PASS")


# ================================================================= A 股休市表 (日历真值, 与被测镜像无关)
# 每年一行 (节日, 休市首日, 休市末日), 照抄交易所「部分节假日休市安排的通知」的区间 (= 国办放假通知的放假区间)。
# 区间里的周末本来就休市; 调休「上班」的周六/周日交易所照样休市 (公告原文「…为周末休市」), 所以只要这些区间 + 周末。
#   2025: 国办发明电〔2024〕12号 (2024-11-12, https://www.gov.cn/zhengce/content/202411/content_6986382.htm)
#         上证公告〔2024〕38号 (2024-12-23)
#   2026: 国办发明电〔2025〕7号 (2025-11-04, https://www.gov.cn/zhengce/content/202511/content_7047090.htm)
#         上证公告〔2025〕45号 (2025-12-22)
#   2027: 国办通知截至 2026-10-12 还没发布 (惯例每年 10 月底-11 月发次年安排, 交易所 12 月发休市通知) —— None = 未覆盖:
#         探针对 2027 的工作日判不了是不是假日, 只核周末, 并 WARN 提醒补表; 选日子时跳过这些判不了的日子。
# 维护点: ① 国办通知 / 交易所休市通知出来后照上面的格式补一行 (两者不一致以交易所为准, 并在这里写明); 交易所临时休市
#   (罕见) 也补进当年这一行。② 补完跑用例 tests/test_tushare_probe_compat.py 的 test_holiday_table_* (各年工作日休市
#   天数 + 用例文件里另抄一份的逐日清单, 两份互核)。③ 2025-01-01..2026-10-12 已与本机价格库 data/pricestore.db 的
#   idx_bars 日期与 bars 去重日逐日对过: 427 个开市日, 0 差异 (卡 TS-PROBE-2, 只读)。
A_CLOSED = {
    2025: (("元旦", "2025-01-01", "2025-01-01"), ("春节", "2025-01-28", "2025-02-04"),
           ("清明", "2025-04-04", "2025-04-06"), ("劳动节", "2025-05-01", "2025-05-05"),
           ("端午", "2025-05-31", "2025-06-02"), ("国庆·中秋", "2025-10-01", "2025-10-08")),
    2026: (("元旦", "2026-01-01", "2026-01-03"), ("春节", "2026-02-15", "2026-02-23"),
           ("清明", "2026-04-04", "2026-04-06"), ("劳动节", "2026-05-01", "2026-05-05"),
           ("端午", "2026-06-19", "2026-06-21"), ("中秋", "2026-09-25", "2026-09-27"),
           ("国庆", "2026-10-01", "2026-10-07")),
    2027: None,
}


def _as_date(d) -> dt.date:
    return d if isinstance(d, dt.date) else dt.date.fromisoformat(str(d)[:10])


def _closed_set(year: int):
    """内置表某年的休市日 (区间内全部自然日, 含周末) -> set[ISO]; 表没覆盖这一年 -> None。"""
    rows = A_CLOSED.get(year)
    if rows is None:
        return None
    out = set()
    for _name, a, b in rows:
        d, e = _as_date(a), _as_date(b)
        while d <= e:
            out.add(d.isoformat())
            d += dt.timedelta(days=1)
    return out


def a_open(d):
    """内置休市表 + 周末: 开市 True / 休市 False / 表没覆盖那一年的工作日 None (判不了)。"""
    d = _as_date(d)
    if d.weekday() >= 5:
        return False
    closed = _closed_set(d.year)
    if closed is None:
        return None
    return d.isoformat() not in closed


def holiday_truth(d0, d1) -> tuple:
    """[d0, d1] 逐自然日 -> ({ISO: 1 开市 / 0 休市} 判得了的, [判不了的工作日 ISO], {判不了的年份})。"""
    d, e = _as_date(d0), _as_date(d1)
    truth, unknown, years = {}, [], set()
    while d <= e:
        v = a_open(d)
        if v is None:
            unknown.append(d.isoformat())
            years.add(d.year)
        else:
            truth[d.isoformat()] = 1 if v else 0
        d += dt.timedelta(days=1)
    return truth, unknown, years


def table_open_days(d0, d1) -> list:
    """内置表里 [d0, d1] 的开市日 (判不了的不算) -> [ISO] 升序。"""
    truth, _, _ = holiday_truth(d0, d1)
    return sorted(d for d, v in truth.items() if v == 1)


def last_open_before(today, max_back: int = 90):
    """内置表里 today 之前 (不含) 最后一个开市日 -> ISO; 判不了的日子跳过; 一路找不到 -> None。"""
    d = _as_date(today)
    for _ in range(max_back):
        d -= dt.timedelta(days=1)
        if a_open(d) is True:
            return d.isoformat()
    return None


def last_holiday_on_or_before(today, max_back: int = 400):
    """内置表里 today 及之前最近的**工作日**休市日 (法定节假日) -> ISO / None。"""
    d = _as_date(today)
    for _ in range(max_back):
        if d.weekday() < 5 and a_open(d) is False:
            return d.isoformat()
        d -= dt.timedelta(days=1)
    return None


def past_by_date_day(today):
    """按日那几次问的日子 P: 内置表里**上个月最后一个开市的周二**, 且至少在 PAST_DAY_MIN_AGE 天前 (月初那几天上月末的
    周二离今天太近, 往前挪一周)。上个月表没覆盖 / 一个都不合格 -> 今天往前至少 2×PAST_DAY_MIN_AGE 天的最后一个开市日;
    还没有 -> None (调用方退回镜像日历并 WARN)。"""
    t = _as_date(today)
    first = t.replace(day=1)
    prev_month = (first - dt.timedelta(days=1)).month
    d = first - dt.timedelta(days=1)                     # 上个月最后一天
    while d.month == prev_month:
        if d.weekday() == 1 and (t - d).days >= PAST_DAY_MIN_AGE and a_open(d) is True:
            return d.isoformat()
        d -= dt.timedelta(days=1)
    return last_open_before(t - dt.timedelta(days=2 * PAST_DAY_MIN_AGE - 1), max_back=120)


def _prev_weekday(today) -> dt.date:
    d = _as_date(today) - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


# ================================================================= token 抹除 (先抹后截)
def scrub_token(text, token: str, min_frag: int = TOKEN_FRAG_MIN) -> str:
    """把文本里的 token 整串换成 <token>, 再把它任何 ≥ min_frag 字的片段换成 <token…> (先长后短)。
    **必须在截断之前调用** —— 先截后抹的话 token 跨在截断边界上整串匹配不上, 前缀会原样漏出去。"""
    s = str(text or "")
    tok = str(token or "")
    if not tok or not s:
        return s
    s = s.replace(tok, "<token>")
    n = len(tok)
    for size in range(n - 1, min_frag - 1, -1):
        for i in range(0, n - size + 1):
            frag = tok[i:i + size]
            if frag in s:
                s = s.replace(frag, "<token…>")
    return s


# ================================================================= 期望字段: 从代码里收集 (AST, 不手抄)
_API_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_QUERY_FUNCS = ("query", "_ts_query", "_pages")          # 第一个位置参数 = api 名
_WRAPPERS = ("trade_cal", "index_daily")                 # tushare_client 的薄封装 (外部以 tsc./tc./别名. 调)
_CLIENT_NAMES = ("tsc", "tc", "tushare_client")
_FIN_TABLES_NAME = "FIN_TABLES"                          # research/lake.py: {api: (表名, 主键, 文本列, 日期列)}
CONSUMER_PROD, CONSUMER_CATALOG, CONSUMER_LAKE = "生产", "数据总览", "研究湖"
TIER_PROD, TIER_LAKE = "生产", "研究湖"
TIER_OF = {CONSUMER_PROD: TIER_PROD, CONSUMER_CATALOG: TIER_PROD, CONSUMER_LAKE: TIER_LAKE}
TIER_DESC = {TIER_PROD: "生产 = a-share 流水线 + 数据总览; 决定退出码",
             TIER_LAKE: "研究湖 = PC data/lake; 不改退出码"}
CORE_SCANS = (("research/lake.py", CONSUMER_LAKE), ("research/data_catalog.py", CONSUMER_CATALOG))


def _lit(node, lookup):
    """AST 节点 -> 能静态求出的值 (str / int / float / list[str]); 求不出 -> None。
    支持: 常量; 字符串的列表/元组/集合字面量; 名字 (查 lookup); `",".join(X)`; `A + B`; `X[a:b]`。"""
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        v = node.value
        return v if isinstance(v, (str, int, float)) and not isinstance(v, bool) else None
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        vals = [_lit(e, lookup) for e in node.elts]
        return vals if all(isinstance(v, str) for v in vals) else None
    if isinstance(node, ast.Name):
        return lookup(node.id)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "join"
            and isinstance(node.func.value, ast.Constant) and isinstance(node.func.value.value, str)
            and len(node.args) == 1 and not node.keywords):
        seq = _lit(node.args[0], lookup)
        return node.func.value.value.join(seq) if isinstance(seq, list) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = _lit(node.left, lookup), _lit(node.right, lookup)
        if isinstance(a, list) and isinstance(b, list):
            return a + b
        if isinstance(a, str) and isinstance(b, str):
            return a + b
        return None
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice) and node.slice.step is None:
        seq = _lit(node.value, lookup)
        lo = _lit(node.slice.lower, lookup) if node.slice.lower is not None else None
        hi = _lit(node.slice.upper, lookup) if node.slice.upper is not None else None
        if isinstance(seq, list) and (lo is None or isinstance(lo, int)) and (hi is None or isinstance(hi, int)):
            return seq[lo:hi]
    return None


def _assign_pairs(stmt) -> list:
    """Assign / AnnAssign -> [(目标, 值)]; `a, b = x, y` 按位配对。"""
    if isinstance(stmt, ast.AnnAssign):
        return [(stmt.target, stmt.value)] if stmt.value is not None else []
    out = []
    for t in stmt.targets:
        if isinstance(t, ast.Tuple) and isinstance(stmt.value, ast.Tuple) and len(t.elts) == len(stmt.value.elts):
            out.extend(zip(t.elts, stmt.value.elts))
        else:
            out.append((t, stmt.value))
    return out


def _scope_nodes(body) -> tuple:
    """一个作用域里的全部节点 (源码顺序, 不下钻嵌套 def / class / lambda) + 直接嵌套的 def / class。"""
    nodes, nested = [], []
    stack = list(reversed(body))
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nested.append(n)
            continue
        if isinstance(n, ast.Lambda):
            continue
        nodes.append(n)
        stack.extend(reversed(list(ast.iter_child_nodes(n))))
    return nodes, nested


def _df_var(node, colvars: dict):
    """`df.columns` / `set(df.columns)` / 别名 (cols = set(df.columns)) -> 'df'; 认不出 -> None。"""
    if isinstance(node, ast.Attribute) and node.attr == "columns" and isinstance(node.value, ast.Name):
        return node.value.id
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in ("set", "list", "frozenset") and len(node.args) == 1):
        return _df_var(node.args[0], colvars)
    if isinstance(node, ast.Name):
        return colvars.get(node.id)
    return None


def _client_aliases(tree) -> tuple:
    """这份源码里 tushare_client 模块的名字: 惯用名 + import 语句里的别名
    (`from ashare import tushare_client as t` / `from . import tushare_client as tsc` / `import ashare.tushare_client as x`)。"""
    names = set(_CLIENT_NAMES)
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            for a in n.names:
                if a.name == "tushare_client":
                    names.add(a.asname or a.name)
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.asname and (a.name == "tushare_client" or a.name.endswith(".tushare_client")):
                    names.add(a.asname)
    return tuple(sorted(names))


def _sock_override(nodes, clients, lookup):
    """本作用域里 `<客户端>.TIMEOUT_SEC = N` (socket 超时被显式改掉, 如数据总览子进程的 10 s) -> N / None。"""
    vals = []
    for n in nodes:
        if not isinstance(n, (ast.Assign, ast.AnnAssign)):
            continue
        for t, v in _assign_pairs(n):
            if (isinstance(t, ast.Attribute) and t.attr == "TIMEOUT_SEC" and isinstance(t.value, ast.Name)
                    and t.value.id in clients):
                x = _lit(v, lookup)
                if isinstance(x, (int, float)) and x > 0:
                    vals.append(x)
    return min(vals) if vals else None


def _call_site(call, lookup, qual: str, ctx: dict):
    """一个 Call 节点是不是 Tushare 调用 -> 站点 dict / None。"""
    f = call.func
    if isinstance(f, ast.Name):
        name, owner = f.id, None
    elif isinstance(f, ast.Attribute):
        name = f.attr
        owner = f.value.id if isinstance(f.value, ast.Name) else ""
    else:
        return None
    clients = ctx["clients"]
    api, via_wrapper, dynamic = None, False, False
    if name in _QUERY_FUNCS and call.args:
        if owner not in (None, "self", "ds", "datasource") + tuple(clients):
            return None                          # df.query(expr) 之类, 不是 Tushare
        v = _lit(call.args[0], lookup)
        if isinstance(v, str):
            if not _API_RE.match(v):
                return None
            api = v
        else:
            dynamic = True                       # 湖的 self._pages(api, …): api 运行时才知道, 只记录不计入
    elif name in _WRAPPERS and owner in clients:
        api, via_wrapper = name, True
    else:
        return None
    kw = {k.arg: k.value for k in call.keywords if k.arg}
    fields = None                                # None = 不给 fields, 走镜像默认字段集
    if "fields" in kw:
        v = _lit(kw["fields"], lookup)
        if isinstance(v, str):
            fields = [x.strip() for x in v.split(",") if x.strip()] or None
        elif isinstance(v, list):
            fields = [x.strip() for x in v if x.strip()] or None
        else:
            fields = "dynamic"
    deadline = _lit(kw["deadline_sec"], lookup) if "deadline_sec" in kw else None
    return {"api": api, "via_wrapper": via_wrapper, "dynamic": dynamic, "fields": fields,
            "deadline": deadline if isinstance(deadline, (int, float)) else None, "sock_cap": None,
            "params": sorted(k for k in kw if k not in ("fields", "retries", "deadline_sec")),
            "file": ctx["rel"], "line": call.lineno + ctx.get("line_off", 0), "qual": qual or "<module>",
            "consumer": ctx["consumer"], "var": None, "refs": [], "_node": call}


def _visit(body, chain: list, qual: str, ctx: dict, out: list, is_class: bool = False, sock_cap=None) -> dict:
    nodes, nested = _scope_nodes(body)
    local: dict = {}

    def lookup(name):
        for sc in (local, *chain):
            if name in sc:
                return sc[name]
        return None

    colvars: dict = {}
    for n in nodes:                              # 本作用域能静态求值的赋值 (源码顺序, 后写覆盖先写)
        if isinstance(n, (ast.Assign, ast.AnnAssign)):
            for t, v in _assign_pairs(n):
                if isinstance(t, ast.Name):
                    val = _lit(v, lookup)
                    if val is not None:
                        local[t.id] = val
                    dv = _df_var(v, colvars)
                    if dv:
                        colvars[t.id] = dv
    cap = _sock_override(nodes, ctx["clients"], lookup)  # 本作用域改了 socket 超时 -> 本作用域及嵌套的站点都受它卡
    if cap is not None:
        sock_cap = cap if sock_cap is None else min(sock_cap, cap)
    sites = []
    for n in nodes:
        if isinstance(n, ast.Call):
            s = _call_site(n, lookup, qual, ctx)
            if s is not None:
                s["sock_cap"] = sock_cap
                sites.append(s)
    by_node = {id(s["_node"]): s for s in sites}
    for n in nodes:                              # 调用结果落到哪个变量 (df = …, df, n = …)
        if isinstance(n, ast.Assign) and id(n.value) in by_node:
            for t in n.targets:
                if isinstance(t, ast.Tuple) and t.elts and isinstance(t.elts[0], ast.Name):
                    t = t.elts[0]
                if isinstance(t, ast.Name):
                    by_node[id(n.value)]["var"] = t.id
    for n in nodes:                              # 列引用: need <= set(df.columns) / "x" in df.columns
        if not (isinstance(n, ast.Compare) and len(n.ops) == 1):
            continue
        op, right = n.ops[0], n.comparators[0]
        cols, var = None, None
        if isinstance(op, ast.LtE):
            v = _lit(n.left, lookup)
            if isinstance(v, list):
                cols, var = v, _df_var(right, colvars)
        elif isinstance(op, (ast.In, ast.NotIn)) and isinstance(n.left, ast.Constant) \
                and isinstance(n.left.value, str):
            cols, var = [n.left.value], _df_var(right, colvars)
        if not cols or not var:
            continue
        cands = [s for s in sites if s["var"] == var and s["line"] - ctx.get("line_off", 0) <= n.lineno]
        if cands:
            max(cands, key=lambda s: s["line"])["refs"].extend(cols)
    out.extend(sites)
    sub_chain = chain if is_class else [local] + chain
    for d in nested:
        name = f"{qual}.{d.name}" if qual else d.name
        _visit(d.body, sub_chain, name, ctx, out, is_class=isinstance(d, ast.ClassDef), sock_cap=sock_cap)
    return local


def _embedded_sources(tree) -> list:
    """模块级字符串常量里的 Python 源码 (子进程脚本, 如 data_catalog 的 `_XCAL_CHILD`): 提到 tushare_client 且能解析的
    -> [(常量名, AST, 行号偏移)]。偏移让站点的行号落回原文件 (脚本第 k 行 = 字符串起始行 + k - 1)。"""
    out = []
    for stmt in tree.body:
        if not isinstance(stmt, (ast.Assign, ast.AnnAssign)) or stmt.value is None:
            continue
        v = stmt.value
        if not (isinstance(v, ast.Constant) and isinstance(v.value, str) and "tushare_client" in v.value):
            continue
        try:
            sub = ast.parse(v.value)
        except SyntaxError:
            continue
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        name = next((t.id for t in targets if isinstance(t, ast.Name)), "?")
        out.append((name, sub, v.lineno - 1))
    return out


def _fin_tables(tree) -> dict:
    """research/lake.py 的 FIN_TABLES 字面量 -> {api: {"table": 表名, "refs": [主键/文本/日期列]}}。"""
    out = {}
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign) or not isinstance(stmt.value, ast.Dict):
            continue
        if not any(isinstance(t, ast.Name) and t.id == _FIN_TABLES_NAME for t in stmt.targets):
            continue
        for k, v in zip(stmt.value.keys, stmt.value.values):
            if not (isinstance(k, ast.Constant) and isinstance(k.value, str)) or not isinstance(v, ast.Tuple):
                continue
            if not v.elts or not isinstance(v.elts[0], ast.Constant):
                continue
            refs = []
            for e in v.elts[1:]:
                for c in ast.walk(e):
                    if isinstance(c, ast.Constant) and isinstance(c.value, str) and c.value not in refs:
                        refs.append(c.value)
            out[k.value] = {"table": str(v.elts[0].value), "refs": refs}
    return out


def scan_source(src: str, rel: str, consumer: str) -> dict:
    """一份源码 -> {"sites": [Tushare 调用站点], "consts": 模块级常量, "fin_tables": {…}}。纯函数, 不 import。
    模块级字符串常量里的子进程脚本 (见 _embedded_sources) 一并扫, 站点的 qual 记成「常量名 (子进程脚本)」。"""
    tree = ast.parse(src)
    sites: list = []
    consts = _visit(tree.body, [], "", {"rel": rel, "consumer": consumer, "clients": _client_aliases(tree),
                                        "line_off": 0}, sites)
    for name, sub, off in _embedded_sources(tree):
        _visit(sub.body, [], f"{name} (子进程脚本)",
               {"rel": rel, "consumer": consumer, "clients": _client_aliases(sub), "line_off": off}, sites)
    for s in sites:
        s.pop("_node", None)
    return {"sites": sites, "consts": consts, "fin_tables": _fin_tables(tree)}


def find_core_dir(explicit: str | None = None) -> str | None:
    """stock-core 在哪 (找 research/lake.py): --core → STOCK_CORE_DIR → 同级 ../stock-core (与 ashare.config 同序)。"""
    for c in (explicit, os.environ.get("STOCK_CORE_DIR"), os.path.join(os.path.dirname(ROOT), "stock-core")):
        if c and os.path.isfile(os.path.join(c, "research", "lake.py")):
            return c
    return None


def read_lake_meta(path: str | None) -> dict:
    """lake.db (只读) -> {"fields": {表: [字段]}, "rows": {(表, 期 ISO): n_raw}, "error": 文案}。打不开就空。"""
    out = {"fields": {}, "rows": {}, "error": ""}
    if not path:
        out["error"] = "未给 lake.db 路径"
        return out
    if not os.path.isfile(path):
        out["error"] = f"没有 {path}"
        return out
    try:
        uri = "file:" + os.path.abspath(path).replace("\\", "/") + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=3.0)
        try:
            for tbl, val in conn.execute("SELECT tbl, value FROM meta WHERE key='fields'"):
                try:
                    f = json.loads(val)
                except (TypeError, ValueError):
                    continue
                if isinstance(f, list) and f:
                    out["fields"][str(tbl)] = [str(x) for x in f]
            for tbl, key, n in conn.execute(
                    "SELECT tbl, key, n_raw FROM pull_log WHERE status='ok' AND tbl IN ('fina_indicator','income')"):
                out["rows"][(str(tbl), str(key))] = int(n or 0)
        finally:
            conn.close()
    except Exception as e:                                     # noqa: BLE001  (锁住 / 坏库: 退回代码期望)
        out["error"] = f"lake.db 只读打开失败 ({type(e).__name__})"
    return out


def _uniq(seq) -> list:
    return list(dict.fromkeys(x for x in seq if x))


def collect_expectations(a_root: str = ROOT, core: str | None = None, lake_meta: dict | None = None) -> dict:
    """扫代码 -> {"apis": {api: 期望}, "dynamic": [动态站点], "consts": {文件: 常量}, "fin_page": int|None, "notes": []}。

    期望 = {"mode": explicit|default, "fields": 显式请求的字段 (各站点并集), "refs": 列引用, "expect": 要核的列,
           "default_sites": 走默认字段集的站点数, "deadline_by": {消费方: 该消费方站点里最严的期限},
           "deadline": 各消费方里最严的, "wait": 各消费方里最宽的 (探针自己等多久), "consumers": [...],
           "sites": ["文件:行 函数 (字段说明)"], "meta_source": 财务字段表来源}。
    站点期限 = min(deadline_sec 没写就是客户端 DEADLINE_SEC, 同作用域显式改的 TIMEOUT_SEC)。"""
    notes, sites, consts = [], [], {}
    for p in sorted(glob.glob(os.path.join(a_root, "ashare", "*.py"))):
        rel = "ashare/" + os.path.basename(p)
        with open(p, encoding="utf-8") as f:
            res = scan_source(f.read(), rel, CONSUMER_PROD)
        sites += res["sites"]
        consts[rel] = res["consts"]
    fin, fin_page = {}, None
    core = find_core_dir(core)
    if core:
        for rel_core, cons in CORE_SCANS:
            p = os.path.join(core, *rel_core.split("/"))
            rel = "stock-core/" + rel_core
            if not os.path.isfile(p):
                notes.append(f"stock-core 里没有 {rel_core}: {cons}的期望未纳入")
                continue
            with open(p, encoding="utf-8") as f:
                res = scan_source(f.read(), rel, cons)
            sites += res["sites"]
            consts[rel] = res["consts"]
            if rel_core == "research/lake.py":
                fin = res["fin_tables"]
                fp = res["consts"].get("FIN_PAGE")
                fin_page = fp if isinstance(fp, int) else None
    else:
        notes.append("找不到 stock-core/research/lake.py (STOCK_CORE_DIR / 同级 ../stock-core): "
                     "研究湖与数据总览的期望未纳入")
    lake_meta = lake_meta or {"fields": {}, "rows": {}, "error": "未读 lake.db"}
    client_dl = consts.get("ashare/tushare_client.py", {}).get("DEADLINE_SEC")
    client_dl = client_dl if isinstance(client_dl, (int, float)) else 120

    apis: dict = {}

    def entry(api):
        return apis.setdefault(api, {"api": api, "fields": [], "refs": [], "default_sites": 0, "explicit_sites": 0,
                                     "dyn_fields_sites": 0, "deadline_by": {}, "consumers": [], "sites": [],
                                     "meta_source": None})

    def note_deadline(e, consumer, secs):
        cur = e["deadline_by"].get(consumer)
        e["deadline_by"][consumer] = secs if cur is None else min(cur, secs)

    dynamic = []
    for s in sites:
        where = f"{s['file']}:{s['line']} {s['qual']}"
        if s["dynamic"]:
            dynamic.append(where)
            continue
        e = entry(s["api"])
        e["consumers"].append(s["consumer"])
        e["refs"] += s["refs"]
        eff = s["deadline"] or client_dl
        if s.get("sock_cap"):
            eff = min(eff, s["sock_cap"])
        note_deadline(e, s["consumer"], eff)
        lim = (f"; 期限 {eff:g}s" + (f" (TIMEOUT_SEC={s['sock_cap']:g})" if s.get("sock_cap") else "")
               if eff != client_dl else "")
        if s["via_wrapper"]:
            e["sites"].append(where + f" (经 tushare_client 封装{lim})")
            continue
        if s["fields"] == "dynamic":
            e["dyn_fields_sites"] += 1
            e["sites"].append(where + f" (fields 运行时才知道{lim})")
        elif s["fields"] is None:
            e["default_sites"] += 1
            e["sites"].append(where + f" (默认字段集{lim})")
        else:
            e["explicit_sites"] += 1
            e["fields"] += s["fields"]
            e["sites"].append(where + f" ({len(s['fields'])} 列{lim})")
    for api, ft in fin.items():
        e = entry(api)
        e["consumers"].append(CONSUMER_LAKE)
        note_deadline(e, CONSUMER_LAKE, client_dl)            # 湖经 tushare_client.query 的默认期限翻页
        e["refs"] += ft["refs"]
        mf = lake_meta["fields"].get(ft["table"])
        if mf:
            e["fields"] += mf
            e["explicit_sites"] += 1
            e["meta_source"] = f"lake.db meta {ft['table']}.fields ({len(mf)} 列, 湖下次真会请求的那份)"
        else:
            e["fields"] += ft["refs"]
            e["explicit_sites"] += 1
            e["meta_source"] = (f"FIN_TABLES 主键/文本/日期列 ({len(ft['refs'])} 列; lake.db 字段表不可用: "
                                f"{lake_meta.get('error') or '无 ' + ft['table'] + ' 字段表'})")
        e["sites"].append(f"stock-core/research/lake.py FIN_TABLES[{api!r}] -> 表 {ft['table']}")
    for e in apis.values():
        e["fields"], e["refs"] = _uniq(e["fields"]), _uniq(e["refs"])
        e["consumers"] = _uniq(e["consumers"])
        if e["explicit_sites"]:
            e["mode"] = "explicit"
            e["expect"] = _uniq(e["fields"] + e["refs"])
        else:
            e["mode"] = "default"
            e["expect"] = list(e["refs"])
        by = e["deadline_by"] or {CONSUMER_PROD: client_dl}
        e["deadline"], e["wait"] = min(by.values()), max(by.values())
    return {"apis": apis, "dynamic": dynamic, "consts": consts, "fin_page": fin_page, "notes": notes,
            "core": core, "lake_meta_error": lake_meta.get("error", ""), "client_deadline": client_dl}


def print_expectations(exp: dict, out=print) -> None:
    out("代码期望字段与期限 (AST 静态收集, 不手抄; 生产 = a-share ashare/*.py, 研究湖 = stock-core research/lake.py, "
        "数据总览 = stock-core research/data_catalog.py)")
    for note in exp["notes"]:
        out(f"  注: {note}")
    for api in sorted(exp["apis"]):
        e = exp["apis"][api]
        mode = (f"显式 {len(e['fields'])} 列" if e["mode"] == "explicit" else "默认字段集")
        dls = " · ".join(f"{c} {s:g}s" for c, s in e["deadline_by"].items())
        out(f"\n  {api}  [{'+'.join(e['consumers'])}]  {mode}  · 要核 {len(e['expect'])} 列 · 期限 {dls}")
        if e["fields"]:
            out(f"    fields: {','.join(e['fields'])}")
        if e["refs"]:
            out(f"    列引用: {','.join(e['refs'])}")
        if e["meta_source"]:
            out(f"    财务字段表: {e['meta_source']}")
        for w in e["sites"]:
            out(f"    ← {w}")
    if exp["dynamic"]:
        out("\n  api 名运行时才知道的站点 (不计入, 财务两表由 FIN_TABLES 兜): " + " · ".join(exp["dynamic"]))


# ================================================================= 联网: 一次 POST (在硬期限工作进程里跑)
def _raw_post(base: str, payload: dict, timeout: float) -> dict:
    """一次 POST -> {"http", "secs", "bytes", "json", "head"}。**必须是模块级函数** (要 pickle 进工作进程)。
    secs 在工作进程里量 = 纯 HTTP 往返 + 下载, 不含进程池调度。head 只在回包不是 JSON 时给 (网关错误页): **先**在前
    RAW_HEAD_SCAN 字里把 token 抹掉 (整串 + ≥ TOKEN_FRAG_MIN 字的片段, scrub_token), **再**截前 120 字 —— 反过来的话
    token 跨在第 120 字上时整串匹配不上, 前缀原样漏出去 (卡 TS-PROBE 校验员实测: 34 字的 token 最多漏 33 字)。"""
    import requests
    t0 = time.perf_counter()
    r = requests.post(base, json=payload, timeout=timeout,
                      headers={"Content-Type": "application/json", "Accept": "application/json"})
    body = r.content
    secs = time.perf_counter() - t0
    try:
        j = r.json()
    except Exception:                                          # noqa: BLE001
        j = None
    head = ""
    if j is None:
        head = scrub_token(r.text[:RAW_HEAD_SCAN], str((payload or {}).get("token") or ""))[:120]
    return {"http": r.status_code, "secs": secs, "bytes": len(body), "json": j, "head": head}


def deadline_post(base: str, payload: dict, deadline: float) -> dict:
    """生产同款执行器: tushare_client._deadline_call → datasource 进程池, 超 deadline 整池 terminate。
    socket 超时用客户端的 TIMEOUT_SEC (与生产一致: 首字节超过它生产也会失败)。"""
    from ashare import tushare_client as tsc
    sock = min(float(getattr(tsc, "TIMEOUT_SEC", 60)), float(deadline))
    return tsc._deadline_call(_raw_post, (base, payload, sock), {}, deadline)


def _classify_msg(code, msg: str) -> str:
    """镜像业务错误 -> RATE / NOPERM / BADPARAM / API (分类沿用 tushare_client._classify, 顺序: 超频 → 参数 → 权限)。"""
    try:
        from ashare import tushare_client as tsc
        err = tsc._classify("probe", code, msg)
        if isinstance(err, tsc.TushareRateLimited):
            return "RATE"
        if isinstance(err, tsc.TushareNoPermission):
            return "NOPERM"
        if isinstance(err, tsc.TushareBadParams):
            return "BADPARAM"
    except Exception:                                          # noqa: BLE001
        pass
    return "API"


class Prober:
    """按计划逐次调用并记录形状。调用数到 max_calls (封顶 MAX_CALLS, 给多大都一样) 或墙钟预算用完就不再发
    (记录 kind=SKIP)。传输失败 / 超频各重试一次 (也算调用数); 硬期限超时不重试 (那就是结论)。"""

    def __init__(self, token: str, base: str, post=None, max_calls: int = MAX_CALLS, gap: float = GAP_SEC,
                 budget: float = BUDGET_SEC, sleep=time.sleep, clock=time.time):
        self.token, self.base = token, base
        self.post = post or deadline_post
        self.max_calls = max(0, min(int(max_calls), MAX_CALLS))     # 硬上限 (卡 TS-PROBE-2: 文档写的「硬上限」要真封顶)
        self.gap, self.budget = float(gap), float(budget)
        self.sleep, self.clock = sleep, clock
        self.t0 = clock()
        self.n = 0
        self.records: list = []

    def clean(self, text) -> str:
        """镜像回的文案: **先**抹 token (整串 + ≥ TOKEN_FRAG_MIN 字片段), 再去换行、截 120 字。"""
        s = scrub_token(str(text or ""), self.token)
        return " ".join(s.split())[:120]

    def call(self, rid: str, label: str, api: str, params: dict, fields: str = "", deadline: float = 120.0) -> dict:
        rec = {"id": rid, "label": label, "api": api,
               "params": {k: v for k, v in params.items() if v is not None}, "fields_req": fields or "",
               "kind": "SKIP", "http": None, "code": None, "msg": "", "secs": None, "rows": 0,
               "fields": [], "items": [], "bytes": 0, "tries": 0, "first_fail": ""}
        self.records.append(rec)
        for attempt in (1, 2):
            if self.n >= self.max_calls:
                why = f"调用上限 {self.max_calls} 次已用完"
                if attempt == 1:
                    rec.update(kind="SKIP", msg=why)
                break
            left = self.budget - (self.clock() - self.t0)
            if left < 5:
                if attempt == 1:
                    rec.update(kind="SKIP", msg=f"墙钟预算 {self.budget:.0f}s 用完")
                break
            if self.n:
                self.sleep(self.gap)
            self.n += 1
            rec["tries"] += 1
            payload = {"api_name": api, "token": self.token, "params": rec["params"], "fields": rec["fields_req"]}
            self._one(rec, payload, max(5.0, min(float(deadline), left)))
            if rec["kind"] in ("TRANSPORT", "RATE") and attempt == 1:
                rec["first_fail"] = f"{rec['kind']}: {rec['msg']}"
                self.sleep(RATE_BACKOFF_SEC if rec["kind"] == "RATE" else 2.0)
                continue
            break
        return rec

    def _one(self, rec: dict, payload: dict, deadline: float) -> None:
        t = self.clock()
        rec.update(http=None, code=None, msg="", rows=0, fields=[], items=[], bytes=0)
        try:
            out = self.post(self.base, payload, deadline)
        except TimeoutError:
            rec.update(kind="DEADLINE", secs=round(self.clock() - t, 2),
                       msg=f"超过 {deadline:.0f}s 硬期限 (工作进程已终止)")
            return
        except Exception as e:                                 # noqa: BLE001  (requests 的连接/读超时等)
            rec.update(kind="TRANSPORT", secs=round(self.clock() - t, 2),
                       msg=f"{type(e).__name__}: {self.clean(e)}")
            return
        rec["http"] = out.get("http")
        rec["secs"] = round(float(out.get("secs") if out.get("secs") is not None else self.clock() - t), 2)
        rec["bytes"] = int(out.get("bytes") or 0)
        j = out.get("json")
        if rec["http"] != 200:
            if isinstance(j, dict):
                rec.update(code=j.get("code"), msg=self.clean(j.get("msg")))
            else:
                rec["msg"] = self.clean(out.get("head"))
            rec["kind"] = "HTTP"
            return
        if not isinstance(j, dict):
            rec.update(kind="NONJSON", msg=self.clean(out.get("head")))
            return
        rec["code"] = j.get("code")
        if rec["code"] != 0:
            rec["msg"] = self.clean(j.get("msg"))
            rec["kind"] = _classify_msg(rec["code"], rec["msg"])
            return
        d = j.get("data") or {}
        rec["fields"] = [str(x) for x in (d.get("fields") or [])]
        rec["items"] = list(d.get("items") or [])
        rec["rows"] = len(rec["items"])
        rec["kind"] = "OK"


# ================================================================= 判读 (纯函数, 用例直接喂假回包)
def _iso(v) -> str:
    s = str(v or "").strip().replace("-", "")[:8]
    return f"{s[:4]}-{s[4:6]}-{s[6:]}" if len(s) == 8 and s.isdigit() else ""


def _ymd(d) -> str:
    return str(d)[:10].replace("-", "")


def _dicts(rec: dict) -> list:
    f = rec.get("fields") or []
    return [dict(zip(f, row)) for row in (rec.get("items") or []) if isinstance(row, (list, tuple))]


def _int_or_none(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _fail_text(rec: dict) -> str:
    k = rec.get("kind")
    if k == "SKIP":
        return f"未调用: {rec.get('msg')}"
    if k == "HTTP":
        return f"HTTP {rec.get('http')}" + (f" code={rec.get('code')}" if rec.get("code") is not None else "") + \
            (f" {rec.get('msg')}" if rec.get("msg") else "")
    if k in ("RATE", "NOPERM", "BADPARAM", "API"):
        label = {"RATE": "超频", "NOPERM": "无权限/积分不足", "BADPARAM": "参数不合法", "API": "业务错误"}[k]
        return f"{label} code={rec.get('code')} {rec.get('msg')}"
    return f"{k}: {rec.get('msg')}"


def call_level(rec: dict):
    """调用本身成没成 -> (级别, 文案) 或 None (= 成了, 继续判形状)。"""
    if rec.get("kind") == "OK":
        return None
    if rec.get("kind") == "SKIP":
        return "SKIP", _fail_text(rec)
    return "FAIL", _fail_text(rec)


def retry_note(rec: dict):
    if rec.get("first_fail") and rec.get("kind") == "OK":
        return "WARN", f"首发失败 ({rec['first_fail'][:60]}), 重试成功"
    return None


def field_check(rec: dict, expect: list):
    """要核的列都回来了? -> (级别, 文案)。"""
    got = set(rec.get("fields") or [])
    if not expect:
        return "PASS", f"返回 {len(got)} 列 (代码无期望字段)"
    miss = [c for c in expect if c not in got]
    if miss:
        return "FAIL", f"缺 {len(miss)}/{len(expect)} 列: {','.join(miss[:8])}{' …' if len(miss) > 8 else ''}"
    return "PASS", f"字段 {len(expect)}/{len(expect)} 齐 (返回 {len(got)} 列)"


def latency_check(rec: dict, deadline: float, who: str = ""):
    """一次调用的延迟对照一个期限 (who = 定这个期限的消费方, 只进文案)。"""
    secs = rec.get("secs")
    if secs is None:
        return None
    lab = f"{who}期限" if who else "生产期限"
    if secs > deadline:
        return "FAIL", f"延迟 {secs:.1f}s > {lab} {deadline:g}s (这次会超时)"
    if secs > deadline * SLOW_FRAC:
        return "WARN", f"延迟 {secs:.1f}s > {lab} {deadline:g}s 的 {SLOW_FRAC:.0%}"
    return "PASS", f"延迟 {secs:.1f}s / {lab} {deadline:g}s"


def round_cap_note(n: int, ref: int | None):
    if n in ROUND_CAPS and (ref is None or n < ref):
        return "WARN", f"行数恰为 {n}, 疑似镜像单页上限截断"
    return None


def combine(parts: list):
    """[(级别, 文案) | None, …] -> (最差级别, 用「; 」连起来的文案)。"""
    parts = [p for p in parts if p]
    if not parts:
        return "PASS", ""
    return _worst(*(p[0] for p in parts)), "; ".join(p[1] for p in parts if p[1])


def date_check(rec: dict, day: str, prod_filters: bool = False):
    """按日回包的 trade_date 是不是都等于所问那天。生产不按日期过滤的端点 (daily / adj_factor / daily_basic) 有一行
    不符就 FAIL —— 别日的数据会被当成这天写进库; 生产按日期过滤的 (ths_daily / index_daily 单日) 判 WARN。
    回包没有 trade_date 列由字段检查管 (这几个端点生产都请求了 trade_date)。"""
    ds = _dicts(rec)
    if not ds or "trade_date" not in ds[0]:
        return None
    want = _ymd(day)
    off = collections.Counter(_ymd(r.get("trade_date")) for r in ds if _ymd(r.get("trade_date")) != want)
    if not off:
        return "PASS", f"回包日期全是 {day}"
    n_off = sum(off.values())
    eg = dict(off.most_common(2))
    if prod_filters:
        return "WARN", f"{n_off}/{len(ds)} 行不是所问的 {day} (如 {eg}; 生产只认所问那天, 这些行被丢掉)"
    return "FAIL", (f"{n_off}/{len(ds)} 行的 trade_date ≠ 所问的 {day} (如 {eg}): 镜像无视 trade_date? "
                    "生产不按日期过滤, 会把别日的数据当成这天写进库")


def cal_table(rec: dict) -> dict:
    """trade_cal 回包 -> {ISO 日期: is_open(int|None)}。"""
    out = {}
    for r in _dicts(rec):
        d = _iso(r.get("cal_date"))
        if d:
            out[d] = _int_or_none(r.get("is_open"))
    return out


def replay_trade_cal(rec: dict, start: str, end: str, is_open: str | None = "1"):
    """把镜像这次的**原样回包**喂给生产客户端 tushare_client.trade_cal (打桩 query; 回放期间禁联网)。
    -> (开市日列表, 文案) ; 回放本身做不了 -> (None, 原因)。"""
    try:
        import pandas as pd
        from ashare import tushare_client as tsc
    except Exception as e:                                     # noqa: BLE001
        return None, f"回放不可用 ({type(e).__name__})"
    df = pd.DataFrame(rec.get("items") or [], columns=(rec.get("fields") or None))
    hit = []

    def fake_query(api, fields="", retries=None, deadline_sec=None, **params):
        hit.append(api)
        return df.copy()

    def no_net(*a, **k):
        raise RuntimeError("回放期间不许联网")

    orig_q, orig_dc = tsc.query, tsc._deadline_call
    tsc.query, tsc._deadline_call = fake_query, no_net
    try:
        days = tsc.trade_cal(start, end, is_open=is_open)
    except Exception as e:                                     # noqa: BLE001
        return None, f"回放失败 ({type(e).__name__}: {str(e)[:60]})"
    finally:
        tsc.query, tsc._deadline_call = orig_q, orig_dc
    if not hit:
        return None, "回放没拦住 (客户端 trade_cal 不再经 query, 回放要跟着改)"
    return list(days), ""


def check_cal_range(r_all: dict, r_open: dict, start: str, end: str, today: str, exp_fields: list) -> list:
    """① 区间两次 -> 恰好两行 [(检查项, 级别, 证据)] (不带 is_open 那次在前)。
    真值 = 内置 A 股休市表 + 周末 (A_CLOSED, 与被测镜像无关); 表没覆盖的年份只核周末并 WARN。"""
    rows = []
    d0, d1 = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    truth, unknown, years = holiday_truth(d0, d1)
    t_open = sorted(d for d, v in truth.items() if v == 1)
    cover = None
    if unknown:
        cover = ("WARN", f"内置休市表未覆盖 {'/'.join(str(y) for y in sorted(years))} (国办通知还没录入 A_CLOSED): "
                         f"这 {len(unknown)} 个工作日 ({unknown[0]}..{unknown[-1]}) 只核周末, 判不了是不是假日 —— 该补表了")
    item = "trade_cal 区间 (不带 is_open) 对照内置休市表"
    bad = call_level(r_all)
    if bad:
        rows.append((item, bad[0], bad[1]))
    else:
        cal = cal_table(r_all)
        want = {(d0 + dt.timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)}
        dates = [_iso(r.get("cal_date")) for r in _dicts(r_all)]
        parts = [field_check(r_all, exp_fields), retry_note(r_all), cover]
        dup, extra = len(dates) - len(set(dates)), sorted(set(dates) - want)
        miss = sorted(want - set(dates))
        if dup or extra:
            parts.append(("FAIL", f"重复 {dup} 行 / 区间外 {len(extra)} 行"))
        past_miss = [d for d in miss if d <= today]
        if past_miss:
            parts.append(("FAIL", f"缺 {len(past_miss)} 个已过去的自然日 (如 {past_miss[0]})"))
        elif miss:
            parts.append(("WARN", f"日历只填到 {max(dates) if dates else '?'} (今天之后缺 {len(miss)} 天)"))
        badv = sorted(d for d, v in cal.items() if v not in (0, 1))
        if badv:
            parts.append(("FAIL", f"is_open 取值异常 {len(badv)} 行 (如 {badv[0]})"))
        wk_open = sorted(d for d, v in cal.items() if v == 1 and dt.date.fromisoformat(d).weekday() >= 5)
        if wk_open:
            parts.append(("FAIL", f"周末标成开市 {len(wk_open)} 天 (如 {wk_open[0]})"))
        hol_open = sorted(d for d, v in cal.items()
                          if v == 1 and truth.get(d) == 0 and dt.date.fromisoformat(d).weekday() < 5)
        if hol_open:
            parts.append(("FAIL", f"把法定假日标成开市 {len(hol_open)} 天 (如 {', '.join(hol_open[:3])}): 生产 trading_days "
                                  "会把假日当开市日, 价格库那天拉到 0 行判「未就绪」就地停更"))
        open_closed = sorted(d for d, v in cal.items() if v == 0 and truth.get(d) == 1)
        if open_closed:
            parts.append(("FAIL", f"把开市日标成休市 {len(open_closed)} 天 (如 {', '.join(open_closed[:3])}): "
                                  "生产会漏掉这些交易日"))
        n_open = sum(1 for v in cal.values() if v == 1)
        n_wkd_closed = sum(1 for d, v in cal.items() if v == 0 and dt.date.fromisoformat(d).weekday() < 5)
        judged = [d for d in truth if d in cal]
        agree = sum(1 for d in judged if cal[d] == truth[d])
        fut = sorted(d for d, v in cal.items() if v == 1 and d > today)
        if not fut:
            parts.append(("WARN", f"今天之后到 {end} 没有开市日: 看板 next_open_day 会写 null, 闸门覆盖自检判不了"))
        lv, txt = combine(parts)
        rows.append((item, lv,
                     f"{start}..{end} {len(dates)} 行 = {len(want)} 个自然日{'' if not miss else ' (有缺)'}; "
                     f"镜像开市 {n_open} / 休市 {len(cal) - n_open} (其中工作日休市 {n_wkd_closed}); "
                     f"与内置休市表逐日一致 {agree}/{len(judged)} 天 (表里开市 {len(t_open)}); "
                     f"今天后首个开市日 {fut[0] if fut else '无'}; {txt}"))
    item = "trade_cal 区间 is_open=1 → 客户端回放"
    bad = call_level(r_open)
    if bad:
        rows.append((item, bad[0], bad[1]))
        return rows
    parts = [field_check(r_open, exp_fields), retry_note(r_open)]
    got = cal_table(r_open)
    closed_back = sorted(d for d, v in got.items() if v == 0)
    if closed_back:
        parts.append(("WARN", f"服务端区间查询也不套过滤: 多给 {len(closed_back)} 行 is_open=0 (客户端本地过滤兜住)"))
    days, why = replay_trade_cal(r_open, start, end, "1")
    if days is None:
        parts.append(("WARN", why))
    else:
        lost = sorted(set(t_open) - set(days))
        wrong = sorted(d for d in days if truth.get(d) == 0)
        if lost:
            parts.append(("FAIL", f"客户端回放漏了 {len(lost)} 个开市日 (内置休市表; 如 {lost[0]})"))
        if wrong:
            parts.append(("FAIL", f"客户端回放把 {len(wrong)} 个休市日当开市 (内置休市表; 如 {wrong[0]})"))
        if not lost and not wrong:
            n_unk = len([d for d in days if d not in truth])
            parts.append(("PASS", f"客户端回放 = 内置休市表 {len(t_open)} 个开市日"
                                  + (f" (另 {n_unk} 天在表没覆盖的年份, 不判)" if n_unk else "")))
    lv, txt = combine(parts)
    rows.append((item, lv, f"{len(got)} 行; {txt}"))
    return rows


def check_cal_day(kind: str, day: str, r_with: dict, r_without: dict, expect_open: bool, exp_fields: list):
    """单日两次 (带 / 不带 is_open) + 客户端回放 -> (检查项, 级别, 证据)。日子本身取自内置休市表。"""
    item = f"trade_cal 单日 {kind} {day}"
    parts, ev = [], []
    t = a_open(day)
    if t is not None and t != expect_open:
        parts.append(("FAIL", f"内置休市表说 {day} {'开市' if t else '休市'}, 与「{kind}」矛盾 (选日子的代码错了)"))
    for tag, rec in (("带 is_open=1", r_with), ("不带", r_without)):
        bad = call_level(rec)
        if bad:
            parts.append((bad[0], f"{tag}: {bad[1]}"))
            continue
        for p in (field_check(rec, exp_fields), retry_note(rec)):
            if p and p[0] != "PASS":                 # 两次调用的字段问题只在有问题时才写进证据, 免得刷屏
                parts.append((p[0], f"{tag}: {p[1]}"))
        tab = cal_table(rec)
        other = sorted(d for d in tab if d != day)
        if other:
            parts.append(("FAIL", f"{tag}: 回了别的日子 {other[:2]}"))
        v = tab.get(day, "无")
        ev.append(f"{tag}: {rec['rows']} 行" + (f" is_open={v}" if rec["rows"] else "") + f" ({rec['secs']}s)")
        if expect_open:
            if rec["rows"] == 0:
                parts.append(("FAIL", f"{tag}: 开市日单日查询返回空 —— 就绪闸门会把交易日当休市放行"))
            elif v != 1:
                parts.append(("FAIL", f"{tag}: 开市日 is_open={v}"))
        elif tag.startswith("带"):
            if rec["rows"] and v == 1:
                parts.append(("FAIL", f"{tag}: 休市日被标成开市"))
            elif rec["rows"] and v == 0:
                parts.append(("WARN", f"{tag}: 服务端单日查询不套 is_open 过滤 (10-10 那个形态; 客户端 f1de80f 起本地过滤)"))
        else:
            if rec["rows"] == 0:
                parts.append(("WARN", f"{tag}: 单日不带 is_open 返回空 (拿不到 is_open=0 那行)"))
            elif v != 0:
                parts.append(("FAIL", f"{tag}: 休市日 is_open={v}"))
    if call_level(r_with) is None:
        days, why = replay_trade_cal(r_with, day, day, "1")
        want = [day] if expect_open else []
        if days is None:
            parts.append(("WARN", why))
        elif days != want:
            parts.append(("FAIL", f"客户端回放 trade_cal({day}) = {days}, 应为 {want}"))
        else:
            ev.append(f"客户端回放 {days} ✓")
    lv, txt = combine(parts)
    return item, lv, "; ".join(x for x in ev + [txt] if x)


def a_code_count(rec: dict, keep) -> tuple:
    """-> (A 股行数, A 股代码集合)。keep = ashare.market.keep_a_code。"""
    codes = [keep(r.get("ts_code")) for r in _dicts(rec)]
    codes = [c for c in codes if c]
    return len(codes), set(codes)


def check_stock_basic(recs: dict, expect: list, keep) -> tuple:
    """recs = {"L": rec, "D": rec, "P": rec} -> ([(检查项, 级别, 证据, [用到的调用])], listed_a | None, L 的 A 股代码集)。"""
    rows, listed_a, l_codes = [], None, set()
    sets = {}
    for st in ("L", "D", "P"):
        rec = recs.get(st)
        item = f"stock_basic list_status={st}"
        bad = call_level(rec) if rec else ("SKIP", "未调用")
        if bad:
            rows.append((item, bad[0], bad[1], [rec]))
            continue
        parts = [field_check(rec, expect), retry_note(rec)]
        ds = _dicts(rec)
        stat = collections.Counter(str(r.get("list_status")) for r in ds if "list_status" in r)
        wrong = {k: v for k, v in stat.items() if k != st}
        if wrong:
            parts.append(("FAIL", f"list_status={st} 却回了 {dict(wrong)} (镜像不认这个参数?)"))
        n_a, codes = a_code_count(rec, keep)
        sets[st] = codes
        ev = f"{rec['rows']} 行 (A 股 {n_a})"
        if st == "L":
            listed_a, l_codes = n_a, codes
            if n_a == 0:
                parts.append(("FAIL", "在市 A 股 0 只"))
        elif st == "D":
            if rec["rows"] == 0:
                parts.append(("FAIL", "退市股 0 只 —— 股票池会丢退市史 (幸存者偏差)"))
            else:
                nd = sum(1 for r in ds if _iso(r.get("delist_date")))
                ev += f", 带退市日 {nd}"
                if "delist_date" in (rec.get("fields") or []) and nd < 0.9 * rec["rows"]:
                    parts.append(("WARN", f"退市日为空的 {rec['rows'] - nd} 行"))
        lv, txt = combine(parts)
        rows.append((item, lv, ev + (f"; {txt}" if txt else ""), [rec]))
    if sets.get("L") and sets.get("D") is not None:
        both = sets["L"] & sets["D"]
        if both and both == sets["L"]:
            rows.append(("stock_basic L/D 是否分得开", "FAIL", "L 与 D 返回同一批代码", []))
        elif both:
            rows.append(("stock_basic L/D 是否分得开", "WARN", f"{len(both)} 只同时在 L 与 D", []))
    return rows, listed_a, l_codes


def check_by_date(item: str, rec: dict, day: str, expect: list, listed_a, ratio: float, parsed_n, extra=None):
    """全市场按 trade_date 的端点 (daily / adj_factor): 回包日期必须全是所问那天 (生产不过滤, 不符 = FAIL),
    行数 vs 在市 A 股 × ready_ratio。"""
    bad = call_level(rec)
    if bad:
        return item, bad[0], bad[1]
    parts = [field_check(rec, expect), retry_note(rec), round_cap_note(rec["rows"], None), date_check(rec, day)]
    ev = f"{rec['rows']} 行, 生产解析后 A 股 {parsed_n}"
    if listed_a:
        r = parsed_n / listed_a
        ev += f" / 在市 A 股 {listed_a} = {r:.1%}"
        if r < ratio:
            parts.append(("FAIL", f"< 未就绪守卫 {ratio:.0%}: 生产会判这天没到, 价格库停更"))
    else:
        parts.append(("WARN", "没有在市 A 股只数 (stock_basic L 没拿到), 无法对照守卫"))
    if extra:
        parts.append(extra)
    lv, txt = combine(parts)
    return item, lv, ev + (f"; {txt}" if txt else "")


def check_range(item: str, rec: dict, open_days: list, expect: list, start: str, end: str, index: bool = False,
                ref_dates=None):
    """按 ts_code 区间 / 指数区间: 内置休市表的每个开市日一行。指数 (index=True, 不停牌) 缺开市日 = FAIL, 个股缺 = WARN
    (停牌也会这样); 任何行落在内置表的休市日上 = FAIL (假数据); 区间外的行 = WARN (镜像不认 start/end)。"""
    bad = call_level(rec)
    if bad:
        return item, bad[0], bad[1]
    parts = [field_check(rec, expect), retry_note(rec)]
    got = sorted({_iso(r.get("trade_date")) for r in _dicts(rec) if _iso(r.get("trade_date"))})
    want = sorted(open_days)
    ev = f"{rec['rows']} 行 / 区间开市日 {len(want)} (内置休市表)"
    if not got:
        parts.append(("FAIL", "0 行"))
    else:
        closed = [d for d in got if a_open(d) is False]
        outside = [d for d in got if not (start <= d <= end)]
        miss = sorted(set(want) - set(got))
        if closed:
            parts.append(("FAIL", f"{len(closed)} 天的行落在休市日上 (内置休市表; 如 {closed[0]}): 假数据"))
        if outside:
            parts.append(("WARN", f"区间外 {len(outside)} 天 (如 {outside[0]}): 镜像不认 start/end"))
        if miss:
            parts.append(("FAIL", f"缺 {len(miss)} 个开市日 (如 {miss[0]}; 指数不停牌)") if index else
                         ("WARN", f"缺 {len(miss)} 个开市日 (如 {miss[0]}; 停牌也会这样)"))
    if ref_dates is not None and got and set(got) != set(ref_dates):
        parts.append(("WARN", f"与配对端点日期不一致 ({len(set(got) ^ set(ref_dates))} 天)"))
    lv, txt = combine(parts)
    return item, lv, ev + (f"; {txt}" if txt else "")


def check_fin(item: str, rec: dict, expect: list, page: int, ref_rows, rec2=None, confirm_min=None):
    """财务一期 (limit = 湖翻页步长 FIN_PAGE): 字段表全回 + 单页是否被镜像上限截断。

    不足一页时补一次 offset=n 的续页调用 (rec2) 定性: 0 行 = 该期就这么多; 还有行 = 镜像单页上限 n < FIN_PAGE。
    上限截断的后果看湖的翻页 (`Lake._pages`): confirm_min = lake.py 的 PAGE_CONFIRM_MIN (stock-core 7df1e67 起才有,
    从代码读) —— 有它且 n >= 它: 湖首页不足一页也会续一页确认、按 n 接着翻, 不丢, 只多花调用 (WARN); 没有它或
    n < 它: 湖「不足一页即停」, **静默少拉** (FAIL)。"""
    bad = call_level(rec)
    if bad:
        return item, bad[0], bad[1]
    parts = [field_check(rec, expect), retry_note(rec)]
    n = rec["rows"]
    ev = f"{n} 行 (limit {page})" + (f"; 湖里该期上次拉取 {ref_rows} 行" if ref_rows else "")
    if n == 0:
        parts.append(("FAIL", "0 行"))
    elif n > page:
        parts.append(("WARN", f"回了 {n} 行 > limit {page}: 镜像不认 limit, 湖的翻页会重复拉"))
    elif n == page:
        ev += " (满页, 湖会接着翻)"
    elif rec2 is None:
        parts.append(("WARN", f"不足一页且没能补续页调用 (调用上限/预算), 判不了是否被单页上限截断"))
    elif call_level(rec2):
        parts.append(("WARN", f"续页 offset={n} 调用失败: {call_level(rec2)[1]}"))
    elif rec2["rows"] > 0 and confirm_min and n >= confirm_min:
        parts.append(("WARN", f"镜像单页上限 {n} 行 < 湖翻页步长 {page} (续页 offset={n} 还有 {rec2['rows']} 行): "
                              f"湖首页 >= PAGE_CONFIRM_MIN {confirm_min} 会续页确认、按 {n} 接着翻, 不丢, 多花调用"))
    elif rec2["rows"] > 0:
        parts.append(("FAIL", f"单页只给 {n} 行 < 湖翻页步长 {page}, 续页 offset={n} 还有 {rec2['rows']} 行: "
                              + (f"湖首页不到 PAGE_CONFIRM_MIN {confirm_min} 行不续页确认" if confirm_min else
                                 "湖的翻页不足一页即停") + ", 会静默少拉"))
    else:
        ev += f"; 续页 offset={n} 0 行 = 该期就 {n} 行"
        if ref_rows and n < 0.9 * min(page, ref_rows):
            parts.append(("WARN", f"比湖里上次那份少 {min(page, ref_rows) - n} 行"))
    lv, txt = combine(parts)
    return item, lv, ev + (f"; {txt}" if txt else "")


def check_filtered(item: str, rec: dict, expect: list, filters: dict, page):
    """按参数过滤的维表 (申万分类 / 成员): 有行 + 回包里的过滤列是否等于所问 + 分页是否满页/超页。
    镜像不认过滤参数 = 湖得自己兜 (湖怎么兜、代价多大随 lake.py 版本变, 见 stock-core HANDBOOK「研究湖镜像兼容性」),
    探针只判 WARN; 调不通 / 0 行判 FAIL。"""
    bad = call_level(rec)
    if bad:
        return item, bad[0], bad[1]
    parts = [field_check(rec, expect), retry_note(rec)]
    ds = _dicts(rec)
    n = rec["rows"]
    ev = f"{n} 行"
    if n == 0:
        parts.append(("FAIL", "0 行"))
    for col, want in filters.items():
        if ds and col in ds[0]:
            wrong = sum(1 for r in ds if str(r.get(col)) != str(want))
            if wrong:
                parts.append(("WARN", f"镜像不认 {col} 过滤: {wrong}/{n} 行不是 {want} (湖得自己兜, 见 HANDBOOK"
                                      "「研究湖镜像兼容性」)"))
    if page:
        if n > page:
            parts.append(("WARN", f"回了 {n} 行 > limit {page}: 镜像不认 limit"))
        elif n == page:
            ev += f" (满页 {page}, 湖会接着翻)"
    lv, txt = combine(parts)
    return item, lv, ev + (f"; {txt}" if txt else "")


def tier_deadlines(e: dict) -> dict:
    """{档: (期限秒, 定这个期限的消费方)} —— 每档取本档消费方里最严的期限。"""
    out = {}
    for cons, secs in (e.get("deadline_by") or {}).items():
        t = TIER_OF.get(cons, TIER_PROD)
        if t not in out or secs < out[t][0]:
            out[t] = (float(secs), cons)
    return out


def judge_row(row: dict, e: dict) -> dict:
    """一行检查 = 形状级别 (row["lv"]) + 用到的那几次调用的延迟, 延迟**按每档自己的期限**判 ->
    {item, consumer, level (各档最差, 用于显示), tiers: {档: 级别}, evidence}。"""
    tiers = tier_deadlines(e) or {TIER_PROD: (float(e.get("deadline") or 120), CONSUMER_PROD)}
    timed = [r for r in row["recs"] if r and r.get("secs") is not None]
    tier_lv, notes = {}, []
    for t, (secs, who) in tiers.items():
        parts = [latency_check(r, secs, who) for r in timed]
        tier_lv[t] = _worst(row["lv"], *(p[0] for p in parts if p))
        for p in parts:
            if p and p[0] != "PASS":
                note = f"{t}档 {p[0]}: {p[1]}"
                if note not in notes:
                    notes.append(note)
    if timed and not notes:
        mx = max(r["secs"] for r in timed)
        secs, who = min(tiers.values())
        notes.append(f"延迟 {mx:.1f}s / 最严期限 {secs:g}s ({who})")
    ev = "; ".join(x for x in (row["ev"], "; ".join(notes)) if x)
    return {"item": row["item"], "consumer": "+".join(e.get("consumers") or []) or "-",
            "level": _worst(*tier_lv.values()), "tiers": tier_lv, "evidence": ev}


def tier_verdicts(checks: list) -> dict:
    """两档各自的结论: 有 FAIL = 不可用; 否则有 SKIP = 未跑完; 否则可用 (只有 WARN 也算可用); 没有检查项 = 未纳入。"""
    out = {}
    for t in (TIER_PROD, TIER_LAKE):
        rs = [c for c in checks if t in c["tiers"]]
        if not rs:
            out[t] = {"verdict": "未纳入", "n": 0, "fail": [], "skip": [], "warn": [],
                      "text": "本次没有这一档的检查项 (找不到 stock-core research/lake.py?)"}
            continue
        cnt = collections.Counter(c["tiers"][t] for c in rs)
        fails = [c["item"] for c in rs if c["tiers"][t] == "FAIL"]
        skips = [c["item"] for c in rs if c["tiers"][t] == "SKIP"]
        warns = [c["item"] for c in rs if c["tiers"][t] == "WARN"]
        tally = (f"{len(rs)} 项 PASS {cnt['PASS']} / WARN {cnt['WARN']} / FAIL {cnt['FAIL']}"
                 + (f" / SKIP {cnt['SKIP']}" if cnt["SKIP"] else ""))
        if fails:
            verdict, text = "不可用", tally + ": " + "; ".join(fails[:4]) + (" …" if len(fails) > 4 else "")
        elif skips:
            verdict, text = "未跑完", tally + " (调用上限/预算用完没跑到: " + "; ".join(skips[:3]) + \
                (" …" if len(skips) > 3 else "") + ")"
        else:
            verdict = "可用"
            text = tally + ("; 全部 PASS" if not warns else
                            "; WARN 换后盯: " + "; ".join(warns[:4]) + (" …" if len(warns) > 4 else ""))
        if t == TIER_LAKE and verdict != "可用":
            text += " —— 研究湖档不改退出码、不阻塞生产 (只换 token 时也别因此还原 .bak), 交 GM"
        out[t] = {"verdict": verdict, "n": len(rs), "fail": fails, "skip": skips, "warn": warns, "text": text}
    return out


# ================================================================= 日期
def latest_weekday(today: dt.date, wd: int) -> dt.date:
    """<= today 的最近一个星期 wd (周一 0 … 周日 6)。"""
    return today - dt.timedelta(days=(today.weekday() - wd) % 7)


def finance_period(today: dt.date) -> str:
    """最近一个**已过法定披露截止**的报告期 (一季报 4/30、半年报 8/31、三季报 10/31; 与 tushare_probe_full 同口径)。"""
    y, m = today.year, today.month
    period = f"{y}0630" if m >= 9 else (f"{y}0331" if m >= 5 else f"{y - 1}0930")
    if m >= 11:
        period = f"{y}0930"
    return period


# ================================================================= compat 主流程
def _host(base: str) -> str:
    try:
        from urllib.parse import urlsplit
        return urlsplit(base).hostname or "?"
    except Exception:                                          # noqa: BLE001
        return "?"


def _read_credentials(secrets_path: str | None) -> tuple:
    """-> (token, base)。默认 = 生产客户端 tushare_client.credentials() (secrets.json + 环境变量覆盖)。"""
    if secrets_path:
        with open(secrets_path, encoding="utf-8") as f:
            s = json.load(f)
        return (str(s.get("tushare_token") or "").strip(),
                str(s.get("tushare_base_url") or DEFAULT_BASE).strip() or DEFAULT_BASE)
    from ashare import tushare_client as tsc
    cred = tsc.credentials(refresh=True)
    return cred[0], cred[1]


def _runtime_thresholds() -> dict:
    """生产代码里的门槛 (运行时读本仓库模块, 不手抄)。读不到就用当前值并标注。"""
    th = {"ready_ratio": 0.9, "db_min_rows": 4000, "ti_suffix": ".TI", "cal_fwd": 20,
          "bench": "sh000300", "src": []}
    try:
        import inspect
        import ashare.config                                   # noqa: F401  (它把 stock-core / vendor 放上 sys.path)
        import leftside_core.pricestore as lp
        th["ready_ratio"] = float(inspect.signature(lp._update_daily_by_date).parameters["ready_ratio"].default)
        where = "/".join(str(lp.__file__).replace("\\", "/").split("/")[-3:])
        th["src"].append(f"未就绪守卫 ready_ratio={th['ready_ratio']:g} ← {where} _update_daily_by_date")
    except Exception as e:                                     # noqa: BLE001
        th["src"].append(f"ready_ratio 读不到 ({type(e).__name__}), 用 0.9")
    try:
        from ashare import datasource as ds
        th["db_min_rows"] = int(ds.TS_DAILY_BASIC_MIN_ROWS)
        th["ti_suffix"] = str(ds.THS_INDEX_SUFFIX)
        th["src"].append(f"daily_basic 行数门槛 {th['db_min_rows']} ← ashare.datasource.TS_DAILY_BASIC_MIN_ROWS")
    except Exception as e:                                     # noqa: BLE001
        th["src"].append(f"datasource 常量读不到 ({type(e).__name__})")
    try:
        from ashare import datadate
        th["cal_fwd"] = int(datadate.CAL_FWD_DAYS)
    except Exception:                                          # noqa: BLE001
        pass
    try:
        from ashare.config import CONFIG
        th["bench"] = str(CONFIG["source"].get("benchmark_index", "sh000300"))
    except Exception:                                          # noqa: BLE001
        pass
    return th


def _fields_arg(e: dict | None) -> str:
    """该端点探针要请求的 fields: 显式模式 = 要核的全部列; 默认模式 = "" (与生产那处一样走默认字段集)。"""
    if not e or e.get("mode") != "explicit":
        return ""
    return ",".join(e["expect"])


def run_compat(args, post=None, now: dt.datetime | None = None, out=print, sleep=time.sleep) -> int:
    try:
        max_calls = int(args.max_calls)
    except (TypeError, ValueError):
        max_calls = 0
    if not 1 <= max_calls <= MAX_CALLS:
        out(f"--max-calls 只能是 1..{MAX_CALLS} (硬上限 {MAX_CALLS} 次), 收到 {args.max_calls!r} —— 拒跑, 一次也没调")
        return 2
    for name in ("today", "date", "holiday", "period"):
        v = getattr(args, name, "")
        try:
            ok = not v or (len(str(v).replace("-", "")) == 8 and bool(dt.date.fromisoformat(_iso(v))))
        except ValueError:
            ok = False
        if not ok:
            out(f"--{name} 要 YYYYMMDD / YYYY-MM-DD, 收到 {v!r}")
            return 2
    lake_db = args.lake_db or os.path.join(ROOT, "data", "lake", "lake.db")
    lake_meta = read_lake_meta(None if args.no_lake_db else lake_db)
    exp = collect_expectations(ROOT, args.core, lake_meta)
    if args.expect:
        print_expectations(exp, out)
        return 0
    A = exp["apis"]

    def E(api):
        cdl = exp["client_deadline"]
        return A.get(api) or {"api": api, "mode": "default", "fields": [], "refs": [], "expect": [],
                              "deadline": cdl, "wait": cdl, "deadline_by": {CONSUMER_PROD: cdl},
                              "consumers": [CONSUMER_PROD], "sites": []}

    nowbj = (now or dt.datetime.now(BJ)).astimezone(BJ)
    today = dt.date.fromisoformat(_iso(args.today)) if args.today else nowbj.date()
    t_iso = today.isoformat()
    # 日子一律从内置休市表取 (与被测镜像无关); 手给的先对表核一遍, 不对就是参数错
    D = last_open_before(today)
    pday = _iso(args.date) if args.date else past_by_date_day(today)
    hol = _iso(args.holiday) if args.holiday else last_holiday_on_or_before(today)
    if args.date and (pday >= t_iso or a_open(pday) is False):
        out(f"--date {args.date}: 要一个今天 ({t_iso}) 之前的开市日 —— 内置休市表说它"
            f"{'休市' if a_open(pday) is False else '不在今天之前'}")
        return 2
    if args.holiday:
        hd = dt.date.fromisoformat(hol)
        if hd.weekday() >= 5 or a_open(hd) is True or hol > t_iso:
            out(f"--holiday {args.holiday}: 要一个今天及之前的工作日休市日 —— 内置休市表说它"
                f"{'是周末' if hd.weekday() >= 5 else ('开市' if a_open(hd) is True else '在今天之后')}")
            return 2

    try:
        token, base = _read_credentials(args.secrets)
    except Exception as e:                                     # noqa: BLE001
        out(f"读凭据失败 ({type(e).__name__}) —— 无法探针")
        return 2
    if not token:
        out("没有 tushare_token (data/secrets.json 或 --secrets) —— 无法探针")
        return 2
    th = _runtime_thresholds()
    from ashare import market as mk
    from ashare import tushare_client as tsc

    start = today - dt.timedelta(days=LOOKBACK_DAYS)
    end = today + dt.timedelta(days=th["cal_fwd"])
    sat, sun = latest_weekday(today, 5), latest_weekday(today, 6)
    pr = Prober(token, base, post=post, max_calls=max_calls, gap=args.gap, budget=args.budget, sleep=sleep)

    out("Tushare 镜像兼容性探针 (只读) · tools/tushare_probe.py compat")
    out(f"镜像主机 {_host(base)} · 北京时间 {nowbj:%Y-%m-%d %H:%M} · 今天(北京) {t_iso} 周{'一二三四五六日'[today.weekday()]}"
        f" · 调用上限 {max_calls} · 间隔 {args.gap:g}s · 墙钟预算 {args.budget:g}s")
    srcs = ["a-share ashare/*.py"] + ([f"stock-core {r}" for r, _ in CORE_SCANS] if exp["core"] else [])
    for api in ("fina_indicator_vip", "income_vip"):
        ms = (A.get(api) or {}).get("meta_source")
        if ms:
            srcs.append(f"{api}: {ms}")
    out("期望字段与期限来源: " + " + ".join(srcs) + " (完整清单: compat --expect)")
    for note in exp["notes"] + th["src"]:
        out(f"  · {note}")

    def dl(api):            # 探针这次调用自己的硬期限: 各消费方里最宽的期限 + 余量 (量出实际耗时, 再按各档期限判)
        return float(E(api)["wait"]) + DEADLINE_MARGIN_SEC

    # ① trade_cal ----------------------------------------------------------------------------------------
    cal_f = _fields_arg(E("trade_cal"))
    cal_dl = dl("trade_cal")
    r_all = pr.call("T01", f"区间 {start:%m-%d}..{end:%m-%d} 不带", "trade_cal",
                    {"exchange": "SSE", "start_date": _ymd(start), "end_date": _ymd(end)}, cal_f, cal_dl)
    r_open = pr.call("T02", f"区间 {start:%m-%d}..{end:%m-%d} is_open=1", "trade_cal",
                     {"exchange": "SSE", "start_date": _ymd(start), "end_date": _ymd(end), "is_open": "1"},
                     cal_f, cal_dl)
    m_open = sorted(d for d, v in cal_table(r_all).items() if v == 1) if r_all["kind"] == "OK" else []
    fallback = []                     # 内置表给不出 D / P (那几年没覆盖) -> 退回镜像日历 (不独立, 记一行 WARN)
    if D is None:
        past = [d for d in m_open if d < t_iso]
        D = past[-1] if past else _prev_weekday(today).isoformat()
        fallback.append(f"开市日 D={D}")
    if pday is None:
        lim = (today - dt.timedelta(days=PAST_DAY_MIN_AGE)).isoformat()
        cands = [d for d in m_open if d <= lim]
        pday = cands[-1] if cands else D
        fallback.append(f"按日 P={pday}")
    out(f"日子 (内置休市表, 与镜像无关{'; 部分退回镜像: ' + ', '.join(fallback) if fallback else ''}): "
        f"开市日 D = {D} · 按日问的已知过去开市日 P = {pday} ({'--date' if args.date else '上月最后一个开市的周二'})"
        f" · 假日一例 {hol or '无'}")
    days = [("周六", sat.isoformat(), False), ("周日", sun.isoformat(), False)]
    if hol:
        days.append(("假日", hol, False))
    days.append(("开市日", D, True))
    day_recs = []
    for i, (kind, day, is_open_day) in enumerate(days):
        p = {"exchange": "SSE", "start_date": _ymd(day), "end_date": _ymd(day)}
        rw = pr.call(f"T{3 + 2 * i:02d}", f"单日 {kind} {day} is_open=1", "trade_cal", dict(p, is_open="1"),
                     cal_f, cal_dl)
        ro = pr.call(f"T{4 + 2 * i:02d}", f"单日 {kind} {day} 不带", "trade_cal", p, cal_f, cal_dl)
        day_recs.append((kind, day, rw, ro, is_open_day))

    # ③ stock_basic (先拿: 它是 daily / adj 行数的分母) ------------------------------------------------------
    sb = E("stock_basic")
    sb_recs = {st: pr.call(f"S0{i + 1}", f"list_status={st}", "stock_basic", {"list_status": st},
                           _fields_arg(sb), dl("stock_basic"))
               for i, st in enumerate(("L", "D", "P"))}
    # ② daily / adj_factor 按 trade_date: 问已知过去的开市日 P ------------------------------------------------
    r_daily = pr.call("B01", f"全市场 {pday}", "daily", {"trade_date": _ymd(pday)}, _fields_arg(E("daily")),
                      dl("daily"))
    r_adj = pr.call("B02", f"全市场 {pday}", "adj_factor", {"trade_date": _ymd(pday)}, _fields_arg(E("adj_factor")),
                    dl("adj_factor"))
    # ③ daily_basic / index_daily / ths_daily --------------------------------------------------------------
    r_db = pr.call("V01", f"全市场 {pday}", "daily_basic", {"trade_date": _ymd(pday)}, _fields_arg(E("daily_basic")),
                   dl("daily_basic"))
    in_win = table_open_days(start, D)
    if fallback:
        in_win = sorted(set(in_win) | {d for d in m_open if d <= D})
    r_start = in_win[-RANGE_OPEN_DAYS] if len(in_win) >= RANGE_OPEN_DAYS else \
        (dt.date.fromisoformat(D) - dt.timedelta(days=28)).isoformat()
    rng_days = [d for d in in_win if r_start <= d <= D]
    bench = tsc.to_ts_code(th["bench"])
    r_idx = pr.call("I01", f"{bench} {r_start}..{D}", "index_daily",
                    {"ts_code": bench, "start_date": _ymd(r_start), "end_date": _ymd(D)},
                    _fields_arg(E("index_daily")), dl("index_daily"))
    r_idx1 = pr.call("I02", f"{bench} 单日 {pday}", "index_daily",
                     {"ts_code": bench, "start_date": _ymd(pday), "end_date": _ymd(pday)},
                     _fields_arg(E("index_daily")), dl("index_daily"))
    r_ths = pr.call("H01", f"全市场 {pday}", "ths_daily", {"trade_date": _ymd(pday)}, _fields_arg(E("ths_daily")),
                    dl("ths_daily"))
    # ② 按 ts_code 区间 -------------------------------------------------------------------------------------
    r_dr = pr.call("B03", f"{RANGE_CODE} {r_start}..{D}", "daily",
                   {"ts_code": RANGE_CODE, "start_date": _ymd(r_start), "end_date": _ymd(D)},
                   _fields_arg(E("daily")), dl("daily"))
    r_ar = pr.call("B04", f"{RANGE_CODE} {r_start}..{D}", "adj_factor",
                   {"ts_code": RANGE_CODE, "start_date": _ymd(r_start), "end_date": _ymd(D)},
                   _fields_arg(E("adj_factor")), dl("adj_factor"))
    # ③ 财务一期 ----------------------------------------------------------------------------------------------
    period = _ymd(args.period) if args.period else finance_period(today)
    page = exp["fin_page"] or 9000
    fin_recs = []
    for i, api in enumerate(("fina_indicator_vip", "income_vip")):
        if api not in A:                                          # 代码里没有湖 (找不到 lake.py) 就没有消费方, 不探
            continue
        rec = pr.call(f"F0{i + 1}", f"period={period} limit={page}", api,
                      {"period": period, "limit": page}, _fields_arg(E(api)), dl(api))
        rec2 = None
        if rec["kind"] == "OK" and 0 < rec["rows"] < page:          # 不足一页: 续页一次, 分清「上限截断」与「该期就这么多」
            rec2 = pr.call(f"F0{i + 1}b", f"period={period} offset={rec['rows']}", api,
                           {"period": period, "limit": page, "offset": rec["rows"]}, _fields_arg(E(api)), dl(api))
        fin_recs.append((api, rec, rec2))
    # ③ 研究湖的申万三件 (湖每月重拉; 代码里没有湖就不探) ----------------------------------------------------------
    lake_c = exp["consts"].get("stock-core/research/lake.py", {})
    sw_src = lake_c.get("SW_SRC") if isinstance(lake_c.get("SW_SRC"), str) else "SW2021"
    m_page = lake_c.get("MEMBER_PAGE") if isinstance(lake_c.get("MEMBER_PAGE"), int) else 2000
    confirm_min = lake_c.get("PAGE_CONFIRM_MIN") if isinstance(lake_c.get("PAGE_CONFIRM_MIN"), int) else None
    r_cls = r_mem = r_swd = None
    l1 = SW_L1_FALLBACK
    if "index_classify" in A:
        r_cls = pr.call("W01", f"level=L1 src={sw_src}", "index_classify", {"level": "L1", "src": sw_src},
                        _fields_arg(E("index_classify")), dl("index_classify"))
        codes = sorted(str(r.get("index_code")) for r in _dicts(r_cls) if r.get("index_code"))
        l1 = codes[0] if codes else SW_L1_FALLBACK
    if "index_member_all" in A:
        r_mem = pr.call("W02", f"l1_code={l1} is_new=Y limit={m_page}", "index_member_all",
                        {"l1_code": l1, "is_new": "Y", "limit": m_page, "offset": 0},
                        _fields_arg(E("index_member_all")), dl("index_member_all"))
    if "sw_daily" in A:
        r_swd = pr.call("W03", f"{l1} {r_start}..{D}", "sw_daily",
                        {"ts_code": l1, "start_date": _ymd(r_start), "end_date": _ymd(D)},
                        _fields_arg(E("sw_daily")), dl("sw_daily"))

    # ④ 逐次调用 ---------------------------------------------------------------------------------------------
    out(f"\n④ 逐次调用 ({pr.n} 次 / 上限 {max_calls}; 延迟 = 工作进程里量的 HTTP 往返+下载)")
    out(f"  {'#':<4}{'端点':<20}{'参数':<34}{'结果':<9}{'HTTP':>5}{'code':>6}{'行':>7}{'列':>5}{'秒':>8}{'KB':>8}")
    for r in pr.records:
        res = r["kind"] + ("*" if r["first_fail"] else "")
        secs = "-" if r["secs"] is None else "%.2f" % r["secs"]
        code = "-" if r["code"] is None else str(r["code"])
        tail = ("  " + r["msg"][:60]) if r["msg"] and r["kind"] != "OK" else ""
        out(f"  {r['id']:<4}{r['api']:<20}{r['label'][:33]:<34}{res:<9}{str(r['http'] or '-'):>5}"
            f"{code:>6}{r['rows']:>7}{len(r['fields']):>5}{secs:>8}{r['bytes'] / 1024:>8.0f}{tail}")
    if any(r["first_fail"] for r in pr.records):
        out("  * = 首发失败, 重试成功 (也算调用数)")

    # ⑤ 判读: 先判形状, 再由 judge_row 按每档自己的期限判延迟 ---------------------------------------------------------
    rows = []

    def add(item, lv, ev, api, *recs):
        rows.append({"item": item, "api": api, "lv": lv, "ev": ev, "recs": [r for r in recs if r]})

    if fallback:
        add("日历真值来源", "WARN", f"内置休市表不覆盖这几天, {' / '.join(fallback)} 取自被测镜像 (不独立) —— 补 A_CLOSED",
            "trade_cal")
    for (item, lv, ev), rec in zip(check_cal_range(r_all, r_open, start.isoformat(), end.isoformat(), t_iso,
                                                   E("trade_cal")["expect"]), (r_all, r_open)):
        add(item, lv, ev, "trade_cal", rec)
    if not hol:
        add("trade_cal 单日 假日", "WARN", "内置休市表里找不到今天及之前的工作日休市日 (表没覆盖?), 假日一例没测",
            "trade_cal")
    for kind, day, rw, ro, is_open_day in day_recs:
        item, lv, ev = check_cal_day(kind, day, rw, ro, is_open_day, E("trade_cal")["expect"])
        add(item, lv, ev, "trade_cal", rw, ro)
    sb_rows, listed_a, _ = check_stock_basic(sb_recs, E("stock_basic")["expect"], mk.keep_a_code)
    for item, lv, ev, recs in sb_rows:
        add(item, lv, ev, "stock_basic", *recs)
    # daily 全市场: 生产解析 = market._rows_from_daily (价格合法性过滤 + A 股代码)
    parsed_daily, daily_codes = 0, set()
    if r_daily["kind"] == "OK":
        try:
            import pandas as pd
            rows_ = mk._rows_from_daily(pd.DataFrame(r_daily["items"], columns=r_daily["fields"] or None))
            parsed_daily, daily_codes = len(rows_), set(rows_)
        except Exception as e:                                 # noqa: BLE001
            parsed_daily = a_code_count(r_daily, mk.keep_a_code)[0]
            out(f"  · daily 生产解析回放失败 ({type(e).__name__}), 退回只数 A 股代码")
    add(*check_by_date(f"daily 按 trade_date {pday}", r_daily, pday, E("daily")["expect"], listed_a,
                       th["ready_ratio"], parsed_daily), "daily", r_daily)
    adj_codes = set()
    for r in _dicts(r_adj):
        c = mk.keep_a_code(r.get("ts_code"))
        try:
            if c and float(r.get("adj_factor")) > 0:
                adj_codes.add(c)
        except (TypeError, ValueError):
            pass
    cover = None
    if daily_codes and r_adj["kind"] == "OK":
        lack = daily_codes - adj_codes
        cover = (("WARN" if len(lack) > 0.01 * len(daily_codes) else "PASS"),
                 f"日线 {len(daily_codes)} 只里缺因子 {len(lack)} 只")
    add(*check_by_date(f"adj_factor 按 trade_date {pday}", r_adj, pday, E("adj_factor")["expect"], listed_a,
                       th["ready_ratio"], len(adj_codes), cover), "adj_factor", r_adj)
    # daily_basic (生产估值兜底不看 trade_date: 日期不符 = FAIL)
    item = f"daily_basic 按 trade_date {pday}"
    bad = call_level(r_db)
    if bad:
        add(item, bad[0], bad[1], "daily_basic", r_db)
    else:
        parts = [field_check(r_db, E("daily_basic")["expect"]), retry_note(r_db), round_cap_note(r_db["rows"], None),
                 date_check(r_db, pday)]
        if r_db["rows"] < th["db_min_rows"]:
            parts.append(("FAIL", f"< 生产门槛 {th['db_min_rows']} 行: 估值兜底会判当日没出、退回前一交易日"))
        lv, txt = combine(parts)
        add(item, lv, f"{r_db['rows']} 行 (门槛 {th['db_min_rows']}); {txt}", "daily_basic", r_db)
    # index_daily: 区间 (指数不停牌, 缺开市日 = FAIL) + 单日 P (生产按日期筛)
    add(*check_range(f"index_daily {bench} 区间 {r_start}..{D}", r_idx, rng_days, E("index_daily")["expect"],
                     r_start, D, index=True), "index_daily", r_idx)
    item = f"index_daily {bench} 单日 {pday}"
    bad = call_level(r_idx1)
    if bad:
        add(item, bad[0], bad[1], "index_daily", r_idx1)
    else:
        got = {_iso(r.get("trade_date")) for r in _dicts(r_idx1)}
        parts = [field_check(r_idx1, E("index_daily")["expect"]), retry_note(r_idx1)]
        if pday not in got:
            parts.append(("FAIL", f"单日查询没有 {pday} 那根 (回了 {sorted(got)[:2]}) —— 生产 fetch_index_by_date "
                                  "当天指数不入库"))
        elif len(got) > 1:
            parts.append(("WARN", f"单日查询回了 {len(got)} 个日期 (生产按日期筛, 不影响)"))
        lv, txt = combine(parts)
        add(item, lv, f"{r_idx1['rows']} 行; {txt}", "index_daily", r_idx1)
    # ths_daily (生产只认所问那天: 别日的行 WARN, 当天的 .TI 一行没有 FAIL)
    item = f"ths_daily 按 trade_date {pday}"
    bad = call_level(r_ths)
    if bad:
        add(item, bad[0], bad[1], "ths_daily", r_ths)
    else:
        sfx = th["ti_suffix"].upper()
        ds_ = _dicts(r_ths)
        n_ti = sum(1 for r in ds_ if str(r.get("ts_code") or "").upper().endswith(sfx))
        on_ti = sum(1 for r in ds_ if str(r.get("ts_code") or "").upper().endswith(sfx)
                    and ("trade_date" not in r or _ymd(r.get("trade_date")) == _ymd(pday)))
        parts = [field_check(r_ths, E("ths_daily")["expect"]), retry_note(r_ths), date_check(r_ths, pday, True)]
        if on_ti == 0:
            parts.append(("FAIL", f"{pday} 当天的 {th['ti_suffix']} 指数行一行没有 —— 行业指数那天的 bar 补不上"))
        lv, txt = combine(parts)
        add(item, lv, f"{r_ths['rows']} 行 ({th['ti_suffix']} {n_ti}, 其中 {pday} 当天 {on_ti}); {txt}", "ths_daily",
            r_ths)
    # 按 ts_code 区间 (个股停牌会缺天: WARN)
    add(*check_range(f"daily {RANGE_CODE} 区间 {r_start}..{D}", r_dr, rng_days, E("daily")["expect"], r_start, D),
        "daily", r_dr)
    dr_dates = sorted({_iso(r.get("trade_date")) for r in _dicts(r_dr)}) if r_dr["kind"] == "OK" else None
    add(*check_range(f"adj_factor {RANGE_CODE} 区间 {r_start}..{D}", r_ar, rng_days, E("adj_factor")["expect"],
                     r_start, D, ref_dates=dr_dates), "adj_factor", r_ar)
    # 财务
    for api, rec, rec2 in fin_recs:
        tbl = {"fina_indicator_vip": "fina_indicator", "income_vip": "income"}.get(api, api)
        ref = lake_meta["rows"].get((tbl, _iso(period)))
        add(*check_fin(f"{api} period={period}", rec, E(api)["expect"], page, ref, rec2, confirm_min), api, rec,
            rec2)
    # 申万三件
    if r_cls is not None:
        add(*check_filtered(f"index_classify level=L1 src={sw_src}", r_cls, E("index_classify")["expect"],
                            {"level": "L1", "src": sw_src}, None), "index_classify", r_cls)
    if r_mem is not None:
        add(*check_filtered(f"index_member_all l1_code={l1} is_new=Y", r_mem, E("index_member_all")["expect"],
                            {"l1_code": l1, "is_new": "Y"}, m_page), "index_member_all", r_mem)
    if r_swd is not None:
        add(*check_range(f"sw_daily {l1} 区间 {r_start}..{D}", r_swd, rng_days, E("sw_daily")["expect"],
                         r_start, D, index=True), "sw_daily", r_swd)

    checks = [judge_row(r, E(r["api"])) for r in rows]
    probed = {"trade_cal", "stock_basic", "daily", "adj_factor", "daily_basic", "index_daily", "ths_daily",
              "fina_indicator_vip", "income_vip", "index_classify", "index_member_all", "sw_daily"}
    uncovered = sorted(a for a in A if a not in probed)

    out("\n⑤ 汇总 (每行: 序号. 结论  检查项  [消费方; 两档结论不同时分写]  证据; 消费方: 生产 = a-share 流水线 / "
        "数据总览 = stock-core data_catalog 服务器 19:30 / 研究湖 = PC data/lake)")
    for i, c in enumerate(checks, 1):
        split = ""
        if len(set(c["tiers"].values())) > 1:
            split = " · " + " / ".join(f"{t}档 {lv}" for t, lv in c["tiers"].items())
        out(f"  {i:>2}. {c['level']:<4}  {c['item']}  [{c['consumer']}{split}]  {c['evidence']}")
    if uncovered:
        out(f"  未覆盖 (代码在用, 本探针不探): {', '.join(uncovered)}")
    verdicts = tier_verdicts(checks)
    vp, vl = verdicts[TIER_PROD]["verdict"], verdicts[TIER_LAKE]["verdict"]
    rc = 0 if vp == "可用" else 1
    out("")
    for t in (TIER_PROD, TIER_LAKE):
        out(f"结论 · {t}档 ({TIER_DESC[t]}): {verdicts[t]['verdict']} —— {verdicts[t]['text']}")
    out(f"结论: 生产档{vp} · 研究湖档{vl} —— 退出码 {rc} (只看生产档) · 调用 {pr.n} 次 · "
        f"用时 {pr.clock() - pr.t0:.0f}s")

    if args.json:
        rep = {"probed_at": nowbj.isoformat(timespec="seconds"), "host": _host(base), "today_bj": t_iso,
               "dates": {"range": [start.isoformat(), end.isoformat()], "sat": sat.isoformat(),
                         "sun": sun.isoformat(), "holiday": hol, "open_day": D, "by_date": pday,
                         "ts_range": [r_start, D], "period": period,
                         "truth": "内置 A 股休市表 A_CLOSED + 周末" + (f"; 退回镜像: {fallback}" if fallback else "")},
               "calls": [{k: v for k, v in r.items() if k != "items"} for r in pr.records],
               "checks": checks, "uncovered": uncovered,
               "verdict": {t: verdicts[t]["verdict"] for t in (TIER_PROD, TIER_LAKE)},
               "tiers": verdicts, "rc": rc, "n_calls": pr.n}
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=1)
        out(f"明细 → {args.json}")
    return rc


# ================================================================= p0 (2026-09 原探针, 原样保留)
def p0_main() -> int:
    """P0: 确定所购档位实际开放的端点、行数、延迟 (2026-09-07 GM 亲跑, 结果见 stock-core design/tushare_adapter_design.md §5)。
    token/基址从 data/secrets.json 读 (永不打印)。日期写死在 2026-09, 只作历史复现。"""
    import requests
    S = json.load(open(os.path.join(ROOT, "data", "secrets.json"), encoding="utf-8"))
    TOKEN, BASE = S["tushare_token"], S.get("tushare_base_url", "https://api.tushare.pro/")

    def call(api, **params):
        t = time.time()
        try:
            r = requests.post(BASE, json={"api_name": api, "token": TOKEN, "params": params, "fields": ""}, timeout=60)
            j = r.json()
        except Exception as e:
            return "ERR", 0, round(time.time() - t, 2), str(e)[:80]
        ms = round(time.time() - t, 2)
        if j.get("code") != 0:
            msg = str(j.get("msg") or "")[:90]
            kind = "NOPERM" if any(k in msg for k in ("权限", "积分", "permission", "点")) else "FAIL"
            return kind, 0, ms, msg
        d = j.get("data") or {}
        items = d.get("items") or []
        return "OK", len(items), ms, ",".join((d.get("fields") or [])[:6])

    # 找最近交易日
    st, n, ms, info = call("trade_cal", exchange="SSE", start_date="20260820", end_date="20260907", is_open="1")
    print(f"trade_cal: {st} rows={n} {ms}s {info}")
    last = "20260905"
    try:
        r = requests.post(BASE, json={"api_name": "trade_cal", "token": TOKEN, "params": {"exchange": "SSE", "start_date": "20260820", "end_date": "20260907", "is_open": "1"}, "fields": "cal_date"}, timeout=60).json()
        dates = sorted(x[0] for x in r["data"]["items"])
        last = [d for d in dates if d <= "20260905"][-1]
    except Exception:
        pass
    print("最近交易日:", last)

    TESTS = [
        ("stock_basic L", "stock_basic", dict(list_status="L")),
        ("stock_basic D (退市)", "stock_basic", dict(list_status="D")),
        ("stock_basic P", "stock_basic", dict(list_status="P")),
        ("daily 全市场一日", "daily", dict(trade_date=last)),
        ("adj_factor 一日", "adj_factor", dict(trade_date=last)),
        ("daily_basic 一日", "daily_basic", dict(trade_date=last)),
        ("index_daily 300", "index_daily", dict(ts_code="000300.SH", start_date="20260801", end_date=last)),
        ("namechange", "namechange", dict(ts_code="000001.SZ")),
        ("suspend_d 一日", "suspend_d", dict(trade_date=last)),
        ("stk_limit 一日", "stk_limit", dict(trade_date=last)),
        ("fina_indicator 单股", "fina_indicator", dict(ts_code="600519.SH", start_date="20250101")),
        ("fina_indicator_vip 按期", "fina_indicator_vip", dict(period="20250630")),
        ("income 单股", "income", dict(ts_code="600519.SH", start_date="20250101")),
        ("income_vip 按期", "income_vip", dict(period="20250630")),
        ("cashflow 单股", "cashflow", dict(ts_code="600519.SH", start_date="20250101")),
        ("forecast 按期", "forecast", dict(period="20250630")),
        ("express 按期", "express", dict(period="20250630")),
        ("disclosure_date", "disclosure_date", dict(end_date="20250630")),
        ("margin_detail 一日", "margin_detail", dict(trade_date=last)),
        ("top_list 一日", "top_list", dict(trade_date=last)),
        ("block_trade 一日", "block_trade", dict(trade_date=last)),
        ("moneyflow_ind_dc 一日", "moneyflow_ind_dc", dict(trade_date=last)),
        ("index_classify 申万", "index_classify", dict(level="L1", src="SW2021")),
        ("index_member_all 申万", "index_member_all", dict(l1_code="801010.SI")),
        ("sw_daily 申万", "sw_daily", dict(ts_code="801010.SI", start_date="20260801")),
        ("ths_index 同花顺", "ths_index", dict(exchange="A", type="N")),
        ("stk_factor_pro 单股", "stk_factor_pro", dict(ts_code="000001.SZ", start_date="20260801")),
        ("fina_mainbz 单股", "fina_mainbz", dict(ts_code="600519.SH", period="20241231")),
        ("stk_managers 单股", "stk_managers", dict(ts_code="600519.SH")),
        ("daily 单股10年", "daily", dict(ts_code="000001.SZ", start_date="20160901", end_date=last)),
        ("daily 退市股示例", "daily", dict(ts_code="000002.SZ", start_date="20160901", end_date="20161231")),
        ("us_basic (美股)", "us_basic", dict()),
        ("us_daily (美股)", "us_daily", dict(ts_code="AAPL", start_date="20260801")),
    ]
    print(f"\n{'端点':<26}{'结果':<8}{'行数':>7}{'耗时':>7}  备注")
    for name, api, params in TESTS:
        st, n, ms, info = call(api, **params)
        print(f"{name:<26}{st:<8}{n:>7}{ms:>6}s  {info}")
        time.sleep(0.4)
    return 0


# ================================================================= 入口
def _utf8_stdout() -> None:
    """只 reconfigure 不换对象 (换对象会在被 GC 时关掉底层 buffer, 见 stock-core a35b159 / tushare_ready_probe)。"""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                      # noqa: BLE001
            pass


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Tushare (兼容镜像) 只读探针: p0 (历史端点清单) / compat (换镜像必跑)")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("p0", help="2026-09 P0 端点清单 (不带子命令时的默认行为)")
    c = sub.add_parser("compat", help="镜像兼容性探针: 只读, 只打印形状; 两档结论 (生产档定退出码 / 研究湖档只报)")
    c.add_argument("--expect", action="store_true", help="只列代码期望字段与各消费方期限, 不联网")
    c.add_argument("--secrets", default="", help="另一份 secrets.json (只读 tushare_token / tushare_base_url); 默认 = 生产客户端凭据")
    c.add_argument("--max-calls", type=int, default=MAX_CALLS,
                   help=f"调用总数上限, 只能 1..{MAX_CALLS} (硬上限, 超了拒跑; 默认 {MAX_CALLS}, 计划 26)")
    c.add_argument("--gap", type=float, default=GAP_SEC, help=f"调用间隔秒 (默认 {GAP_SEC:g})")
    c.add_argument("--budget", type=float, default=BUDGET_SEC, help=f"墙钟预算秒 (默认 {BUDGET_SEC:g})")
    c.add_argument("--today", default="", help="把哪天当「今天」(北京日历, YYYYMMDD; 默认现在)")
    c.add_argument("--date", default="", help="全市场按日那几次问哪个交易日 P (默认: 内置休市表里上个月最后一个开市的周二; "
                   "必须是今天之前的开市日)")
    c.add_argument("--holiday", default="", help="假日一例用哪天 (默认: 内置休市表里今天及之前最近的工作日休市日)")
    c.add_argument("--period", default="", help="财务一期 (默认: 最近已过法定披露截止的报告期)")
    c.add_argument("--core", default="", help="stock-core 目录 (找 research/lake.py; 默认 STOCK_CORE_DIR → 同级 ../stock-core)")
    c.add_argument("--lake-db", default="", help="lake.db 路径 (只读; 默认 data/lake/lake.db)")
    c.add_argument("--no-lake-db", action="store_true", help="不读 lake.db, 财务字段只按 FIN_TABLES 核")
    c.add_argument("--json", default="", help="把明细 (不含回包数据与 token) 写到这个文件")
    return ap


def main(argv=None) -> int:
    _utf8_stdout()
    args = build_parser().parse_args(argv)
    if args.cmd == "compat":
        return run_compat(args)
    return p0_main()


if __name__ == "__main__":
    raise SystemExit(main())
