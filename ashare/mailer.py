#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
刷新完把「左侧抄底监视器」摘要发到邮箱 (Gmail SMTP)。

设计要点 (与涨停复盘 report/mailer.py 一脉相承):
- 正文放纯文本摘要 (深跌抄底 / 综合评分榜 / 行业景气榜), 任何客户端都能读;
  完整看盘请点正文里的线上地址 (GitHub Pages), 或打开附件 CSV。
- 数据不查库, 直接读已生成的 dashboard/dashboard_data.js 产物, 与发布解耦。
- 应用专用密码只从环境变量 GMAIL_APP_PASSWORD 读, 不落代码/配置。
- 幂等: 同一 run_date 成功发过就不再发 (data/.email_sent_<date> 标记),
  force=True 可强制补发。任何失败只记日志、返回 False, 绝不抛出影响主流程。
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate

from ashare.config import CONFIG, DATA_DIR, DASHBOARD_DATA_JS

logger = logging.getLogger("ashare.mailer")


# --------------------------------------------------------------------------- #
#  读取仪表盘数据产物
# --------------------------------------------------------------------------- #
def load_payload_from_js(path: str = DASHBOARD_DATA_JS) -> dict:
    """解析 window.__ASHARE__ = {...}; 取出 JSON 对象。"""
    with open(path, encoding="utf-8") as f:
        txt = f.read()
    i, j = txt.find("{"), txt.rfind("}")
    if i < 0 or j < 0:
        raise ValueError(f"{path} 不是预期的 dashboard_data.js 结构")
    return json.loads(txt[i:j + 1])


def _fmt_pct(v, digits=1, sign=False):
    if v is None:
        return "—"
    try:
        return f"{v:+.{digits}f}%" if sign else f"{v:.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def _industry_score(row: dict):
    """行业榜的分数字段名各版本可能不同, 逐个兜底。"""
    for k in ("score", "final_score", "composite", "prosperity",
              "prosperity_score", "norm", "total"):
        if isinstance(row.get(k), (int, float)):
            return row[k]
    return None


# --------------------------------------------------------------------------- #
#  正文摘要
# --------------------------------------------------------------------------- #
def build_summary_text(payload: dict, cfg: dict = CONFIG) -> str:
    ecfg = cfg.get("email") or {}
    meta = payload.get("meta") or {}
    cands = payload.get("candidates") or []
    inds = payload.get("industries") or []
    rd = meta.get("run_date") or "?"
    live = ecfg.get("live_url", "")
    top_n = int(ecfg.get("summary_top_n", 12))

    L = [f"【A股左侧抄底监视器 · {rd}】", ""]
    scanned = meta.get("n_scanned")
    hit = meta.get("n_hit")
    # 2026-09-08 起 n_scanned 换了口径 (按价格库的点时股票池裁候选池, 5180 -> ~4930), 与
    # 09-08 之前的历史邮件有断层。GM 定的文案把断层写进正文, 免得收信人拿两天的数直接比。
    # scan_basis='raw_spot' 那天 (裁池被关/降级放行) 分母又是东财原池, 那句注解会变成假话,
    # 所以按口径分两句 —— 邮件是对外的数字, 不许"平时说得对、回滚那天说错"。
    basis = str(meta.get("scan_basis") or "raw_spot")
    if scanned and basis.startswith("store_universe"):
        L.append(f"全市场扫描 {scanned} 只 (库内在市 A 股; 09-08 前口径含退市老代码约 5180)"
                 f" → 命中 {hit} 只候选")
    elif scanned:
        L.append(f"全市场扫描 {scanned} 只 (东财快照原池, 含退市老代码) → 命中 {hit} 只候选")
    else:
        L.append(f"命中 {len(cands)} 只候选")
    sel = meta.get("selected_industries") or []
    if sel:
        L.append("入选景气行业:" + "、".join(sel))
    L.append("")

    # —— 🚀 蓄势待发: 用户每天首要关注, 必须排在最前 ——
    brk = [c for c in cands if str(c.get("tag", "")).startswith("🚀")]
    brk.sort(key=lambda c: -(c.get("breakout_score") or 0.0))
    if brk:
        L.append(f"🚀 蓄势待发 ({len(brk)} 只) —— 强左侧 + 横盘吸筹 + 蓄势将尽")
        for c in brk:
            L.append(
                f"  · {c.get('name','?')}({c.get('code','')})"
                f"  现价 {c.get('price','—')}"
                f"  综合{c.get('final_score','—')}"
                f"  距支撑 {_fmt_pct(c.get('dist_support_pct'))}"
                f"  行业:{c.get('industry') or '—'}"
            )
            if c.get("breakout_note"):
                L.append(f"      蓄势信号：{c['breakout_note']}")
            if c.get("guid_disp"):
                L.append(f"      盈利指引：{c['guid_disp']}")
        L.append("")
    else:
        L.append("🚀 蓄势待发：今日无 —— 该标签条件严格(需同时满足强左侧、横盘吸筹、"
                 "量价背离/波动压缩/位置就绪), 无符合的属正常。")
        L.append("")

    # —— 深跌抄底桶 (dip 标记 / dip_score 高的) ——
    dips = [c for c in cands if c.get("dip") or c.get("dip_confirm")]
    dips.sort(key=lambda c: -(c.get("dip_score") or 0.0))
    if dips:
        L.append(f"■ 深跌抄底关注 ({len(dips)} 只，取前 {min(8, len(dips))}):")
        for c in dips[:8]:
            L.append(
                f"  · {c.get('name','?')}({c.get('code','')})"
                f"  现价 {c.get('price','—')}"
                f"  距支撑 {_fmt_pct(c.get('dist_support_pct'))}"
                f"  {c.get('support_disp') or c.get('support_label') or ''}"
                f"  | {c.get('conclusion','')}"
            )
        L.append("")

    # —— 综合评分榜 ——
    ranked = sorted(cands, key=lambda c: -(c.get("final_score") or 0.0))
    if ranked:
        L.append(f"■ 综合评分榜 (前 {min(top_n, len(ranked))}):")
        for i, c in enumerate(ranked[:top_n], 1):
            ind = c.get("industry") or "—"
            L.append(
                f"  {i:>2}. {c.get('name','?')}({c.get('code','')})"
                f"  {c.get('tag','')}"
                f"  综合{c.get('final_score','—')}"
                f"  技术{c.get('tech_norm','—')}"
                f"  行业:{ind}"
                f"  距支撑{_fmt_pct(c.get('dist_support_pct'))}"
                f"  52周位{_fmt_pct(c.get('pos_52w_pct'),0)}"
            )
        L.append("")

    # —— 行业景气榜 ——
    scored = [(r, _industry_score(r)) for r in inds]
    scored = [(r, s) for r, s in scored if s is not None]
    scored.sort(key=lambda t: -t[1])
    if scored:
        L.append(f"■ 行业景气榜 (前 {min(8, len(scored))}):")
        for r, s in scored[:8]:
            nm = r.get("name") or r.get("industry") or "?"
            L.append(f"  · {nm}  景气 {s:.1f}")
        L.append("")

    if live:
        L.append(f"完整看盘(K线/支撑/基本面/公告):{live}")
    L += [
        "",
        "(本邮件由左侧抄底监视器自动生成，仅为技术形态与数据整理，不构成投资建议。"
        "『左侧买入』是在下跌中、支撑确认前进场，风险自负。)",
    ]
    return "\n".join(L)


# --------------------------------------------------------------------------- #
#  发送
# --------------------------------------------------------------------------- #
def _marker_path(run_date: str) -> str:
    return os.path.join(DATA_DIR, f".email_sent_{run_date}")


def send_summary_email(payload: dict | None = None,
                       cfg: dict = CONFIG,
                       force: bool = False) -> bool:
    ecfg = cfg.get("email") or {}
    if not ecfg.get("enabled"):
        logger.info("邮件未启用 (config.email.enabled=False), 跳过")
        return False

    if payload is None:
        try:
            payload = load_payload_from_js()
        except Exception as e:  # noqa: BLE001
            logger.error("读取仪表盘数据失败, 不发信: %s: %s", type(e).__name__, e)
            return False

    meta = payload.get("meta") or {}
    run_date = meta.get("run_date")
    if not run_date:
        logger.warning("payload 无 run_date, 不发信")
        return False

    if not force and os.path.exists(_marker_path(run_date)):
        logger.info("[%s] 当日摘要邮件已发过, 跳过 (force=True 可补发)", run_date)
        return True

    sender = ecfg.get("sender", "")
    pw = os.environ.get(ecfg.get("app_password_env", "GMAIL_APP_PASSWORD"), "")
    recipients = ecfg.get("recipients") or [sender]
    if not sender or not pw:
        logger.warning("[%s] 邮件配置不完整 (缺 sender 或 %s 环境变量), 跳过。"
                       "应用专用密码需在 Gmail 开两步验证后生成, 放进 launchd 的"
                       " EnvironmentVariables (参考涨停系统)", run_date,
                       ecfg.get("app_password_env", "GMAIL_APP_PASSWORD"))
        return False

    msg = EmailMessage()
    n_hit = meta.get("n_hit") or len(payload.get("candidates") or [])
    n_brk = sum(1 for c in (payload.get("candidates") or [])
                if str(c.get("tag", "")).startswith("🚀"))
    # 🚀 是用户每天首要关注的, 直接放进标题, 不用点开就知道今天有没有
    msg["Subject"] = (f"[左侧抄底] {run_date} · 🚀蓄势待发{n_brk}只 · 共{n_hit}只候选"
                      if n_brk else f"[左侧抄底] {run_date} · {n_hit}只候选")
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(build_summary_text(payload, cfg))

    # 附件: 当日候选 CSV
    if ecfg.get("attach_csv", True):
        csv_path = os.path.join(DATA_DIR, f"candidates_{run_date}.csv")
        if os.path.exists(csv_path):
            with open(csv_path, "rb") as f:
                msg.add_attachment(f.read(), maintype="text", subtype="csv",
                                   filename=os.path.basename(csv_path))
        else:
            logger.info("[%s] 未找到 %s, 只发正文", run_date, csv_path)

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(ecfg.get("smtp_host", "smtp.gmail.com"),
                              int(ecfg.get("smtp_port", 465)),
                              context=ctx, timeout=30) as srv:
            srv.login(sender, pw)
            srv.send_message(msg)
        logger.info("[%s] 摘要邮件已发送至 %s", run_date, recipients)
        try:
            with open(_marker_path(run_date), "w") as f:
                f.write("ok\n")
        except OSError:
            pass
        return True
    except smtplib.SMTPAuthenticationError as e:
        logger.error("[%s] Gmail 认证失败: 请确认用的是『应用专用密码』且账号已开两步验证。%s",
                     run_date, e)
    except Exception as e:  # noqa: BLE001
        logger.error("[%s] 邮件发送失败 (本机网络对 smtp.gmail.com 可能不通, "
                     "下轮会自动重试): %s: %s", run_date, type(e).__name__, e)
    return False


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description="发送左侧抄底摘要邮件")
    ap.add_argument("--force", action="store_true", help="忽略当日已发标记, 强制补发")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印正文摘要, 不发信 (不需要密码)")
    args = ap.parse_args()
    if args.dry_run:
        print(build_summary_text(load_payload_from_js()))
    else:
        ok = send_summary_email(force=args.force)
        raise SystemExit(0 if ok else 1)
