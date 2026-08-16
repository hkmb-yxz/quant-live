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


# ---------------- 五维共振 ----------------
def _fetch_res_bars(tickers: list[str]):
    """共振评分用日线（Close + Volume）。"""
    try:
        df = yf.download(tickers, period="1y", interval="1d",
                         auto_adjust=True, progress=False, threads=True)
        if df is None or df.empty:
            return None, None
        if isinstance(df.columns, pd.MultiIndex):
            return df["Close"], df["Volume"]
        return pd.DataFrame({tickers[0]: df["Close"]}), pd.DataFrame({tickers[0]: df["Volume"]})
    except Exception as e:  # noqa: BLE001
        print(f"[options] 共振行情获取失败: {e}")
        return None, None


def _news_resonance(sym: str, news_items: list[dict]) -> tuple[float, int, int]:
    """热点消息维度：48 小时内提及该标的的新闻热度、多空净倾向、条数。"""
    rel = [it for it in news_items if sym in (it.get("tickers") or [])]
    score = sum(float(it.get("score", 0)) for it in rel)
    bull = sum(1 for it in rel if it.get("sentiment") == "利好")
    bear = sum(1 for it in rel if it.get("sentiment") == "利空")
    return score, bull - bear, len(rel)


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def compute_resonance(sym: str, close: pd.Series, volume: pd.Series,
                      news_items: list[dict], w_cfg: dict) -> dict:
    """四维评分（新闻/周期/技术/资金）+ 多空共振方向，输出 0-100 分。"""
    close = close.dropna()
    if len(close) < 70:
        return {"score": 0, "bias": 0, "news": 0, "cycle": 0.5, "technical": 0.5, "capital": 0.5, "news_hits": 0}
    volume = volume.reindex(close.index).fillna(0.0)
    sma20 = close.rolling(20).mean()
    sma50 = close.rolling(50).mean()
    sma200 = close.rolling(200).mean()
    last = close.iloc[-1]
    rets = close.pct_change()

    # 1) 热点消息
    n_score, n_net, n_count = _news_resonance(sym, news_items)
    news_v = _clip01(n_score / 30.0)

    # 2) 行情周期规律（趋势位/均线结构）
    cyc = 0.4 * _clip01((last / sma50.iloc[-1]) - 1 + 0.5) if not np.isnan(sma50.iloc[-1]) else 0.2
    if not np.isnan(sma200.iloc[-1]) and last > sma200.iloc[-1]:
        cyc += 0.3
    if not np.isnan(sma20.iloc[-1]) and sma20.iloc[-1] > sma50.iloc[-1]:
        cyc += 0.3
    cycle_v = _clip01(cyc)

    # 3) 技术分析（MACD/RSI/布林，按方向给分）
    rsi = _rsi_series(close, 14)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = (ema12 - ema26).iloc[-1]
    std20 = close.rolling(20).std().iloc[-1] or 1e-9
    bb_pos = (last - sma20.iloc[-1]) / (2 * std20)
    tech_bull = 0.4 * (1 if macd > 0 else 0) + 0.3 * (1 if 40 <= rsi.iloc[-1] <= 70 else 0) + 0.3 * (1 if bb_pos > -0.5 else 0)
    tech_bear = 0.4 * (1 if macd < 0 else 0) + 0.3 * (1 if 30 <= rsi.iloc[-1] <= 60 else 0) + 0.3 * (1 if bb_pos < 0.5 else 0)

    # 4) 资金投入（量能突增 + OBV 斜率 + 成交额分位）
    vol5 = volume.rolling(5).mean().iloc[-1]
    vol60 = volume.rolling(60).mean().iloc[-1]
    surge = _clip01((vol5 / vol60 if vol60 > 0 else 0.0) / 1.5)
    obv = (np.sign(rets.fillna(0)) * volume).cumsum()
    obv_slope = (obv.iloc[-1] - obv.iloc[-20]) / max(float(obv.rolling(20).std().iloc[-1] or 1.0) * 20, 1.0)
    obv_dir = _clip01((math.tanh(obv_slope) + 1) / 2) if obv_slope >= 0 else 0.0
    cap_bull = 0.5 * surge + 0.5 * obv_dir
    cap_bear = 0.5 * surge + 0.5 * (1 - obv_dir)

    # 多空方向投票：新闻倾向 + 均线结构 + MACD + OBV
    votes = []
    votes.append(1 if n_net > 0 else (-1 if n_net < 0 else 0))
    votes.append(1 if last > sma50.iloc[-1] else -1)
    votes.append(1 if macd > 0 else -1)
    votes.append(1 if obv_slope > 0 else -1)
    bias = 1 if sum(votes) > 0 else (-1 if sum(votes) < 0 else 0)
    tech_v = tech_bull if bias >= 0 else tech_bear
    cap_v = cap_bull if bias >= 0 else cap_bear
    w = w_cfg.get("weights", {"news": 0.35, "cycle": 0.20, "technical": 0.25, "capital": 0.20})
    score = round(100 * (w.get("news", 0.35) * news_v + w.get("cycle", 0.2) * cycle_v
                         + w.get("technical", 0.25) * tech_v + w.get("capital", 0.2) * cap_v), 1)
    return {
        "score": score, "bias": bias,
        "news": round(news_v, 3), "cycle": round(cycle_v, 3),
        "technical": round(tech_v, 3), "capital": round(cap_v, 3),
        "news_hits": n_count, "news_net": n_net,
    }


def _rsi_series(close: pd.Series, period: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rsi = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    return rsi.fillna(50)


def _strike_grid(chain: pd.DataFrame, S: float, win: float, side: str) -> pd.DataFrame:
    """side='put': K∈[S(1-w),S]；side='call': K∈[S,S(1+w)]"""
    if side == "put":
        return chain[(chain["strike"] >= S * (1 - win)) & (chain["strike"] <= S)]
    return chain[(chain["strike"] >= S) & (chain["strike"] <= S * (1 + win))]


def _liquidity_ok(row, cfg) -> bool:
    return (float(row.get("openInterest", 0) or 0) >= cfg["min_oi"]
            and float(row.get("volume", 0) or 0) >= cfg["min_volume"]
            and float(row.get("bid", 0) or 0) >= cfg["min_bid"])


def _evaluate_ic(S, Kp, Kc, T, sigma, r, q, credit, paths, seed, dte):
    """铁鹰 4 腿蒙特卡洛评估：卖沽 Kp / 买沽 Kp-w / 卖购 Kc / 买购 Kc+w（w=5）。"""
    rng = np.random.default_rng(seed)
    z = rng.standard_normal(paths)
    st = S * np.exp((r - q - 0.5 * sigma ** 2) * T + sigma * math.sqrt(T) * z)
    p = (credit - np.maximum(Kp - st, 0.0) + np.maximum(Kp - 5.0 - st, 0.0)
         - np.maximum(st - Kc, 0.0) + np.maximum(st - Kc - 5.0, 0.0))
    wins, losses = p[p > 0], p[p <= 0]
    win_rate = float(len(wins) / len(p))
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(-losses.mean()) if len(losses) else 0.0
    pl_ratio = float(avg_win / avg_loss) if avg_loss > 1e-12 else (99.0 if avg_win > 0 else 0.0)
    ann = float(p.mean() / 5.0 * 365.0 / max(dte, 1))
    return {
        "win_rate": round(win_rate, 4), "pl_ratio": round(pl_ratio, 2),
        "ann_return": round(ann, 4), "exp_per_share": round(float(p.mean()), 3),
        "p95": round(float(np.percentile(p, 5)), 3),
    }


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

    # 五维共振评分（新闻 + 周期 + 技术 + 资金）
    res_cfg = cfg.get("resonance", {})
    resonance_map = {}
    if res_cfg.get("enabled", True):
        news_data = utils.load_json(utils.DATA_DIR + "/news.json", default={})
        win_hours = int(res_cfg.get("news_window_hours", 48)) * 60
        news_items = [
            it for it in news_data.get("items", [])
            if utils.minutes_ago(it.get("first_seen")) is not None
            and utils.minutes_ago(it.get("first_seen")) <= win_hours
        ]
        res_closes, res_vols = _fetch_res_bars(tickers)
        for sym in tickers:
            try:
                if res_closes is not None and sym in res_closes.columns:
                    cs = res_closes[sym].dropna()
                    vs = res_vols[sym].reindex(cs.index).fillna(0.0) if res_vols is not None else pd.Series(0.0, index=cs.index)
                    resonance_map[sym] = compute_resonance(sym, cs, vs, news_items, res_cfg)
            except Exception as e:  # noqa: BLE001
                print(f"[options] {sym} 共振计算失败: {e}")

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
                    chain = tkr.option_chain(exp_str)
                    if hasattr(chain, "calls"):
                        calls, puts = chain.calls, chain.puts
                    else:
                        calls, puts = chain[0], chain[1]
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
                            m = _evaluate_ic(spot, Kp, Kc, T, sigma, r, q, credit, paths, seed, dte)
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

    # 去重 + 排序 + 共振评分注入
    seen, unique = set(), []
    for h in hits:
        if h["key"] in seen:
            continue
        seen.add(h["key"])
        res = resonance_map.get(h["ticker"])
        if res:
            h["resonance"] = res["score"]
            h["res_bias"] = res["bias"]
            h["res_parts"] = {k: res[k] for k in ("news", "cycle", "technical", "capital")}
        else:
            h["resonance"] = 0
            h["res_bias"] = 0
            h["res_parts"] = None
        unique.append(h)
    unique.sort(key=lambda x: -(x["ann_return"] * (0.3 + x["win_rate"]) + x["resonance"] / 500.0))
    new_hits = [h for h in unique if h["key"] not in hist_keys]

    # 邮件过滤：仅"共振达标 + 方向对齐"的新命中才推送
    min_res = float(res_cfg.get("min_resonance", 60))
    resonance_on = res_cfg.get("enabled", True)

    def email_eligible(h):
        if not resonance_on:
            return True
        if h.get("resonance", 0) < min_res:
            return False
        st_bias = cfg["strategies"].get(h["strategy"], {}).get("bias", 0)
        rb = h.get("res_bias", 0)
        if st_bias == 1 and rb < 0:
            return False
        if st_bias == -1 and rb > 0:
            return False
        return True

    new_hits_email = [h for h in new_hits if email_eligible(h)]

    # 更新历史
    hist_items = history["items"] + [dict(h, first_seen=utils.bj_now_str()) for h in new_hits]
    hist_items = hist_items[-800:]
    utils.save_json(HISTORY_FILE, {"updated_at": utils.bj_now_str(), "items": hist_items})

    out = {
        "updated_at": utils.bj_now_str(),
        "conditions": cond,
        "resonance": {"enabled": resonance_on, "min_resonance": min_res, "map": resonance_map},
        "run": {"scanned": scanned, "hits": len(unique), "new": len(new_hits),
                "resonance_hits": len(new_hits_email)},
        "items": unique[:25],
        "new_items": new_hits_email[:25],
        "new_items_all": new_hits[:25],
        "history": hist_items[-200:],
    }
    ok = utils.save_json(FILE, out)
    return {"ok": ok, "scanned": scanned, "hits": len(unique), "new": len(new_hits_email),
            "new_hits": new_hits_email, "sec": utils.elapsed_sec(t0)}
