# -*- coding: utf-8 -*-
"""实时回测盈利分析 v2：
- 样本内(IS=历史) / 样本外(OOS=近端) 分段回测，对比检验过拟合
- 参数网格 walk-forward 选择（按 OOS 夏普选参，防止对历史过拟合）
- 目标检测：胜率≥60%、盈亏比>2、夏普>2（可配置）
"""
import time

import numpy as np
import pandas as pd
import yfinance as yf

from . import utils

FILE = utils.DATA_DIR + "/backtest.json"
CACHE_CSV = utils.CACHE_DIR + "/bars_backtest.csv"


# ---------------- 数据 ----------------
def fetch_bars(tickers: list[str], lookback_days: int) -> pd.DataFrame | None:
    """批量下载日线收市价；失败回退本地缓存。"""
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
    return (close.rolling(p["fast"]).mean() > close.rolling(p["slow"]).mean()).astype(float)


def rsi_reversion(close, p):
    rsi = _rsi(close, p["period"])
    sig = pd.Series(np.where(rsi < p["buy"], 1.0, np.where(rsi > p["sell"], 0.0, np.nan)), index=close.index)
    return _state_pos(sig)


def momentum(close, p):
    mom = close.pct_change(p["mom_days"])
    cond = ((mom > 0) & (close > close.rolling(p["ma"]).mean())).astype(float)
    return cond.resample("ME").last().reindex(close.index, method="ffill").fillna(0.0)


def donchian(close, p):
    hi = close.rolling(p["entry"]).max().shift(1)
    lo = close.rolling(p["exit"]).min().shift(1)
    sig = pd.Series(np.where(close > hi, 1.0, np.where(close < lo, 0.0, np.nan)), index=close.index)
    return _state_pos(sig)


STRATEGIES = {"sma_cross": sma_cross, "rsi_reversion": rsi_reversion, "momentum": momentum, "donchian": donchian}


# ---------------- 评估 ----------------
def _extract_trades(pos: pd.Series) -> list[tuple]:
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


def _eval_segment(close: pd.Series, pos: pd.Series, capital: float, start_idx=0):
    """对一段行情评估，返回指标字典；样本不足返回 None。"""
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
    idx = np.linspace(0, len(equity) - 1, min(90, len(equity))).astype(int)
    curve = [[d.strftime("%Y-%m-%d"), round(float(v), 2)] for d, v in zip(equity.index[idx], equity.values[idx])]
    return {
        "total_return": round(total, 4), "ann_return": round(ann, 4), "sharpe": round(sharpe, 2),
        "maxdd": round(maxdd, 4), "win_rate": round(win_rate, 4), "pl_ratio": round(pl_ratio, 2),
        "profit_factor": round(pf, 2), "n_trades": len(tr_ret), "n_days": len(rets), "equity": curve,
    }


def _check_targets(m: dict, targets: dict) -> dict:
    return {
        "win_rate": bool(m and m["win_rate"] >= targets.get("min_win_rate", 0.6)),
        "pl_ratio": bool(m and m["pl_ratio"] > targets.get("min_pl_ratio", 2.0)),
        "sharpe": bool(m and m["sharpe"] > targets.get("min_sharpe", 2.0)),
        "all": bool(m and m["win_rate"] >= targets.get("min_win_rate", 0.6)
                    and m["pl_ratio"] > targets.get("min_pl_ratio", 2.0)
                    and m["sharpe"] > targets.get("min_sharpe", 2.0)),
    }


def _eval_full(close: pd.Series, pos: pd.Series, capital: float, split_idx: int, targets: dict):
    """按 split_idx 切分 IS/OOS 两段评估 + 合并指标。"""
    is_close, oos_close = close.iloc[:split_idx], close.iloc[split_idx:]
    is_pos, oos_pos = pos.iloc[:split_idx], pos.iloc[split_idx:]
    m_is = _eval_segment(is_close, is_pos, capital)
    m_oos = _eval_segment(oos_close, oos_pos, capital)
    m_all = _eval_segment(close, pos, capital)
    if m_oos is None or m_is is None:
        return None
    ratio = round(m_oos["sharpe"] / m_is["sharpe"], 2) if m_is["sharpe"] > 0 else None
    overfit = {
        "oos_is_sharpe_ratio": ratio,
        "flag": bool(m_is["sharpe"] > 1.0 and (ratio is None or ratio < 0.5)),
        "note": "样本外显著弱于样本内，疑似过拟合" if (m_is["sharpe"] > 1.0 and (ratio is None or ratio < 0.5)) else "样本内外表现一致",
    }
    return {
        "is": m_is, "oos": m_oos, "combined": m_all,
        "pass_targets": {
            "is": _check_targets(m_is, targets),
            "oos": _check_targets(m_oos, targets),
            "combined": _check_targets(m_all, targets),
        },
        "overfit": overfit,
    }


def run_backtests(bt_cfg: dict, watchlist: dict) -> dict:
    """回测主入口 v2。"""
    t0 = utils.timer_ms()
    tickers = watchlist.get("backtest_tickers", [])
    lookback = int(bt_cfg.get("lookback_days", 504))
    capital = float(bt_cfg.get("initial_capital", 100000))
    targets = bt_cfg.get("targets", {})
    wf_cfg = bt_cfg.get("walk_forward", {})
    oos_ratio = float(wf_cfg.get("oos_ratio", 0.30))
    grid = wf_cfg.get("grid", {})
    close = fetch_bars(tickers, lookback)
    if close is None:
        return {"ok": False, "error": "行情下载失败"}
    close = close.dropna(axis=1, thresh=int(len(close) * 0.9))
    strategies = [s for s in bt_cfg.get("strategies", []) if s.get("enabled", True)]

    results, ranking = {}, []
    benchmark = {}
    for tkr in close.columns:
        s = close[tkr].dropna()
        if len(s) < 160:
            continue
        split_idx = int(len(s) * (1 - oos_ratio))
        s_is, s_oos = s.iloc[:split_idx], s.iloc[split_idx:]

        def bh_metrics(x):
            r = x.pct_change().dropna()
            if len(r) < 30:
                return None
            eq = (1 + r).cumprod() * capital
            tot = float((1 + r).prod() - 1)
            return {
                "total_return": round(tot, 4),
                "ann_return": round(float((1 + tot) ** (252 / len(r)) - 1), 4),
                "sharpe": round(float(r.mean() / r.std() * np.sqrt(252)), 2),
                "maxdd": round(float((eq / eq.cummax() - 1).min()), 4),
            }

        benchmark[tkr] = {"is": bh_metrics(s_is), "oos": bh_metrics(s_oos), "combined": bh_metrics(s)}
        results.setdefault(tkr, {})
        for st in strategies:
            fn = STRATEGIES.get(st["id"])
            if fn is None:
                continue
            try:
                candidates = grid.get(st["id"]) or [st.get("params", {})]
                best = None
                for p in candidates:
                    try:
                        pos = fn(s, p)
                        full = _eval_full(s, pos, capital, split_idx, targets)
                        if full is None:
                            continue
                        score = full["oos"]["sharpe"]
                        if best is None or score > best[0]:
                            best = (score, p, full)
                    except Exception as e:  # noqa: BLE001
                        print(f"[backtest] {tkr}/{st['id']}/{p} 失败: {e}")
                if best is None:
                    continue
                _, sel_p, full = best
                full["params"] = sel_p
                full["grid_tested"] = len(candidates)
                full["strategy_name"] = st["name"]
                results[tkr][st["id"]] = full
                oos = full["oos"]
                ranking.append({
                    "ticker": tkr, "strategy": st["name"], "params": sel_p,
                    "oos_sharpe": oos["sharpe"], "oos_ann": oos["ann_return"],
                    "oos_win": oos["win_rate"], "oos_pl": oos["pl_ratio"], "oos_dd": oos["maxdd"],
                    "is_sharpe": full["is"]["sharpe"], "is_win": full["is"]["win_rate"],
                    "is_pl": full["is"]["pl_ratio"],
                    "overfit": full["overfit"]["flag"],
                    "pass_oos": full["pass_targets"]["oos"]["all"],
                    "pass_is": full["pass_targets"]["is"]["all"],
                })
            except Exception as e:  # noqa: BLE001
                print(f"[backtest] {tkr}/{st['id']} 整体失败: {e}")
    ranking.sort(key=lambda x: (x["oos_sharpe"], x["oos_ann"]), reverse=True)
    out = {
        "updated_at": utils.bj_now_str(),
        "lookback_days": lookback,
        "oos_ratio": oos_ratio,
        "targets": targets,
        "strategies": [{"id": s["id"], "name": s["name"], "params": s.get("params", {})} for s in strategies],
        "benchmark": benchmark,
        "results": results,
        "ranking": ranking[:40],
        "summary": {
            "pass_is": sum(1 for r in ranking if r["pass_is"]),
            "pass_oos": sum(1 for r in ranking if r["pass_oos"]),
            "overfit_count": sum(1 for r in ranking if r["overfit"]),
            "combos": len(ranking),
        },
    }
    ok = utils.save_json(FILE, out)
    return {"ok": ok, "updated_at": out["updated_at"], "tickers": len(results),
            "combos": len(ranking), "pass_oos": out["summary"]["pass_oos"],
            "overfit": out["summary"]["overfit_count"], "sec": utils.elapsed_sec(t0)}
