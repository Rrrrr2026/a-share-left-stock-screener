#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/tushare_probe.py compat (镜像兼容性探针, 卡 TS-PROBE / TS-PROBE-2) 离线用例 —— 不联网, 假镜像按剧本回包。

覆盖: ① 期望字段与期限从代码静态收集 (常量 / ",".join / 外层函数局部变量 / 守卫集合按变量归属 / 封装 / 动态 api /
客户端别名 / 字符串里的子进程脚本 + TIMEOUT_SEC 覆写 / FIN_TABLES + lake.db meta); 真代码上的不变式。② 内置 A 股休市表:
各年工作日休市天数 + 本文件另抄一份的逐日清单两份互核 (双录), 调休上班的周末照样休市, 2027 未覆盖; 日子 (D / P / 假日)
全从表取。③ 端到端: 10-10 那种「单日 trade_cal 不套 is_open」镜像 → 生产档可用 (单日三例 WARN、客户端回放 []); 日历缺
假日 / 把开市日标休市 / 指数区间缺开市日 / 按日回包日期不对 (无视 trade_date) / 开市日单日返回空 / daily_basic 缺列 /
list_status 不认 → 生产档不可用 (rc 1); 研究湖不兼容 (kaixin8 实测形态) / 财务单页上限 → 只研究湖档不可用, 退出码仍 0;
调用上限 → SKIP = 未跑完; --max-calls 超 30 拒跑 (rc 2); 数据总览 trade_cal 10 s 期限只算生产档。④ token 不进输出与
--json (JSON msg 回显; 非 JSON 错误页回显且跨在 120 字截断边界上)。⑤ 导入本模块无副作用。
跑法: python -X utf8 -m pytest -c ../stock-core/pytest.ini --rootdir . tests/test_tushare_probe_compat.py -q
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import re
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TOKEN = "TEST-TOKEN-xyz-0123456789-NOT-REAL"
BJ = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2026, 10, 13, 9, 30, tzinfo=BJ)            # 北京 10-13 周二; 上一开市日 10-12, 周六 10-10

# A 股**工作日**休市日 —— 双录: 照交易所休市通知 (2025 上证公告〔2024〕38号 / 2026 上证公告〔2025〕45号) 逐日手抄一份,
# 与 tools/tushare_probe.py 的 A_CLOSED 区间表互核 (test_holiday_table_*)。调休上班的周末不在这里: 周末一律休市。
WEEKDAY_CLOSED = {
    2025: ["2025-01-01", "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31", "2025-02-03", "2025-02-04",
           "2025-04-04", "2025-05-01", "2025-05-02", "2025-05-05", "2025-06-02",
           "2025-10-01", "2025-10-02", "2025-10-03", "2025-10-06", "2025-10-07", "2025-10-08"],
    2026: ["2026-01-01", "2026-01-02", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",
           "2026-02-23", "2026-04-06", "2026-05-01", "2026-05-04", "2026-05-05", "2026-06-19", "2026-09-25",
           "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07"],
}
HOLIDAYS = {d for days in WEEKDAY_CLOSED.values() for d in days}


def _load():
    path = os.path.join(ROOT, "tools", "tushare_probe.py")
    spec = importlib.util.spec_from_file_location("_ts_probe_compat", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


P = _load()


# ---------------------------------------------------------------- 假镜像
def _days(a8, b8):
    a = dt.date(int(a8[:4]), int(a8[4:6]), int(a8[6:]))
    b = dt.date(int(b8[:4]), int(b8[4:6]), int(b8[6:]))
    return [a + dt.timedelta(days=i) for i in range((b - a).days + 1)]


def _is_open(d: dt.date) -> int:
    return 0 if d.weekday() >= 5 or d.isoformat() in HOLIDAYS else 1


L_CODES = [f"{600000 + i}.SH" for i in range(300)] + [f"{1 + i:06d}.SZ" for i in range(300)] + \
    [f"{830000 + i}.BJ" for i in range(20)]
D_CODES = [f"{300900 + i}.SZ" for i in range(15)]


class FakeMirror:
    """协议兼容镜像的假实现。开关: single_quirk = 单日 trade_cal 不套 is_open (kaixin8 10-10 形态);
    open_day_single_empty = 开市日单日返回空; page_cap = 财务单页上限; drop = {api: {不给的列}};
    status_ignored = stock_basic 不认 list_status; echo_token_api = 这个端点回包 msg 里回显 token;
    errors = {api: (code, msg)} 业务错误 (kaixin8 实测: _vip 两表 404 unknown_api, index_classify 400 invalid_params);
    member_ignores_l1 = index_member_all 不认 l1_code (回全市场成员); cal_override = {ISO: is_open} 只改 trade_cal
    回包 (行情照真日历出, 只测日历判据); ignore_trade_date = "YYYYMMDD" 按日 (trade_date / 单日区间) 一律回这一天
    (无视所问日期); range_drop = {api: {YYYYMMDD}} 区间查询里缺这些天; slow = {api: 秒} 回包延迟;
    raw_api = 这个端点走真的 _raw_post (用例把 requests.post 打桩成非 JSON 错误页)。"""

    def __init__(self, **kw):
        self.single_quirk = kw.get("single_quirk", True)
        self.open_day_single_empty = kw.get("open_day_single_empty", False)
        self.page_cap = kw.get("page_cap", 20000)
        self.drop = kw.get("drop", {})
        self.status_ignored = kw.get("status_ignored", False)
        self.echo_token_api = kw.get("echo_token_api", "")
        self.fin_total = kw.get("fin_total", 10609)
        self.errors = kw.get("errors", {})
        self.member_ignores_l1 = kw.get("member_ignores_l1", False)
        self.cal_override = kw.get("cal_override", {})
        self.ignore_trade_date = kw.get("ignore_trade_date", "")
        self.range_drop = kw.get("range_drop", {})
        self.slow = kw.get("slow", {})
        self.raw_api = kw.get("raw_api", "")
        self.calls = []

    def __call__(self, base, payload, deadline):
        assert payload["token"] == TOKEN
        api, p = payload["api_name"], payload["params"]
        self.calls.append((api, dict(p)))
        if api == self.raw_api:
            return P._raw_post(base, payload, 5.0)
        if api == self.echo_token_api:
            return {"http": 200, "secs": 0.05, "bytes": 99,
                    "json": {"code": 40203, "msg": f"抱歉，您没有接口访问权限 token={payload['token']}", "data": None}}
        if api in self.errors:
            code, msg = self.errors[api]
            return {"http": 200, "secs": 0.05, "bytes": 99, "json": {"code": code, "msg": msg, "data": None}}
        fields = [f for f in (payload["fields"] or "").split(",") if f]
        self._api = api
        cols, rows = getattr(self, "_" + api)(p, fields)
        drop = self.drop.get(api, set())
        keep = [i for i, c in enumerate(cols) if c not in drop]
        cols = [cols[i] for i in keep]
        rows = [[r[i] for i in keep] for r in rows]
        return {"http": 200, "secs": self.slow.get(api, 0.2), "bytes": 1000 + 50 * len(rows),
                "json": {"code": 0, "msg": "", "data": {"fields": cols, "items": rows}}}

    def _cal_open(self, d: dt.date) -> int:
        return self.cal_override.get(d.isoformat(), _is_open(d))

    def _trade_cal(self, p, fields):
        days = _days(p["start_date"], p["end_date"])
        single = p["start_date"] == p["end_date"]
        rows = [{"exchange": "SSE", "cal_date": d.strftime("%Y%m%d"), "is_open": self._cal_open(d)} for d in days]
        if single and self.open_day_single_empty and rows and rows[0]["is_open"] == 1:
            rows = []
        if p.get("is_open") == "1" and not (single and self.single_quirk):
            rows = [r for r in rows if r["is_open"] == 1]
        cols = fields or ["exchange", "cal_date", "is_open"]
        return cols, [[r.get(c) for c in cols] for r in rows]

    def _stock_basic(self, p, fields):
        st = p.get("list_status", "L")
        codes = L_CODES if (st == "L" or self.status_ignored) else (D_CODES if st == "D" else [])
        real = "L" if self.status_ignored else st
        cols = fields or ["ts_code", "symbol", "name"]
        out = []
        for c in codes:
            row = {"ts_code": c, "symbol": c[:6], "name": "股" + c[:6], "industry": "银行", "market": "主板",
                   "list_date": "20100101", "delist_date": "20200101" if real == "D" else None, "list_status": real}
            out.append([row.get(k) for k in cols])
        return cols, out

    def _td(self, p):
        return self.ignore_trade_date or p["trade_date"]

    def _open_days(self, p):
        if "trade_date" in p:
            return [self._td(p)]
        if self.ignore_trade_date and p["start_date"] == p["end_date"]:
            return [self.ignore_trade_date]
        drop = self.range_drop.get(self._api, set())
        return [d.strftime("%Y%m%d") for d in _days(p["start_date"], p["end_date"])
                if _is_open(d) and d.strftime("%Y%m%d") not in drop]

    def _bars(self, p, fields, extra):
        codes = [p["ts_code"]] if "ts_code" in p else L_CODES
        cols = fields or ["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount"]
        out = []
        for d in self._open_days(p):
            for c in codes:
                row = {"ts_code": c, "trade_date": d, "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5,
                       "vol": 1000.0, "amount": 1050.0, "pre_close": 10.0, "pct_change": 5.0}
                row.update(extra)
                out.append([row.get(k) for k in cols])
        return cols, out

    def _daily(self, p, fields):
        return self._bars(p, fields, {})

    def _adj_factor(self, p, fields):
        return self._bars(p, fields or ["ts_code", "trade_date", "adj_factor"], {"adj_factor": 1.5})

    def _daily_basic(self, p, fields):
        cols = fields or ["ts_code", "trade_date", "close"]
        return cols, [[{"ts_code": c, "trade_date": self._td(p)}.get(k, 1.0) for k in cols] for c in L_CODES * 8][:5600]

    def _index_daily(self, p, fields):
        cols = fields or ["ts_code", "trade_date", "close", "open", "high", "low", "pre_close", "change",
                          "pct_chg", "vol", "amount"]
        return self._bars(p, cols, {})

    def _ths_daily(self, p, fields):
        cols = fields or ["ts_code", "trade_date", "close"]
        codes = [f"8811{i:02d}.TI" for i in range(90)]
        return cols, [[{"ts_code": c, "trade_date": self._td(p)}.get(k, 100.0) for k in cols] for c in codes]

    def _fin(self, p, fields):
        n = max(0, min(int(p.get("limit") or 9000), self.page_cap, self.fin_total - int(p.get("offset") or 0)))
        cols = fields or ["ts_code", "ann_date", "end_date"]
        return cols, [[{"ts_code": f"{i:06d}.SZ", "end_date": p["period"]}.get(k, "20260830") for k in cols]
                      for i in range(n)]

    _fina_indicator_vip = _fin
    _income_vip = _fin

    SW_L1 = [f"8010{i:02d}.SI" for i in range(10, 41)]

    def _index_classify(self, p, fields):
        cols = fields or ["index_code", "industry_name", "level", "industry_code", "is_pub", "parent_code", "src"]
        return cols, [[{"index_code": c, "industry_name": "行业" + c[:6], "level": p.get("level"),
                        "industry_code": c[:6], "is_pub": "1", "parent_code": "0", "src": p.get("src")}.get(k)
                       for k in cols] for c in self.SW_L1]

    def _index_member_all(self, p, fields):
        cols = fields or ["l1_code", "l1_name", "l2_code", "l2_name", "l3_code", "l3_name", "ts_code", "name",
                          "in_date", "out_date", "is_new"]
        l1s = self.SW_L1 if self.member_ignores_l1 else [p["l1_code"]]
        allrows = [{"l1_code": l1, "ts_code": f"{600000 + i}.SH", "is_new": p.get("is_new", "Y"), "in_date": "20200101"}
                   for l1 in l1s for i in range(150)]
        off, lim = int(p.get("offset") or 0), int(p.get("limit") or 2000)
        return cols, [[r.get(k) for k in cols] for r in allrows[off:off + lim]]

    def _sw_daily(self, p, fields):
        return self._bars(p, fields or ["ts_code", "trade_date", "close"], {"name": "农林牧渔", "change": 1.0,
                                                                            "pe": 20.0, "pb": 2.0, "float_mv": 1.0,
                                                                            "total_mv": 1.0})


# ---------------------------------------------------------------- 迷你 stock-core / lake.db
MINI_LAKE = '''
FIN_PAGE = 9000
FIN_TABLES = {
    "fina_indicator_vip": ("fina_indicator", ["ts_code", "end_date", "ann_date", "update_flag"],
                           {"ts_code", "ann_date", "end_date", "update_flag"}, ["ann_date", "end_date"]),
    "income_vip": ("income", ["ts_code", "end_date", "ann_date", "f_ann_date", "report_type", "update_flag"],
                   {"ts_code"}, ["ann_date", "f_ann_date", "end_date"]),
}
DAILY_BASIC_FIELDS = ["ts_code", "trade_date", "close", "pe_ttm", "total_mv"]
SW_SRC = "SW2021"
MEMBER_PAGE = 2000
SW_DAILY_FIELDS = ["ts_code", "trade_date", "close", "pct_change"]


class Lake:
    def query(self, api, **params):
        return self._query(api, **params)

    def open_days(self):
        df = self.query("trade_cal", fields="cal_date,is_open", exchange="SSE")
        return list(df["cal_date"]) if "cal_date" in df.columns else []

    def pull_daily_basic(self):
        fields = ",".join(DAILY_BASIC_FIELDS)

        def fetch(d):
            return d, self.query("daily_basic", trade_date=d, fields=fields), None
        return fetch

    def pull_sw(self, code="801010.SI"):
        cls = self.query("index_classify", level="L1", src=SW_SRC)
        mem, n = self._pages("index_member_all", MEMBER_PAGE, l1_code=code, is_new="Y")
        df = self.query("sw_daily", ts_code=code, start_date="20260101", end_date="20260131",
                        fields=",".join(SW_DAILY_FIELDS))
        return cls, mem, df
'''

# 数据总览的形状: trade_cal 在一段字符串形式的子进程脚本里, 客户端用别名 t, socket 超时改成 10 s (这里故意让
# deadline_sec=30 比它宽, 用例核「期限取 min」); 脚本外的 t 是别的东西, 不能被认成客户端。
MINI_CATALOG = """
import subprocess, sys
XCAL_CHILD_TIMEOUT = 20.0
_XCAL_CHILD = r'''
import json, sys
a_dir, start, end = sys.argv[1:4]
sys.path.insert(0, a_dir)
from ashare import tushare_client as t
t.USE_PROCESS_DEADLINE = False
t.TIMEOUT_SEC = 10
print(json.dumps(t.trade_cal(start, end, retries=1, deadline_sec=30)))
'''


def calendar(a_dir, s, e):
    return subprocess.run([sys.executable, "-c", _XCAL_CHILD, a_dir, s, e], timeout=XCAL_CHILD_TIMEOUT)


def not_the_client(t):
    return t.trade_cal("x")
"""


def _mini_core(tmp_path):
    core = tmp_path / "stock-core"
    (core / "research").mkdir(parents=True, exist_ok=True)
    (core / "research" / "lake.py").write_text(MINI_LAKE, encoding="utf-8")
    (core / "research" / "data_catalog.py").write_text(MINI_CATALOG, encoding="utf-8")
    return str(core)


def _mini_lake_db(tmp_path, fina_fields=None, income_ref=10609):
    path = tmp_path / "lake.db"
    if path.exists():
        path.unlink()
    c = sqlite3.connect(str(path))
    c.execute("CREATE TABLE meta(tbl TEXT, key TEXT, value TEXT, PRIMARY KEY(tbl, key))")
    c.execute("CREATE TABLE pull_log(tbl TEXT, key TEXT, status TEXT, n_raw INT, n_kept INT, n_drop_bj INT, "
              "n_drop_other INT, calls INT, secs REAL, fetched_at TEXT, note TEXT, PRIMARY KEY(tbl, key))")
    ff = fina_fields or ["ts_code", "ann_date", "end_date", "eps", "roe", "update_flag"]
    c.execute("INSERT INTO meta VALUES('fina_indicator','fields',?)", (json.dumps(ff),))
    c.execute("INSERT INTO meta VALUES('income','fields',?)",
              (json.dumps(["ts_code", "ann_date", "f_ann_date", "end_date", "report_type", "n_income", "update_flag"]),))
    c.execute("INSERT INTO pull_log VALUES('income','2026-06-30','ok',?,0,0,0,2,1.0,'x','')", (income_ref,))
    c.execute("INSERT INTO pull_log VALUES('fina_indicator','2026-06-30','ok',10747,0,0,0,2,1.0,'x','')")
    c.commit()
    c.close()
    return str(path)


def _secrets(tmp_path):
    p = tmp_path / "secrets.json"
    p.write_text(json.dumps({"tushare_token": TOKEN, "tushare_base_url": "https://mirror.example.invalid/tushare/sdk"}),
                 encoding="utf-8")
    return str(p)


def _run(tmp_path, mirror, extra=(), lake_db=True, now=NOW):
    tmp_path.mkdir(parents=True, exist_ok=True)
    args = ["compat", "--secrets", _secrets(tmp_path), "--core", _mini_core(tmp_path), "--gap", "0"]
    args += (["--lake-db", _mini_lake_db(tmp_path)] if lake_db else ["--no-lake-db"])
    args += list(extra)
    ns = P.build_parser().parse_args(args)
    lines = []
    rc = P.run_compat(ns, post=mirror, now=now, out=lines.append, sleep=lambda s: None)
    return rc, "\n".join(lines)


_ROW = re.compile(r"^\s+\d+\. (PASS|WARN|FAIL|SKIP)\s+(.+?)  \[")


class _Rows(dict):
    """汇总表 -> {检查项: 结论}; 取值按前缀匹配 (检查项里带日期, 用例只写前缀)。"""

    def __getitem__(self, prefix):
        hits = [v for k, v in self.items() if k.startswith(prefix)]
        assert hits, f"汇总表里没有以 {prefix!r} 开头的检查项: {list(self)}"
        return hits[0]


def _check_rows(text):
    out = _Rows()
    for ln in text.splitlines():
        m = _ROW.match(ln)
        if m:
            out[m.group(2)] = m.group(1)
    return out


def _leaks(s: str, tok: str = TOKEN, n: int = 8) -> list:
    """s 里出现的 token 片段 (≥ n 字)。"""
    return [tok[i:i + n] for i in range(len(tok) - n + 1) if tok[i:i + n] in s]


# ---------------------------------------------------------------- ① 期望字段与期限收集
SYN = '''
from ashare import tushare_client as tsc
F_CONST = "ts_code,trade_date,close"
COLS = ["a", "b"]
DL = 45


def by_const():
    return tsc.query("daily", trade_date="20260101", fields=F_CONST, deadline_sec=DL)


def by_join_local():
    f = ",".join(COLS + ["c"])
    df = tsc.query("daily_basic", trade_date="x", fields=f)
    fa = tsc.query("adj_factor", trade_date="x", fields="ts_code,adj_factor")
    if "pe_ttm" in df.columns and "adj_factor" in fa.columns:
        return df


def outer():
    fields = ",".join(COLS)

    def inner(d):
        return tsc.query("ths_daily", trade_date=d, fields=fields)
    return inner


def wrapper_guard(sym):
    df = tsc.index_daily(sym, "20260101", "20260102")
    need = {"trade_date", "close"}
    cols = set(df.columns)
    if not need <= cols:
        return None


def dynamic(api):
    return tsc.query(api, limit=1)


def not_tushare(df):
    return df.query("x > 1")
'''


def test_scan_source_resolves_fields_refs_wrappers_and_dynamic():
    res = P.scan_source(SYN, "syn.py", "生产")
    by = {}
    for s in res["sites"]:
        by.setdefault(s["api"], []).append(s)
    assert by["daily"][0]["fields"] == ["ts_code", "trade_date", "close"]          # 模块常量
    assert by["daily"][0]["deadline"] == 45
    assert by["daily_basic"][0]["fields"] == ["a", "b", "c"]                        # ",".join(A + [..]) 局部变量
    assert by["daily_basic"][0]["refs"] == ["pe_ttm"]                                # 只归 df 那次
    assert by["adj_factor"][0]["refs"] == ["adj_factor"]                             # 只归 fa 那次
    assert by["ths_daily"][0]["fields"] == ["a", "b"]                                # 外层函数的局部变量
    w = by["index_daily"][0]
    assert w["via_wrapper"] and sorted(w["refs"]) == ["close", "trade_date"]         # 守卫集合经别名 cols 归属
    assert by[None][0]["dynamic"] and by[None][0]["qual"] == "dynamic"               # api 运行时才知道
    assert len(res["sites"]) == 6                                                     # df.query(expr) 不算
    assert res["consts"]["DL"] == 45
    assert all(s["sock_cap"] is None for s in res["sites"])                          # 没改 socket 超时


def test_scan_embedded_child_script_alias_and_socket_cap():
    """数据总览那种字符串里的子进程脚本: 别名 t 认成客户端, TIMEOUT_SEC=10 卡住 deadline_sec=30, 行号折回原文件;
    脚本外参数名叫 t 的函数不算 (那份源码没 import 客户端)。"""
    res = P.scan_source(MINI_CATALOG, "stock-core/research/data_catalog.py", "数据总览")
    tc = [s for s in res["sites"] if s["api"] == "trade_cal"]
    assert len(tc) == 1, res["sites"]
    s = tc[0]
    assert s["via_wrapper"] and s["deadline"] == 30 and s["sock_cap"] == 10 and s["consumer"] == "数据总览"
    assert s["qual"] == "_XCAL_CHILD (子进程脚本)" and s["line"] == 11       # MINI_CATALOG 第 11 行是 print(…trade_cal…)
    assert MINI_CATALOG.splitlines()[s["line"] - 1].startswith("print(json.dumps(t.trade_cal(")


def test_collect_expectations_real_code_invariants(tmp_path):
    exp = P.collect_expectations(ROOT, _mini_core(tmp_path), P.read_lake_meta(_mini_lake_db(tmp_path)))
    A = exp["apis"]
    for col in ("ts_code", "trade_date", "open", "high", "low", "close", "vol"):
        assert col in A["daily"]["expect"]
    assert "adj_factor" not in A["daily"]["expect"]           # fetch_bars_bulk_tushare 里对 fa 的引用不冤枉 daily
    assert "adj_factor" in A["adj_factor"]["expect"]
    assert A["trade_cal"]["mode"] == "explicit" and set(A["trade_cal"]["expect"]) == {"cal_date", "is_open"}
    assert A["index_daily"]["mode"] == "default"
    assert {"trade_date", "open", "high", "low", "close", "vol"} <= set(A["index_daily"]["expect"])
    assert {"list_status", "delist_date", "industry"} <= set(A["stock_basic"]["expect"])
    assert {"pe_ttm", "total_mv"} <= set(A["daily_basic"]["expect"])
    assert "研究湖" in A["daily_basic"]["consumers"] and "生产" in A["daily_basic"]["consumers"]
    assert A["ths_daily"]["deadline"] < A["daily"]["deadline"]   # ths_daily 生产期限收紧过 (TS_THS_DAILY_DEADLINE_SEC)
    # 期限按消费方分开记: trade_cal 的数据总览 = min(deadline_sec 30, TIMEOUT_SEC 10); 生产/研究湖 = 客户端默认
    by = A["trade_cal"]["deadline_by"]
    assert by["数据总览"] == 10 and by["生产"] == by["研究湖"] == exp["client_deadline"]
    assert A["trade_cal"]["deadline"] == 10 and A["trade_cal"]["wait"] == exp["client_deadline"]
    assert set(A["trade_cal"]["consumers"]) == {"生产", "研究湖", "数据总览"}
    # 财务: lake.db meta 那份字段表 + FIN_TABLES 主键列
    assert A["fina_indicator_vip"]["expect"][:6] == ["ts_code", "ann_date", "end_date", "eps", "roe", "update_flag"]
    assert "lake.db meta" in A["fina_indicator_vip"]["meta_source"]
    assert exp["fin_page"] == 9000


def test_real_catalog_trade_cal_deadline_is_collected():
    """真 stock-core 的 research/data_catalog.py (同级 ../stock-core 或 STOCK_CORE_DIR): trade_cal 子进程的 10 s 被收进来。"""
    core = P.find_core_dir()
    if not core or not os.path.isfile(os.path.join(core, "research", "data_catalog.py")):
        pytest.skip("没有 stock-core/research/data_catalog.py")
    exp = P.collect_expectations(ROOT, core, P.read_lake_meta(None))
    by = exp["apis"]["trade_cal"]["deadline_by"]
    assert by["数据总览"] <= 10 < by["生产"], by
    assert any("_XCAL_CHILD (子进程脚本)" in w for w in exp["apis"]["trade_cal"]["sites"])


def test_fin_fields_fall_back_to_fin_tables_without_lake_db(tmp_path):
    exp = P.collect_expectations(ROOT, _mini_core(tmp_path), P.read_lake_meta(None))
    e = exp["apis"]["income_vip"]
    assert set(e["expect"]) == {"ts_code", "end_date", "ann_date", "f_ann_date", "report_type", "update_flag"}
    assert "FIN_TABLES" in e["meta_source"]


# ---------------------------------------------------------------- ② 内置休市表与选日子
def test_holiday_table_matches_double_entry_and_counts():
    for y, days in WEEKDAY_CLOSED.items():
        got = sorted(d for d in P._closed_set(y) if dt.date.fromisoformat(d).weekday() < 5)
        assert got == days, y
    assert len(WEEKDAY_CLOSED[2025]) == 18 and len(WEEKDAY_CLOSED[2026]) == 19
    for d in ("2026-09-20", "2026-10-10", "2026-02-14", "2026-02-28", "2026-05-09", "2026-01-04",
              "2025-09-28", "2025-10-11", "2025-01-26", "2025-02-08", "2025-04-27"):
        assert P.a_open(d) is False, d                      # 调休上班的周末, 交易所照样休市
    for d in ("2026-10-08", "2026-10-09", "2026-02-24", "2026-01-05", "2026-09-28", "2026-09-29",
              "2025-10-09", "2025-02-05", "2025-01-02"):
        assert P.a_open(d) is True, d                       # 节后首个开市日
    # 10-13 实跑窗口 03-27..11-02 里表给 146 个开市日 (与镜像 kaixin8 那次回包、本机价格库一致)
    assert len(P.table_open_days("2026-03-27", "2026-11-02")) == 146


def test_holiday_table_2027_not_covered_yet():
    assert P.A_CLOSED.get(2027) is None                     # 国办 2027 安排截至 2026-10-12 未发布
    assert P.a_open("2027-01-04") is None and P.a_open("2027-01-02") is False   # 工作日判不了 / 周末照样休市
    truth, unknown, years = P.holiday_truth("2026-12-30", "2027-01-05")
    assert years == {2027} and unknown == ["2027-01-01", "2027-01-04", "2027-01-05"]
    assert truth["2026-12-31"] == 1 and truth["2027-01-02"] == 0


def test_dates_come_from_builtin_table():
    t = dt.date(2026, 10, 13)
    assert P.last_open_before(t) == "2026-10-12"
    assert P.last_holiday_on_or_before(t) == "2026-10-07"
    assert P.past_by_date_day(t) == "2026-09-29"                      # 上月 (9 月) 最后一个开市的周二
    assert P.past_by_date_day(dt.date(2026, 4, 1)) == "2026-03-24"    # 03-31 也是周二, 但离今天不到 7 天
    assert P.last_open_before(dt.date(2026, 10, 8)) == "2026-09-30"   # 国庆前最后一个开市日
    assert P.last_open_before(dt.date(2027, 1, 5)) == "2026-12-31"    # 2027 没覆盖: 判不了的日子跳过
    assert P.past_by_date_day(dt.date(2027, 1, 5)) == "2026-12-29"


# ---------------------------------------------------------------- ③ 端到端
def test_kaixin8_like_mirror_production_usable_with_single_day_warn(tmp_path):
    m = FakeMirror(single_quirk=True)
    rc, text = _run(tmp_path, m)
    rows = _check_rows(text)
    assert rc == 0, text
    assert "结论: 生产档可用 · 研究湖档可用" in text
    assert len(m.calls) == 26 and "调用 26 次" in text
    assert rows["trade_cal 单日 周六"] == "WARN" and rows["trade_cal 单日 周日"] == "WARN"
    assert rows["trade_cal 单日 假日"] == "WARN" and rows["trade_cal 单日 开市日"] == "PASS"
    assert rows["trade_cal 区间 (不带 is_open)"] == "PASS" and "逐日一致 221/221 天" in text
    assert "客户端回放 [] ✓" in text and "客户端回放 ['2026-10-12'] ✓" in text
    sat = [c for c in m.calls if c[0] == "trade_cal" and c[1]["start_date"] == c[1]["end_date"] == "20261010"]
    assert len(sat) == 2                                       # 周六带/不带各一次
    hol = [c for c in m.calls if c[0] == "trade_cal" and c[1]["start_date"] == c[1]["end_date"] == "20261007"]
    assert len(hol) == 2                                       # 假日 = 内置休市表里今天及之前最近的工作日休市日
    by_date = [c for c in m.calls if "trade_date" in c[1]]
    assert sorted(c[0] for c in by_date) == ["adj_factor", "daily", "daily_basic", "ths_daily"]
    assert all(c[1]["trade_date"] == "20260929" for c in by_date)          # 按日问已知过去的开市日 P
    idx1 = [c for c in m.calls if c[0] == "index_daily" and c[1]["start_date"] == c[1]["end_date"]]
    assert len(idx1) == 1 and idx1[0][1]["start_date"] == "20260929"
    assert "回包日期全是 2026-09-29" in text
    assert TOKEN not in text


def test_strict_mirror_all_pass(tmp_path):
    rc, text = _run(tmp_path, FakeMirror(single_quirk=False))
    rows = _check_rows(text)
    assert rc == 0 and text.count("全部 PASS") == 2, text
    assert set(rows.values()) == {"PASS"}, text


def test_calendar_without_any_holiday_is_fail(tmp_path):
    """校验员 S1: 日历只把周末标休市 (法定假日全标开市) —— 以前真值表取自镜像本身, 判可切换; 现在对照内置表 FAIL,
    假日一例仍是内置表的 10-07 (镜像说开市 → FAIL)。"""
    m = FakeMirror(single_quirk=False, cal_override={d: 1 for d in HOLIDAYS})
    rc, text = _run(tmp_path, m)
    rows = _check_rows(text)
    assert rc == 1 and "结论: 生产档不可用" in text, text
    assert rows["trade_cal 区间 (不带 is_open)"] == "FAIL" and "把法定假日标成开市" in text
    assert rows["trade_cal 区间 is_open=1"] == "FAIL" and "客户端回放把" in text
    assert rows["trade_cal 单日 假日 2026-10-07"] == "FAIL"
    hol = [c for c in m.calls if c[0] == "trade_cal" and c[1]["start_date"] == c[1]["end_date"] == "20261007"]
    assert len(hol) == 2


def test_calendar_missing_only_national_day_is_fail(tmp_path):
    """校验员 S2: 只缺国庆 (06-19 / 09-25 仍是假日) —— 以前假日一例改测 09-25 全 PASS; 现在 FAIL。"""
    nd = [d for d in HOLIDAYS if d.startswith("2026-10-0")]
    rc, text = _run(tmp_path, FakeMirror(single_quirk=False, cal_override={d: 1 for d in nd}))
    rows = _check_rows(text)
    assert rc == 1 and rows["trade_cal 区间 (不带 is_open)"] == "FAIL" and "把法定假日标成开市 5 天" in text
    assert rows["trade_cal 单日 假日 2026-10-07"] == "FAIL"


def test_calendar_open_day_marked_closed_is_fail(tmp_path):
    rc, text = _run(tmp_path, FakeMirror(single_quirk=False, cal_override={"2026-09-29": 0, "2026-10-09": 0}))
    rows = _check_rows(text)
    assert rc == 1 and "把开市日标成休市 2 天" in text
    assert rows["trade_cal 区间 (不带 is_open)"] == "FAIL"
    assert rows["trade_cal 区间 is_open=1"] == "FAIL" and "客户端回放漏了 2 个开市日" in text


def test_index_range_missing_open_day_is_fail_stock_range_warn(tmp_path):
    rc, text = _run(tmp_path / "idx", FakeMirror(single_quirk=False, range_drop={"index_daily": {"20261009"}}))
    rows = _check_rows(text)
    assert rc == 1 and rows["index_daily 000300.SH 区间"] == "FAIL" and "指数不停牌" in text
    rc2, text2 = _run(tmp_path / "stk", FakeMirror(single_quirk=False, range_drop={"daily": {"20261009"}}))
    rows2 = _check_rows(text2)
    assert rc2 == 0 and rows2["daily 000001.SZ 区间"] == "WARN" and "停牌也会这样" in text2


def test_by_date_mirror_ignoring_trade_date_is_fail(tmp_path):
    """校验员 S3 (反着来): 问已知过去的 P=09-29, 镜像无视 trade_date 一律回最新一天 10-12 —— 生产不按日期过滤的三个端点
    FAIL, ths_daily / index_daily 单日当天一行没有也 FAIL。问最近开市日的话这种镜像分不出来。"""
    rc, text = _run(tmp_path, FakeMirror(single_quirk=False, ignore_trade_date="20261012"))
    rows = _check_rows(text)
    assert rc == 1 and "结论: 生产档不可用" in text, text
    for item in ("daily 按 trade_date", "adj_factor 按 trade_date", "daily_basic 按 trade_date",
                 "ths_daily 按 trade_date", "index_daily 000300.SH 单日"):
        assert rows[item] == "FAIL", (item, text)
    assert "trade_date ≠ 所问的 2026-09-29" in text


def test_open_day_single_query_empty_is_not_usable(tmp_path):
    rc, text = _run(tmp_path, FakeMirror(open_day_single_empty=True))
    assert rc == 1 and "结论: 生产档不可用" in text
    assert _check_rows(text)["trade_cal 单日 开市日"] == "FAIL"


def test_kaixin8_lake_incompat_fails_only_lake_tier(tmp_path):
    """10-13 实测形态: _vip 两表 unknown_api + 申万分类 invalid_params —— 研究湖档不可用, 但生产档可用, 退出码 0:
    只换 token 的流程 A 不会因为研究湖 FAIL 去还原 .bak (校验员 MEDIUM ④)。"""
    m = FakeMirror(errors={"fina_indicator_vip": (404, "[unknown_api] 接口不存在或未开放"),
                           "income_vip": (404, "[unknown_api] 接口不存在或未开放"),
                           "index_classify": (400, "[invalid_params] 业务参数缺失或格式不合法")})
    out_json = str(tmp_path / "rep.json")
    rc, text = _run(tmp_path, m, extra=["--json", out_json])
    rows = _check_rows(text)
    assert rc == 0, text
    assert "结论: 生产档可用 · 研究湖档不可用" in text
    assert re.search(r"结论 · 研究湖档 .*: 不可用 —— .*FAIL 3", text) and "不改退出码" in text
    assert rows["fina_indicator_vip"] == "FAIL" and rows["income_vip"] == "FAIL" and rows["index_classify"] == "FAIL"
    assert rows["index_member_all l1_code=801010.SI"] == "PASS"         # 分类没拿到 -> 用兜底 L1
    assert rows["sw_daily 801010.SI"] == "PASS" and rows["daily 按 trade_date"] == "PASS"
    assert rows["trade_cal 单日 周六"] == "WARN"
    rep = json.load(open(out_json, encoding="utf-8"))
    assert rep["verdict"] == {"生产": "可用", "研究湖": "不可用"} and rep["rc"] == 0
    assert rep["tiers"]["研究湖"]["fail"][0].startswith("fina_indicator_vip")


def test_fin_page_cap_below_lake_page_fails_lake_tier_only(tmp_path):
    rc, text = _run(tmp_path, FakeMirror(page_cap=5000))
    rows = _check_rows(text)
    assert rc == 0 and "研究湖档不可用" in text
    assert rows["income_vip period=20260630"] == "FAIL" and "静默少拉" in text


def test_fin_period_shorter_than_page_gets_one_followup_and_passes(tmp_path):
    m = FakeMirror(fin_total=6890)
    rc, text = _run(tmp_path, m)
    assert rc == 0, text
    follow = [c for c in m.calls if c[0] in ("fina_indicator_vip", "income_vip") and c[1].get("offset")]
    assert len(follow) == 2 and len(m.calls) == 28
    assert "续页 offset=6890 0 行 = 该期就 6890 行" in text
    assert _check_rows(text)["income_vip period=20260630"] == "WARN"      # 比湖里上次 (10609 行) 少一截


def test_missing_requested_field_is_fail_both_tiers(tmp_path):
    rc, text = _run(tmp_path, FakeMirror(drop={"daily_basic": {"pe_ttm"}}))
    assert rc == 1 and "结论: 生产档不可用 · 研究湖档不可用" in text      # daily_basic 两档都在用
    assert _check_rows(text)["daily_basic 按 trade_date"] == "FAIL" and "pe_ttm" in text


def test_list_status_ignored_is_fail(tmp_path):
    rc, text = _run(tmp_path, FakeMirror(status_ignored=True))
    rows = _check_rows(text)
    assert rc == 1 and rows["stock_basic list_status=D"] == "FAIL"


def test_call_cap_marks_rest_skip_and_not_usable(tmp_path):
    m = FakeMirror()
    rc, text = _run(tmp_path, m, extra=["--max-calls", "5"])
    assert len(m.calls) == 5
    assert rc == 1 and "结论: 生产档未跑完" in text and "SKIP" in text


def test_max_calls_hard_cap_refuses_over_30(tmp_path):
    """校验员 LOW: 文档写「硬上限 30」, 以前 --max-calls 100 照收。现在超 30 (或 < 1) 拒跑, 一次也不调。"""
    for bad in ("31", "100", "0", "-3"):
        m = FakeMirror()
        rc, text = _run(tmp_path, m, extra=["--max-calls", bad])
        assert rc == 2 and not m.calls and "拒跑" in text, (bad, text)
    m = FakeMirror()
    rc, text = _run(tmp_path, m, extra=["--max-calls", "30"])
    assert rc == 0 and len(m.calls) == 26
    assert P.Prober(TOKEN, "https://x.invalid/", max_calls=100).max_calls == P.MAX_CALLS == 30


def test_catalog_trade_cal_deadline_counts_for_production_tier_only(tmp_path):
    """校验员 LOW: 数据总览的 trade_cal 是 10 s (子进程 socket 超时)。7.5 s: 生产档过半 WARN、研究湖档 (120 s) PASS;
    12 s: 生产档 FAIL (退出码 1), 研究湖档照样可用。"""
    rc, text = _run(tmp_path / "a", FakeMirror(single_quirk=False, slow={"trade_cal": 7.5}))
    rows = _check_rows(text)
    assert rc == 0 and rows["trade_cal 区间 (不带 is_open)"] == "WARN", text
    assert "生产档 WARN: 延迟 7.5s > 数据总览期限 10s 的 50%" in text and "生产档 WARN / 研究湖档 PASS" in text
    rc2, text2 = _run(tmp_path / "b", FakeMirror(single_quirk=False, slow={"trade_cal": 12.0}))
    assert rc2 == 1 and "结论: 生产档不可用 · 研究湖档可用" in text2, text2
    assert "延迟 12.0s > 数据总览期限 10s (这次会超时)" in text2


def test_window_into_uncovered_year_warns_only(tmp_path):
    """今天 2026-12-20: 区间日历末端进到 2027 (国办通知未录入) —— 那几天只核周末, WARN 提醒补表, 生产档仍可用。"""
    now = dt.datetime(2026, 12, 20, 9, 30, tzinfo=BJ)
    m = FakeMirror(single_quirk=False)
    rc, text = _run(tmp_path, m, now=now)
    rows = _check_rows(text)
    assert rc == 0, text
    assert rows["trade_cal 区间 (不带 is_open)"] == "WARN" and "内置休市表未覆盖 2027" in text
    assert all(c[1]["trade_date"] == "20261124" for c in m.calls if "trade_date" in c[1])   # 11 月最后一个开市的周二


def test_token_never_printed_or_saved_json_echo(tmp_path):
    out_json = str(tmp_path / "rep.json")
    rc, text = _run(tmp_path, FakeMirror(echo_token_api="ths_daily"), extra=["--json", out_json])
    assert TOKEN not in text and "<token>" in text
    with open(out_json, encoding="utf-8") as f:
        saved = f.read()
    assert TOKEN not in saved and json.loads(saved)["n_calls"] == 26
    assert _check_rows(text)["ths_daily 按 trade_date"] == "FAIL"     # 无权限 = FAIL
    assert rc == 1


class _EchoResp:
    """网关的非 JSON 错误页 (requests.Response 的替身)。"""

    def __init__(self, text, status=502):
        self.status_code, self.text, self.content = status, text, text.encode("utf-8")

    def json(self):
        raise ValueError("not json")


def test_nonjson_echo_scrubbed_before_truncation(monkeypatch):
    """校验员 MEDIUM ③: 非 JSON 回包以前先截 120 字再抹 token, token 跨在截断边界上 (k=87..119) 前缀原样漏出。
    现在 _raw_post 先抹 (整串 + ≥8 字片段) 后截, Prober.clean 再抹一遍: 任何偏移都不漏 ≥8 字的片段。"""
    import requests
    for k in (0, 40, 86, 87, 100, 110, 113, 119, 120, 121, 150, 400):
        body = "E" * k + TOKEN + " | 截断的回显 " + TOKEN[3:15] + " | tail"
        monkeypatch.setattr(requests, "post", lambda *a, **kw: _EchoResp(body))
        raw = P._raw_post("https://mirror.example.invalid/x", {"api_name": "daily", "token": TOKEN, "params": {}}, 5.0)
        assert raw["json"] is None and len(raw["head"]) <= 120
        assert not _leaks(raw["head"]), (k, raw["head"])
        pr = P.Prober(TOKEN, "https://mirror.example.invalid/x", post=lambda b, p, d: P._raw_post(b, p, 5.0),
                      gap=0, sleep=lambda s: None)
        rec = pr.call("X", "t", "daily", {})
        assert rec["kind"] == "HTTP" and not _leaks(rec["msg"]), (k, rec["msg"])
    assert P.scrub_token("a" + TOKEN[5:20] + "b", TOKEN) == "a<token…>b"            # 片段 (≥ 8 字) 也抹
    assert P.scrub_token("x" + TOKEN[:7] + "y", TOKEN) == "x" + TOKEN[:7] + "y"     # 7 字以下不当 token


def test_token_never_printed_or_saved_nonjson_page(tmp_path, monkeypatch):
    """端到端: ths_daily 那次走真 _raw_post, 网关回 502 HTML 错误页并在第 100 字回显请求体里的 token (老代码在这个偏移
    漏 20/34 字) —— 屏幕输出与 --json 文件里都没有 ≥8 字的 token 片段。"""
    import requests

    def fake_post(url, json=None, timeout=None, headers=None):
        return _EchoResp("<html>" + "x" * 94 + json["token"] + "</html>")
    monkeypatch.setattr(requests, "post", fake_post)
    out_json = str(tmp_path / "rep.json")
    rc, text = _run(tmp_path, FakeMirror(raw_api="ths_daily"), extra=["--json", out_json])
    with open(out_json, encoding="utf-8") as f:
        saved = f.read()
    assert not _leaks(text) and not _leaks(saved), text
    assert _check_rows(text)["ths_daily 按 trade_date"] == "FAIL" and "HTTP 502" in text and rc == 1


def test_expect_mode_makes_no_calls(tmp_path):
    m = FakeMirror()
    rc, text = _run(tmp_path, m, extra=["--expect"])
    assert rc == 0 and not m.calls and "代码期望字段" in text
    assert "数据总览 10s" in text and "_XCAL_CHILD (子进程脚本)" in text


def test_member_l1_filter_ignored_is_warn(tmp_path):
    rc, text = _run(tmp_path, FakeMirror(single_quirk=False, member_ignores_l1=True))
    rows = _check_rows(text)
    assert rc == 0 and rows["index_member_all"] == "WARN" and "不认 l1_code 过滤" in text, text
    assert "满页 2000" in text


def test_without_lake_no_lake_calls(tmp_path, monkeypatch):
    """找不到 stock-core/research/lake.py = 研究湖没有消费方: 财务与申万都不探 (21 次), 研究湖档「未纳入」, 生产照常判。"""
    monkeypatch.setattr(P, "find_core_dir", lambda explicit=None: None)
    m = FakeMirror(single_quirk=False)
    rc, text = _run(tmp_path, m, lake_db=False)
    assert rc == 0 and len(m.calls) == 21, text
    assert not [c for c in m.calls if c[0] in ("fina_indicator_vip", "income_vip", "index_classify",
                                                  "index_member_all", "sw_daily")]
    assert "研究湖与数据总览的期望未纳入" in text and "结论: 生产档可用 · 研究湖档未纳入" in text


def test_bad_date_arguments_are_rc2(tmp_path):
    for extra in (["--date", "2026-13-99x"], ["--date", "2026-10-07"], ["--date", "2026-10-14"],
                  ["--holiday", "2026-10-08"], ["--holiday", "2026-10-10"]):
        m = FakeMirror()
        rc, text = _run(tmp_path, m, extra=extra)
        assert rc == 2 and not m.calls and extra[0] in text, (extra, text)


# ---------------------------------------------------------------- ④ 纯函数
def test_latency_and_field_checks():
    assert P.latency_check({"secs": 130.0}, 120)[0] == "FAIL"
    assert P.latency_check({"secs": 70.0}, 120)[0] == "WARN"
    assert P.latency_check({"secs": 3.0}, 120)[0] == "PASS"
    assert P.latency_check({"secs": 7.5}, 10, "数据总览") == ("WARN", "延迟 7.5s > 数据总览期限 10s 的 50%")
    assert P.field_check({"fields": ["a", "b"]}, ["a", "c"])[0] == "FAIL"
    assert P.field_check({"fields": ["a", "b", "x"]}, ["a", "b"])[0] == "PASS"


def test_prober_retries_transport_once_and_counts_calls():
    seq = [OSError("reset"), {"http": 200, "secs": 0.1, "bytes": 10,
                              "json": {"code": 0, "data": {"fields": ["a"], "items": [[1]]}}}]

    def post(base, payload, deadline):
        x = seq.pop(0)
        if isinstance(x, Exception):
            raise x
        return x
    pr = P.Prober(TOKEN, "https://x.invalid/", post=post, gap=0, sleep=lambda s: None)
    rec = pr.call("X1", "t", "daily", {"trade_date": "20261012"})
    assert rec["kind"] == "OK" and rec["tries"] == 2 and pr.n == 2 and rec["first_fail"].startswith("TRANSPORT")
    assert P.retry_note(rec)[0] == "WARN"


def test_import_has_no_side_effects():
    before = sys.stdout
    mod = _load()
    assert sys.stdout is before and hasattr(mod, "run_compat") and hasattr(mod, "p0_main")
