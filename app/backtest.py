# -*- coding: utf-8 -*-
"""实时回测盈利分析：对关注列表运行多种策略，输出收益/回撤/胜率/盈亏比。"""
import time

import numpy as np
import pandas as pd
import yfinance as yf

from . import utils

FILE = utils.DATA_DIR + "/backtest.json"
CACHE_CSV = utils.CACHE_DIR + "/bars_backtest.csv"


# ---------------- 数据 ----------------
def fetch_bars(tickers: list[str], lookback_days: int) -> pd.DataFrame | None:
    """批量下载日线（收市价），失败时回退到本地缓存。返回 index=日期, columns=代码 的收盘价表。"""
    last_err = None
    for attempt in range(2):
        try:
            df = yf.download(
                tickers, period=f"{lookback_days + 120}d", interval="1d",
                auto_adjust=True, progress=False, threads=True,
            )
            if df is None or df.empty:
                raise ValueError("empty download")
            close = df["Close"] if isinstance(df.columns, pd.MultiIndex) else pd.DataFrame({tickers[0]: df["Close"]})
            close = close.dropna(axis=1, how="all")
            if close.empty:
                raise ValueError("all tickers empty")
            utils.os.makedirs(utils.CACHE_DIR, exist_ok=True)
            close.to_csv(CACHE_CSV)
            return close
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt == 0:
                time.sleep(10)
    print(f"[backtest] 下载失败（重试 2 次），尝试缓存: {last_err}")
    try:
        cached = pd.read_csv(CACHE_CSV, index_col=0, parse_dates=True)
        if (utils.bj_now() - cached.index[-1].tz_localize(utils.TZ)).days < 2:
            print("[backtest] 使用本地缓存数据")
            return cached
    except Exception as ce:  # noqa: BLE001
        print(f"[backtest] 缓存不可用: {ce}")
    return None


# ---------------- 策略 ----------------
def _rsi(close: pd.Series, period: int) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rsi = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    return rsi.fillna(50)


def _state_pos(signal: pd.Series) -> pd.Series:
    return signal.ffill().fillna(0.0)


def sma_cross(close, p):
    pos = (close.rolling(p["fast"]).mean() > close.rolling(p["slow"]).mean()).astype(float)
    return pos


def rsi_reversion(close, p):
    rsi = _rsi(close, p["period"])
    sig = pd.Series(np.where(rsi < p["buy"], 1.0, np.where(rsi > p["sell"], 0.0, np.nan)), index=close.index)
    return _state_pos(sig)


def momentum(close, p):
    mom = close.pct_change(p["mom_days"])
    cond = ((mom > 0) & (close > close.rolling(p["ma"]).mean())).astype(float)
    pos = cond.resample("ME").last().reindex(close.index, method="ffill").fillna(0.0)
    return pos


def donchian(close, p):
    hi = close.rolling(p["entry"]).max().shift(1)
    lo = close.rolling(p["exit"]).min().shift(1)
    sig = pd.Series(np.where(close > hi, 1.0, np.where(close < lo, 0.0, np.nan)), index=close.index)
    return _state_pos(sig)


STRATEGIES = {"sma_cross": sma_cross, "rsi_reversion": rsi_reversion, "momentum": momentum, "donchian": donchian}


# ---------------- 评估 ----------------
def _extract_trades(pos: pd.Series) -> list[tuple]:
    """从 0/1 持仓序列提取 (进场日, 出场日) 完整交易对。"""
    trades, entry = [], None
    for i, v in pos.items():
        if v > 0 and entry is None:
            entry = i
        elif v == 0 and entry is not None:
            trades.append((entry, i))
            entry = None
    if entry is not None:
        trades.append((entry, pos.index[-1]))
    return trades


def _eval(close: pd.Series, pos: pd.Series, capital: float) -> dict | None:
    rets = (pos.shift(1) * close.pct_change()).dropna()
    if len(rets) < 30 or rets.std() == 0:
        return None
    equity = (1 + rets).cumprod() * capital
    total = float((1 + rets).prod() - 1)
    ann = float((1 + total) ** (252 / len(rets)) - 1)
    sharpe = float(rets.mean() / rets.std() * np.sqrt(252))
    maxdd = float((equity / equity.cummax() - 1).min())
    trades = _extract_trades(pos)
    tr_ret = [float(close[e] / close[b] - 1) for b, e in trades if b != e]
    wins = [r for r in tr_ret if r > 0]
    losses = [r for r in tr_ret if r <= 0]
    win_rate = len(wins) / len(tr_ret) if tr_ret else 0.0
    avg_win = float(np.mean(wins)) if wins else 0.0
    avg_loss = float(abs(np.mean(losses))) if losses else 0.0
    pl_ratio = float(avg_win / avg_loss) if avg_loss > 0 else (99.0 if avg_win > 0 else 0.0)
    pf = float(sum(wins) / abs(sum(losses))) if losses else (99.0 if wins else 0.0)
    idx = np.linspace(0, len(equity) - 1, min(120, len(equity))).astype(int)
    curve = [[d.strftime("%Y-%m-%d"), round(float(v), 2)] for d, v in zip(equity.index[idx], equity.values[idx])]
    return {
        "total_return": round(total, 4), "ann_return": round(ann, 4), "sharpe": round(sharpe, 2),
        "maxdd": round(maxdd, 4), "win_rate": round(win_rate, 4), "pl_ratio": round(pl_ratio, 2),
        "profit_factor": round(pf, 2), "n_trades": len(tr_ret), "equity": curve,
    }


def run_backtests(bt_cfg: dict, watchlist: dict) -> dict:
    """回测主入口。返回 {ok, updated_at, ...}，并写 data/backtest.json。"""
    t0 = utils.timer_ms()
    tickers = watchlist.get("backtest_tickers", [])
    lookback = int(bt_cfg.get("lookback_days", 504))
    capital = float(bt_cfg.get("initial_capital", 100000))
    close = fetch_bars(tickers, lookback)
    if close is None:
        return {"ok": False, "error": "行情下载失败"}
    close = close.dropna(axis=1, thresh=int(len(close) * 0.9))
    strategies = [s for s in bt_cfg.get("strategies", []) if s.get("enabled", True)]

    results, ranking = {}, []
    benchmark = {}
    for tkr in close.columns:
        s = close[tkr].dropna()
        if len(s) < 120:
            continue
        bh = (s.iloc[-1] / s.iloc[0] - 1)
        bh_ann = (1 + bh) ** (252 / len(s)) - 1
        bh_rets = s.pct_change().dropna()
        bh_equity = (1 + bh_rets).cumprod() * capital
        benchmark[tkr] = {
            "total_return": round(float(bh), 4), "ann_return": round(float(bh_ann), 4),
            "sharpe": round(float(bh_rets.mean() / bh_rets.std() * np.sqrt(252)), 2),
            "maxdd": round(float((bh_equity / bh_equity.cummax() - 1).min()), 4),
        }
        results.setdefault(tkr, {})
        for st in strategies:
            fn = STRATEGIES.get(st["id"])
            if fn is None:
                continue
            try:
                pos = fn(s, st.get("params", {}))
                m = _eval(s, pos, capital)
                if m is None:
                    continue
                results[tkr][st["id"]] = m
                ranking.append({
                    "ticker": tkr, "strategy": st["name"],
                    "ann_return": m["ann_return"], "sharpe": m["sharpe"],
                    "maxdd": m["maxdd"], "win_rate": m["win_rate"],
                    "pl_ratio": m["pl_ratio"], "total_return": m["total_return"],
                })
            except Exception as e:  # noqa: BLE001
                print(f"[backtest] {tkr}/{st['id']} 失败: {e}")
    ranking.sort(key=lambda x: x["sharpe"], reverse=True)
    out = {
        "updated_at": utils.bj_now_str(),
        "lookback_days": lookback,
        "strategies": [{"id": s["id"], "name": s["name"], "params": s.get("params", {})} for s in strategies],
        "benchmark": benchmark,
        "results": results,
        "ranking": ranking[:40],
    }
    ok = utils.save_json(FILE, out)
    return {"ok": ok, "updated_at": out["updated_at"], "tickers": len(results),
            "combos": len(ranking), "sec": utils.elapsed_sec(t0)}
