#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
优质榜 快照缺估值列 / 行业映射 的 Tushare 兜底 离线自测 (不联网; 2026-09-23 卡 QL-SPOT)。

事故: 09-21 10:01 起东财 push2 对海外 IP 502/连接失败 (服务器与老板 PC 同样), 09-22/23 快照退到新浪 —— 新浪
stock_zh_a_spot 只有 代码/名称/最新价/涨跌额/涨跌幅/…/成交额 8 列, **没有市盈率/市净率/总市值** (老版 akshare 才有,
datasource 的新浪列映射还留着期待) → spot_map 里 pe_ttm/total_mv 全空 → 优质榜 pe/cap/up 三门必挂; 行业映射同时挂
(dom) → 最多过 q4/y4/roe 三条 < 4 → 入池 0 → 按 QL-EMPTY 规则拒发、看板沿用 09-21 的榜 (总览 STALE)。报告期 20/20 到齐,
不是财报或榜逻辑的问题, 是数据源缺字段。

修法 (datasource 层, 东财优先不变):
  · fetch_spot_snapshot 出口统一过 _spot_with_fallbacks: 缺估值列 → fill_spot_valuation 用 Tushare daily_basic 补
    pe_ttm/pb/total_mv/float_mv (+换手率/量比), **万元→元** (TS_MV_UNIT); 缺行业列 → fill_spot_industry 用批量行业映射补;
    东财直连快照两样都齐 → 原对象原样返回, 一次 Tushare 调用都不发。
  · daily_basic 的交易日 = 价格库个股末日 (→ trade_cal → 工作日近似); 当日行数 < 4000 (还没出) → 退回前一交易日 + warning;
    (09-23 回修) 短表本进程 30 分钟后允许再打 (最多 2 次, 端点上限 1+2), 优质榜阶段拿当日; 前一交易日缓存按 72h 读。
  · (09-23 卡 IND-PE, 老板拍板 ①) 行业口径唯一 = 东财全市场分类: quality._industry_map_ex 只走 ds.fetch_industry_map() —— 东财批量
    f100 (INFO) / 东财当日缓存 (INFO) / Tushare stock_basic (WARNING) 三态, **成分口径 (fetch_industry_list × fetch_industry_cons)
    永不再用, 成分能给 5000 只也不选**; 覆盖 < 3000 标 (部分) 并 WARNING 降级; 研发豁免 is_fin_industry 去掉 f100 的 Ⅱ/Ⅲ 后缀再精确比。
  · fetch_industry_map: 东财 push2 各主机都不可达 → Tushare stock_basic, 名字经 TS_INDUSTRY_ALIAS 换成东财口径, 记
    映射到东财口径 / 原样沿用 Tushare 名 / 空 三类计数。
  · Tushare 也挂 → 快照原样、不造数, 优质榜照 QL-EMPTY 拒发沿用旧榜。
  · quality meta 增 spot_source / valuation_source / valuation_trade_date / industry_source / industry_map_coverage;
    (卡 IND-PE) 再增 pe_basis = "ttm" / industry_basis = "em_f100" (PE 口径的用例在 test_ind_pe_basis.py)。

对照数 (服务器 09-23 真数据, 见 CHRONICLE): 688578 艾力斯 daily_basic total_mv 5,058,900 万元 → 506 亿 (09-21 东财 459 亿,
差两天涨幅 +9.3%); 300750 宁德时代 1.39e8 万元 → 13,928 亿 (09-21 13,748)。用例里用 4,590,000 万元 → mcap_b 459 钉住换算。

变异 (隔离副本树逐个跑, 应红的用例见交付): 单位不换算 (TS_MV_UNIT=1) / 兜底不触发 / 别名表删空 / Tushare 挂了造数 /
东财可用时也走 Tushare / 缓存键不含日期 / 不退前一交易日 / fetch_industry_map 不退 Tushare。
运行:  python -m pytest tests/test_quality_spot_fallback.py -q
"""
from __future__ import annotations
import datetime as dt
import json
import logging
import os
import sys
import time

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ashare import datasource as ds        # noqa: E402
from ashare import quality as q            # noqa: E402
from ashare import tushare_client as tc    # noqa: E402
from ashare.config import CONFIG           # noqa: E402

TODAY = dt.date.today().isoformat()
PERIODS = ds._report_periods(20)           # 新→旧 'YYYYMMDD'
REAL_FETCH_INDUSTRY_MAP = ds.fetch_industry_map
D0, D1, D2 = "20260923", "20260922", "20260921"

# ---------------------------------------------------------------- 东财口径词表 (用例锁「别名表的目标名必须真实存在」)
# fetch_industry_list 给的一级名 (同花顺/东财一级, 服务器 09-23 缓存 90 个, quality._industry_map 的分组名就是它):
EM_LEVEL1 = (
    'IT服务', '专用设备', '中药', '互联网电商', '保险', '元件', '光伏设备', '光学光电子', '公路铁路运输', '其他电子', '其他电源设备',
    '其他社会服务', '养殖业', '军工电子', '军工装备', '农产品加工', '农化制品', '包装印刷', '化学制品', '化学制药', '化学原料',
    '化学纤维', '医疗器械', '医疗服务', '医药商业', '半导体', '厨卫电器', '塑料制品', '多元金融', '家居用品', '小家电', '小金属',
    '工业金属', '工程机械', '建筑材料', '建筑装饰', '影视院线', '房地产', '教育', '文化传媒', '旅游及酒店', '服装家纺', '机场航运',
    '橡胶制品', '汽车整车', '汽车服务及其他', '汽车零部件', '油气开采及服务', '消费电子', '港口航运', '游戏', '煤炭开采加工', '燃气',
    '物流', '环保设备', '环境治理', '生物制品', '电力', '电子化学品', '电机', '电池', '电网设备', '白色家电', '白酒', '石油加工贸易',
    '种植业与林业', '纺织制造', '综合', '美容护理', '能源金属', '自动化设备', '计算机设备', '证券', '贵金属', '贸易', '轨交设备',
    '软件开发', '通信服务', '通信设备', '通用设备', '造纸', '金属新材料', '钢铁', '银行', '零售', '非金属材料', '风电设备',
    '食品加工制造', '饮料制造', '黑色家电')
# 东财快照 f100 (所属行业) 的二级名 (本机 09-07 批量映射缓存 128 个; 历史榜里的 '游戏Ⅱ' '饮料乳品' '银行Ⅱ' 都出自这里):
EM_F100 = (
    'IT服务Ⅱ', '一般零售', '专业工程', '专业服务', '专业连锁Ⅱ', '专用设备', '个护用品', '中药Ⅱ', '乘用车', '互联网电商', '休闲食品',
    '体育Ⅱ', '保险Ⅱ', '元件', '光伏设备', '光学光电子', '其他家电Ⅱ', '其他电子Ⅱ', '其他电源设备Ⅱ', '养殖业', '军工电子Ⅱ',
    '农业综合Ⅱ', '农产品加工', '农化制品', '冶钢原料', '出版', '动物保健Ⅱ', '包装印刷', '化妆品', '化学制品', '化学制药', '化学原料',
    '化学纤维', '医疗器械', '医疗服务', '医疗美容', '医药商业', '半导体', '厨卫电器', '商用车', '地面兵装Ⅱ', '基础建设', '塑料',
    '多元金融', '家居用品', '家电零部件Ⅱ', '小家电', '小金属', '工业金属', '工程咨询服务Ⅱ', '工程机械', '广告营销', '影视院线',
    '房地产开发', '房地产服务', '房屋建设Ⅱ', '摩托车及其他', '教育', '数字媒体', '文娱用品', '旅游及景区', '旅游零售Ⅱ', '普钢',
    '服装家纺', '林业Ⅱ', '橡胶', '水泥', '汽车服务', '汽车零部件', '油服工程', '油气开采Ⅱ', '消费电子', '渔业', '游戏Ⅱ',
    '炼化及贸易', '焦炭Ⅱ', '煤炭开采', '照明设备Ⅱ', '燃气Ⅱ', '物流', '特钢Ⅱ', '环保设备Ⅱ', '环境治理', '玻璃玻纤', '生物制品',
    '电力', '电子化学品Ⅱ', '电机Ⅱ', '电池', '电网设备', '电视广播Ⅱ', '白色家电', '白酒Ⅱ', '种植业', '纺织制造', '综合Ⅱ',
    '能源金属', '自动化设备', '航天装备Ⅱ', '航海装备Ⅱ', '航空机场', '航空装备Ⅱ', '航运港口', '装修建材', '装修装饰Ⅱ',
    '计算机设备', '证券Ⅱ', '调味发酵品Ⅱ', '贵金属', '贸易Ⅱ', '轨交设备Ⅱ', '软件开发', '通信服务', '通信设备', '通用设备', '造纸',
    '酒店餐饮', '金属新材料', '铁路公路', '银行Ⅱ', '非白酒', '非金属材料Ⅱ', '风电设备', '食品加工', '饮料乳品', '饰品', '饲料',
    '黑色家电')
EM_VOCAB = frozenset(EM_LEVEL1) | frozenset(EM_F100)
# Tushare stock_basic.industry 的全部取值 (服务器 09-23 实测 list_status=L 5570 只, 111 个含空):
TS_NAMES = (
    '电气设备', '元器件', '专用机械', '软件服务', '汽车配件', '化工原料', '半导体', '医疗保健', '化学制药', '机械基件', '通信设备',
    '建筑工程', '环境保护', '电器仪表', '家用电器', '食品', '生物制药', 'IT设备', '互联网', '塑料', '家居用品', '中成药', '小金属',
    '服饰', '农药化肥', '广告包装', '航空', '文教休闲', '证券', '运输设备', '仓储物流', '供气供热', '纺织', '区域地产', '银行',
    '农业综合', '工程机械', '医药商业', '染料涂料', '矿物制品', '影视音像', '百货', '造纸', '化纤', '新型电力', '火力发电', '其他建材',
    '铝', '出版业', '普钢', '全国地产', '煤炭开采', '装修装饰', '机床制造', '多元金融', '饲料', '钢加工', '水泥', '汽车整车',
    '水力发电', '石油开采', '日用化工', '乳制品', '综合类', '玻璃', '路桥', '水运', '白酒', '种植业', '旅游景点', '铜', '橡胶', '港口',
    '商贸代理', '水务', '石油加工', '铅锌', '摩托车', '园区开发', '房产服务', '农用机械', '化工机械', '其他商业', '纺织机械', '船舶',
    '黄金', '特种钢', '汽车服务', '酒店餐饮', '软饮料', '红黄酒', '空运', '旅游服务', '啤酒', '超市连锁', '铁路', '焦炭加工', '渔业',
    '公共交通', '轻工机械', '机场', '批发业', '电信运营', '保险', '陶瓷', '商品城', '石油贸易', '林业', '公路', '', '电器连锁')
# CYCLICAL_KEYS 里在东财口径词表内根本没有含该字的名 (或 Tushare 没有这一类) 的键 —— 东财日也命不中, 不是别名表的缺口:
CYCLICAL_KEYS_UNREACHABLE = ("有色", "化工", "航空", "农牧", "船舶", "能源金属", "猪")


# ---------------------------------------------------------------- 合成数据
def _synthetic_reports(codes, growth=1.2, base=1e8, periods=PERIODS):
    """每年增长 growth 倍、季内逐季递增的累计口径报表 → q4/y4 全正, ROE 20 (与 test_quality_guard 同款)。"""
    reports = {}
    for k, code in enumerate(codes):
        plist, ni, rev, roe = [], [], [], []
        for p in sorted(periods):
            y, m = int(p[:4]), p[4:6]
            qi = {"03": 1, "06": 2, "09": 3, "12": 4}[m]
            yr = base * (1 + k * 0.01) * (growth ** (y - 2020))
            cum = sum(yr * (1 + 0.1 * j) for j in range(1, qi + 1))
            plist.append(f"{p[:4]}-{p[4:6]}-{p[6:]}")
            ni.append(cum)
            rev.append(cum * 10)
            roe.append(20.0)
        reports[code] = {"periods": plist, "ni_cum": ni, "rev_cum": rev, "roe_cum": roe}
    return reports


CODES = ["688578"] + [f"{600000 + k:06d}" for k in range(30)]      # 艾力斯排第一 (同分时按插入序稳定排序, 必入榜)


def _cov(ok=PERIODS, expected=PERIODS):
    return {"expected": list(expected), "ok": list(ok), "fallback": [], "empty": [], "failed": []}


def _sina_spot(codes):
    """新浪快照的真实形态: 服务器 09-23 实测恰好这 8 列, 没有估值列也没有行业列。"""
    n = len(codes)
    return pd.DataFrame({"code": list(codes), "name": [f"N{c}" for c in codes],
                         "price": [50.0] * n, "pct_chg": [0.5] * n, "volume": [1e6] * n,
                         "amount": [5e7] * n, "high": [51.0] * n, "low": [49.0] * n})


def _em_spot(codes, pe=15.0, mv=500e8, pb=3.0, industry="化学制药"):
    """东财直连快照: 估值列 + f100 行业列都在。"""
    df = _sina_spot(codes)
    df["turnover"] = 1.2
    df["pe_ttm"] = pe
    df["volume_ratio"] = 0.9
    df["total_mv"] = mv
    df["float_mv"] = mv * 0.8
    df["pb"] = pb
    df["industry"] = industry
    return df


def _daily_basic(trade_date, special=None, n_pad=4300, pe=15.0, pb=3.0, mv_wan=5_000_000.0):
    """假 daily_basic 原始表 (Tushare 口径: total_mv/circ_mv **万元**)。n_pad 只垫底票保证 >= TS_DAILY_BASIC_MIN_ROWS。
    special = {code: {"pe_ttm":..., "pb":..., "total_mv":...(万元)}}。"""
    rows = []
    for i in range(1, n_pad + 1):
        rows.append((f"{100000 + i:06d}.SZ", trade_date, 10.0, pe, pe, pb, mv_wan, mv_wan * 0.8, 1.0, 1.0))   # 垫底票 1xxxxx, 不撞真代码
    for code, v in (special or {}).items():
        rows.append((f"{code}.SH" if code[0] in "69" else f"{code}.SZ", trade_date, 10.0, v.get("pe", v.get("pe_ttm")),
                     v.get("pe_ttm", pe), v.get("pb", pb), v.get("total_mv", mv_wan), v.get("circ_mv", v.get("total_mv", mv_wan)),
                     v.get("turnover_rate", 2.0), v.get("volume_ratio", 1.5)))
    return pd.DataFrame(rows, columns=["ts_code", "trade_date", "close", "pe", "pe_ttm", "pb", "total_mv", "circ_mv",
                                       "turnover_rate", "volume_ratio"])


def _stock_basic(industry_of=None, n_pad=5000, pad_industry="化学制药"):
    """假 stock_basic 原始表: n_pad 只垫底票 + 指定票 {code: Tushare 行业名}。"""
    rows = [(f"{100000 + i:06d}.SZ", f"{100000 + i:06d}", f"垫{i}", pad_industry, "主板", "20100101")
            for i in range(1, n_pad + 1)]                                   # 垫底票 1xxxxx, 不撞真代码
    for code, ind in (industry_of or {}).items():
        rows.append((f"{code}.SH" if code[0] in "69" else f"{code}.SZ", code, f"N{code}", ind, "主板", "20100101"))
    return pd.DataFrame(rows, columns=["ts_code", "symbol", "name", "industry", "market", "list_date"])


class FakeTS:
    """假 Tushare 出口 (datasource._ts_query): 按 api 返回剧本; 记录每次调用; 剧本里放异常就抛。"""

    def __init__(self):
        self.calls = []
        self.daily = {}             # trade_date -> DataFrame | Exception
        self.stock_basic = None     # DataFrame | Exception

    def query(self, api, **kw):
        self.calls.append((api, dict(kw)))
        if api == "daily_basic":
            r = self.daily.get(kw.get("trade_date"), _daily_basic(kw.get("trade_date"), n_pad=0))
        elif api == "stock_basic":
            r = self.stock_basic
        else:
            raise AssertionError("用例没准备这个 api: " + api)
        if isinstance(r, Exception):
            raise r
        return r

    def n(self, api, **match):
        return sum(1 for a, kw in self.calls if a == api and all(kw.get(k) == v for k, v in match.items()))


@pytest.fixture
def ts_box(tmp_path, monkeypatch):
    """Tushare 出口打桩 + 缓存目录指到临时目录 + 进程级记忆全部清零; 东财 push2 与东财成分默认**不可达**。"""
    fake = FakeTS()
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(ds, "_CACHE_DIR", str(cache))
    monkeypatch.setitem(CONFIG["source"], "use_cache", True)
    monkeypatch.setattr(ds, "_ts_query", fake.query)
    monkeypatch.setattr(ds, "_ts_available", lambda: True)
    monkeypatch.setattr(ds, "_valuation_trade_dates", lambda n=3: [D0, D1, D2])
    monkeypatch.setattr(ds, "_industry_map_em_hosts", lambda: ({}, None))
    monkeypatch.setattr(ds, "fetch_industry_list", lambda: None)
    monkeypatch.setattr(ds, "_ind_map", None)
    monkeypatch.setattr(ds, "_ind_map_source", None)
    monkeypatch.setattr(ds, "_ind_map_info", {})
    monkeypatch.setattr(ds, "_ts_daily_basic_short", {})
    monkeypatch.setitem(ds._ts_state, "valuation_warned", False)
    fake.daily[D0] = _daily_basic(D0, {"688578": {"pe_ttm": 14.9, "pb": 5.9, "total_mv": 4_590_000.0},
                                       **{c: {"pe_ttm": 15.0, "total_mv": 5_000_000.0} for c in CODES[1:]}})
    fake.stock_basic = _stock_basic({"688578": "化学制药", **{c: "化学制药" for c in CODES[1:]},
                                     "601398": "银行", "601899": "铜", "300750": "电气设备", "000001": ""})
    fake.cache = cache
    return fake


@pytest.fixture
def sandbox(tmp_path, monkeypatch, ts_box):
    """优质榜产物全部指到临时目录; 快照走**真** _spot_with_fallbacks (假 Tushare); 网络函数打桩。"""
    import ashare.prob20 as prob20
    box = {"reports": _synthetic_reports(CODES), "cov": _cov(), "spot": _sina_spot(CODES)}
    dash, data = tmp_path / "dashboard", tmp_path / "data"
    dash.mkdir()
    data.mkdir()
    monkeypatch.setattr(q, "DASHBOARD_DIR", str(dash))
    monkeypatch.setattr(q, "QL_JS", str(dash / "quality_data.js"))
    monkeypatch.setattr(q, "QL_JSON", str(data / "quality_result.json"))
    monkeypatch.setattr(q, "QL_LAST_GOOD", str(data / "quality_last_good.js"))
    monkeypatch.setattr(q, "QL_DOCS_JS", str(tmp_path / "docs" / "quality_data.js"))
    monkeypatch.setattr(ds, "fetch_profit_reports_ex", lambda n: (box["reports"], box["cov"]))
    monkeypatch.setattr(ds, "fetch_spot_snapshot", lambda force=False: ds._spot_with_fallbacks(box["spot"]))
    monkeypatch.setattr(q, "_rd_intensity", lambda code: None)
    monkeypatch.setattr(q, "_deep_profiles", lambda picks: {})
    monkeypatch.setattr(q, "_drawer_profiles", lambda picks, reports=None: {})
    monkeypatch.setattr(q, "_merge_fundamental_fields", lambda profiles: 0)
    monkeypatch.setattr(prob20, "annotate", lambda *a, **k: None)
    box["dash"], box["data"], box["tmp"], box["ts"] = dash, data, tmp_path, ts_box
    return box


def _outputs(box):
    return (os.path.exists(q.QL_JS), os.path.exists(q.QL_JSON),
            os.path.exists(os.path.join(str(box["dash"]), "history", f"quality_{TODAY}.json")))


def _msgs(caplog, level=logging.WARNING):
    return [r.getMessage() for r in caplog.records if r.levelno >= level]


# ============================================================ (c) 别名表
def test_alias_targets_exist_in_em_vocab_and_model_keys_hit():
    """别名表的每个目标名都是流水线里真出现过的东财/一级名 (不许编名字); FIN_INDUSTRIES 四个名精确命中; 估值模型的
    金融/周期键 (子串) 在能命中的范围内都命中; 111 个 Tushare 名逐个过 alias_industry, 原样沿用的恰好是 TS_INDUSTRY_KEEP。"""
    targets = set(ds.TS_INDUSTRY_ALIAS.values())
    assert targets <= EM_VOCAB, sorted(targets - EM_VOCAB)
    assert set(q.FIN_INDUSTRIES) <= targets                      # 精确匹配用 (研发豁免)
    for k in q.FIN_PB_KEYS:
        assert any(k in t for t in targets), k
    for k in q.CYCLICAL_KEYS:
        reachable = any(k in name for name in EM_VOCAB)
        if k in CYCLICAL_KEYS_UNREACHABLE:
            assert not reachable or k == "能源金属" or k == "航空", k   # 词表里有 (能源金属/航空机场) 但 Tushare 没有可整体对应的类
            continue
        assert reachable and any(k in t for t in targets), k
    kinds = {}
    for name in TS_NAMES:
        out, kind = ds.alias_industry(name)
        kinds.setdefault(kind, set()).add(name)
        if kind == "mapped":
            assert out == ds.TS_INDUSTRY_ALIAS[name]
        elif kind == "kept":
            assert out == name
        else:
            assert out is None and name == ""
    assert kinds["kept"] == set(ds.TS_INDUSTRY_KEEP)
    assert kinds["empty"] == {""}
    assert len(kinds["mapped"]) == len(TS_NAMES) - len(ds.TS_INDUSTRY_KEEP) - 1 == 101
    assert set(ds.TS_INDUSTRY_ALIAS) == kinds["mapped"]           # 别名表里没有 Tushare 根本不用的名
    for bad in (None, float("nan"), "nan", " ", "-"):
        assert ds.alias_industry(bad) == (None, "empty")
    # 历史榜 quality_2026-09-*.json 里出现过的名, 与本表目标名同一词表 (锁住"东财口径"这个说法)
    for hist in ("光伏设备", "化学制药", "医疗服务", "小金属", "工业金属", "摩托车及其他", "消费电子", "游戏Ⅱ", "电池",
                 "电网设备", "白色家电", "白酒Ⅱ", "贵金属", "通用设备", "银行", "银行Ⅱ", "饮料乳品"):
        assert hist in EM_VOCAB, hist


# ============================================================ 快照来源判别
def test_spot_source_detection_by_columns_and_attrs():
    sina, em = _sina_spot(CODES), _em_spot(CODES)
    assert ds.spot_source_of(sina) == "新浪" and ds.spot_lacks_valuation(sina)
    assert ds.spot_source_of(em) == "东财直连" and not ds.spot_lacks_valuation(em)
    ak = em.drop(columns=["industry"])
    assert ds.spot_source_of(ak) == "东财(akshare)" and not ds.spot_lacks_valuation(ak)
    hollow = em.copy()
    hollow["pe_ttm"] = np.nan
    assert ds.spot_lacks_valuation(hollow)                          # 列在但整列空 = 缺
    assert ds.spot_source_of(None) == "无" and not ds.spot_lacks_valuation(None)
    assert not ds.spot_lacks_valuation(sina.iloc[0:0])              # 空表不算缺 (上游自己会报股票池为空)
    assert ds.spot_sources(em) == {"spot_source": "东财直连", "valuation_source": "东财", "pe_basis": "ttm"}
    assert ds.spot_sources(sina)["valuation_source"] == "缺失" and ds.spot_sources(sina)["pe_basis"] == "ttm"


# ============================================================ (a) 估值列兜底: 单位 / 列 / 只警告一次 / 缓存
def test_fill_valuation_from_tushare_converts_wan_to_yuan(ts_box, caplog):
    sina = _sina_spot(CODES)
    with caplog.at_level(logging.INFO, logger="ashare.datasource"):
        out, info = ds.fill_spot_valuation(sina)
    assert list(sina.columns) == ["code", "name", "price", "pct_chg", "volume", "amount", "high", "low"]   # 原表不动
    for c in ("pe_ttm", "pb", "total_mv", "float_mv", "turnover", "volume_ratio"):
        assert c in out.columns, c
    row = out.set_index("code").loc["688578"]
    assert row["total_mv"] == pytest.approx(4_590_000.0 * 1e4)          # 万元 → 元
    assert row["float_mv"] == pytest.approx(4_590_000.0 * 1e4)
    assert row["pe_ttm"] == 14.9 and row["pb"] == 5.9 and row["turnover"] == 2.0 and row["volume_ratio"] == 1.5
    assert round(row["total_mv"] / 1e8) == 459                          # 与 09-21 真榜 mcap_b 459 同量级 (量级错会是 0 或 4.59e6)
    assert info["valuation_source"] == "tushare_daily_basic" and info["valuation_trade_date"] == D0
    assert info["fell_back_from"] is None and info["spot_source"] == "新浪"
    assert out.attrs["valuation_source"] == "tushare_daily_basic" and out.attrs["spot_source"] == "新浪"
    assert ds.spot_sources(out) == {"spot_source": "新浪", "valuation_source": "tushare_daily_basic", "pe_basis": "ttm",
                                    "valuation_trade_date": D0,
                                    "valuation_filled": ["pe_ttm", "pb", "total_mv", "float_mv", "turnover", "volume_ratio"]}
    warns = [m for m in _msgs(caplog) if "新浪快照无估值列" in m]
    assert len(warns) == 1 and "Tushare daily_basic" in warns[0]
    assert any("新浪快照无估值列 → Tushare daily_basic (20260923) 补" in m for m in _msgs(caplog, logging.INFO))
    assert ts_box.n("daily_basic") == 1
    # 第二次 (同进程再取快照): 读 daily_basic 缓存, 不再打端点, 也不再重复那条 warning
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="ashare.datasource"):
        out2, _ = ds.fill_spot_valuation(sina)
    assert ts_box.n("daily_basic") == 1
    assert not [m for m in _msgs(caplog) if "新浪快照无估值列" in m]
    pd.testing.assert_frame_equal(out, out2)


def test_fill_valuation_only_fills_empty_columns(ts_box):
    """akshare 东财那种"有估值列但没行业列"的快照不该被碰; 列在但整列空的才补, 已有数的一个都不改。"""
    ak = _em_spot(CODES, pe=99.0).drop(columns=["industry"])
    same, info = ds.fill_spot_valuation(ak)
    assert same is ak and info["valuation_source"] == "东财" and ts_box.n("daily_basic") == 0
    hollow = _sina_spot(CODES)
    hollow["pe_ttm"] = np.nan                      # 列在但空 → 补
    hollow["pb"] = 7.0                             # 已有数 → 不动
    out, _ = ds.fill_spot_valuation(hollow)
    assert out.set_index("code").loc["688578", "pe_ttm"] == 14.9
    assert (out["pb"] == 7.0).all()


def test_daily_basic_cache_key_includes_trade_date(ts_box):
    """同一交易日第二次读缓存; 换一个交易日必须再打一次 (键不含日期会把 09-22 的估值当 09-23 的用)。"""
    a = ds.fetch_daily_basic_tushare(D0)
    b = ds.fetch_daily_basic_tushare(D0)
    assert ts_box.n("daily_basic", trade_date=D0) == 1 and len(a) == len(b)
    ts_box.daily[D1] = _daily_basic(D1, {"688578": {"pe_ttm": 13.0, "total_mv": 4_000_000.0}})
    c = ds.fetch_daily_basic_tushare(D1)
    assert ts_box.n("daily_basic") == 2
    assert float(c.set_index("ts_code").loc["688578.SH", "pe_ttm"]) == 13.0
    assert ds._cache_key("ts_daily_basic", D0) != ds._cache_key("ts_daily_basic", D1)
    files = sorted(os.listdir(str(ts_box.cache)))
    assert ds._cache_key("ts_daily_basic", D0) + ".pkl" in files and ds._cache_key("ts_daily_basic", D1) + ".pkl" in files
    assert ds.fetch_daily_basic_tushare("2026-09-23") is not None and ts_box.n("daily_basic") == 2   # ISO 日期同键


# ============================================================ (d) 当日 daily_basic 还没出 → 退前一交易日 + warning
def test_daily_basic_today_empty_falls_back_to_previous_day(ts_box, caplog):
    ts_box.daily[D0] = _daily_basic(D0, n_pad=0)                    # 0 行: 首取时当日还没出 (Tushare 文档口径 15~17 点更新)
    ts_box.daily[D1] = _daily_basic(D1, {"688578": {"pe_ttm": 13.0, "total_mv": 4_000_000.0}})
    with caplog.at_level(logging.WARNING, logger="ashare.datasource"):
        val, info = ds.fetch_valuation_tushare()
    assert info["trade_date"] == D1 and info["fell_back_from"] == D0 and info["source"] == "tushare_daily_basic"
    assert val.set_index("code").loc["688578", "total_mv"] == pytest.approx(4e10)
    assert any("退回前一交易日 20260922" in m and "20260923" in m for m in _msgs(caplog))
    assert not os.path.exists(os.path.join(str(ts_box.cache), ds._cache_key("ts_daily_basic", D0) + ".pkl"))   # 空结果不落盘
    assert os.path.exists(os.path.join(str(ts_box.cache), ds._cache_key("ts_daily_basic", D1) + ".pkl"))
    # 同进程再来一次: 当日那份记住"还没出", 不再打; 前一日读缓存 → 一次端点都不打
    n0 = len(ts_box.calls)
    val2, info2 = ds.fetch_valuation_tushare()
    assert len(ts_box.calls) == n0 and info2["trade_date"] == D1
    # 「明显偏少」(只出了一部分, 100 行) 同样退回
    monkeypatch_short = _daily_basic(D0, n_pad=100)
    ts_box.daily[D0] = monkeypatch_short
    ds._ts_daily_basic_short.clear()
    val3, info3 = ds.fetch_valuation_tushare()
    assert info3["trade_date"] == D1 and info3["fell_back_from"] == D0
    # 填到快照上也把这件事写进日志与 info
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="ashare.datasource"):
        out, finfo = ds.fill_spot_valuation(_sina_spot(CODES))
    assert finfo["valuation_trade_date"] == D1 and finfo["fell_back_from"] == D0
    assert any("用的是前一交易日" in m for m in _msgs(caplog, logging.INFO))


def test_daily_basic_all_days_empty_gives_nothing(ts_box, caplog):
    for d in (D0, D1, D2):
        ts_box.daily[d] = _daily_basic(d, n_pad=0)
    val, info = ds.fetch_valuation_tushare()
    assert val is None and info["source"] is None and "没有可用交易日" in info["error"]


def test_valuation_trade_dates_prefer_store_then_calendar_then_weekdays(monkeypatch):
    monkeypatch.setattr(ds, "bars_from_store_on", lambda: True)
    monkeypatch.setattr(ds, "_store_max_date", lambda: "2026-09-23")
    monkeypatch.setattr(ds, "_store_trade_days_before", lambda mx, n: ["2026-09-22", "2026-09-21"][:n])
    assert ds._valuation_trade_dates() == [D0, D1, D2]
    monkeypatch.setattr(ds, "bars_from_store_on", lambda: False)
    monkeypatch.setattr(tc, "trade_cal", lambda s, e, **k: ["2026-09-18", "2026-09-19", "2026-09-22", "2026-09-23"])
    monkeypatch.setattr(ds.dt, "date", _FixedDate)
    assert ds._valuation_trade_dates() == [D0, D1, "20260919"]
    monkeypatch.setattr(tc, "trade_cal", lambda s, e, **k: (_ for _ in ()).throw(tc.TushareTransport("cal down", "trade_cal")))
    assert ds._valuation_trade_dates() == [D0, D1, D2]          # 09-23 周三 → 工作日近似: 23, 22, 21


class _FixedDate(dt.date):
    @classmethod
    def today(cls):
        return cls(2026, 9, 23)


# ============================================================ (b) 东财可用: 兜底不触发, 结果一字不差
def _push2_recorder(monkeypatch):
    """记录 push2 批量映射被问了几次 (用记录不用抛错: 抛错会被兜底路径的 try/except 吞掉, 断言就成了空话)。"""
    seen = []
    monkeypatch.setattr(ds, "_industry_map_em_hosts", lambda: (seen.append("push2"), ({}, None))[1])
    return seen


def test_eastmoney_snapshot_untouched_and_tushare_never_called(ts_box, monkeypatch):
    seen = _push2_recorder(monkeypatch)
    em = _em_spot(CODES)
    before = em.copy()
    out = ds._spot_with_fallbacks(em)
    assert out is em                                             # 原对象
    pd.testing.assert_frame_equal(out, before)                   # 一个数都没动
    assert ds.spot_sources(out) == {"spot_source": "东财直连", "valuation_source": "东财", "pe_basis": "ttm"}
    assert ts_box.calls == [] and seen == []                      # 假 Tushare 与 push2 记录器都是空的


def _cons_recorder(monkeypatch, n=5000, industry="银行"):
    """东财成分接口的记录器: 假装成分口径今天真能给 n 只 (生产 09-01 起从没有过) —— 卡 IND-PE 起 quality 一次都不许问它。"""
    asked = []
    codes = [f"{i:06d}" for i in range(1, n + 1)]
    monkeypatch.setattr(ds, "fetch_industry_list", lambda: (asked.append("list"), pd.DataFrame({"industry": [industry]}))[1])
    monkeypatch.setattr(ds, "fetch_industry_cons",
                        lambda ind: (asked.append("cons:" + str(ind)), pd.DataFrame({"code": codes, "name": codes}))[1])
    return asked


def test_build_with_eastmoney_spot_takes_f100_batch_and_never_asks_cons(sandbox, monkeypatch, caplog):
    """(c) 东财直连快照 + 东财批量 f100 可达 + 成分接口能给 5000 只: 榜的分组按 f100 批量 (与直接喂这份映射的 _score_rows 一字
    不差), meta 记 东财 / pe_basis ttm / industry_basis em_f100, 成分接口零调用, 零 Tushare。"""
    box = sandbox
    box["spot"] = _em_spot(CODES)
    batch = {f"{i:06d}": "化学制药" for i in range(1, 5001)}
    batch.update({c: "化学制药" for c in CODES})
    monkeypatch.setattr(ds, "_industry_map_em_hosts", lambda: (dict(batch), "push2delay.eastmoney.com"))
    asked = _cons_recorder(monkeypatch)
    spot_map = {str(r["code"]).zfill(6): r for r in box["spot"].to_dict("records")}
    expect = q._score_rows(box["reports"], spot_map, batch)
    with caplog.at_level(logging.INFO):
        res = q.build_quality()
    assert res is not None and len(res["picks"]) == q.TOP_N
    m = res["meta"]
    assert (m["spot_source"], m["valuation_source"], m["industry_source"]) == ("东财直连", "东财", "东财")
    assert m["pe_basis"] == "ttm" and m["industry_basis"] == "em_f100" and m["valuation_trade_date"] is None
    assert m["industry_map_coverage"]["source"] == "东财" and m["industry_map_coverage"]["n"] == len(batch)
    assert m["industry_map_coverage"]["basis"] == "em_f100" and "em_cons_n" not in m["industry_map_coverage"]
    got = sorted(expect, key=lambda r: (-r["n_pass"], -r["score"]))[:q.TOP_N]
    assert [(p["code"], p["pe"], p["mcap_b"], p["gates"], p["score"]) for p in res["picks"]] == \
           [(p["code"], p["pe"], p["mcap_b"], p["gates"], p["score"]) for p in got]
    assert box["ts"].calls == [] and asked == []                     # 零 Tushare / 成分接口一次没问
    msgs = [r.getMessage() for r in caplog.records]
    assert any(x.startswith("行业映射: 来源 东财批量 (push2 clist f100 全市场分类, 主机 push2delay.eastmoney.com), 覆盖 %d 只" % len(batch))
               for x in msgs)
    assert any("| PE 口径 ttm (行业口径 em_f100)" in x and x.startswith("优质榜数据来源: 快照 东财直连 | 估值 东财 | 行业 东财 覆盖")
               for x in msgs)
    hist = json.load(open(os.path.join(str(box["dash"]), "history", f"quality_{TODAY}.json"), encoding="utf-8"))
    assert hist["pe_basis"] == "ttm" and hist["industry_basis"] == "em_f100" and len(hist["picks"]) == q.TOP_N


# ============================================================ (a) 全链路: 新浪快照 + Tushare 补 → 入池 > 0, 单位对
def test_build_with_sina_spot_publishes_via_tushare(sandbox, caplog):
    box = sandbox
    with caplog.at_level(logging.INFO):
        res = q.build_quality()
    assert res is not None and len(res["picks"]) == q.TOP_N and res["meta"]["n_pool"] == len(CODES)
    assert _outputs(box) == (True, True, True)
    m = res["meta"]
    assert m["spot_source"] == "新浪" and m["valuation_source"] == "tushare_daily_basic" and m["valuation_trade_date"] == D0
    assert m["industry_source"] == "tushare_stock_basic"
    cov = m["industry_map_coverage"]
    assert cov["source"] == "tushare_stock_basic" and cov["total"] == 5000 + 31 + 4
    assert cov["mapped"] == 5000 + 31 + 2 and cov["kept"] == 1 and cov["empty"] == 1 and cov["kept_names"] == ["电气设备"]
    assert "em_cons_n" not in cov and cov["basis"] == "em_f100" and m["pe_basis"] == "ttm" and m["industry_basis"] == "em_f100"
    ai = next(p for p in res["picks"] if p["code"] == "688578")
    assert ai["mcap_b"] == 459 and ai["pe"] == 14.9 and ai["industry"] == "化学制药"
    assert ai["gates"]["pe"] and ai["gates"]["cap"] and ai["gates"]["q4"] and ai["gates"]["y4"] and ai["gates"]["roe"]
    others = [p for p in res["picks"] if p["code"] != "688578"]
    assert all(p["mcap_b"] == 500 and p["pe"] == 15.0 for p in others)     # 5,000,000 万元 → 500 亿
    # 龙头判定: 31 只都在 Tushare '化学制药' (垫底票营收报表没有, 不参与排名) → 按最近年度营收排 → 前 3 过 dom
    assert sum(1 for p in res["picks"] if p["gates"]["dom"]) == 3
    # 每种 api 各打了一次
    assert box["ts"].n("daily_basic") == 1 and box["ts"].n("stock_basic") == 1
    msgs = [r.getMessage() for r in caplog.records]
    assert any("新浪快照无估值列 → Tushare daily_basic (20260923) 补" in x for x in msgs)
    assert any(x.startswith("行业映射: 东财不可达 (所有 push2 主机), Tushare stock_basic 覆盖 5034/5035") for x in msgs)
    assert any("优质榜数据来源: 快照 新浪 | 估值 tushare_daily_basic (20260923) | 行业 tushare_stock_basic 覆盖 5034 只" in x
               and x.endswith("| PE 口径 ttm (行业口径 em_f100)") for x in msgs)
    assert any(x.startswith("行业映射: 来源 tushare_stock_basic (东财 push2 不可达), 覆盖 5034/5035 只 (映射到东财口径 5033")
               for x in _msgs(caplog))                                  # 三态之三: Tushare → WARNING
    # 看板文件里的 meta 同样带来源留痕
    assert q._parse_ql_js(q.QL_JS)["meta"]["valuation_source"] == "tushare_daily_basic"


# ============================================================ (e) Tushare 也挂 → 不造数, 按 QL-EMPTY 拒发沿用旧榜
def test_tushare_down_keeps_snapshot_bare_and_board_refuses(sandbox, caplog):
    box = sandbox
    err = tc.TushareTransport("daily_basic: 超过 120s 硬期限", "daily_basic")
    for d in (D0, D1, D2):
        box["ts"].daily[d] = err
    box["ts"].stock_basic = tc.TushareNoPermission("stock_basic: 无权限", "stock_basic")
    with caplog.at_level(logging.INFO):
        out = ds._spot_with_fallbacks(_sina_spot(CODES))
    assert list(out.columns) == list(_sina_spot(CODES).columns)          # 一列都没造
    assert ds.spot_sources(out)["valuation_source"] == "缺失"
    assert any("Tushare daily_basic 也不可用" in m and "QL-EMPTY" in m for m in _msgs(caplog))
    # 旧榜候选在: 拒发后必须摆回它, 且三个产物一个不写
    os.makedirs(os.path.dirname(q.QL_LAST_GOOD), exist_ok=True)
    with open(q.QL_LAST_GOOD, "w", encoding="utf-8") as f:
        f.write('window.__QL__ = {"meta": {"date": "2026-09-21"}, "picks": [{"code": "688578"}]};\n')
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert q.build_quality() is None
    assert _outputs(box) == (True, False, False)                          # 看板 = 沿用的旧榜, 其余不写
    assert q._parse_ql_js(q.QL_JS)["meta"]["date"] == "2026-09-21"
    errs = _msgs(caplog, logging.ERROR)
    assert len(errs) == 1 and "入池 0" in errs[0]
    assert any("优质榜看板沿用 2026-09-21 的榜" in m for m in _msgs(caplog, logging.INFO))
    assert any("优质榜数据来源: 快照 新浪 | 估值 缺失 | 行业 缺失 覆盖 0 只" in m for m in _msgs(caplog, logging.INFO))
    assert any(m.startswith("行业映射: 来源 缺失 (东财 f100 批量 / Tushare stock_basic 都不可用") for m in _msgs(caplog))


# ============================================================ 行业映射兜底
def test_industry_map_tushare_alias_counts_and_daily_cache(ts_box):
    ts_box.stock_basic = _stock_basic({"601398": "银行", "601288": "银行", "601899": "铜", "300750": "电气设备",
                                       "000001": "", "000002": None}, n_pad=0)
    m, info = ds.fetch_industry_map_tushare()
    assert m == {"601398": "银行", "601288": "银行", "601899": "工业金属", "300750": "电气设备"}
    assert info == {"source": "tushare_stock_basic", "total": 6, "mapped": 3, "kept": 1, "empty": 2,
                    "n_groups": 3, "kept_names": ["电气设备"]}
    assert ds._coverage_text(info) == "映射到东财口径 3, 原样沿用 Tushare 名 1 (电气设备), 空 2"
    m2, _ = ds.fetch_industry_map_tushare()
    assert m2 == m and ts_box.n("stock_basic") == 1                      # 同日读缓存
    assert os.path.exists(os.path.join(str(ts_box.cache), ds._cache_key("ts_stock_basic", TODAY) + ".pkl"))
    # 键含日期: 与「昨天」比 (09-24 回修: 原来写死 "2026-09-24", 到了那天 TODAY 就是它, 用例自炸 —— 干净树 1 failed / 202 passed)
    assert ds._cache_key("ts_stock_basic", TODAY) != ds._cache_key("ts_stock_basic", (dt.date.today() - dt.timedelta(days=1)).isoformat())


def test_fetch_industry_map_falls_back_to_tushare_only_when_em_hosts_dead(ts_box, monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="ashare.datasource"):
        m = ds.fetch_industry_map()
    assert m["601398"] == "银行" and m["601899"] == "工业金属" and m["300750"] == "电气设备" and "000001" not in m
    src, info = ds.industry_map_source()
    assert src == "tushare_stock_basic" and info["total"] == 5035 and info["mapped"] == 5033
    assert any(x.startswith("行业映射: 东财不可达 (所有 push2 主机), Tushare stock_basic 覆盖 5034/5035") for x in _msgs(caplog))
    assert ds.fetch_industry_map() is m and ts_box.n("stock_basic") == 1     # 进程内记忆
    assert not os.path.exists(os.path.join(str(ts_box.cache), ds._cache_key("ind_map", TODAY) + ".pkl"))   # 不冒充东财缓存
    # 东财主机通了: 用东财, 一次 Tushare 都不打, 来源记东财
    monkeypatch.setattr(ds, "_ind_map", None)
    monkeypatch.setattr(ds, "_industry_map_em_hosts", lambda: ({"601398": "银行Ⅱ", "688578": "化学制药"}, "push2delay.eastmoney.com"))
    n0 = len(ts_box.calls)
    em = ds.fetch_industry_map()
    assert em == {"601398": "银行Ⅱ", "688578": "化学制药"} and len(ts_box.calls) == n0
    assert ds.industry_map_source()[0] == "东财"
    assert os.path.exists(os.path.join(str(ts_box.cache), ds._cache_key("ind_map", TODAY) + ".pkl"))


def test_fill_spot_industry_from_batch_map(ts_box):
    sina = _sina_spot(["601398", "601899", "300750", "000001", "999999"])
    out = ds.fill_spot_industry(sina)
    assert list(out["industry"].iloc[:3]) == ["银行", "工业金属", "电气设备"]
    assert out["industry"].isna().iloc[3] and out["industry"].isna().iloc[4]     # 空行业 / 不在表里 → NaN (与东财 '-' 同义)
    assert out.attrs["industry_source"] == "tushare_stock_basic"
    em = _em_spot(["601398"], industry="银行Ⅱ")
    assert ds.fill_spot_industry(em) is em                                       # 自带 f100 → 不碰
    full = ds._spot_with_fallbacks(_sina_spot(["688578"]))
    assert full.loc[0, "industry"] == "化学制药" and full.loc[0, "pe_ttm"] == 14.9


def test_quality_industry_map_ex_never_uses_cons_even_when_cons_is_huge(ts_box, monkeypatch, caplog):
    """(c) 三态顺序 东财批量 → Tushare → (部分); 成分接口给 5000 只也不问、不选 (卡 IND-PE, 老板拍板 ①)。"""
    asked = _cons_recorder(monkeypatch, n=5000)
    batch = {f"{i:06d}": "化学制药" for i in range(1, 3100)}
    monkeypatch.setattr(ds, "_industry_map_em_hosts", lambda: (dict(batch), "push2.eastmoney.com"))
    with caplog.at_level(logging.INFO, logger="ashare.quality"):
        m, src, info = q._industry_map_ex(ds)
    assert (m, src) == (batch, "东财") and info["n"] == len(batch) and info["basis"] == "em_f100" and "em_cons_n" not in info
    assert asked == [] and ts_box.calls == []                                 # 成分零调用, Tushare 零调用
    assert _msgs(caplog) == []
    assert any(x.startswith("行业映射: 来源 东财批量 (push2 clist f100 全市场分类, 主机 push2.eastmoney.com), 覆盖 3099 只")
               for x in [r.getMessage() for r in caplog.records])
    # 东财不可达 → Tushare (WARNING), 成分仍零调用 (先删掉上一步落盘的东财按日缓存 ind_map, 否则读到的是「东财当日缓存」—— 那正是生产要的)
    monkeypatch.setattr(ds, "_ind_map", None)
    monkeypatch.setattr(ds, "_industry_map_em_hosts", lambda: ({}, None))
    os.remove(os.path.join(str(ts_box.cache), ds._cache_key("ind_map", TODAY) + ".pkl"))
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="ashare.quality"):
        m, src, info = q._industry_map_ex(ds)
    assert src == "tushare_stock_basic" and len(m) == 5034 and info["mapped"] == 5033 and "em_cons_n" not in info
    assert any(x.startswith("行业映射: 来源 tushare_stock_basic (东财 push2 不可达), 覆盖 5034/5035 只 (映射到东财口径 5033")
               for x in _msgs(caplog))
    assert asked == []
    # Tushare 那份也不够 3000 → 仍用, 标 (部分) + 降级 WARNING。先删掉按日缓存的 stock_basic 原始表, 否则下一次读的还是上面那份
    sb_pkl = os.path.join(str(ts_box.cache), ds._cache_key("ts_stock_basic", TODAY) + ".pkl")
    monkeypatch.setattr(ds, "_ind_map", None)
    os.remove(sb_pkl)
    ts_box.stock_basic = _stock_basic({"601398": "银行"}, n_pad=600)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="ashare.quality"):
        m, src, info = q._industry_map_ex(ds)
    assert src == "tushare_stock_basic(部分)" and len(m) == 601 and info["n"] == 601
    assert any("覆盖 601/601 只" in x and x.endswith("— 低于 3000 只, 龙头判定降级") for x in _msgs(caplog))
    # 东财批量也可能"部分" (主机只回了一页): 同样标 (部分) 并 WARNING, 不悄悄当全量
    monkeypatch.setattr(ds, "_ind_map", None)
    monkeypatch.setattr(ds, "_industry_map_em_hosts", lambda: ({f"{i:06d}": "化学制药" for i in range(1, 101)}, "push2.eastmoney.com"))
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="ashare.quality"):
        m, src, info = q._industry_map_ex(ds)
    assert src == "东财(部分)" and len(m) == 100 and asked == []
    assert any(x.startswith("行业映射: 来源 东财批量") and x.endswith("— 低于 3000 只, 龙头判定降级") for x in _msgs(caplog))
    assert not hasattr(q, "_industry_map")                                   # 成分口径那条函数已删, 不留复活的口子


# ============================================================ (f) 09-23 回修: 东财日常态 / 金融后缀 / daily_basic 重试与前一日缓存
def test_fin_industry_exemption_strips_em_level_suffix():
    """东财 f100 的 银行Ⅱ/证券Ⅱ/保险Ⅱ 去后缀后精确命中 FIN_INDUSTRIES; 子串相近的 (非银金融) 不收; 非字符串不炸。"""
    for name in ("银行", "银行Ⅱ", "证券Ⅱ", "保险Ⅱ", "多元金融", "银行 Ⅱ", "银行II", "证券III"):
        assert q.is_fin_industry(name), name
    for name in ("非银金融", "银行理财", "化学制药", "", None, float("nan"), "Ⅱ", "股份制银行Ⅲ"):
        assert not q.is_fin_industry(name), name
    assert q.industry_base_name("股份制银行Ⅲ") == "股份制银行" and q.industry_base_name(None) == ""
    assert q.industry_base_name("证券III") == "证券"                       # 先剥长的, 不留 '证券I'
    # 本机 09-07 批量映射缓存的 128 个 f100 名里, 豁免的恰好是金融四类 (去后缀后 = FIN_INDUSTRIES)
    assert {q.industry_base_name(n) for n in EM_F100 if q.is_fin_industry(n)} == set(q.FIN_INDUSTRIES)
    assert sum(1 for n in EM_F100 if q.is_fin_industry(n)) == 4


def _em_day(monkeypatch, cons_n=656, batch=None, host="push2delay.eastmoney.com"):
    """东财日的真实形态: 成分口径只 cons_n 只 (09-15 实测 656 只 / 12 个行业; 记录器, quality 不许问), 东财批量 f100 全市场可达。"""
    asked = _cons_recorder(monkeypatch, n=cons_n, industry="化学制药")
    if batch is None:
        batch = {f"{i:06d}": ("银行Ⅱ" if i % 50 == 0 else "化学制药") for i in range(1, 5201)}
    monkeypatch.setattr(ds, "_industry_map_em_hosts", lambda: (dict(batch), host))
    monkeypatch.setattr(ds, "fetch_industry_map", REAL_FETCH_INDUSTRY_MAP)
    return asked, batch


def test_industry_map_ex_em_day_norm_is_f100_batch_info_not_warning(ts_box, monkeypatch, caplog):
    """东财日: 东财批量 f100 → 来源 东财, INFO 一行、零 WARNING、零 Tushare 调用、成分零调用; 同日第二个进程 → 「东财当日缓存」。"""
    asked, batch = _em_day(monkeypatch)
    with caplog.at_level(logging.INFO, logger="ashare.quality"):
        m, src, info = q._industry_map_ex(ds)
    assert src == "东财" and m == batch and info["source"] == "东财" and info["n"] == 5200
    assert info["host"] == "push2delay.eastmoney.com" and "em_cons_n" not in info
    assert _msgs(caplog) == []                                              # 不再天天 WARNING
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(x.startswith("行业映射: 来源 东财批量 (push2 clist f100 全市场分类, 主机 push2delay.eastmoney.com), 覆盖 5200 只")
               for x in infos)
    assert ts_box.calls == [] and asked == []
    # 同日第二个进程读 ind_map 按日缓存: 仍是 INFO, 来源标「东财当日缓存」
    monkeypatch.setattr(ds, "_ind_map", None)
    monkeypatch.setattr(ds, "_industry_map_em_hosts", lambda: (_ for _ in ()).throw(AssertionError("有缓存不该再问 push2")))
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="ashare.quality"):
        m2, src2, info2 = q._industry_map_ex(ds)
    assert src2 == "东财" and m2 == batch and info2.get("cached") is True and _msgs(caplog) == []
    assert any(x.startswith("行业映射: 来源 东财当日缓存 (push2 clist f100 全市场分类), 覆盖 5200 只")
               for x in [r.getMessage() for r in caplog.records])
    assert asked == []


def test_build_on_em_day_bank_with_suffix_is_rd_exempt(sandbox, monkeypatch, caplog):
    """东财日全链路: 东财直连快照 (f100 带后缀) + 成分 656 只 + 东财批量可达 → 榜的行业来源 东财, 银行Ⅱ 研发豁免
    (不打 THS), 其余票照常问研发强度; 零 Tushare 调用; 优质榜数据来源行 = 东财直连 | 东财 | 东财。"""
    box = sandbox
    spot = _em_spot(CODES, pb=1.0)
    spot.loc[spot["code"] == "688578", "industry"] = "银行Ⅱ"
    box["spot"] = spot
    batch = {c: "化学制药" for c in CODES}
    batch["688578"] = "银行Ⅱ"
    batch.update({f"{i:06d}": "化学制药" for i in range(1, 5001)})
    cons_asked, _ = _em_day(monkeypatch, batch=batch)
    asked = []
    monkeypatch.setattr(q, "_rd_intensity", lambda code: (asked.append(code), None)[1])
    with caplog.at_level(logging.INFO):
        res = q.build_quality()
    assert res is not None and len(res["picks"]) == q.TOP_N
    m = res["meta"]
    assert (m["spot_source"], m["valuation_source"], m["industry_source"]) == ("东财直连", "东财", "东财")
    assert m["industry_map_coverage"]["n"] == len(batch) and cons_asked == []
    bank = next(p for p in res["picks"] if p["code"] == "688578")
    assert bank["industry"] == "银行Ⅱ" and bank["rd_exempt"] == 1 and bank["rd"] is None
    assert bank["gates"]["dom"] and bank["val_model"] == "PB-ROE"           # 分组来自 f100 批量: 独占 '银行Ⅱ' 组 → 龙头
    assert "688578" not in asked and len(asked) >= 1                         # 银行不打 THS, 其余票照常
    assert all(c != "688578" for c in asked)
    assert box["ts"].calls == []
    msgs = [r.getMessage() for r in caplog.records]
    assert any("优质榜数据来源: 快照 东财直连 | 估值 东财 | 行业 东财 覆盖 %d 只" % len(batch) in x for x in msgs)
    assert not [x for x in _msgs(caplog) if x.startswith("行业映射")]      # 东财日零「行业映射」WARNING


def test_daily_basic_short_retries_after_window_and_caps(ts_box, caplog):
    """当日短表: 30 分钟内不再打; 过了窗口再试一次, 出了就用当日并落盘; 连着短最多 1 + MAX_RETRIES 次端点调用。"""
    ts_box.daily[D0] = _daily_basic(D0, n_pad=0)
    ts_box.daily[D1] = _daily_basic(D1, {"688578": {"pe_ttm": 13.0, "total_mv": 4_000_000.0}})
    val, info = ds.fetch_valuation_tushare()                                 # 首取 (北京 16:05 形态): 当日 0 行 → 退前一日
    assert info["trade_date"] == D1 and info["fell_back_from"] == D0 and ts_box.n("daily_basic", trade_date=D0) == 1
    st = ds._ts_daily_basic_short[D0]
    assert st["n"] == 0 and st["tries"] == 1
    val, info = ds.fetch_valuation_tushare()                                 # 窗口内: 当日不再打
    assert ts_box.n("daily_basic", trade_date=D0) == 1 and info["trade_date"] == D1
    ds._ts_daily_basic_short[D0]["at"] -= ds.TS_DAILY_BASIC_RETRY_AFTER_SEC   # 时钟拨过 30 分钟 (优质榜阶段)
    ts_box.daily[D0] = _daily_basic(D0, {"688578": {"pe_ttm": 18.9, "total_mv": 5_058_900.0}})   # 当日出了
    with caplog.at_level(logging.INFO, logger="ashare.datasource"):
        val, info = ds.fetch_valuation_tushare()
    assert info["trade_date"] == D0 and info["fell_back_from"] is None and ts_box.n("daily_basic", trade_date=D0) == 2
    assert val.set_index("code").loc["688578", "total_mv"] == pytest.approx(5_058_900.0 * 1e4)   # 艾力斯 09-23 真值 506 亿
    assert any("再试一次 (第 1/2 次重试)" in x for x in _msgs(caplog, logging.INFO))
    assert D0 not in ds._ts_daily_basic_short
    d0_pkl = os.path.join(str(ts_box.cache), ds._cache_key("ts_daily_basic", D0) + ".pkl")
    assert os.path.exists(d0_pkl)
    ds.fetch_valuation_tushare()
    assert ts_box.n("daily_basic", trade_date=D0) == 2                        # 之后读缓存
    # 上限: 连着短 → 首取 + MAX_RETRIES 次重试, 之后不再打
    ds._ts_daily_basic_short.clear()
    os.remove(d0_pkl)
    ts_box.daily[D0] = _daily_basic(D0, n_pad=100)
    n0 = ts_box.n("daily_basic", trade_date=D0)
    for _ in range(6):
        val, info = ds.fetch_valuation_tushare()
        assert info["trade_date"] == D1
        if D0 in ds._ts_daily_basic_short:
            ds._ts_daily_basic_short[D0]["at"] -= ds.TS_DAILY_BASIC_RETRY_AFTER_SEC
    assert ts_box.n("daily_basic", trade_date=D0) - n0 == 1 + ds.TS_DAILY_BASIC_MAX_RETRIES
    assert ds._ts_daily_basic_short[D0]["tries"] == 1 + ds.TS_DAILY_BASIC_MAX_RETRIES
    assert not os.path.exists(d0_pkl)                                        # 短表始终不落盘


def test_previous_day_daily_basic_cache_outlives_normal_ttl(ts_box):
    """前一交易日的 daily_basic 齐了就不会再变: 落盘 20 小时后 (> 普通 TTL 12h) 退回前一日时仍读缓存不重打; 当日那份不享受长 TTL。"""
    ts_box.daily[D1] = _daily_basic(D1, {"688578": {"pe_ttm": 13.0, "total_mv": 4_000_000.0}})
    assert ds.fetch_daily_basic_tushare(D1, final=True) is not None
    d1_pkl = os.path.join(str(ts_box.cache), ds._cache_key("ts_daily_basic", D1) + ".pkl")
    old = time.time() - 20 * 3600
    os.utime(d1_pkl, (old, old))
    assert ds._cache_load(ds._cache_key("ts_daily_basic", D1)) is None        # 普通 TTL: 过期
    assert ds._cache_load(ds._cache_key("ts_daily_basic", D1), ds.TS_DAILY_BASIC_FINAL_TTL_H) is not None
    ts_box.daily[D0] = _daily_basic(D0, n_pad=0)
    n1 = ts_box.n("daily_basic", trade_date=D1)
    val, info = ds.fetch_valuation_tushare()
    assert info["trade_date"] == D1 and info["fell_back_from"] == D0 and ts_box.n("daily_basic", trade_date=D1) == n1
    assert val.set_index("code").loc["688578", "pe_ttm"] == 13.0
    # 最新交易日那份: 20 小时前的缓存照旧按普通 TTL 重取
    ts_box.daily[D0] = _daily_basic(D0, {"688578": {"pe_ttm": 18.9}})
    ds._ts_daily_basic_short.clear()
    assert ds.fetch_daily_basic_tushare(D0) is not None
    d0_pkl = os.path.join(str(ts_box.cache), ds._cache_key("ts_daily_basic", D0) + ".pkl")
    os.utime(d0_pkl, (old, old))
    n0 = ts_box.n("daily_basic", trade_date=D0)
    assert ds.fetch_daily_basic_tushare(D0) is not None and ts_box.n("daily_basic", trade_date=D0) == n0 + 1
