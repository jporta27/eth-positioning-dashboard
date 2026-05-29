"""
Historical backtest of the Market State Score (partial reconstruction).

The live `computeSignals` function in frontend/src/Dashboard.jsx fuses 12
factors. Only 6 can be reconstructed from our 5-year parquets:

  Available historically:
    1. Funding             (binance_funding.parquet)
    3. Taker imbalance     (taker_buy_base in klines)
    5. Vol amplifier       (computed from klines)
    7. ETH/BTC             (klines + macro BTC)
    9. Volume profile      (klines OHLCV)
   10. Stochastics         (klines)

  NOT available (need persistence data we never had):
    2. OI                  — Binance API only retains 30 days
    4. L/S divergence       — same
    6. IV/RV spread         — no options history
    8. Gamma flip           — no options history
   11. Money Quality        — depends on OI history
   12. Setup detector       — depends on stoch + MQ history

So the score reconstructed here is **partial**: max |score| ≈ 7 vs ≈ 17
for the full 12-factor version. The hypothesis tested is whether the
6 factors we CAN reconstruct have predictive power on their own. If not,
the full 12 are unlikely to either.

Output: JSONL appended to data/state_log/_historical_backtest.jsonl
        (underscore prefix so live snapshots aren't mixed in by default)
        Schema matches the live logger so validate_score_magnitude.py
        consumes it directly.

Run:
    python scripts/backtest_score_historical.py --days 60 --period 4h
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import warnings
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

sys.stdout.reconfigure(encoding="utf-8")
warnings.filterwarnings("ignore", category=RuntimeWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KLINES_PATH = os.path.join(REPO_ROOT, "data", "backfill", "binance_klines_1h.parquet")
FUNDING_PATH = os.path.join(REPO_ROOT, "data", "backfill", "binance_funding.parquet")
MACRO_PATH = os.path.join(REPO_ROOT, "data", "backfill", "macro.parquet")
OUT_PATH = os.path.join(REPO_ROOT, "data", "state_log", "_historical_backtest.jsonl")


# ── Weight matrix (must match Dashboard.jsx exactly) ────────────────
W = {
    "5m":  {"funding": 0, "taker": 2, "vol": 0, "ethBtc": 0, "vp": 1, "stoch": 1},
    "15m": {"funding": 0, "taker": 2, "vol": 0, "ethBtc": 0, "vp": 1, "stoch": 1},
    "1h":  {"funding": 1, "taker": 1, "vol": 0, "ethBtc": 0, "vp": 1, "stoch": 1},
    "4h":  {"funding": 1, "taker": 1, "vol": 1, "ethBtc": 1, "vp": 1, "stoch": 2},
    "12h": {"funding": 2, "taker": 1, "vol": 1, "ethBtc": 1, "vp": 1, "stoch": 2},
    "1d":  {"funding": 2, "taker": 1, "vol": 1, "ethBtc": 1, "vp": 1, "stoch": 2},
    "15d": {"funding": 2, "taker": 0, "vol": 1, "ethBtc": 2, "vp": 1, "stoch": 2},
}

# Thresholds match Dashboard.jsx
FUNDING_HIGH = 0.0003
FUNDING_MID = 0.0001
TAKER_STRONG = 1.15
TAKER_MID = 1.05
ETHBTC_TRIGGER_PCT = 2.0
VOL_RATIO_COMPRESSED = 0.7

# Modulator (P1)
TAKER_TYPICAL_LOG_INTENSITY = 0.05  # same constant as Dashboard.jsx


# ── Data loading ────────────────────────────────────────────────────
def load_klines() -> pd.DataFrame:
    t = pq.read_table(KLINES_PATH)
    df = t.to_pandas()[["ts_utc_ms", "open", "high", "low", "close",
                        "volume", "taker_buy_base"]].sort_values("ts_utc_ms")
    df["ts"] = pd.to_datetime(df["ts_utc_ms"], unit="ms", utc=True)
    return df.set_index("ts")


def load_funding(target_index: pd.DatetimeIndex) -> pd.Series:
    t = pq.read_table(FUNDING_PATH)
    df = t.to_pandas()[["ts_utc_ms", "funding_rate"]].sort_values("ts_utc_ms")
    df["ts"] = pd.to_datetime(df["ts_utc_ms"], unit="ms", utc=True)
    df = df.set_index("ts")["funding_rate"]
    return df.reindex(target_index, method="ffill")


def load_macro_btc(target_index: pd.DatetimeIndex) -> pd.Series:
    t = pq.read_table(MACRO_PATH)
    df = t.to_pandas()[["ts_utc_ms", "label", "close"]].sort_values("ts_utc_ms")
    df["ts"] = pd.to_datetime(df["ts_utc_ms"], unit="ms", utc=True)
    btc = df[df["label"] == "BTC"].set_index("ts")["close"]
    return btc.reindex(target_index, method="ffill")


# ── Factor computations ─────────────────────────────────────────────
def factor_funding(rate: float, w: int) -> int:
    """Factor 1. Returns signed integer contribution."""
    if rate is None or math.isnan(rate) or w == 0:
        return 0
    if rate > FUNDING_HIGH:
        return -w
    if rate > FUNDING_MID:
        return -math.ceil(w / 2)
    if rate < -FUNDING_HIGH:
        return +w
    if rate < -FUNDING_MID:
        return +math.ceil(w / 2)
    return 0


def factor_taker(taker_ratio: float, w: int, with_modulator: bool) -> int:
    """Factor 3. taker_ratio = buy_volume / sell_volume."""
    if taker_ratio is None or math.isnan(taker_ratio) or w == 0 or taker_ratio <= 0:
        return 0
    if taker_ratio > TAKER_STRONG:
        base = +w
    elif taker_ratio > TAKER_MID:
        base = +math.ceil(w / 2)
    elif taker_ratio < (1.0 / TAKER_STRONG):  # ~0.87, but JS uses 0.85
        base = -w
    elif taker_ratio < (1.0 / TAKER_MID):     # ~0.95
        base = -math.ceil(w / 2)
    else:
        base = 0
    if base == 0:
        return 0
    if not with_modulator:
        return base
    intensity = abs(math.log(taker_ratio))
    m = max(0.5, min(1.5, intensity / TAKER_TYPICAL_LOG_INTENSITY))
    return int(round(base * m))


def factor_vol_amplifier(vol_ratio: float, score_so_far: int, w: int) -> int:
    """Factor 5. Amplifies the existing bias if vol is compressed."""
    if w == 0 or vol_ratio is None or math.isnan(vol_ratio):
        return 0
    if vol_ratio >= VOL_RATIO_COMPRESSED or abs(score_so_far) < 1:
        return 0
    return int(np.sign(score_so_far)) * w


def factor_ethbtc(ethbtc_chg_pct: float, w: int) -> int:
    """Factor 7. % change of ETH/BTC ratio in 24h."""
    if w == 0 or ethbtc_chg_pct is None or math.isnan(ethbtc_chg_pct):
        return 0
    if ethbtc_chg_pct > ETHBTC_TRIGGER_PCT:
        return +w
    if ethbtc_chg_pct < -ETHBTC_TRIGGER_PCT:
        return -w
    return 0


def factor_vp(close: float, vwap: float, vwap_std: float, w: int) -> tuple:
    """Factor 9. We approximate VP with VWAP ± 1σ as the value area.
    Returns (contribution, position_label).
    Real VP would use intra-bar volume distribution; this is the cleanest
    proxy at 1h granularity."""
    if w == 0 or any(v is None or (isinstance(v, float) and math.isnan(v))
                     for v in (close, vwap, vwap_std)) or vwap_std == 0:
        return 0, "unknown"
    va_high = vwap + vwap_std
    va_low = vwap - vwap_std
    if close > va_high:
        return -w, "above_va"
    if close < va_low:
        return +w, "below_va"
    return 0, "in_va"


def compute_stoch_series(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                          k_period: int, k_smooth: int, d_period: int) -> tuple:
    """Vectorised stochastic %K and %D series — computed once for the whole
    klines window, then sliced per-bar. Returns (smooth_k_series, smooth_d_series).

    O(N × K) where K is k_period (vectorised by pandas rolling).
    """
    h = pd.Series(highs)
    l = pd.Series(lows)
    c = pd.Series(closes)
    hi_n = h.rolling(k_period).max()
    lo_n = l.rolling(k_period).min()
    rng = hi_n - lo_n
    raw_k = np.where(rng > 0, (c - lo_n) / rng * 100.0, 50.0)
    smooth_k = pd.Series(raw_k).rolling(k_smooth).mean()
    smooth_d = smooth_k.rolling(d_period).mean()
    return smooth_k.values, smooth_d.values


def factor_stoch(slow_k_now: float, slow_d_now: float,
                 fast_k_now: float, fast_d_now: float,
                 fast_k_prev: float, w: int) -> int:
    """Factor 10. Slow stoch for régimen + fast stoch cross for timing."""
    if w == 0:
        return 0
    contribution = 0
    # Slow (régimen)
    if slow_k_now is not None and slow_d_now is not None:
        if slow_k_now >= 80 and slow_d_now >= 80:
            contribution -= w
        elif slow_k_now <= 20 and slow_d_now <= 20:
            contribution += w
    # Fast cross in OS/OB zones (only if slow agrees with zone)
    if (fast_k_now is not None and fast_d_now is not None
            and fast_k_prev is not None and slow_k_now is not None):
        # Cross al alza in OS
        if (fast_k_now > fast_d_now and fast_k_now < 30 and slow_k_now < 40
                and fast_k_prev <= fast_d_now):
            contribution += math.ceil(w / 2)
        # Cross a la baja in OB
        elif (fast_k_now < fast_d_now and fast_k_now > 70 and slow_k_now > 60
                and fast_k_prev >= fast_d_now):
            contribution -= math.ceil(w / 2)
    return contribution


# ── Score → state level mapping (mirrors Dashboard.jsx) ─────────────
def score_to_level(score: int) -> str:
    if score >= 3: return "bullish"
    if score == 2 or score == 1: return "caution"
    if score == 0: return "neutral"
    if score == -1 or score == -2: return "caution"
    return "bearish"


# ── Main backtest loop ─────────────────────────────────────────────
def run_backtest(days: int, period: str, with_modulator: bool = True) -> int:
    print(f"Loading parquets ...", file=sys.stderr)
    klines = load_klines()

    # Filter to last `days` days + lookback buffer for slow stoch (~400 bars + a bit)
    end_ts = klines.index.max()
    start_main = end_ts - pd.Timedelta(days=days)
    start_buffered = start_main - pd.Timedelta(hours=500)
    klines = klines[klines.index >= start_buffered]
    print(f"  klines window: {klines.index.min()} → {klines.index.max()}  ({len(klines):,} bars)", file=sys.stderr)

    funding = load_funding(klines.index)
    btc_close = load_macro_btc(klines.index)

    # Pre-compute series we need over the window
    close = klines["close"].values
    high = klines["high"].values
    low = klines["low"].values
    volume = klines["volume"].values
    taker_buy = klines["taker_buy_base"].values

    # Realized vol (close-to-close, 24h)
    log_ret = np.log(close[1:] / close[:-1])
    log_ret = np.concatenate([[np.nan], log_ret])
    rv_24 = pd.Series(log_ret).rolling(24).std().values
    # Vol ratio: 2h vs 24h proxy (compression detection)
    rv_2 = pd.Series(log_ret).rolling(2).std().values
    vol_ratio_series = np.where(rv_24 > 0, rv_2 / rv_24, np.nan)

    # Taker ratio (buy / sell)
    sell_vol = volume - taker_buy
    taker_ratio_series = np.where(sell_vol > 0, taker_buy / sell_vol, np.nan)

    # ETH/BTC ratio + 24h change
    eth_btc_ratio = close / btc_close.values
    eth_btc_chg_24h = pd.Series(eth_btc_ratio).pct_change(24).values * 100.0

    # VWAP-based VP proxy (24h window)
    typical_price = (high + low + close) / 3.0
    # VWAP over rolling 24h
    pv = typical_price * volume
    vwap_24 = (pd.Series(pv).rolling(24).sum().values /
               pd.Series(volume).rolling(24).sum().values)
    # std around VWAP (price stddev, weighted by volume — use simple stdev for proxy)
    vwap_std_24 = pd.Series(close).rolling(24).std().values

    # Stoch series — computed ONCE for the whole window (vectorised)
    print(f"  Pre-computing stoch series (vectorised) ...", file=sys.stderr)
    slow_k_series, slow_d_series = compute_stoch_series(high, low, close,
                                                          k_period=400, k_smooth=10, d_period=40)
    fast_k_series, fast_d_series = compute_stoch_series(high, low, close,
                                                          k_period=100, k_smooth=4, d_period=10)

    snapshots = []
    period_w = W[period]
    eligible_idx = klines.index >= start_main
    eligible_positions = np.where(eligible_idx)[0]
    print(f"  Emitting snapshots from {klines.index[eligible_positions[0]]} ({len(eligible_positions)} bars)", file=sys.stderr)

    out_path = OUT_PATH
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    open(out_path, "w").close()  # truncate
    n_written = 0

    def _safe(v):
        if v is None: return None
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)): return None
        return float(v)

    for i in eligible_positions:
        if i < 410:
            continue
        slow_k_now = _safe(slow_k_series[i])
        slow_d_now = _safe(slow_d_series[i])
        fast_k_now = _safe(fast_k_series[i])
        fast_d_now = _safe(fast_d_series[i])
        fast_k_prev = _safe(fast_k_series[i - 1])

        # ── Compute score ──
        ts_ms = int(klines.index[i].timestamp() * 1000)
        funding_val = funding.iloc[i]
        taker_val = taker_ratio_series[i]
        vol_ratio_val = vol_ratio_series[i]
        ethbtc_chg = eth_btc_chg_24h[i]
        close_now = close[i]
        vwap_now = vwap_24[i]
        vwap_std = vwap_std_24[i]

        # PRE-modulator score
        c_funding = factor_funding(funding_val, period_w["funding"])
        c_taker_pre = factor_taker(taker_val, period_w["taker"], with_modulator=False)
        score_pre = c_funding + c_taker_pre
        c_vol_pre = factor_vol_amplifier(vol_ratio_val, score_pre, period_w["vol"])
        score_pre += c_vol_pre
        c_ethbtc = factor_ethbtc(ethbtc_chg, period_w["ethBtc"])
        score_pre += c_ethbtc
        c_vp, vp_pos = factor_vp(close_now, vwap_now, vwap_std, period_w["vp"])
        score_pre += c_vp
        c_stoch = factor_stoch(slow_k_now, slow_d_now, fast_k_now, fast_d_now,
                               fast_k_prev, period_w["stoch"])
        score_pre += c_stoch
        score_pre = max(-10, min(10, score_pre))

        # POST-modulator (only differs on taker)
        c_taker_post = factor_taker(taker_val, period_w["taker"], with_modulator=True)
        score_post = (c_funding + c_taker_post)
        c_vol_post = factor_vol_amplifier(vol_ratio_val, score_post, period_w["vol"])
        score_post += c_vol_post
        score_post += c_ethbtc + c_vp + c_stoch
        score_post = max(-10, min(10, score_post))

        snapshot = {
            "timestamp": ts_ms,
            "period": period,
            "stochTf": period,
            "score": int(score_post),
            "scoreLevel": score_to_level(score_post),
            "scorePreModulator": int(score_pre),
            "scorePostModulator": int(score_post),
            "factors": [
                f"funding={c_funding}", f"taker_pre={c_taker_pre}",
                f"taker_post={c_taker_post}", f"vol_amp={c_vol_post}",
                f"ethbtc={c_ethbtc}", f"vp={c_vp} ({vp_pos})",
                f"stoch={c_stoch}",
            ],
            "price": float(close_now),
            "_historical": True,
            "_partial_factors": ["funding", "taker", "vol", "ethBtc", "vp", "stoch"],
        }
        snapshots.append(snapshot)
        n_written += 1
        if n_written % 100 == 0:
            print(f"  {n_written} snapshots ...", file=sys.stderr)

    with open(out_path, "w", encoding="utf-8") as f:
        for s in snapshots:
            f.write(json.dumps(s, default=str) + "\n")
    print(f"\nWrote {len(snapshots):,} snapshots to {out_path}", file=sys.stderr)
    return len(snapshots)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60, help="Days of history to backtest")
    ap.add_argument("--period", type=str, default="4h",
                    help="Score period to simulate (matches W matrix key)")
    ap.add_argument("--no-modulator", action="store_true",
                    help="Don't apply the P1 modulator (raw 12-factor score, debugging)")
    args = ap.parse_args()
    if args.period not in W:
        print(f"Invalid period: {args.period}. Valid: {list(W)}", file=sys.stderr)
        sys.exit(1)
    n = run_backtest(args.days, args.period, with_modulator=not args.no_modulator)
    print(f"\nDone. {n:,} snapshots written.", file=sys.stderr)


if __name__ == "__main__":
    main()
