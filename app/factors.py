# -*- coding: utf-8 -*-
"""实时量化因子挖掘：因子库 + 横截面 IC/ICIR 检验 + AI 解读。"""
import time

import numpy as np
import pandas as pd
import yfinance as yf

from . import utils
from .ai import DeepSeekClient

FILE = utils.DATA_DIR + "/factors.json"
CACHE_CSV = utils.CACHE_DIR + "/bars_factors.csv"


def _fetch(tickers: list[str], lookback_days: int):
    """返回 (Close 宽表, Volume 宽表)。"""
    last_err = None
    for attempt in range(2):
        try:
            df = yf.download(
                tickers, period=f"{lookback_days + 60}d", interval="1d",
                auto_adjust=True, progress=False, threads=True,
            )
            if df is None or df.empty:
                raise ValueError("empty")
            if isinstance(df.columns, pd.MultiIndex):
                close = df["Close"]
                volume = df["Volume"]
            else:
                close = pd.DataFrame({tickers[0]: df["Close"]})
                volume = pd.DataFrame({tickers[0]: df["Volume"]})
            close = close.dropna(axis=1, how="all")
            volume = volume.reindex(columns=close.columns).fillna(0.0)
            utils.os.makedirs(utils.CACHE_DIR, exist_ok=True)
            close.to_csv(CACHE_CSV)
            return close, volume
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt == 0:
                time.sleep(10)
    print(f"[factors] 下载失败（重试 2 次），尝试缓存: {last_err}")
    try:
        c = pd.read_csv(CACHE_CSV, index_col=0, parse_dates=True)
        if (utils.bj_now() - c.index[-1].tz_localize(utils.TZ)).days < 3:
            return c, pd.DataFrame(1.0, index=c.index, columns=c.columns)
    except Exception:  # noqa: BLE001
        pass
    return None, None


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    return (100 - 100 / (1 + up / dn.replace(0, np.nan))).fillna(50)


def compute_factor(ticker: str, close: pd.Series, volume: pd.Series, all_close: pd.DataFrame) -> dict:
    rets = close.pct_change()
    out = {}
    out["mom_20"] = close.pct_change(20)
    out["mom_60"] = close.pct_change(60)
    out["mom_250"] = close.pct_change(250)
    out["rev_5"] = -close.pct_change(5)
    out["vol_20"] = rets.rolling(20).std() * np.sqrt(252)
    out["vol_ratio_10_60"] = rets.rolling(10).std() / rets.rolling(60).std().replace(0, np.nan)
    out["rsi_14"] = _rsi(close, 14)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    out["macd_hist"] = (macd - signal) / close
    sma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    out["bb_pos"] = (close - sma20) / (2 * std20.replace(0, np.nan))
    out["vol_surge"] = volume.rolling(5).mean() / volume.rolling(60).mean().replace(0, np.nan)
    out["dollar_vol"] = np.log((close * volume).replace(0, np.nan))
    out["amihud"] = -(rets.abs() / (close * volume).replace(0, np.nan)).rolling(20).mean()
    out["gap_sma20"] = close / sma20 - 1
    out["skew_60"] = rets.rolling(60).skew()
    roll_max = close.rolling(60).max()
    out["maxdd_60"] = -(close / roll_max - 1)
    if "SPY" in all_close.columns and ticker != "SPY":
        cov = rets.rolling(60).cov(all_close["SPY"].pct_change())
        var = all_close["SPY"].pct_change().rolling(60).var()
        out["beta_spy"] = cov / var.replace(0, np.nan)
    else:
        out["beta_spy"] = pd.Series(1.0, index=close.index)
    return out


def _weekly_dates(idx: pd.DatetimeIndex) -> list:
    return list(idx.to_series().resample("W-FRI").last().dropna().index)


def run_factors(f_cfg: dict, client: DeepSeekClient, force_ai: bool = False,
                ai_interval_min: int = 60) -> dict:
    """因子挖掘主入口。"""
    t0 = utils.timer_ms()
    factor_ids = [f["id"] for f in f_cfg.get("factors", [])]
    names = {f["id"]: f["name"] for f in f_cfg.get("factors", [])}
    universe = list(dict.fromkeys(f_cfg.get("universe", [])))
    if "SPY" not in universe:
        universe.insert(0, "SPY")
    lookback = int(f_cfg.get("lookback_days", 756))
    close, vol_df = _fetch(universe, lookback)
    if close is None or close.shape[1] < 10:
        return {"ok": False, "error": "行情下载失败"}

    panels = {}
    for tkr in close.columns:
        s = close[tkr].dropna()
        if len(s) < 300:
            continue
        v = vol_df[tkr].reindex(s.index).fillna(0.0)
        try:
            panels[tkr] = compute_factor(tkr, s, v, close)
        except Exception as e:  # noqa: BLE001
            print(f"[factors] {tkr} 计算失败: {e}")

    weekly = _weekly_dates(close.index)
    weekly = [d for d in weekly if d >= close.index[min(250, len(close) - 1)]]

    horizons = [int(h) for h in f_cfg.get("forward_returns", [5, 20])]
    stats_by_h = {}
    for fwd in horizons:
        stats = []
        for fid in factor_ids:
            ics = []
            for d in weekly:
                try:
                    x = pd.Series({t: panels[t][fid].get(d, np.nan) for t in panels})
                    fwd_close = close.shift(-fwd)
                    y = fwd_close.loc[d] / close.loc[d] - 1
                    both = pd.concat([x, y], axis=1).dropna()
                    if len(both) < 8:
                        continue
                    ic = both.iloc[:, 0].rank().corr(both.iloc[:, 1], method="pearson")
                    ics.append(ic)
                except Exception:  # noqa: BLE001
                    continue
            ics = [i for i in ics if i == i]  # 去 NaN
            if len(ics) < 5:
                stats.append({"id": fid, "name": names.get(fid, fid), "n": len(ics)})
                continue
            mean_ic = float(np.mean(ics))
            std_ic = float(np.std(ics, ddof=1)) or 1e-9
            stats.append({
                "id": fid, "name": names.get(fid, fid),
                "mean_ic": round(mean_ic, 4),
                "icir": round(mean_ic / std_ic, 3),
                "tstat": round(mean_ic / std_ic * np.sqrt(len(ics)), 2),
                "hit_rate": round(float(np.mean([1 if (i > 0) == (mean_ic > 0) else 0 for i in ics])), 3),
                "n": len(ics),
            })
        stats.sort(key=lambda x: abs(x.get("icir", 0)), reverse=True)
        stats_by_h[str(fwd)] = stats

    # 最新一期热力图：top5 因子 × |zscore| 最大 10 只
    last_d = weekly[-1]
    top5 = [s["id"] for s in stats_by_h[str(horizons[0])][:5]]
    zs = {}
    for fid in top5:
        vals = pd.Series({t: panels[t][fid].get(last_d, np.nan) for t in panels}).dropna()
        z = (vals - vals.mean()) / (vals.std() + 1e-9)
        zs[fid] = z
    heat_tickers = sorted(panels, key=lambda t: max(abs(zs[f].get(t, 0)) for f in top5), reverse=True)[:10]
    heatmap = {
        "factors": [names.get(f, f) for f in top5],
        "tickers": heat_tickers,
        "values": [[round(float(zs[f].get(t, 0)), 2) for f in top5] for t in heat_tickers],
        "as_of": last_d.strftime("%Y-%m-%d"),
    }

    prev = utils.load_json(FILE, default={})
    ai_due = utils.minutes_ago(prev.get("ai_comment", {}).get("updated_at")) is None or \
        utils.minutes_ago(prev.get("ai_comment", {}).get("updated_at")) >= ai_interval_min
    ai_comment = prev.get("ai_comment", {})
    if (force_ai or ai_due) and client.available and stats_by_h.get(str(horizons[0])):
        top3 = stats_by_h[str(horizons[0])][:3]
        payload = {
            "top_factors": top3,
            "heatmap_tickers": heat_tickers,
        }
        system = (
            "你是量化研究员。根据因子 IC 检验结果（mean_ic、icir、tstat、hit_rate 为横截面 RankIC 统计）"
            "输出 JSON：{\"comment\":\"一段中文解读(120字内)，说明当前哪些因子有效、可能的宏观原因\","
            "\"ideas\":[\"2-3条可执行的中文因子组合建议\"],\"risk_note\":\"一句风险提示\"}"
        )
        res = client.chat_json(system, "因子统计：" + str(payload), expect_keys={"comment"})
        if res:
            ai_comment = {"comment": res.get("comment", ""), "ideas": res.get("ideas", []),
                          "risk_note": res.get("risk_note", ""), "updated_at": utils.bj_now_str()}

    out = {
        "updated_at": utils.bj_now_str(),
        "universe_n": len(panels),
        "horizons": stats_by_h,
        "top_factors": stats_by_h[str(horizons[0])],
        "heatmap": heatmap,
        "ai_comment": ai_comment,
    }
    ok = utils.save_json(FILE, out)
    return {"ok": ok, "updated_at": out["updated_at"], "factors": len(factor_ids),
            "universe": len(panels), "sec": utils.elapsed_sec(t0)}
