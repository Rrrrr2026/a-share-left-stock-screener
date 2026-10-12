# -*- coding: utf-8 -*-
"""
Tushare (协议兼容镜像) 只读探针 —— 两个子命令
=============================================
    python -X utf8 tools/tushare_probe.py                    # p0: 2026-09 P0 端点清单 (历史用法, 行为不变)
    python -X utf8 tools/tushare_probe.py compat             # 镜像兼容性探针 (卡 TS-PROBE, **换镜像时必跑**)
    python -X utf8 tools/tushare_probe.py compat --expect    # 只列「代码期望字段」, 不联网 (0 次调用)

compat 为什么要有 (2026-10-10 教训, CHRONICLE「GM 记 10-10 12:55」): 换到镜像 tl.kaixin8.top 后, 它对
start==end 的**单日** trade_cal 不套 is_open=1 过滤 (区间查询才套), 客户端数到 1 行就把周六 10-10 当开市日,
就绪闸门等了 60 分钟拒发 10-09 产物 (a-share f1de80f 起 `tushare_client.trade_cal` 一律本地过滤)。旧镜像没这毛病,
所以从未暴露 —— 换镜像 = 换了一个「协议兼容」但细节行为未知的实现, 必须先探再切。本子命令把流水线真正依赖的
那几种调用形态各打一次, 只核**形状** (行数 / 字段集 / is_open 语义 / 延迟 / HTTP 状态), 最后给一张
PASS/WARN/FAIL 汇总表和一句「可切换 / 不可切换」。

探什么 (默认 26 次调用, 财务不足一页时各多 1 次续页; 硬上限 --max-calls 30, 超了就不再发, 剩下的判 SKIP = 未跑完):
  ① trade_cal: 区间 [今天-200 天, 今天+CAL_FWD_DAYS] 带/不带 is_open 各一次 (不带的那次当真值表: 逐自然日一行,
     周末必须 is_open=0, 要找得到工作日休市日); 单日 周六 / 周日 / 假日 / 开市日 各带/不带 is_open 一次 ——
     核返回行数与 is_open 值是否符合参数, 并把镜像的**原样回包**喂给客户端 `tushare_client.trade_cal` 回放
     (打桩 query, 不联网), 证明本地过滤兜得住;
  ② daily 按 trade_date (全市场一天) 与按 ts_code 区间 (000001.SZ 最近 20 个开市日), 配对的 adj_factor 两次:
     行数 (过 `market._rows_from_daily` / keep_a_code 后与 stock_basic L 的 A 股只数比, 门槛 = 生产未就绪守卫的
     ready_ratio)、字段集、因子是否覆盖每根日线;
  ③ daily_basic / stock_basic (L、D、P 各一次) / index_daily (区间 + 单日) / ths_daily / fina_indicator_vip +
     income_vip (最近已过法定披露截止的一期, limit = 湖的翻页步长 FIN_PAGE; 不足一页就补一次 offset 续页, 分清
     「镜像单页上限 < FIN_PAGE → 湖静默少拉」与「该期就这么多」); 研究湖每月重拉的申万三件 index_classify (L1) /
     index_member_all (一个 L1, is_new=Y, limit = MEMBER_PAGE; 核回包是否真按 l1_code / is_new 过滤) / sw_daily:
     字段集与行数是否与代码期望一致;
  ④ 每次调用: HTTP 状态 / code / 行数 / 列数 / 延迟 (工作进程里量的纯 HTTP 时间) / 字节, 延迟对照该端点在生产
     代码里的最严硬期限 (> 期限 = FAIL, > 一半 = WARN);
  ⑤ 汇总表 + 「结论: 可切换 / 不可切换」。退出码 0 = 可切换 (PASS 或只有 WARN), 1 = 不可切换 (有 FAIL 或没跑完),
     2 = 判不了 (没 token / 参数错)。

「期望字段」从代码里收集, **不手抄** (`collect_expectations`, 纯 AST, 不 import 被扫的模块):
  · 扫 a-share `ashare/*.py` 全部 (生产: tushare_client / datasource / market (= pricestore 的取数钩子) / datadate …)
    与 stock-core `research/lake.py` (研究湖; 找法同 ashare.config: STOCK_CORE_DIR → 同级 ../stock-core)。
  · 认的调用形态: `query("api", fields=…)` / `_ts_query(…)` / `self.query(…)` / `self._pages(…)` 第一个参数是 api 名,
    `tsc.trade_cal(…)` / `tsc.index_daily(…)` 是 tushare_client 的薄封装 (字段取封装内部那处)。
  · fields 参数能静态求值就算: 字符串字面量、模块常量 (TS_DAILY_BASIC_FIELDS)、`",".join(列表常量)`、
    函数里的局部变量 (含外层函数的, 如湖 pull_daily_basic 里嵌套 fetch 用的 fields)。
  · 列引用: 同一函数里对**这次调用结果那个变量**做的 `need <= set(df.columns)` 守卫集合与 `"x" in df.columns`
    判断 (按变量名 + 行号就近归属, 所以 fetch_bars_bulk_tushare 里 `"adj_factor" in fa.columns` 只算 adj_factor 的,
    不会冤枉 daily)。没给 fields 的调用 (index_daily) 走镜像默认字段集, 期望 = 这些列引用。
  · 湖的财务两表字段是运行时从镜像发现、缓存在 lake.db meta 的 (`Lake._fin_fields`), 代码里只有 FIN_TABLES 的
    主键/文本/日期列: 能只读打开 lake.db 就用 meta 里那份完整字段表 (湖下次真会请求的就是它, 缺列会被 `_records`
    **静默写成 NULL**), 打不开就退回 FIN_TABLES 列。
  以后有人在代码里加一处 Tushare 调用或一个字段, 探针的期望自动跟着变; 代码在用而本探针不探的端点打在「未覆盖」行。

铁律: 只读 (不写任何库; lake.db 以 `mode=ro` 只读 meta / pull_log 两张小表; 结果只在 --json 时落一个文件);
token 只在请求体里, **永不打印/落盘** (镜像回的 msg 里若出现 token 也会被抹掉, 地址只打主机名); 每次 HTTP 经
`tushare_client._deadline_call` 走 datasource 的**进程级**墙钟硬期限 (与生产同一个执行器); 默认凭据就是生产
客户端 `tushare_client.credentials()` 的那一份 (data/secrets.json, 环境变量 TUSHARE_TOKEN / TUSHARE_BASE_URL 可覆盖),
`--secrets PATH` 可指向另一份 secrets.json (只读其 tushare_token / tushare_base_url 两键)。
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

MAX_CALLS = 30              # 卡 TS-PROBE: 一次探针的调用总数上限 (默认计划 26 次, 续页/重试也算)
GAP_SEC = 1.0               # 两次调用之间的静默秒数 (trade_cal 连打 10 次, 别把镜像的分钟频控打满)
BUDGET_SEC = 900.0          # 整个探针的墙钟预算; 用完不再发调用 (剩下的判 SKIP)
DEADLINE_MARGIN_SEC = 30.0  # 硬期限 = 该端点生产最严期限 + 这点余量 (超了生产期限仍判 FAIL, 只是能量出实际耗时)
SLOW_FRAC = 0.5             # 延迟超过生产期限的这个比例 -> WARN
LOOKBACK_DAYS = 200         # 区间日历往回看的自然日: A 股两次放假最多隔 ~4 个月, 200 天里必有工作日休市日
RANGE_OPEN_DAYS = 20        # 按 ts_code 区间取最近多少个开市日
RANGE_CODE = "000001.SZ"    # 按 ts_code 区间的样本股 (平安银行, 极少停牌)
SW_L1_FALLBACK = "801010.SI"  # 申万分类没拿到时, 成员/日线那两次用的 L1 (农林牧渔)
ROUND_CAPS = (1000, 2000, 3000, 4000, 5000, 6000, 8000, 9000, 10000)   # 行数恰为整数 -> 疑似单页上限截断
RATE_BACKOFF_SEC = 12.0     # 超频后重试前等多久 (= tushare_client.BACKOFF_RATE_SEC)

LEVELS = ("PASS", "WARN", "FAIL", "SKIP")
_RANK = {lv: i for i, lv in enumerate(LEVELS)}


def _worst(*levels: str) -> str:
    return max((lv for lv in levels if lv), key=lambda lv: _RANK[lv], default="PASS")


# ================================================================= 期望字段: 从代码里收集 (AST, 不手抄)
_API_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_QUERY_FUNCS = ("query", "_ts_query", "_pages")          # 第一个位置参数 = api 名
_WRAPPERS = ("trade_cal", "index_daily")                 # tushare_client 的薄封装 (外部以 tsc./tc. 调)
_CLIENT_NAMES = ("tsc", "tc", "tushare_client")
_FIN_TABLES_NAME = "FIN_TABLES"                          # research/lake.py: {api: (表名, 主键, 文本列, 日期列)}
CONSUMER_PROD, CONSUMER_LAKE = "生产", "研究湖"


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
    api, via_wrapper, dynamic = None, False, False
    if name in _QUERY_FUNCS and call.args:
        if owner not in (None, "self", "ds", "datasource") + _CLIENT_NAMES:
            return None                          # df.query(expr) 之类, 不是 Tushare
        v = _lit(call.args[0], lookup)
        if isinstance(v, str):
            if not _API_RE.match(v):
                return None
            api = v
        else:
            dynamic = True                       # 湖的 self._pages(api, …): api 运行时才知道, 只记录不计入
    elif name in _WRAPPERS and owner in _CLIENT_NAMES:
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
            "deadline": deadline if isinstance(deadline, (int, float)) else None,
            "params": sorted(k for k in kw if k not in ("fields", "retries", "deadline_sec")),
            "file": ctx["rel"], "line": call.lineno, "qual": qual or "<module>",
            "consumer": ctx["consumer"], "var": None, "refs": [], "_node": call}


def _visit(body, chain: list, qual: str, ctx: dict, out: list, is_class: bool = False) -> dict:
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
    sites = []
    for n in nodes:
        if isinstance(n, ast.Call):
            s = _call_site(n, lookup, qual, ctx)
            if s is not None:
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
        cands = [s for s in sites if s["var"] == var and s["line"] <= n.lineno]
        if cands:
            max(cands, key=lambda s: s["line"])["refs"].extend(cols)
    out.extend(sites)
    sub_chain = chain if is_class else [local] + chain
    for d in nested:
        name = f"{qual}.{d.name}" if qual else d.name
        _visit(d.body, sub_chain, name, ctx, out, is_class=isinstance(d, ast.ClassDef))
    return local


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
    """一份源码 -> {"sites": [Tushare 调用站点], "consts": 模块级常量, "fin_tables": {…}}。纯函数, 不 import。"""
    tree = ast.parse(src)
    sites: list = []
    consts = _visit(tree.body, [], "", {"rel": rel, "consumer": consumer}, sites)
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
           "default_sites": 走默认字段集的站点数, "deadline": 各站点最严硬期限 (没写的按客户端 DEADLINE_SEC),
           "consumers": [...], "sites": ["文件:行 函数 (字段说明)"], "meta_source": 财务字段表来源}。"""
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
        with open(os.path.join(core, "research", "lake.py"), encoding="utf-8") as f:
            res = scan_source(f.read(), "stock-core/research/lake.py", CONSUMER_LAKE)
        sites += res["sites"]
        consts["stock-core/research/lake.py"] = res["consts"]
        fin = res["fin_tables"]
        fp = res["consts"].get("FIN_PAGE")
        fin_page = fp if isinstance(fp, int) else None
    else:
        notes.append("找不到 stock-core/research/lake.py (STOCK_CORE_DIR / 同级 ../stock-core): 研究湖的期望未纳入")
    lake_meta = lake_meta or {"fields": {}, "rows": {}, "error": "未读 lake.db"}
    client_dl = consts.get("ashare/tushare_client.py", {}).get("DEADLINE_SEC")
    client_dl = client_dl if isinstance(client_dl, (int, float)) else 120

    apis: dict = {}

    def entry(api):
        return apis.setdefault(api, {"api": api, "fields": [], "refs": [], "default_sites": 0, "explicit_sites": 0,
                                     "dyn_fields_sites": 0, "deadlines": [], "consumers": [], "sites": [],
                                     "meta_source": None})

    dynamic = []
    for s in sites:
        where = f"{s['file']}:{s['line']} {s['qual']}"
        if s["dynamic"]:
            dynamic.append(where)
            continue
        e = entry(s["api"])
        e["consumers"].append(s["consumer"])
        e["refs"] += s["refs"]
        if s["deadline"]:
            e["deadlines"].append(s["deadline"])
        if s["via_wrapper"]:
            e["sites"].append(where + " (经 tushare_client 封装)")
            continue
        if s["fields"] == "dynamic":
            e["dyn_fields_sites"] += 1
            e["sites"].append(where + " (fields 运行时才知道)")
        elif s["fields"] is None:
            e["default_sites"] += 1
            e["sites"].append(where + " (默认字段集)")
        else:
            e["explicit_sites"] += 1
            e["fields"] += s["fields"]
            e["sites"].append(where + f" ({len(s['fields'])} 列)")
    for api, ft in fin.items():
        e = entry(api)
        e["consumers"].append(CONSUMER_LAKE)
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
        e["deadline"] = min(e["deadlines"] + [client_dl])
    return {"apis": apis, "dynamic": dynamic, "consts": consts, "fin_page": fin_page, "notes": notes,
            "core": core, "lake_meta_error": lake_meta.get("error", ""), "client_deadline": client_dl}


def print_expectations(exp: dict, out=print) -> None:
    out("代码期望字段 (AST 静态收集, 不手抄; 生产 = a-share ashare/*.py, 研究湖 = stock-core research/lake.py)")
    for note in exp["notes"]:
        out(f"  注: {note}")
    for api in sorted(exp["apis"]):
        e = exp["apis"][api]
        mode = (f"显式 {len(e['fields'])} 列" if e["mode"] == "explicit" else "默认字段集")
        out(f"\n  {api}  [{'+'.join(e['consumers'])}]  {mode}  · 要核 {len(e['expect'])} 列 · 生产最严期限 {e['deadline']:g}s")
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
    secs 在工作进程里量 = 纯 HTTP 往返 + 下载, 不含进程池调度; head 只在回包不是 JSON 时给前 120 字 (网关错误页)。"""
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
    return {"http": r.status_code, "secs": secs, "bytes": len(body), "json": j,
            "head": "" if j is not None else r.text[:120]}


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
    """按计划逐次调用并记录形状。调用数 > max_calls 或墙钟预算用完就不再发 (记录 kind=SKIP)。
    传输失败 / 超频各重试一次 (也算调用数); 硬期限超时不重试 (那就是结论)。"""

    def __init__(self, token: str, base: str, post=None, max_calls: int = MAX_CALLS, gap: float = GAP_SEC,
                 budget: float = BUDGET_SEC, sleep=time.sleep, clock=time.time):
        self.token, self.base = token, base
        self.post = post or deadline_post
        self.max_calls, self.gap, self.budget = int(max_calls), float(gap), float(budget)
        self.sleep, self.clock = sleep, clock
        self.t0 = clock()
        self.n = 0
        self.records: list = []

    def clean(self, text) -> str:
        """镜像回的文案: 抹 token、去换行、截 120 字。"""
        s = str(text or "")
        if self.token:
            s = s.replace(self.token, "<token>")
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


def latency_check(rec: dict, deadline: float):
    secs = rec.get("secs")
    if secs is None:
        return None
    if secs > deadline:
        return "FAIL", f"延迟 {secs:.1f}s > 生产硬期限 {deadline:g}s (生产这次会超时)"
    if secs > deadline * SLOW_FRAC:
        return "WARN", f"延迟 {secs:.1f}s > 生产期限 {deadline:g}s 的 {SLOW_FRAC:.0%}"
    return "PASS", f"延迟 {secs:.1f}s / 期限 {deadline:g}s"


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


def check_cal_range(r_all: dict, r_open: dict, start: str, end: str, today: str, exp_fields: list,
                    deadline: float) -> list:
    """① 区间两次 -> [(检查项, 级别, 证据)]。"""
    rows = []
    bad = call_level(r_all)
    if bad:
        rows.append(("trade_cal 区间 (不带 is_open) = 真值表", bad[0], bad[1]))
    else:
        cal = cal_table(r_all)
        d0, d1 = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
        want = {(d0 + dt.timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)}
        dates = [_iso(r.get("cal_date")) for r in _dicts(r_all)]
        parts = [field_check(r_all, exp_fields), retry_note(r_all), latency_check(r_all, deadline)]
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
        n_open = sum(1 for v in cal.values() if v == 1)
        n_wkd_closed = sum(1 for d, v in cal.items() if v == 0 and dt.date.fromisoformat(d).weekday() < 5)
        fut = sorted(d for d, v in cal.items() if v == 1 and d > today)
        if not fut:
            parts.append(("WARN", f"今天之后到 {end} 没有开市日: 看板 next_open_day 会写 null, 闸门覆盖自检判不了"))
        lv, txt = combine(parts)
        rows.append(("trade_cal 区间 (不带 is_open) = 真值表", lv,
                     f"{start}..{end} {len(dates)} 行 = {len(want)} 个自然日{'' if not miss else ' (有缺)'}; "
                     f"开市 {n_open} / 休市 {len(cal) - n_open} (其中工作日休市 {n_wkd_closed}); "
                     f"今天后首个开市日 {fut[0] if fut else '无'}; {txt}"))
    bad = call_level(r_open)
    if bad:
        rows.append(("trade_cal 区间 is_open=1", bad[0], bad[1]))
        return rows
    parts = [field_check(r_open, exp_fields), retry_note(r_open), latency_check(r_open, deadline)]
    got = cal_table(r_open)
    closed_back = sorted(d for d, v in got.items() if v == 0)
    if call_level(r_all) is None:
        truth = {d for d, v in cal_table(r_all).items() if v == 1}
        lost = sorted(truth - set(got))
        if lost:
            parts.append(("FAIL", f"漏了 {len(lost)} 个开市日 (如 {lost[0]})"))
        if closed_back:
            parts.append(("WARN", f"服务端区间查询也不套过滤: 多给 {len(closed_back)} 行 is_open=0 (客户端本地过滤兜住)"))
        days, why = replay_trade_cal(r_open, start, end, "1")
        if days is None:
            parts.append(("WARN", why))
        elif days != sorted(truth):
            parts.append(("FAIL", f"客户端回放 trade_cal 得 {len(days)} 个开市日 ≠ 真值 {len(truth)}"))
        else:
            parts.append(("PASS", f"客户端回放 = 真值 {len(truth)} 个开市日"))
    elif closed_back:
        parts.append(("WARN", f"带 is_open=1 仍回 {len(closed_back)} 行 is_open=0"))
    lv, txt = combine(parts)
    rows.append(("trade_cal 区间 is_open=1", lv, f"{len(got)} 行; {txt}"))
    return rows


def check_cal_day(kind: str, day: str, r_with: dict, r_without: dict, expect_open: bool, truth: dict,
                  exp_fields: list, deadline: float):
    """单日两次 (带 / 不带 is_open) + 客户端回放 -> (检查项, 级别, 证据)。"""
    item = f"trade_cal 单日 {kind} {day}"
    parts, ev = [], []
    t = truth.get(day)
    if t is not None and t != (1 if expect_open else 0):
        parts.append(("FAIL", f"区间真值表说 {day} is_open={t}, 与「{kind}」矛盾"))
    for tag, rec in (("带 is_open=1", r_with), ("不带", r_without)):
        bad = call_level(rec)
        if bad:
            parts.append((bad[0], f"{tag}: {bad[1]}"))
            continue
        for p in (field_check(rec, exp_fields), retry_note(rec), latency_check(rec, deadline)):
            if p and p[0] != "PASS":                 # 两次调用的字段/延迟只在有问题时才写进证据, 免得刷屏
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
    return item, lv, "; ".join(ev) + (f"; {txt}" if txt else "")


def a_code_count(rec: dict, keep) -> tuple:
    """-> (A 股行数, A 股代码集合)。keep = ashare.market.keep_a_code。"""
    codes = [keep(r.get("ts_code")) for r in _dicts(rec)]
    codes = [c for c in codes if c]
    return len(codes), set(codes)


def check_stock_basic(recs: dict, expect: list, keep, deadline: float) -> tuple:
    """recs = {"L": rec, "D": rec, "P": rec} -> ([检查行], listed_a | None, L 的 A 股代码集)。"""
    rows, listed_a, l_codes = [], None, set()
    sets = {}
    for st in ("L", "D", "P"):
        rec = recs.get(st)
        item = f"stock_basic list_status={st}"
        bad = call_level(rec) if rec else ("SKIP", "未调用")
        if bad:
            rows.append((item, bad[0], bad[1]))
            continue
        parts = [field_check(rec, expect), retry_note(rec), latency_check(rec, deadline)]
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
        rows.append((item, lv, ev + (f"; {txt}" if txt else "")))
    if sets.get("L") and sets.get("D") is not None:
        both = sets["L"] & sets["D"]
        if both and both == sets["L"]:
            rows.append(("stock_basic L/D 是否分得开", "FAIL", "L 与 D 返回同一批代码"))
        elif both:
            rows.append(("stock_basic L/D 是否分得开", "WARN", f"{len(both)} 只同时在 L 与 D"))
    return rows, listed_a, l_codes


def check_by_date(item: str, rec: dict, day: str, expect: list, deadline: float, listed_a, ratio: float,
                  parsed_n, extra=None):
    """全市场按 trade_date 的端点 (daily / adj_factor): 行数 vs 在市 A 股 × ready_ratio。"""
    bad = call_level(rec)
    if bad:
        return item, bad[0], bad[1]
    parts = [field_check(rec, expect), retry_note(rec), latency_check(rec, deadline),
             round_cap_note(rec["rows"], None)]
    dates = collections.Counter(_ymd(r.get("trade_date")) for r in _dicts(rec) if "trade_date" in r)
    off = {k: v for k, v in dates.items() if k != _ymd(day)}
    if off:
        parts.append(("WARN", f"混进别的日期 {dict(list(off.items())[:2])}"))
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


def check_range(item: str, rec: dict, open_days: list, expect: list, deadline: float, ref_dates=None):
    """按 ts_code 区间 / index_daily 区间: 一个开市日一行。"""
    bad = call_level(rec)
    if bad:
        return item, bad[0], bad[1]
    parts = [field_check(rec, expect), retry_note(rec), latency_check(rec, deadline)]
    got = sorted({_iso(r.get("trade_date")) for r in _dicts(rec) if _iso(r.get("trade_date"))})
    want = sorted(open_days)
    ev = f"{rec['rows']} 行 / 区间开市日 {len(want)}"
    if not got:
        parts.append(("FAIL", "0 行"))
    else:
        miss, extra = sorted(set(want) - set(got)), sorted(set(got) - set(want))
        if extra:
            parts.append(("WARN", f"区间外/非开市日 {len(extra)} 行 (如 {extra[0]})"))
        if miss:
            parts.append(("WARN", f"缺 {len(miss)} 个开市日 (如 {miss[0]}; 停牌也会这样)"))
    if ref_dates is not None and got and set(got) != set(ref_dates):
        parts.append(("WARN", f"与配对端点日期不一致 ({len(set(got) ^ set(ref_dates))} 天)"))
    lv, txt = combine(parts)
    return item, lv, ev + (f"; {txt}" if txt else "")


def check_fin(item: str, rec: dict, expect: list, deadline: float, page: int, ref_rows, rec2=None):
    """财务一期 (limit = 湖翻页步长 FIN_PAGE): 字段表全回 + 单页是否被镜像上限截断。

    湖的翻页 (`Lake._pages`) 是「不足一页即停」: 镜像单页上限若低于 FIN_PAGE, 第一页就不足一页, 湖会**静默少拉**。
    所以不足一页时补一次 offset=n 的续页调用 (rec2) 定性: 续页还有行 = 上限截断 (FAIL); 0 行 = 该期就这么多。"""
    bad = call_level(rec)
    if bad:
        return item, bad[0], bad[1]
    parts = [field_check(rec, expect), retry_note(rec), latency_check(rec, deadline)]
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
    elif rec2["rows"] > 0:
        parts.append(("FAIL", f"单页只给 {n} 行 < 湖翻页步长 {page}, 续页 offset={n} 还有 {rec2['rows']} 行: "
                              "湖的翻页不足一页即停, 会静默少拉"))
    else:
        ev += f"; 续页 offset={n} 0 行 = 该期就 {n} 行"
        if ref_rows and n < 0.9 * min(page, ref_rows):
            parts.append(("WARN", f"比湖里上次那份少 {min(page, ref_rows) - n} 行"))
    lv, txt = combine(parts)
    return item, lv, ev + (f"; {txt}" if txt else "")


def check_filtered(item: str, rec: dict, expect: list, deadline: float, filters: dict, page):
    """按参数过滤的维表 (申万分类 / 成员): 有行 + 回包里的过滤列是否等于所问 + 分页是否满页/超页。
    镜像不认过滤参数时湖的数据靠主键去重多半仍对, 只是调用量翻倍 —— 判 WARN; 调不通 / 0 行判 FAIL。"""
    bad = call_level(rec)
    if bad:
        return item, bad[0], bad[1]
    parts = [field_check(rec, expect), retry_note(rec), latency_check(rec, deadline)]
    ds = _dicts(rec)
    n = rec["rows"]
    ev = f"{n} 行"
    if n == 0:
        parts.append(("FAIL", "0 行"))
    for col, want in filters.items():
        if ds and col in ds[0]:
            wrong = sum(1 for r in ds if str(r.get(col)) != str(want))
            if wrong:
                parts.append(("WARN", f"镜像不认 {col} 过滤: {wrong}/{n} 行不是 {want} (湖会多拉, 靠主键去重)"))
    if page:
        if n > page:
            parts.append(("WARN", f"回了 {n} 行 > limit {page}: 镜像不认 limit"))
        elif n == page:
            ev += f" (满页 {page}, 湖会接着翻)"
    lv, txt = combine(parts)
    return item, lv, ev + (f"; {txt}" if txt else "")


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
        return A.get(api) or {"api": api, "mode": "default", "fields": [], "refs": [], "expect": [],
                              "deadline": exp["client_deadline"], "consumers": [], "sites": []}

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

    nowbj = (now or dt.datetime.now(BJ)).astimezone(BJ)
    today = dt.date.fromisoformat(_iso(args.today)) if args.today else nowbj.date()
    t_iso = today.isoformat()
    start = today - dt.timedelta(days=LOOKBACK_DAYS)
    end = today + dt.timedelta(days=th["cal_fwd"])
    sat, sun = latest_weekday(today, 5), latest_weekday(today, 6)
    pr = Prober(token, base, post=post, max_calls=args.max_calls, gap=args.gap, budget=args.budget, sleep=sleep)

    out("Tushare 镜像兼容性探针 (只读) · tools/tushare_probe.py compat")
    out(f"镜像主机 {_host(base)} · 北京时间 {nowbj:%Y-%m-%d %H:%M} · 今天(北京) {t_iso} 周{'一二三四五六日'[today.weekday()]}"
        f" · 调用上限 {args.max_calls} · 间隔 {args.gap:g}s · 墙钟预算 {args.budget:g}s")
    srcs = ["a-share ashare/*.py"] + (["stock-core research/lake.py"] if exp["core"] else [])
    for api in ("fina_indicator_vip", "income_vip"):
        ms = (A.get(api) or {}).get("meta_source")
        if ms:
            srcs.append(f"{api}: {ms}")
    out("期望字段来源: " + " + ".join(srcs) + " (完整清单: compat --expect)")
    for note in exp["notes"] + th["src"]:
        out(f"  · {note}")

    def dl(api):
        return float(E(api)["deadline"]) + DEADLINE_MARGIN_SEC

    # ① trade_cal ----------------------------------------------------------------------------------------
    cal_f = _fields_arg(E("trade_cal"))
    cal_dl = dl("trade_cal")
    r_all = pr.call("T01", f"区间 {start:%m-%d}..{end:%m-%d} 不带", "trade_cal",
                    {"exchange": "SSE", "start_date": _ymd(start), "end_date": _ymd(end)}, cal_f, cal_dl)
    r_open = pr.call("T02", f"区间 {start:%m-%d}..{end:%m-%d} is_open=1", "trade_cal",
                     {"exchange": "SSE", "start_date": _ymd(start), "end_date": _ymd(end), "is_open": "1"},
                     cal_f, cal_dl)
    truth = cal_table(r_all) if r_all["kind"] == "OK" else {}
    open_days = sorted(d for d, v in truth.items() if v == 1)
    if args.date:
        D = _iso(args.date)
    else:
        past = [d for d in open_days if d < t_iso]
        if past:
            D = past[-1]
        else:                                                  # 没有真值表: 退到上一个工作日 (只为能接着探)
            d = today - dt.timedelta(days=1)
            while d.weekday() >= 5:
                d -= dt.timedelta(days=1)
            D = d.isoformat()
    if args.holiday:
        hol = _iso(args.holiday)
    else:
        hs = sorted(d for d, v in truth.items() if v == 0 and d <= t_iso and dt.date.fromisoformat(d).weekday() < 5)
        hol = hs[-1] if hs else None
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
    # ② daily / adj_factor 按 trade_date ---------------------------------------------------------------------
    r_daily = pr.call("B01", f"全市场 {D}", "daily", {"trade_date": _ymd(D)}, _fields_arg(E("daily")), dl("daily"))
    r_adj = pr.call("B02", f"全市场 {D}", "adj_factor", {"trade_date": _ymd(D)}, _fields_arg(E("adj_factor")),
                    dl("adj_factor"))
    # ③ daily_basic / index_daily / ths_daily --------------------------------------------------------------
    r_db = pr.call("V01", f"全市场 {D}", "daily_basic", {"trade_date": _ymd(D)}, _fields_arg(E("daily_basic")),
                   dl("daily_basic"))
    in_win = [d for d in open_days if d <= D]
    r_start = in_win[-RANGE_OPEN_DAYS] if len(in_win) >= RANGE_OPEN_DAYS else \
        (dt.date.fromisoformat(D) - dt.timedelta(days=28)).isoformat()
    rng_days = [d for d in open_days if r_start <= d <= D]
    bench = tsc.to_ts_code(th["bench"])
    r_idx = pr.call("I01", f"{bench} {r_start}..{D}", "index_daily",
                    {"ts_code": bench, "start_date": _ymd(r_start), "end_date": _ymd(D)},
                    _fields_arg(E("index_daily")), dl("index_daily"))
    r_idx1 = pr.call("I02", f"{bench} 单日 {D}", "index_daily",
                     {"ts_code": bench, "start_date": _ymd(D), "end_date": _ymd(D)},
                     _fields_arg(E("index_daily")), dl("index_daily"))
    r_ths = pr.call("H01", f"全市场 {D}", "ths_daily", {"trade_date": _ymd(D)}, _fields_arg(E("ths_daily")),
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
    out(f"\n④ 逐次调用 ({pr.n} 次 / 上限 {args.max_calls}; 延迟 = 工作进程里量的 HTTP 往返+下载)")
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

    # ⑤ 判读 -------------------------------------------------------------------------------------------------
    checks = []                                     # (检查项, 消费方, 级别, 证据)

    def add(item, lv, ev, api):
        checks.append((item, "+".join(E(api)["consumers"]) or "-", lv, ev))

    for item, lv, ev in check_cal_range(r_all, r_open, start.isoformat(), end.isoformat(), t_iso,
                                        E("trade_cal")["expect"], float(E("trade_cal")["deadline"])):
        add(item, lv, ev, "trade_cal")
    if not hol:
        add("trade_cal 单日 假日", "WARN", f"窗口 {start}..{t_iso} 里找不到工作日休市日 (区间真值表没拿到?), 假日一例没测",
            "trade_cal")
    for kind, day, rw, ro, is_open_day in day_recs:
        item, lv, ev = check_cal_day(kind, day, rw, ro, is_open_day, truth, E("trade_cal")["expect"],
                                     float(E("trade_cal")["deadline"]))
        add(item, lv, ev, "trade_cal")
    sb_rows, listed_a, _ = check_stock_basic(sb_recs, E("stock_basic")["expect"], mk.keep_a_code,
                                             float(E("stock_basic")["deadline"]))
    for item, lv, ev in sb_rows:
        add(item, lv, ev, "stock_basic")
    # daily 全市场: 生产解析 = market._rows_from_daily (价格合法性过滤 + A 股代码)
    parsed_daily, daily_codes = 0, set()
    if r_daily["kind"] == "OK":
        try:
            import pandas as pd
            rows = mk._rows_from_daily(pd.DataFrame(r_daily["items"], columns=r_daily["fields"] or None))
            parsed_daily, daily_codes = len(rows), set(rows)
        except Exception as e:                                 # noqa: BLE001
            parsed_daily = a_code_count(r_daily, mk.keep_a_code)[0]
            out(f"  · daily 生产解析回放失败 ({type(e).__name__}), 退回只数 A 股代码")
    add(*check_by_date(f"daily 按 trade_date {D}", r_daily, D, E("daily")["expect"],
                       float(E("daily")["deadline"]), listed_a, th["ready_ratio"], parsed_daily), "daily")
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
    add(*check_by_date(f"adj_factor 按 trade_date {D}", r_adj, D, E("adj_factor")["expect"],
                       float(E("adj_factor")["deadline"]), listed_a, th["ready_ratio"], len(adj_codes), cover),
        "adj_factor")
    # daily_basic
    bad = call_level(r_db)
    if bad:
        add(f"daily_basic 按 trade_date {D}", bad[0], bad[1], "daily_basic")
    else:
        parts = [field_check(r_db, E("daily_basic")["expect"]), retry_note(r_db),
                 latency_check(r_db, float(E("daily_basic")["deadline"])), round_cap_note(r_db["rows"], None)]
        if r_db["rows"] < th["db_min_rows"]:
            parts.append(("FAIL", f"< 生产门槛 {th['db_min_rows']} 行: 估值兜底会判当日没出、退回前一交易日"))
        lv, txt = combine(parts)
        add(f"daily_basic 按 trade_date {D}", lv, f"{r_db['rows']} 行 (门槛 {th['db_min_rows']}); {txt}",
            "daily_basic")
    # index_daily
    add(*check_range(f"index_daily {bench} 区间 {r_start}..{D}", r_idx, rng_days, E("index_daily")["expect"],
                     float(E("index_daily")["deadline"])), "index_daily")
    bad = call_level(r_idx1)
    if bad:
        add(f"index_daily {bench} 单日 {D}", bad[0], bad[1], "index_daily")
    else:
        got = {_iso(r.get("trade_date")) for r in _dicts(r_idx1)}
        parts = [field_check(r_idx1, E("index_daily")["expect"]), retry_note(r_idx1),
                 latency_check(r_idx1, float(E("index_daily")["deadline"]))]
        if D not in got:
            parts.append(("FAIL", f"单日查询没有 {D} 那根 —— 生产 fetch_index_by_date 当天指数不入库"))
        elif len(got) > 1:
            parts.append(("WARN", f"单日查询回了 {len(got)} 个日期 (生产按日期筛, 不影响)"))
        lv, txt = combine(parts)
        add(f"index_daily {bench} 单日 {D}", lv, f"{r_idx1['rows']} 行; {txt}", "index_daily")
    # ths_daily
    bad = call_level(r_ths)
    if bad:
        add(f"ths_daily 按 trade_date {D}", bad[0], bad[1], "ths_daily")
    else:
        ds_ = _dicts(r_ths)
        n_ti = sum(1 for r in ds_ if str(r.get("ts_code") or "").upper().endswith(th["ti_suffix"].upper()))
        off = sum(1 for r in ds_ if "trade_date" in r and _ymd(r.get("trade_date")) != _ymd(D))
        parts = [field_check(r_ths, E("ths_daily")["expect"]), retry_note(r_ths),
                 latency_check(r_ths, float(E("ths_daily")["deadline"]))]
        if n_ti == 0:
            parts.append(("FAIL", f"没有 {th['ti_suffix']} 指数行 —— 行业指数当日那根补不上"))
        if off:
            parts.append(("WARN", f"{off} 行不是 {D} (生产只认问的那天)"))
        lv, txt = combine(parts)
        add(f"ths_daily 按 trade_date {D}", lv, f"{r_ths['rows']} 行 ({th['ti_suffix']} {n_ti}); {txt}", "ths_daily")
    # 按 ts_code 区间
    add(*check_range(f"daily {RANGE_CODE} 区间 {r_start}..{D}", r_dr, rng_days, E("daily")["expect"],
                     float(E("daily")["deadline"])), "daily")
    dr_dates = sorted({_iso(r.get("trade_date")) for r in _dicts(r_dr)}) if r_dr["kind"] == "OK" else None
    add(*check_range(f"adj_factor {RANGE_CODE} 区间 {r_start}..{D}", r_ar, rng_days, E("adj_factor")["expect"],
                     float(E("adj_factor")["deadline"]), ref_dates=dr_dates), "adj_factor")
    # 财务
    for api, rec, rec2 in fin_recs:
        e = E(api)
        tbl = {"fina_indicator_vip": "fina_indicator", "income_vip": "income"}.get(api, api)
        ref = lake_meta["rows"].get((tbl, _iso(period)))
        add(*check_fin(f"{api} period={period}", rec, e["expect"], float(e["deadline"]), page, ref, rec2), api)
    # 申万三件
    if r_cls is not None:
        add(*check_filtered(f"index_classify level=L1 src={sw_src}", r_cls, E("index_classify")["expect"],
                            float(E("index_classify")["deadline"]), {"level": "L1", "src": sw_src}, None),
            "index_classify")
    if r_mem is not None:
        add(*check_filtered(f"index_member_all l1_code={l1} is_new=Y", r_mem, E("index_member_all")["expect"],
                            float(E("index_member_all")["deadline"]), {"l1_code": l1, "is_new": "Y"}, m_page),
            "index_member_all")
    if r_swd is not None:
        add(*check_range(f"sw_daily {l1} 区间 {r_start}..{D}", r_swd, rng_days, E("sw_daily")["expect"],
                         float(E("sw_daily")["deadline"])), "sw_daily")

    probed = {"trade_cal", "stock_basic", "daily", "adj_factor", "daily_basic", "index_daily", "ths_daily",
              "fina_indicator_vip", "income_vip", "index_classify", "index_member_all", "sw_daily"}
    uncovered = sorted(a for a in A if a not in probed)

    out("\n⑤ 汇总 (每行: 序号. 结论  检查项  [消费方: 生产 = a-share 流水线 / 研究湖 = PC data/lake]  证据)")
    for i, (item, cons, lv, ev) in enumerate(checks, 1):
        out(f"  {i:>2}. {lv:<4}  {item}  [{cons}]  {ev}")
    if uncovered:
        out(f"  未覆盖 (代码在用, 本探针不探): {', '.join(uncovered)}")
    cnt = collections.Counter(lv for _, _, lv, _ in checks)
    fails = [c for c in checks if c[2] in ("FAIL", "SKIP")]
    warns = [c for c in checks if c[2] == "WARN"]
    tally = f"PASS {cnt['PASS']} / WARN {cnt['WARN']} / FAIL {cnt['FAIL']}" + (f" / SKIP {cnt['SKIP']}" if cnt["SKIP"] else "")
    if fails:
        verdict = "不可切换"
        by_cons = collections.Counter(cons for c in fails for cons in c[1].split("+"))
        why = ("未跑完 (调用上限/预算)" if all(c[2] == "SKIP" for c in fails) else "有 FAIL") + \
            " (按消费方: " + " / ".join(f"{k} {v}" for k, v in by_cons.items()) + "): " + \
            "; ".join(c[0] for c in fails[:4]) + (" …" if len(fails) > 4 else "")
        rc = 1
    else:
        verdict = "可切换"
        why = ("全部 PASS" if not warns else "WARN 项生产已兜住或只影响边角, 换后盯: " + "; ".join(c[0] for c in warns[:4]))
        rc = 0
    out(f"\n结论: {verdict} —— {tally} · 调用 {pr.n} 次 · 用时 {pr.clock() - pr.t0:.0f}s · {why}")

    if args.json:
        rep = {"probed_at": nowbj.isoformat(timespec="seconds"), "host": _host(base), "today_bj": t_iso,
               "dates": {"range": [start.isoformat(), end.isoformat()], "sat": sat.isoformat(),
                         "sun": sun.isoformat(), "holiday": hol, "open_day": D, "ts_range": [r_start, D],
                         "period": period},
               "calls": [{k: v for k, v in r.items() if k != "items"} for r in pr.records],
               "checks": [{"item": c[0], "consumer": c[1], "level": c[2], "evidence": c[3]} for c in checks],
               "uncovered": uncovered, "verdict": verdict, "rc": rc, "n_calls": pr.n}
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
    c = sub.add_parser("compat", help="镜像兼容性探针: 只读, 只打印形状, 结论 可切换/不可切换")
    c.add_argument("--expect", action="store_true", help="只列代码期望字段, 不联网")
    c.add_argument("--secrets", default="", help="另一份 secrets.json (只读 tushare_token / tushare_base_url); 默认 = 生产客户端凭据")
    c.add_argument("--max-calls", type=int, default=MAX_CALLS, help=f"调用总数上限 (默认 {MAX_CALLS}, 计划 26)")
    c.add_argument("--gap", type=float, default=GAP_SEC, help=f"调用间隔秒 (默认 {GAP_SEC:g})")
    c.add_argument("--budget", type=float, default=BUDGET_SEC, help=f"墙钟预算秒 (默认 {BUDGET_SEC:g})")
    c.add_argument("--today", default="", help="把哪天当「今天」(北京日历, YYYYMMDD; 默认现在)")
    c.add_argument("--date", default="", help="全市场按日那几次用哪个交易日 (默认: 今天之前最后一个开市日)")
    c.add_argument("--holiday", default="", help="假日一例用哪天 (默认: 真值表里今天及之前最近的工作日休市日)")
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
