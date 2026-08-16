# -*- coding: utf-8 -*-
"""QQ 邮箱 SMTP 通知：新命中即时提醒 + 每日摘要。"""
import os
import smtplib
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

from . import utils

SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465

STATE_FILE = utils.DATA_DIR + "/email_state.json"


def _creds() -> tuple[str, str, str]:
    sender = os.environ.get("QQ_SMTP_SENDER", "").strip()
    code = os.environ.get("QQ_SMTP_CODE", "").strip()
    receiver = os.environ.get("EMAIL_RECEIVER", "").strip() or sender
    return sender, code, receiver


def available() -> bool:
    s, c, _ = _creds()
    return bool(s and c)


def _send(subject: str, html_body: str) -> tuple[bool, str]:
    sender, code, receiver = _creds()
    if not (sender and code and receiver):
        return False, "SMTP 凭证未配置"
    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr(("量化实时监控", sender))
    msg["To"] = receiver
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.login(sender, code)
            s.sendmail(sender, [receiver], msg.as_string())
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def _budget_ok(max_per_day: int) -> bool:
    st = utils.load_json(STATE_FILE, default={})
    if st.get("date") != utils.bj_date_str():
        return True
    return int(st.get("count", 0)) < max_per_day


def _mark_sent():
    st = utils.load_json(STATE_FILE, default={})
    if st.get("date") != utils.bj_date_str():
        st = {"date": utils.bj_date_str(), "count": 0}
    st["count"] = int(st.get("count", 0)) + 1
    utils.save_json(STATE_FILE, st)


def fmt_pct(x) -> str:
    if x is None:
        return "—"
    return f"{x*100:.1f}%"


def send_hits_alert(hits: list[dict], o_cfg: dict, site_url: str = "") -> tuple[bool, str]:
    """发送新命中提醒；受每日条数预算限制。o_cfg 为 options.json 配置。"""
    if not hits or not available():
        return False, "无新命中或未配置 SMTP"
    alert_cfg = o_cfg.get("alert", {})
    cond = o_cfg.get("conditions", {})
    max_day = int(alert_cfg.get("max_emails_per_day", 4))
    if not _budget_ok(max_day):
        return False, "今日邮件预算已用尽"
    hits = hits[: int(alert_cfg.get("max_hits_per_email", 12))]
    site = site_url
    rows = []
    for h in hits:
        leg = f" 价差[{h.get('strike2','')}]" if h.get("strike2") else ""
        rows.append(
            "<tr>"
            f"<td>{h['ticker']}</td><td>{h['strategy_name']}</td><td>{h['expiry']}</td>"
            f"<td>{h['strike']}{leg}</td><td>{h['bid']:.2f}</td>"
            f"<td><b>{fmt_pct(h['win_rate'])}</b></td><td><b>{h['pl_ratio']:.2f}</b></td>"
            f"<td>{fmt_pct(h['ann_return'])}</td><td>{fmt_pct(h.get('iv',0))}</td>"
            f"<td>{h.get('delta',0):.2f}</td><td>{h['dte']}</td>"
            "</tr>"
        )
    body = f"""
    <h3>🎯 期权形态扫描命中提醒（{utils.bj_now_str()}）</h3>
    <p>以下合约满足你在 <code>config/options.json</code> 中配置的胜率 / 盈亏比 / 年化期望抓捕条件
    （各形态阈值可单独设定；基于 Black-Scholes + 蒙特卡洛估算，非投资建议）：</p>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-size:13px">
      <tr style="background:#222;color:#fff">
        <th>代码</th><th>形态</th><th>到期</th><th>行权价</th><th>权利金(买价)</th>
        <th>预估胜率</th><th>盈亏比</th><th>年化期望</th><th>IV</th><th>Δ</th><th>剩余天</th>
      </tr>
      {''.join(rows)}
    </table>
    <p>完整面板：<a href="{site}">{site}</a></p>
    <p style="color:#888;font-size:12px">本邮件由 quant-live 自动发送。参数可在 config/options.json 调整。</p>
    """
    ok, err = _send(f"【量化监控】期权形态新命中 {len(hits)} 条", body)
    if ok:
        _mark_sent()
    return ok, err


def send_review(review: dict, app_cfg: dict) -> tuple[bool, str]:
    """每日复盘报告邮件（每天只发一次）。"""
    if not available():
        return False, "未配置 SMTP"
    st = utils.load_json(STATE_FILE, default={})
    today = utils.bj_date_str()
    if st.get("review_date") == today:
        return False, f"今日复盘邮件已发送（{today}）"
    site = app_cfg.get("site_url", "")
    news = review.get("news", {})
    opts = review.get("options", {})
    tune = review.get("tune", {})
    stats = opts.get("stats", {})
    wc_rows = "".join(
        f"<li>{it.get('title','')[:80]} → {','.join(it.get('tickers', []))}，"
        f"波动为平均日波动的 <b>{it.get('atr_ratio')}×</b></li>"
        for it in news.get("world_class", [])[:8]
    ) or "<li>今日无世界级热点</li>"
    acc = news.get("ai_accuracy", {})
    acc_txt = ""
    if acc.get("high_impact", {}).get("n"):
        acc_txt = (f"AI 高影响(≥4分)新闻实际波动确认率 <b>{fmt_pct(acc['high_impact'].get('confirmed_rate'))}</b>"
                   f"（{acc['high_impact']['n']} 条），其余 {fmt_pct(acc.get('others', {}).get('confirmed_rate') or 0)}")
    tune_rows = "".join(f"<li>{a}</li>" for a in tune.get("actions", [])) or "<li>无调整（样本不足或表现达标）</li>"
    body = f"""
    <h3>🔁 每日复盘报告（{utils.bj_now_str()}）</h3>
    <h4>🌍 新闻波动验证</h4>
    <p>已核验 <b>{news.get('checked', 0)}</b> 条热点：{news.get('confirmed', 0)} 条实际波动超过标的平均日波动
    （确认率 {fmt_pct(news.get('confirmed_rate'))}）。{acc_txt}</p>
    <h4>世界级热点（实际波动 ≥ 1.5× 平均日波动且热度≥4）</h4>
    <ol>{wc_rows}</ol>
    <h4>🎯 期权命中实际战绩</h4>
    <p>累计结算 <b>{stats.get('settled_total', 0)}</b> 笔：实际胜率 <b>{fmt_pct(stats.get('actual_win_rate'))}</b>，
    平均单笔收益 <b>{fmt_pct(stats.get('avg_actual_return'))}</b>；
    共振高分(≥60)命中胜率 <b>{fmt_pct(stats.get('resonance_win_rate'))}</b></p>
    <h4>⚙️ 自动调参</h4><ul>{tune_rows}</ul>
    <p>完整面板：<a href="{site}">{site}</a></p>
    <p style="color:#888;font-size:12px">复盘由 quant-live 每日 23:00 自动运行。数据基于历史统计，不构成投资建议。</p>
    """
    ok, err = _send(f"【量化监控】每日复盘报告 {utils.bj_date_str()}", body)
    if ok:
        _mark_sent()
        st = utils.load_json(STATE_FILE, default={})
        st["review_date"] = utils.bj_date_str()
        utils.save_json(STATE_FILE, st)
    return ok, err


def send_digest(app_cfg: dict) -> tuple[bool, str]:
    """每日摘要：新闻 Top5 + 因子 Top3 + 当日命中（每天只发一次）。"""
    if not available():
        return False, "未配置 SMTP"
    st = utils.load_json(STATE_FILE, default={})
    today = utils.bj_date_str()
    if st.get("digest_date") == today:
        return False, f"今日摘要已发送（{today}）"
    news = utils.load_json(utils.DATA_DIR + "/news.json", default={})
    factors = utils.load_json(utils.DATA_DIR + "/factors.json", default={})
    hits = utils.load_json(utils.DATA_DIR + "/options_hits.json", default={})
    site = app_cfg.get("site_url", "")

    news_rows = "".join(
        f"<li>{it.get('title_cn') or it.get('title')} "
        f"（{it.get('sentiment','中性')}，影响 {it.get('impact',0)}/5）</li>"
        for it in news.get("items", [])[:5]
    )
    fac_rows = "".join(
        f"<li><b>{f.get('name')}</b>：IC {f.get('mean_ic',0):.3f}，ICIR {f.get('icir',0):.2f}，"
        f"胜率 {fmt_pct(f.get('hit_rate',0))}</li>"
        for f in factors.get("top_factors", [])[:3]
    )
    today_hits = [h for h in hits.get("history", []) if str(h.get("first_seen", "")).startswith(today)]
    hit_rows = "".join(
        f"<li>{h.get('ticker')} {h.get('strategy_name')} {h.get('expiry')} 行权 {h.get('strike')}："
        f"胜率 {fmt_pct(h.get('win_rate',0))}，盈亏比 {h.get('pl_ratio',0):.2f}</li>"
        for h in today_hits[:10]
    ) or "<li>今日暂无新命中</li>"
    body = f"""
    <h3>📊 每日量化摘要（{utils.bj_now_str()}）</h3>
    <h4>🔥 今日热点 Top5</h4><ol>{news_rows}</ol>
    <h4>🧬 当前最强因子 Top3</h4><ul>{fac_rows}</ul>
    <h4>🎯 今日期权形态命中</h4><ul>{hit_rows}</ul>
    <p>完整面板：<a href="{site}">{site}</a></p>
    """
    ok, err = _send(f"【量化监控】每日摘要 {today}", body)
    if ok:
        _mark_sent()
        st = utils.load_json(STATE_FILE, default={})
        st["digest_date"] = today
        utils.save_json(STATE_FILE, st)
    return ok, err
