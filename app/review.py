# -*- coding: utf-8 -*-
"""每日复盘改善循环（mode=review，每天北京 23:00 自动运行）：
1. 新闻影响验证：昨日热点实际波动 vs 该标的 60 日平均日波动（ATR%），标记"世界级热点"
2. 期权命中到期结算：实际盈亏与胜率统计（共振高分 vs 全体对照）
3. AI 预测准确率：高影响(≥4 分)新闻的实际波动确认率
4. 自动调参：基于实际胜率微调 options.json 各形态 min_win_rate（带边界 + 日志）
"""
import math
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import yfinance as yf

from . import options as opt_mod
from . import utils

FILE = utils.DATA_DIR + "/review.json"
TUNE_STATE = utils.DATA_DIR + "/tune_state.json"


# ---------------- 新闻波动验证 ----------------
def _fetch_ohlcv(tickers: list[str]) -> pd.DataFrame | None:
    try:
        df = yf.download(tickers, period="4mo", interval="1d",
                         auto_adjust=True, progress=False, threads=True)
        if df is None or df.empty:
            return None
        return df
    except Exception as e:  # noqa: BLE001
        print(f"[review] 行情获取失败: {e}")
        return None


def verify_news(app_cfg: dict) -> dict:
    """验证近两日热点新闻的实际波动是否超过平均日波动。"""
    news_data = utils.load_json(utils.DATA_DIR + "/news.json", default={})
    items = news_data.get("items", [])
    cfg = app_cfg.get("news_impact", {})
    atr_win = int(cfg.get("atr_window", 60))
    min_ratio = float(cfg.get("min_atr_ratio", 1.5))
    world_score = float(cfg.get("world_class_score", 4))

    today = utils.bj_now().date()
    cutoff = today - timedelta(days=2)
    pending = [
        it for it in items
        if it.get("tickers") and "impact_confirmed" not in it
        and (lambda s: True if not s else datetime.strptime(s[:10], "%Y-%m-%d").date() >= cutoff)(it.get("first_seen", ""))
    ][:60]
    print(f"[review] 待核验新闻 {len(pending)} 条（共 {len(items)} 条，有标的 {sum(1 for it in items if it.get('tickers'))} 条）")
    if not pending:
        return {"checked": 0, "confirmed": 0, "world_class": [], "ai_accuracy": {}}

    tickers = sorted({t for it in pending for t in it["tickers"]})
    df = _fetch_ohlcv(tickers)
    if df is None or df.empty:
        return {"checked": 0, "confirmed": 0, "world_class": [], "ai_accuracy": {}, "error": "行情获取失败"}
    if isinstance(df.columns, pd.MultiIndex):
        close_all = df["Close"]
        high_all = df["High"]
        low_all = df["Low"]
    else:
        close_all = pd.DataFrame({tickers[0]: df["Close"]})
        high_all = pd.DataFrame({tickers[0]: df["High"]})
        low_all = pd.DataFrame({tickers[0]: df["Low"]})

    # 各标的 ATR%（60 日平均日内振幅）
    atr_pct = {}
    for t in tickers:
        if t not in close_all.columns:
            continue
        h = high_all[t].dropna()
        l = low_all[t].dropna()
        c = close_all[t].dropna()
        n = min(len(h), len(l), len(c))
        rng = ((h.iloc[-n:] - l.iloc[-n:]) / c.iloc[-n:]).tail(atr_win).mean()
        if rng == rng and rng > 0:
            atr_pct[t] = float(rng)

    checked, confirmed = 0, 0
    world_class = []
    updated_map = {}
    for it in pending:
        ratios = []
        for t in it["tickers"]:
            if t not in close_all.columns or t not in atr_pct:
                continue
            c = close_all[t].dropna()
            d = datetime.strptime(it["first_seen"][:10], "%Y-%m-%d").date()
            idx = c.index
            pos = idx.searchsorted(pd.Timestamp(d))
            if pos >= len(idx) - 1:
                continue
            move = abs(float(c.iloc[pos + 1] / c.iloc[pos] - 1))
            ratio = move / atr_pct[t]
            ratios.append(ratio)
        if not ratios:
            continue
        best = max(ratios)
        ok = best >= min_ratio
        checked += 1
        confirmed += 1 if ok else 0
        wc = ok and it.get("score", 0) >= world_score
        upd = {"atr_ratio": round(best, 2), "impact_confirmed": ok, "world_class": wc}
        updated_map[it["key"]] = upd
        if wc:
            world_class.append({
                "title": it.get("title_cn") or it.get("title", ""),
                "tickers": it["tickers"], "atr_ratio": round(best, 2),
                "score": it.get("score", 0), "first_seen": it.get("first_seen"),
                "link": it.get("link", ""),
            })
    world_class.sort(key=lambda x: -x["atr_ratio"])

    # 回写 news.json
    for it in items:
        if it["key"] in updated_map:
            it.update(updated_map[it["key"]])
    news_data["items"] = items
    utils.save_json(utils.DATA_DIR + "/news.json", news_data)

    # AI 准确率：影响≥4 vs 其他
    def acc_of(sub):
        n, c = 0, 0
        for it in sub:
            if it["key"] in updated_map:
                n += 1
                c += 1 if updated_map[it["key"]]["impact_confirmed"] else 0
        return {"n": n, "confirmed_rate": round(c / n, 3) if n else None}

    high = [it for it in pending if (it.get("impact") or 0) >= 4]
    low = [it for it in pending if (it.get("impact") or 0) < 4]
    return {
        "checked": checked, "confirmed": confirmed,
        "confirmed_rate": round(confirmed / checked, 3) if checked else None,
        "world_class": world_class[:15],
        "ai_accuracy": {"high_impact": acc_of(high), "others": acc_of(low)},
    }


# ---------------- 期权到期结算 ----------------
def _capital_of(h: dict) -> float:
    s = h.get("strategy")
    if s == "csp":
        return float(h.get("strike", 0)) or 1.0
    if s == "cc":
        return float(h.get("spot", 0)) or 1.0
    if s in ("bps", "ic"):
        w = abs(float(h.get("strike", 0)) - float(h.get("strike2", 0) or 0)) or 5.0
        return w
    return float(h.get("bid", 0)) or 0.01


def _settle_pnl(h: dict, st: float) -> float:
    """到期结算每股盈亏（用扫描时的权利金）。"""
    kind = h.get("strategy")
    K = float(h.get("strike", 0))
    credit = float(h.get("bid", 0))
    if kind == "csp":
        return credit - max(K - st, 0.0)
    if kind == "cc":
        return credit + min(st, K) - float(h.get("spot", st))
    if kind == "bps":
        K2 = float(h.get("strike2", K))
        return credit - max(K - st, 0.0) + max(K2 - st, 0.0)
    if kind == "ic":
        Kp, Kc = K, float(h.get("strike2", K))
        return (credit - max(Kp - st, 0.0) + max(Kp - 5.0 - st, 0.0)
                - max(st - Kc, 0.0) + max(st - Kc - 5.0, 0.0))
    if kind == "lc":
        return max(st - K, 0.0) - credit
    if kind == "lp":
        return max(K - st, 0.0) - credit
    return 0.0


def settle_options() -> dict:
    """结算已到期但未结算的期权命中。"""
    hist = utils.load_json(opt_mod.HISTORY_FILE, default={"items": []})
    items = hist["items"]
    today = pd.Timestamp.today().date()
    pending = [h for h in items if not h.get("settled") and h.get("expiry")
               and pd.Timestamp(h["expiry"]).date() < today]
    if not pending:
        return {"settled": 0, "actual_win_rate": None, "stats": {}}
    tickers = sorted({h["ticker"] for h in pending})
    df = _fetch_ohlcv(tickers)
    settled = 0
    for h in pending:
        try:
            t = h["ticker"]
            if df is None:
                break
            close = df["Close"][t].dropna() if isinstance(df.columns, pd.MultiIndex) else df["Close"].dropna()
            exp = pd.Timestamp(h["expiry"])
            sub = close[close.index <= exp]
            if sub.empty:
                sub = close[close.index <= exp + pd.Timedelta(days=5)]
            if sub.empty:
                continue
            st = float(sub.iloc[-1])
            pnl = _settle_pnl(h, st)
            cap = _capital_of(h)
            h["settled"] = True
            h["settled_at"] = utils.bj_now_str()
            h["won"] = bool(pnl > 0)
            h["actual_pnl"] = round(pnl, 3)
            h["actual_return"] = round(pnl / cap, 4)
            settled += 1
        except Exception as e:  # noqa: BLE001
            print(f"[review] 结算失败 {h.get('key')}: {e}")
    if settled:
        utils.save_json(opt_mod.HISTORY_FILE, {"updated_at": utils.bj_now_str(), "items": items})

    st_items = [h for h in items if h.get("settled")]
    won = [h for h in st_items if h.get("won")]
    res_items = [h for h in st_items if h.get("resonance", 0) >= 60]
    res_won = [h for h in res_items if h.get("won")]
    stats = {
        "settled_total": len(st_items),
        "actual_win_rate": round(len(won) / len(st_items), 3) if st_items else None,
        "resonance_win_rate": round(len(res_won) / len(res_items), 3) if res_items else None,
        "avg_actual_return": round(float(np.mean([h.get("actual_return", 0) for h in st_items])), 4) if st_items else None,
        "avg_actual_return_res": round(float(np.mean([h.get("actual_return", 0) for h in res_items])), 4) if res_items else None,
    }
    return {"settled": settled, "actual_win_rate": stats["actual_win_rate"], "stats": stats,
            "recent": [
                {"key": h.get("key"), "ticker": h.get("ticker"), "strategy_name": h.get("strategy_name"),
                 "won": h.get("won"), "actual_return": h.get("actual_return"),
                 "win_rate_pred": h.get("win_rate"), "resonance": h.get("resonance", 0)}
                for h in st_items[-15:]
            ]}


# ---------------- 自动调参 ----------------
def auto_tune(settle_stats: dict) -> list[str]:
    """按实际胜率微调 options.json 各形态 min_win_rate（边界 ±0.15）。"""
    actions = []
    if not settle_stats.get("stats", {}).get("settled_total", 0) >= 8:
        return actions
    st_items = utils.load_json(opt_mod.HISTORY_FILE, default={"items": []})["items"]
    settled = [h for h in st_items if h.get("settled")]
    state = utils.load_json(TUNE_STATE, default={"base": {}})
    o_cfg = utils.load_json(utils.CONFIG_DIR + "/options.json", default={})
    overrides = o_cfg.get("conditions", {}).get("overrides", {})
    changed = False
    for strat in overrides:
        sub = [h for h in settled if h.get("strategy") == strat]
        if len(sub) < 8:
            continue
        wr = sum(1 for h in sub if h.get("won")) / len(sub)
        cur = float(overrides[strat].get("min_win_rate", 0.55))
        base = state["base"].get(strat)
        if base is None:
            state["base"][strat] = cur
            base = cur
        new_cur = None
        if wr < cur - 0.05 and cur < base + 0.15:
            new_cur = round(min(cur + 0.02, base + 0.15), 2)
        elif wr > cur + 0.10 and cur > base - 0.05:
            new_cur = round(max(cur - 0.01, base - 0.05), 2)
        if new_cur is not None and abs(new_cur - cur) > 0.001:
            overrides[strat]["min_win_rate"] = new_cur
            actions.append(f"{strat}: 实际胜率 {wr:.1%}，min_win_rate {cur:.2f} → {new_cur:.2f}（基准 {base:.2f}）")
            changed = True
    if changed:
        o_cfg["conditions"]["overrides"] = overrides
        utils.save_json(utils.CONFIG_DIR + "/options.json", o_cfg)
        utils.save_json(TUNE_STATE, state)
    return actions


# ---------------- 主入口 ----------------
def run_review(app_cfg: dict) -> dict:
    t0 = utils.timer_ms()
    result = {"news": {}, "options": {}, "tune": {"actions": []}}
    try:
        result["news"] = verify_news(app_cfg)
    except Exception as e:  # noqa: BLE001
        result["news"] = {"error": f"{type(e).__name__}: {e}"}
        print(f"[review] 新闻验证失败: {e}")
    try:
        result["options"] = settle_options()
    except Exception as e:  # noqa: BLE001
        result["options"] = {"error": f"{type(e).__name__}: {e}"}
        print(f"[review] 期权结算失败: {e}")
    try:
        if app_cfg.get("auto_tune", True):
            result["tune"]["actions"] = auto_tune(result.get("options", {}))
    except Exception as e:  # noqa: BLE001
        print(f"[review] 自动调参失败: {e}")
    result["updated_at"] = utils.bj_now_str()
    result["sec"] = utils.elapsed_sec(t0)
    ok = utils.save_json(FILE, result)
    result["ok"] = ok
    return result
