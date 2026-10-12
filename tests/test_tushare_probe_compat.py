#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/tushare_probe.py compat (镜像兼容性探针, 卡 TS-PROBE) 离线用例 —— 不联网, 假镜像按剧本回包。

覆盖: ① 期望字段从代码静态收集 (常量 / ",".join / 外层函数局部变量 / 守卫集合按变量归属 / 封装 / 动态 api /
FIN_TABLES + lake.db meta); 真代码上的不变式 (adj_factor 的列引用不算到 daily 头上)。② 端到端: 10-10 那种
「单日 trade_cal 不套 is_open」镜像 → 单日三例 WARN、客户端回放 [] → 结论可切换 (rc 0); 开市日单日返回空 /
财务单页上限 5000 / daily_basic 缺列 / list_status 不认 → 不可切换 (rc 1); 调用上限 → SKIP = 未跑完 (rc 1)。
③ token 不进输出与 --json 文件 (镜像 msg 里回显 token 也抹掉)。④ 导入本模块无副作用 (不读 secrets / 不换 stdout)。
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TOKEN = "TEST-TOKEN-xyz-0123456789-NOT-REAL"
BJ = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2026, 10, 13, 9, 30, tzinfo=BJ)            # 北京 10-13 周二; 上一开市日 10-12, 周六 10-10
HOLIDAYS = {f"2026-10-0{i}" for i in range(1, 9)} | {"2026-06-19", "2026-09-25"}


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
    member_ignores_l1 = index_member_all 不认 l1_code (回全市场成员)。"""

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
        self.calls = []

    def __call__(self, base, payload, deadline):
        assert payload["token"] == TOKEN
        api, p = payload["api_name"], payload["params"]
        self.calls.append((api, dict(p)))
        if api == self.echo_token_api:
            return {"http": 200, "secs": 0.05, "bytes": 99,
                    "json": {"code": 40203, "msg": f"抱歉，您没有接口访问权限 token={payload['token']}", "data": None}}
        if api in self.errors:
            code, msg = self.errors[api]
            return {"http": 200, "secs": 0.05, "bytes": 99, "json": {"code": code, "msg": msg, "data": None}}
        fields = [f for f in (payload["fields"] or "").split(",") if f]
        cols, rows = getattr(self, "_" + api)(p, fields)
        drop = self.drop.get(api, set())
        keep = [i for i, c in enumerate(cols) if c not in drop]
        cols = [cols[i] for i in keep]
        rows = [[r[i] for i in keep] for r in rows]
        return {"http": 200, "secs": 0.2, "bytes": 1000 + 50 * len(rows),
                "json": {"code": 0, "msg": "", "data": {"fields": cols, "items": rows}}}

    def _trade_cal(self, p, fields):
        days = _days(p["start_date"], p["end_date"])
        single = p["start_date"] == p["end_date"]
        rows = [{"exchange": "SSE", "cal_date": d.strftime("%Y%m%d"), "is_open": _is_open(d)} for d in days]
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

    def _open_days(self, p):
        if "trade_date" in p:
            return [p["trade_date"]]
        return [d.strftime("%Y%m%d") for d in _days(p["start_date"], p["end_date"]) if _is_open(d)]

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
        return cols, [[{"ts_code": c, "trade_date": p["trade_date"]}.get(k, 1.0) for k in cols] for c in L_CODES * 8][:5600]

    def _index_daily(self, p, fields):
        cols = fields or ["ts_code", "trade_date", "close", "open", "high", "low", "pre_close", "change",
                          "pct_chg", "vol", "amount"]
        return self._bars(p, cols, {})

    def _ths_daily(self, p, fields):
        cols = fields or ["ts_code", "trade_date", "close"]
        codes = [f"8811{i:02d}.TI" for i in range(90)]
        return cols, [[{"ts_code": c, "trade_date": p["trade_date"]}.get(k, 100.0) for k in cols] for c in codes]

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


def _mini_core(tmp_path):
    core = tmp_path / "stock-core"
    (core / "research").mkdir(parents=True)
    (core / "research" / "lake.py").write_text(MINI_LAKE, encoding="utf-8")
    return str(core)


def _mini_lake_db(tmp_path, fina_fields=None, income_ref=10609):
    path = tmp_path / "lake.db"
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


def _run(tmp_path, mirror, extra=(), lake_db=True):
    args = ["compat", "--secrets", _secrets(tmp_path), "--core", _mini_core(tmp_path), "--gap", "0"]
    args += (["--lake-db", _mini_lake_db(tmp_path)] if lake_db else ["--no-lake-db"])
    args += list(extra)
    ns = P.build_parser().parse_args(args)
    lines = []
    rc = P.run_compat(ns, post=mirror, now=NOW, out=lines.append, sleep=lambda s: None)
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


# ---------------------------------------------------------------- ① 期望字段收集
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
    # 财务: lake.db meta 那份字段表 + FIN_TABLES 主键列
    assert A["fina_indicator_vip"]["expect"][:6] == ["ts_code", "ann_date", "end_date", "eps", "roe", "update_flag"]
    assert "lake.db meta" in A["fina_indicator_vip"]["meta_source"]
    assert exp["fin_page"] == 9000


def test_fin_fields_fall_back_to_fin_tables_without_lake_db(tmp_path):
    exp = P.collect_expectations(ROOT, _mini_core(tmp_path), P.read_lake_meta(None))
    e = exp["apis"]["income_vip"]
    assert set(e["expect"]) == {"ts_code", "end_date", "ann_date", "f_ann_date", "report_type", "update_flag"}
    assert "FIN_TABLES" in e["meta_source"]


# ---------------------------------------------------------------- ② 端到端
def test_kaixin8_like_mirror_is_switchable_with_single_day_warn(tmp_path):
    m = FakeMirror(single_quirk=True)
    rc, text = _run(tmp_path, m)
    rows = _check_rows(text)
    assert rc == 0, text
    assert "结论: 可切换" in text
    assert len(m.calls) == 26 and "调用 26 次" in text
    assert rows["trade_cal 单日 周六"] == "WARN" and rows["trade_cal 单日 周日"] == "WARN"
    assert rows["trade_cal 单日 假日"] == "WARN" and rows["trade_cal 单日 开市日"] == "PASS"
    assert "客户端回放 [] ✓" in text and "客户端回放 ['2026-10-12'] ✓" in text
    sat = [c for c in m.calls if c[0] == "trade_cal" and c[1]["start_date"] == c[1]["end_date"] == "20261010"]
    assert len(sat) == 2                                       # 周六带/不带各一次
    hol = [c for c in m.calls if c[0] == "trade_cal" and c[1]["start_date"] == c[1]["end_date"] == "20261008"]
    assert len(hol) == 2                                       # 假日 = 今天及之前最近的工作日休市日
    assert all(c[1].get("trade_date") == "20261012" for c in m.calls if c[0] in ("daily_basic", "ths_daily"))
    assert TOKEN not in text


def test_strict_mirror_all_pass(tmp_path):
    rc, text = _run(tmp_path, FakeMirror(single_quirk=False))
    rows = _check_rows(text)
    assert rc == 0 and "全部 PASS" in text, text
    assert rows["trade_cal 单日 周六"] == "PASS"


def test_open_day_single_query_empty_is_not_switchable(tmp_path):
    rc, text = _run(tmp_path, FakeMirror(open_day_single_empty=True))
    assert rc == 1 and "结论: 不可切换" in text
    assert _check_rows(text)["trade_cal 单日 开市日"] == "FAIL"


def test_fin_page_cap_below_lake_page_is_fail(tmp_path):
    rc, text = _run(tmp_path, FakeMirror(page_cap=5000))
    rows = _check_rows(text)
    assert rc == 1
    assert rows["income_vip period=20260630"] == "FAIL" and "静默少拉" in text


def test_fin_period_shorter_than_page_gets_one_followup_and_passes(tmp_path):
    m = FakeMirror(fin_total=6890)
    rc, text = _run(tmp_path, m)
    assert rc == 0, text
    follow = [c for c in m.calls if c[0] in ("fina_indicator_vip", "income_vip") and c[1].get("offset")]
    assert len(follow) == 2 and len(m.calls) == 28
    assert "续页 offset=6890 0 行 = 该期就 6890 行" in text
    assert _check_rows(text)["income_vip period=20260630"] == "WARN"      # 比湖里上次 (10609 行) 少一截


def test_missing_requested_field_is_fail(tmp_path):
    rc, text = _run(tmp_path, FakeMirror(drop={"daily_basic": {"pe_ttm"}}))
    assert rc == 1
    assert _check_rows(text)["daily_basic 按 trade_date"] == "FAIL" and "pe_ttm" in text


def test_list_status_ignored_is_fail(tmp_path):
    rc, text = _run(tmp_path, FakeMirror(status_ignored=True))
    rows = _check_rows(text)
    assert rc == 1 and rows["stock_basic list_status=D"] == "FAIL"


def test_call_cap_marks_rest_skip_and_not_switchable(tmp_path):
    m = FakeMirror()
    rc, text = _run(tmp_path, m, extra=["--max-calls", "5"])
    assert len(m.calls) == 5
    assert rc == 1 and "未跑完" in text and "SKIP" in text


def test_token_never_printed_or_saved(tmp_path):
    out_json = str(tmp_path / "rep.json")
    rc, text = _run(tmp_path, FakeMirror(echo_token_api="ths_daily"), extra=["--json", out_json])
    assert TOKEN not in text and "<token>" in text
    with open(out_json, encoding="utf-8") as f:
        saved = f.read()
    assert TOKEN not in saved and json.loads(saved)["n_calls"] == 26
    assert _check_rows(text)["ths_daily 按 trade_date"] == "FAIL"     # 无权限 = FAIL
    assert rc == 1


def test_expect_mode_makes_no_calls(tmp_path):
    m = FakeMirror()
    rc, text = _run(tmp_path, m, extra=["--expect"])
    assert rc == 0 and not m.calls and "代码期望字段" in text


def test_kaixin8_lake_incompat_fails_only_lake_rows(tmp_path):
    """10-13 实测形态: _vip 两表 unknown_api + 申万分类 invalid_params -> 不可切换, 但 FAIL 全落在研究湖。"""
    m = FakeMirror(errors={"fina_indicator_vip": (404, "[unknown_api] 接口不存在或未开放"),
                           "income_vip": (404, "[unknown_api] 接口不存在或未开放"),
                           "index_classify": (400, "[invalid_params] 业务参数缺失或格式不合法")})
    rc, text = _run(tmp_path, m)
    rows = _check_rows(text)
    assert rc == 1 and "按消费方: 研究湖 3" in text, text
    assert rows["fina_indicator_vip"] == "FAIL" and rows["index_classify"] == "FAIL"
    assert rows["index_member_all l1_code=801010.SI"] == "PASS"         # 分类没拿到 -> 用兜底 L1
    assert rows["sw_daily 801010.SI"] == "PASS" and rows["daily 按 trade_date"] == "PASS"


def test_member_l1_filter_ignored_is_warn(tmp_path):
    rc, text = _run(tmp_path, FakeMirror(single_quirk=False, member_ignores_l1=True))
    rows = _check_rows(text)
    assert rc == 0 and rows["index_member_all"] == "WARN" and "不认 l1_code 过滤" in text, text
    assert "满页 2000" in text


def test_without_lake_no_lake_calls(tmp_path, monkeypatch):
    """找不到 stock-core/research/lake.py = 研究湖没有消费方: 财务与申万都不探 (21 次), 生产照常判。"""
    monkeypatch.setattr(P, "find_core_dir", lambda explicit=None: None)
    m = FakeMirror(single_quirk=False)
    rc, text = _run(tmp_path, m, lake_db=False)
    assert rc == 0 and len(m.calls) == 21, text
    assert not [c for c in m.calls if c[0] in ("fina_indicator_vip", "income_vip", "index_classify",
                                                  "index_member_all", "sw_daily")]
    assert "研究湖的期望未纳入" in text


def test_bad_date_argument_is_rc2(tmp_path):
    m = FakeMirror()
    rc, text = _run(tmp_path, m, extra=["--date", "2026-13-99x"])
    assert rc == 2 and not m.calls and "--date" in text


# ---------------------------------------------------------------- ③ 纯函数
def test_latency_and_field_checks():
    assert P.latency_check({"secs": 130.0}, 120)[0] == "FAIL"
    assert P.latency_check({"secs": 70.0}, 120)[0] == "WARN"
    assert P.latency_check({"secs": 3.0}, 120)[0] == "PASS"
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
