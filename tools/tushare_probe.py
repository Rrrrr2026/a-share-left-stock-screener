# -*- coding: utf-8 -*-
"""Tushare(兼容协议) 只读探针 — P0: 确定所购档位实际开放的端点、行数、延迟。
token/基址从 data/secrets.json 读 (永不打印)。用法: python tools/tushare_probe.py
"""
import io, json, os, sys, time
import requests
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
