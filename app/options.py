# -*- coding: utf-8 -*-
"""期权形态抓捕：扫描期权链，用 Black-Scholes + 蒙特卡洛估算每种形态的
胜率 / 盈亏比 / 年化期望，按用户条件过滤，输出命中并支持邮件提醒。"""
import math
import time

import numpy as np
import pandas as pd
import yfinance as yf

from . import utils

FILE = utils.DATA_DIR + "/options_hits.json"
HISTORY_FILE = utils.DATA_DIR + "/options_history.json"


# ---------------- 数学 ----------------
def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_delta(S, K, T, sigma, r, q, is_call: bool) -> float:
    if T <= 0 or sigma <= 0:
        return 1.0 if (is_call and S >= K) else (0.0 if is_call else 1.0 if S < K else 0.0)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return norm_cdf(d1) if is_call else norm_cdf(d1) - 1.0


def _mc_payoffs(S, K1, K2, T, sigma, r, q, kind: str, credit: float, paths: int, seed: int):
    """蒙特卡洛计算每股收益数组。kind: csp/cc/bps/ic/lc/lp；K2 仅价差使用。"""
    rng = np.random.default_rng(seed)
    z = rng.standard_normal(paths)
    st = S * np.exp((r - q - 0.5 * sigma ** 2) * T + sigma * math.sqrt(T) * z)
    if kind == "csp":
        return credit - np.maximum(K1 - st, 0.0)
    if kind == "cc":
        return credit + np.minimum(st, K1) - S
    if kind == "bps":
        return credit - np.maximum(K1 - st, 0.0) + np.maximum(K2 - st, 0.0)
    if kind == "ic":
        return (credit - np.maximum(K1 - st, 0.0) + np.maximum(K2 - st, 0.0)
                - np.maximum(st - K1, 0.0) + np.maximum(st - K2, 0.0))
    if kind == "lc":
        return np.maximum(st - K1, 0.0) - credit
    if kind == "lp":
        return np.maximum(K1 - st, 0.0) - credit
    raise ValueError(kind)


def _evaluate(S, K1, K2, T, sigma, r, q, kind, credit, capital, paths, seed, dte):
    p = _mc_payoffs(S, K1, K2, T, sigma, r, q, kind, credit, paths, seed)
    wins, losses = p[p > 0], p[p <= 0]
    win_rate = float(len(wins) / len(p))
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(-losses.mean()) if len(losses) else 0.0
    pl_ratio = float(avg_win / avg_loss) if avg_loss > 1e-12 else (99.0 if avg_win > 0 else 0.0)
    exp_ret = float(p.mean())
    ann = float(exp_ret / capital * 365.0 / max(dte, 1))
    return {
        "win_rate": round(win_rate, 4), "pl_ratio": round(pl_ratio, 2),
        "ann_return": round(ann, 4), "exp_per_share": round(exp_ret, 3),
        "p95": round(float(np.percentile(p, 5)), 3),
    }


# ---------------- 数据 ----------------
def _get_spot(tkr: yf.Ticker) -> float | None:
    try:
        fi = tkr.fast_info
        for k in ("lastPrice", "last_price", "regularMarketPrice"):
            try:
                v = fi[k]
                if v:
                    return float(v)
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    try:
        h = tkr.history(period="5d")
        if not h.empty:
            return float(h["Close"].iloc[-1])
    except Exception:  # noqa: BLE001
        pass
    return None


def _pct_rank(vals: np.ndarray, v: float) -> float:
    if len(vals) == 0:
        return 0.0
    return float((vals < v).mean())


def _strike_grid(chain: pd.DataFrame, S: float, win: float, side: str) -> pd.DataFrame:
    """side='put': K∈[S(1-w),S]；side='call': K∈[S,S(1+w)]"""
    if side == "put":
        return chain[(chain["strike"] >= S * (1 - win)) & (chain["strike"] <= S)]
    return chain[(chain["strike"] >= S) & (chain["strike"] <= S * (1 + win))]


def _liquidity_ok(row, cfg) -> bool:
    return (float(row.get("openInterest", 0) or 0) >= cfg["min_oi"]
            and float(row.get("volume", 0) or 0) >= cfg["min_volume"]
            and float(row.get("bid", 0) or 0) >= cfg["min_bid"])


def scan_options(o_cfg: dict) -> dict:
    """期权扫描主入口。返回 {ok, scanned, hits, new_hits}，并写 JSON。"""
    t0 = utils.timer_ms()
    tickers = o_cfg.get("tickers", [])
    cfg = o_cfg
    cond = dict(cfg.get("conditions", {}))
    overrides = cond.pop("overrides", {})

    def eff_cond(kind: str) -> dict:
        """各形态可单独覆盖抓捕条件。"""
        c = dict(cond)
        c.update(overrides.get(kind, {}))
        return c

    r, q = float(cfg.get("risk_free_rate", 0.042)), float(cfg.get("dividend_yield", 0.0))
    paths = int(cfg.get("mc_paths", 2000))
    dte_min, dte_max = cfg.get("dte_min", 7), cfg.get("dte_max", 45)
    win = float(cfg.get("strike_window", 0.10))
    max_exp = int(cfg.get("max_expiries_per_ticker", 3))
    sleep_s = float(cfg.get("sleep_between_chains", 1.2))

    history = utils.load_json(HISTORY_FILE, default={"items": []})
    hist_keys = {it.get("key") for it in history["items"]}
    hits, scanned = [], 0

    for sym in tickers:
        try:
            tkr = yf.Ticker(sym)
            spot = _get_spot(tkr)
            if not spot or spot <= 0:
                print(f"[options] {sym} 无行情，跳过")
                continue
            exps = tkr.options
            if not exps:
                continue
            cand_exp = []
            for e in exps:
                try:
                    dte = (pd.Timestamp(e).date() - pd.Timestamp.today().date()).days
                except Exception:  # noqa: BLE001
                    continue
                if dte_min <= dte <= dte_max:
                    cand_exp.append((e, dte))
            cand_exp = cand_exp[:max_exp]
            for exp_str, dte in cand_exp:
                try:
                    calls, puts = tkr.option_chain(exp_str)
                except Exception as e:  # noqa: BLE001
                    print(f"[options] {sym} {exp_str} 链获取失败: {e}")
                    continue
                calls = _strike_grid(calls, spot, win, "call")
                puts = _strike_grid(puts, spot, win, "put")
                if calls.empty and puts.empty:
                    continue
                T = dte / 365.0
                call_ivs = calls["impliedVolatility"].dropna().to_numpy()
                put_ivs = puts["impliedVolatility"].dropna().to_numpy()

                def make(side_df, kind, ivs):
                    nonlocal scanned
                    for _, row in side_df.iterrows():
                        scanned += 1
                        K = float(row["strike"])
                        iv = float(row["impliedVolatility"]) if row["impliedVolatility"] == row["impliedVolatility"] else None
                        if iv is None or iv <= 0.02:
                            continue
                        if not _liquidity_ok(row, cfg):
                            continue
                        delta = bs_delta(spot, K, T, iv, r, q, kind in ("cc", "lc", "ic"))
                        st_cfg = cfg["strategies"].get(kind, {})
                        if not (st_cfg.get("delta_min", 0) <= abs(delta) <= st_cfg.get("delta_max", 1)):
                            continue
                        K2, credit, capital = 0.0, float(row["bid"]), spot
                        seed = abs(hash((sym, kind, exp_str, K))) % (2 ** 31)
                        if kind == "csp":
                            capital = K
                        elif kind in ("lc", "lp"):
                            credit = float(row["ask"])  # 买方成本
                            capital = credit
                        m = _evaluate(spot, K, K2, T, iv, r, q, kind, credit, capital, paths, seed, dte)
                        c = eff_cond(kind)
                        if m["win_rate"] < c.get("min_win_rate", 0.55) or m["pl_ratio"] < c.get("min_pl_ratio", 1.5) \
                                or m["ann_return"] < c.get("min_ann_return", 0.08):
                            continue
                        hits.append({
                            "key": f"{sym}|{kind}|{exp_str}|{K}",
                            "ticker": sym, "strategy": kind,
                            "strategy_name": st_cfg.get("name", kind),
                            "expiry": exp_str, "strike": K, "strike2": None,
                            "spot": round(spot, 2), "dte": dte,
                            "bid": round(float(row["bid"]), 3), "ask": round(float(row["ask"]), 3),
                            "iv": round(iv, 4), "iv_pct": round(_pct_rank(ivs, iv), 3),
                            "delta": round(delta, 3), "oi": int(row.get("openInterest", 0) or 0),
                            "volume": int(row.get("volume", 0) or 0),
                            **m,
                        })

                def make_spread(puts_df, kind):
                    nonlocal scanned
                    st_cfg = cfg["strategies"].get(kind, {})
                    wmax = float(st_cfg.get("width_max", 10))
                    strikes = sorted(puts_df["strike"].unique().tolist())
                    for _, row in puts_df.iterrows():
                        scanned += 1
                        Ks = float(row["strike"])
                        iv = float(row["impliedVolatility"]) if row["impliedVolatility"] == row["impliedVolatility"] else None
                        if iv is None or iv <= 0.02 or not _liquidity_ok(row, cfg):
                            continue
                        delta = bs_delta(spot, Ks, T, iv, r, q, False)
                        if not (st_cfg.get("delta_min", 0) <= abs(delta) <= st_cfg.get("delta_max", 1)):
                            continue
                        width = None
                        for w in (2.5, 5.0, 10.0):
                            if w > wmax + 1e-9:
                                break
                            if (Ks - w) in strikes:
                                width = w
                                break
                        if width is None:
                            continue
                        Kl = Ks - width
                        long_row = puts_df[puts_df["strike"] == Kl]
                        if long_row.empty:
                            continue
                        credit = float(row["bid"]) - float(long_row["ask"].iloc[0])
                        if credit <= 0.02:
                            continue
                        capital = width
                        seed = abs(hash((sym, kind, exp_str, Ks, Kl))) % (2 ** 31)
                        m = _evaluate(spot, Ks, Kl, T, iv, r, q, "bps", credit, capital, paths, seed, dte)
                        c = eff_cond(kind)
                        if m["win_rate"] < c.get("min_win_rate", 0.55) or m["pl_ratio"] < c.get("min_pl_ratio", 1.5) \
                                or m["ann_return"] < c.get("min_ann_return", 0.08):
                            continue
                        hits.append({
                            "key": f"{sym}|{kind}|{exp_str}|{Ks}|{Kl}",
                            "ticker": sym, "strategy": kind,
                            "strategy_name": st_cfg.get("name", kind),
                            "expiry": exp_str, "strike": Ks, "strike2": Kl,
                            "spot": round(spot, 2), "dte": dte,
                            "bid": round(credit, 3), "ask": round(credit, 3),
                            "iv": round(iv, 4), "iv_pct": round(_pct_rank(put_ivs, iv), 3),
                            "delta": round(delta, 3), "oi": int(row.get("openInterest", 0) or 0),
                            "volume": int(row.get("volume", 0) or 0),
                            **m,
                        })

                s_cfg = cfg["strategies"]
                if s_cfg.get("csp", {}).get("enabled"):
                    make(puts, "csp", put_ivs)
                if s_cfg.get("cc", {}).get("enabled"):
                    make(calls, "cc", call_ivs)
                if s_cfg.get("bps", {}).get("enabled"):
                    make_spread(puts, "bps")
                if s_cfg.get("lc", {}).get("enabled"):
                    make(calls, "lc", call_ivs)
                if s_cfg.get("lp", {}).get("enabled"):
                    make(puts, "lp", put_ivs)
                # 铁鹰：卖沽价差 + 卖购价差，同宽度同到期
                if s_cfg.get("ic", {}).get("enabled"):
                    st_cfg = s_cfg["ic"]
                    wmax = float(st_cfg.get("width_max", 10))
                    pstrikes = sorted(puts["strike"].unique().tolist())
                    cstrikes = sorted(calls["strike"].unique().tolist())
                    for _, prow in puts.iterrows():
                        for _, crow in calls.iterrows():
                            Kp, Kc = float(prow["strike"]), float(crow["strike"])
                            if not _liquidity_ok(prow, cfg) or not _liquidity_ok(crow, cfg):
                                continue
                            ivp = prow["impliedVolatility"]
                            ivc = crow["impliedVolatility"]
                            if ivp != ivp or ivc != ivc:
                                continue
                            dp = bs_delta(spot, Kp, T, ivp, r, q, False)
                            dc = bs_delta(spot, Kc, T, ivc, r, q, True)
                            if not (st_cfg.get("delta_min", 0) <= abs(dp) <= st_cfg.get("delta_max", 1)):
                                continue
                            if not (st_cfg.get("delta_min", 0) <= abs(dc) <= st_cfg.get("delta_max", 1)):
                                continue
                            if (Kp - 5.0) not in pstrikes or (Kc + 5.0) not in cstrikes:
                                continue
                            credit = (float(prow["bid"]) + float(crow["bid"])
                                      - float(puts[puts["strike"] == Kp - 5.0]["ask"].iloc[0])
                                      - float(calls[calls["strike"] == Kc + 5.0]["ask"].iloc[0]))
                            if credit <= 0.02:
                                continue
                            sigma = float(np.mean([ivp, ivc]))
                            seed = abs(hash((sym, "ic", exp_str, Kp, Kc))) % (2 ** 31)
                            m = _evaluate(spot, Kp, Kc, T, sigma, r, q, "ic", credit, 5.0, paths, seed, dte)
                            c = eff_cond("ic")
                            if m["win_rate"] < c.get("min_win_rate", 0.55) or m["pl_ratio"] < c.get("min_pl_ratio", 1.5) \
                                    or m["ann_return"] < c.get("min_ann_return", 0.08):
                                continue
                            hits.append({
                                "key": f"{sym}|ic|{exp_str}|{Kp}|{Kc}",
                                "ticker": sym, "strategy": "ic",
                                "strategy_name": st_cfg.get("name", "ic"),
                                "expiry": exp_str, "strike": Kp, "strike2": Kc,
                                "spot": round(spot, 2), "dte": dte,
                                "bid": round(credit, 3), "ask": round(credit, 3),
                                "iv": round(sigma, 4),
                                "iv_pct": round((_pct_rank(put_ivs, ivp) + _pct_rank(call_ivs, ivc)) / 2, 3),
                                "delta": round(float(np.mean([abs(dp), abs(dc)])), 3),
                                "oi": int(min(prow.get("openInterest", 0), crow.get("openInterest", 0)) or 0),
                                "volume": int(min(prow.get("volume", 0), crow.get("volume", 0)) or 0),
                                **m,
                            })
                time.sleep(sleep_s)
        except Exception as e:  # noqa: BLE001
            print(f"[options] {sym} 扫描异常: {e}")

    # 去重 + 排序
    seen, unique = set(), []
    for h in hits:
        if h["key"] in seen:
            continue
        seen.add(h["key"])
        unique.append(h)
    unique.sort(key=lambda x: -(x["ann_return"] * (0.3 + x["win_rate"])))
    new_hits = [h for h in unique if h["key"] not in hist_keys]

    # 更新历史
    hist_items = history["items"] + [dict(h, first_seen=utils.bj_now_str()) for h in new_hits]
    hist_items = hist_items[-800:]
    utils.save_json(HISTORY_FILE, {"updated_at": utils.bj_now_str(), "items": hist_items})

    out = {
        "updated_at": utils.bj_now_str(),
        "conditions": cond,
        "run": {"scanned": scanned, "hits": len(unique), "new": len(new_hits)},
        "items": unique[:25],
        "new_items": new_hits[:25],
        "history": hist_items[-200:],
    }
    ok = utils.save_json(FILE, out)
    return {"ok": ok, "scanned": scanned, "hits": len(unique), "new": len(new_hits),
            "new_hits": new_hits, "sec": utils.elapsed_sec(t0)}
