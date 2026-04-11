"""
ETH Positioning Dashboard — Backend API
Fetches data from Binance & OKX public APIs and serves unified endpoints.
Includes order book depth snapshots with rolling history.
Designed for Railway deployment.
"""

import os
import time
import math
import statistics
import asyncio
import logging
from typing import Optional
from contextlib import asynccontextmanager
from collections import deque
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# ── Config ────────────────────────────────────────────────────────────
BINANCE_FAPI = "https://fapi.binance.com"
BINANCE_SPOT = "https://api.binance.com"
OKX_API     = "https://www.okx.com"
DERIBIT_API = "https://www.deribit.com"
BYBIT_API   = "https://api.bybit.com"
HYPERLIQUID_API = "https://api.hyperliquid.xyz"
CACHE_TTL = 10  # seconds
DEPTH_INTERVAL = 5  # seconds between depth snapshots
DEPTH_HISTORY_MINUTES = 240  # 4 hours of history
DEPTH_SUMMARY_INTERVAL = 300  # save summary every 5 min (seconds)
DEPTH_CLUSTER_SIZE = 0.25  # group price levels into $0.25 buckets
VOLATILITY_LOOKBACK_DAYS = 30  # for percentile calculation
PORT = int(os.getenv("PORT", 8000))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dashboard")

# ── Cache ─────────────────────────────────────────────────────────────
cache: dict = {}
cache_ts: float = 0

# ── Order Book State ──────────────────────────────────────────────────
current_depth: dict = {}
current_depth_ts: float = 0
depth_history: deque = deque(maxlen=(DEPTH_HISTORY_MINUTES * 60) // DEPTH_SUMMARY_INTERVAL)
last_depth_summary_ts: float = 0


def cluster_levels(levels: list, cluster_size: float, side: str = "bid") -> list:
    """Group order book levels into price clusters."""
    if not levels:
        return []
    clusters = {}
    for price_str, qty_str in levels:
        price = float(price_str)
        qty = float(qty_str)
        bucket = round(price / cluster_size) * cluster_size
        if bucket not in clusters:
            clusters[bucket] = 0.0
        clusters[bucket] += qty

    result = [{"price": p, "qty": round(q, 2)} for p, q in clusters.items() if q > 0]
    result.sort(key=lambda x: x["price"], reverse=(side == "bid"))
    return result


def build_volume_profile(klines: list, cluster_size: float = 5.0, price_center: float = None, range_pct: float = 0.10) -> list:
    """Build volume-at-price profile from klines. Distributes each candle's volume uniformly across its high-low range."""
    if not klines:
        return []
    if price_center is None:
        price_center = float(klines[-1][4])
    low_bound = price_center * (1 - range_pct)
    high_bound = price_center * (1 + range_pct)
    clusters: dict = {}
    for k in klines:
        try:
            high = float(k[2])
            low = float(k[3])
            volume = float(k[5])
        except (IndexError, ValueError):
            continue
        if high <= low or volume <= 0:
            continue
        price_range = high - low
        # Walk through $cluster_size steps across candle range
        p = low
        steps = []
        while p <= high + cluster_size:
            bucket = round(p / cluster_size) * cluster_size
            if low_bound <= bucket <= high_bound:
                steps.append(bucket)
            p += cluster_size
        if not steps:
            continue
        vol_per_step = volume / max(len(steps), 1)
        for bucket in steps:
            clusters[bucket] = clusters.get(bucket, 0.0) + vol_per_step

    result = [{"price": round(p, 2), "vol": round(v, 1)} for p, v in clusters.items()]
    result.sort(key=lambda x: x["price"], reverse=True)
    return result


def calculate_volatility(klines: list) -> dict:
    """Calculate realized volatility metrics from 1h kline data. Returns RV windows + percentile."""
    if not klines or len(klines) < 24:
        return {}
    closes = []
    for k in klines:
        try:
            closes.append(float(k[4]))
        except (IndexError, ValueError):
            continue
    if len(closes) < 24:
        return {}
    returns = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            returns.append(math.log(closes[i] / closes[i - 1]))
    if len(returns) < 24:
        return {}
    ann_factor = math.sqrt(24 * 365)

    def rv(rets, window):
        if len(rets) < window:
            return None
        subset = rets[-window:]
        std = statistics.stdev(subset) if len(subset) > 1 else 0
        return round(std * ann_factor * 100, 2)

    rv_4h = rv(returns, 4)
    rv_24h = rv(returns, 24)
    rv_7d = rv(returns, 24 * 7) if len(returns) >= 24 * 7 else None

    # Rolling daily RVs for percentile
    daily_rvs = []
    for start in range(0, len(returns) - 23, 24):
        window_rets = returns[start:start + 24]
        if len(window_rets) >= 24:
            std = statistics.stdev(window_rets)
            daily_rvs.append(std * ann_factor * 100)

    percentile = None
    if rv_24h is not None and len(daily_rvs) >= 5:
        below = sum(1 for x in daily_rvs if x <= rv_24h)
        percentile = round((below / len(daily_rvs)) * 100, 1)

    rv_ratio = None
    if rv_4h is not None and rv_24h is not None and rv_24h > 0:
        rv_ratio = round(rv_4h / rv_24h, 3)

    # Spark history: rolling 24h RV, one per data point
    rv_history = []
    for i in range(max(0, len(returns) - 24 * 30), len(returns) - 23):
        w = returns[i:i + 24]
        if len(w) >= 2:
            rv_history.append(round(statistics.stdev(w) * ann_factor * 100, 2))

    return {
        "rv4h": rv_4h,
        "rv24h": rv_24h,
        "rv7d": rv_7d,
        "percentile": percentile,
        "ratio": rv_ratio,
        "history": rv_history[-60:],
    }


def calculate_volume_profile(klines: list, cluster_size: float = 5.0) -> dict:
    """Build structured volume profile with POC, Value Area, HVN, LVN from 1h klines."""
    if not klines or len(klines) < 10:
        return {}
    price_volume: dict = {}
    last_close = None
    for k in klines:
        try:
            high = float(k[2])
            low = float(k[3])
            close = float(k[4])
            volume = float(k[5])
        except (IndexError, ValueError):
            continue
        last_close = close
        if high <= low or volume <= 0:
            continue
        steps = max(int((high - low) / cluster_size), 1)
        vol_per_step = volume / steps
        for i in range(steps):
            price = round((low + i * cluster_size) / cluster_size) * cluster_size
            price_volume[price] = price_volume.get(price, 0) + vol_per_step

    if not price_volume or last_close is None:
        return {}

    poc_price = max(price_volume, key=price_volume.get)
    poc_volume = price_volume[poc_price]
    sorted_levels = sorted(price_volume.items(), key=lambda x: x[1], reverse=True)
    avg_volume = sum(price_volume.values()) / len(price_volume)

    # Top 5 HVN
    hvn = [{"price": p, "volume": round(v, 0)} for p, v in sorted_levels[:5]]
    # LVN: < 30% of average
    lvn = sorted(
        [{"price": p, "volume": round(v, 0)} for p, v in sorted_levels if v < avg_volume * 0.3],
        key=lambda x: x["price"], reverse=True
    )[:5]

    # Value Area (70% of volume)
    total_vol = sum(price_volume.values())
    accumulated = 0.0
    va_levels = []
    for p, v in sorted_levels:
        accumulated += v
        va_levels.append(p)
        if accumulated >= total_vol * 0.70:
            break
    vah = max(va_levels) if va_levels else None
    val = min(va_levels) if va_levels else None

    # Price position
    price_position = None
    if vah is not None and val is not None:
        if last_close > vah:
            price_position = "above_va"
        elif last_close < val:
            price_position = "below_va"
        elif abs(last_close - poc_price) <= cluster_size * 2:
            price_position = "at_poc"
        else:
            price_position = "in_va"

    return {
        "poc": {"price": poc_price, "volume": round(poc_volume, 0)},
        "vah": vah,
        "val": val,
        "hvn": hvn,
        "lvn": lvn[:3],
        "pricePosition": price_position,
        "currentPrice": last_close,
    }


def stochastic(klines: list, k_period: int, k_smooth: int, d_smooth: int, history_len: int = 60) -> Optional[dict]:
    """
    Stochastic oscillator with K smoothing.
    Formula:
      raw_k[i]    = (close[i] - min_low[k_period]) / (max_high[k_period] - min_low[k_period]) * 100
      smooth_k[i] = SMA(raw_k, k_smooth)
      %D[i]       = SMA(smooth_k, d_smooth)
    Returns {k, d, kHistory, dHistory} or None if insufficient data.
    """
    required = k_period + k_smooth + d_smooth - 2
    if not klines or len(klines) < required:
        return None
    try:
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        closes = [float(k[4]) for k in klines]
    except (IndexError, ValueError, TypeError):
        return None
    n = len(closes)
    raw_k = [None] * n
    for i in range(k_period - 1, n):
        window_h = max(highs[i - k_period + 1: i + 1])
        window_l = min(lows[i - k_period + 1: i + 1])
        rng = window_h - window_l
        raw_k[i] = 50.0 if rng == 0 else (closes[i] - window_l) / rng * 100
    smooth_k = [None] * n
    for i in range(n):
        start = i - k_smooth + 1
        if start < 0:
            continue
        window = raw_k[start:i + 1]
        if any(v is None for v in window):
            continue
        smooth_k[i] = sum(window) / k_smooth
    d_vals = [None] * n
    for i in range(n):
        start = i - d_smooth + 1
        if start < 0:
            continue
        window = smooth_k[start:i + 1]
        if any(v is None for v in window):
            continue
        d_vals[i] = sum(window) / d_smooth
    k_hist = [round(v, 2) if v is not None else None for v in smooth_k[-history_len:]]
    d_hist = [round(v, 2) if v is not None else None for v in d_vals[-history_len:]]
    latest_k = smooth_k[-1]
    latest_d = d_vals[-1]
    return {
        "k": round(latest_k, 2) if latest_k is not None else None,
        "d": round(latest_d, 2) if latest_d is not None else None,
        "kHistory": k_hist,
        "dHistory": d_hist,
    }


def compute_stochastics_multi(klines_by_tf: dict) -> dict:
    """Compute both slow (400,40,10) and fast (100,10,4) stochastics for each timeframe."""
    out = {}
    for tf, klines in klines_by_tf.items():
        if not klines:
            continue
        slow = stochastic(klines, 400, 40, 10)
        fast = stochastic(klines, 100, 10, 4)
        if slow or fast:
            out[tf] = {"slow": slow, "fast": fast}
    return out


def bs_gamma(S: float, K: float, T: float, sigma: float, r: float = 0.0) -> float:
    """Black-Scholes gamma. S=spot, K=strike, T=years to expiry, sigma=annual vol."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        pdf_d1 = math.exp(-0.5 * d1 ** 2) / math.sqrt(2 * math.pi)
        return pdf_d1 / (S * sigma * math.sqrt(T))
    except (ValueError, ZeroDivisionError):
        return 0.0


def calculate_iv_term_structure(instruments: list, spot_price: float) -> list:
    """Build IV term structure: average IV by DTE bucket from options data."""
    if not instruments or not spot_price:
        return []
    now = datetime.now(timezone.utc)
    dte_iv: dict = {}  # dte -> list of IVs (weighted by OI proximity to ATM)
    for inst in instruments:
        name = inst.get("instrument_name", "")
        parts = name.split("-")
        if len(parts) != 4 or parts[0] != "ETH":
            continue
        try:
            expiry = datetime.strptime(parts[1], "%d%b%y").replace(tzinfo=timezone.utc)
            strike = float(parts[2])
        except (ValueError, IndexError):
            continue
        dte = (expiry - now).days
        if dte < 0 or dte > 180:
            continue
        mark_iv = float(inst.get("mark_iv") or 0)
        oi = float(inst.get("open_interest") or 0)
        if mark_iv <= 0 or oi <= 0:
            continue
        # Weight ATM options more (within ±15% of spot)
        moneyness = abs(strike - spot_price) / spot_price
        if moneyness > 0.20:
            continue
        weight = max(1 - moneyness * 5, 0.1) * oi  # closer to ATM = higher weight
        if dte not in dte_iv:
            dte_iv[dte] = {"sum_iv_w": 0.0, "sum_w": 0.0}
        dte_iv[dte]["sum_iv_w"] += mark_iv * weight
        dte_iv[dte]["sum_w"] += weight

    result = []
    for dte in sorted(dte_iv.keys()):
        d = dte_iv[dte]
        if d["sum_w"] > 0:
            avg_iv = d["sum_iv_w"] / d["sum_w"]
            result.append({"dte": dte, "iv": round(avg_iv, 2)})
    return result


def estimate_liquidation_map(oi_value: float, spot_price: float, funding_rate: float = 0) -> list:
    """
    Estimate liquidation clusters based on OI distribution across leverage levels.
    Returns price levels where liquidations would cluster.
    Assumptions: OI is distributed across leverage tiers (2x-50x).
    Liquidation price for longs = entry * (1 - 1/leverage)
    Liquidation price for shorts = entry * (1 + 1/leverage)
    """
    if not oi_value or not spot_price or spot_price <= 0:
        return []

    # Estimated leverage distribution (based on typical exchange data)
    # Higher funding = more longs = skew long side OI
    long_bias = 0.5
    if funding_rate:
        long_bias = min(max(0.5 + funding_rate * 5000, 0.3), 0.7)

    leverage_tiers = [
        {"leverage": 2,  "pct": 0.05},
        {"leverage": 3,  "pct": 0.08},
        {"leverage": 5,  "pct": 0.15},
        {"leverage": 10, "pct": 0.25},
        {"leverage": 20, "pct": 0.22},
        {"leverage": 25, "pct": 0.12},
        {"leverage": 50, "pct": 0.08},
        {"leverage": 75, "pct": 0.03},
        {"leverage": 100,"pct": 0.02},
    ]

    clusters = []
    for tier in leverage_tiers:
        lev = tier["leverage"]
        oi_at_tier = oi_value * tier["pct"]
        long_oi = oi_at_tier * long_bias
        short_oi = oi_at_tier * (1 - long_bias)

        # Long liquidation: price drops to entry * (1 - 1/lev * 0.8) (80% maintenance margin)
        long_liq_price = round(spot_price * (1 - 0.8 / lev), 2)
        # Short liquidation: price rises to entry * (1 + 1/lev * 0.8)
        short_liq_price = round(spot_price * (1 + 0.8 / lev), 2)

        pct_from_spot_long = round((long_liq_price - spot_price) / spot_price * 100, 2)
        pct_from_spot_short = round((short_liq_price - spot_price) / spot_price * 100, 2)

        clusters.append({
            "leverage": lev,
            "longLiqPrice": long_liq_price,
            "shortLiqPrice": short_liq_price,
            "longOiUsd": round(long_oi),
            "shortOiUsd": round(short_oi),
            "longPctFromSpot": pct_from_spot_long,
            "shortPctFromSpot": pct_from_spot_short,
        })

    return clusters


def calculate_options_analytics(instruments: list, spot_price: float) -> dict:
    """
    Calculate GEX, gamma flip, call/put walls and max pain from Deribit ETH options.
    Greeks computed via Black-Scholes using mark_iv from Deribit book summary.
    Convention: Net GEX = Σ call_OI×gamma - Σ put_OI×gamma, scaled by spot².
    Positive GEX → dealers long gamma → market stabilizes.
    Negative GEX → dealers short gamma → moves are amplified.
    """
    if not instruments or not spot_price:
        return {}

    now = datetime.now(timezone.utc)
    options = []
    for inst in instruments:
        name = inst.get("instrument_name", "")
        parts = name.split("-")
        if len(parts) != 4 or parts[0] != "ETH":
            continue
        try:
            expiry = datetime.strptime(parts[1], "%d%b%y").replace(tzinfo=timezone.utc)
            strike = float(parts[2])
            opt_type = parts[3]  # "C" or "P"
        except (ValueError, IndexError):
            continue
        days_to_expiry = (expiry - now).days
        if days_to_expiry < 0 or days_to_expiry > 60:
            continue
        oi = float(inst.get("open_interest") or 0)
        if oi <= 0:
            continue
        # Deribit returns mark_iv as percentage (e.g. 66.03 = 66.03%), convert to decimal for BS
        mark_iv = float(inst.get("mark_iv") or 0) / 100.0
        T = days_to_expiry / 365.0
        gamma = bs_gamma(spot_price, strike, T, mark_iv) if mark_iv > 0 else 0.0
        options.append({
            "strike": strike, "type": opt_type, "expiry": expiry,
            "oi": oi, "gamma": gamma, "dte": days_to_expiry,
        })

    if not options:
        return {}

    # ── GEX by strike ──────────────────────────────────────────────────
    gex_map: dict = {}   # strike → net GEX
    oi_map:  dict = {}   # strike → {call, put}

    for o in options:
        s = o["strike"]
        gex_contrib = o["oi"] * o["gamma"] * spot_price * spot_price
        if s not in gex_map:
            gex_map[s] = 0.0
            oi_map[s]  = {"call": 0.0, "put": 0.0}
        if o["type"] == "C":
            gex_map[s] += gex_contrib
            oi_map[s]["call"] += o["oi"]
        else:
            gex_map[s] -= gex_contrib
            oi_map[s]["put"] += o["oi"]

    total_gex = sum(gex_map.values())

    # ── Gamma flip: per-strike GEX sign change (where calls start dominating over puts) ──
    # Find the strike closest to spot where GEX crosses from negative to positive.
    # More practical than cumulative: shows where local gamma composition changes.
    gamma_flip = None
    sorted_strikes = sorted(gex_map.keys())
    # Scan bottom-up: find where per-strike GEX crosses from negative to positive
    for i in range(1, len(sorted_strikes)):
        prev_s = sorted_strikes[i - 1]
        curr_s = sorted_strikes[i]
        prev_gex = gex_map[prev_s]
        curr_gex = gex_map[curr_s]
        if prev_gex < 0 and curr_gex >= 0:
            # Interpolate
            span = curr_gex - prev_gex
            if span > 0:
                gamma_flip = round(prev_s + (curr_s - prev_s) * (-prev_gex / span), 0)
            else:
                gamma_flip = curr_s
            break
    # Fallback: if all GEX negative, flip is above range; if all positive, below range
    if gamma_flip is None:
        if all(gex_map[s] < 0 for s in sorted_strikes):
            gamma_flip = sorted_strikes[-1] if sorted_strikes else None
        elif all(gex_map[s] >= 0 for s in sorted_strikes):
            gamma_flip = sorted_strikes[0] if sorted_strikes else None

    # ── Key levels within ±20% of spot ─────────────────────────────────
    nearby = {s: oi_map[s] for s in gex_map if spot_price * 0.80 <= s <= spot_price * 1.20}
    above  = {s: v for s, v in nearby.items() if s > spot_price}
    below  = {s: v for s, v in nearby.items() if s < spot_price}

    call_wall = max(above, key=lambda s: above[s]["call"]) if above else None
    put_wall  = max(below, key=lambda s: below[s]["put"])  if below else None

    # ── GEX bars for chart (±15% of spot, $50 buckets) — includes C/P OI ──
    bucket = 50.0
    def build_gex_bars(opts_subset):
        bmap: dict = {}  # bucket -> {gex, call_oi, put_oi}
        for o in opts_subset:
            s = o["strike"]
            if spot_price * 0.85 <= s <= spot_price * 1.15:
                b = round(s / bucket) * bucket
                if b not in bmap:
                    bmap[b] = {"gex": 0.0, "call_oi": 0.0, "put_oi": 0.0}
                contrib = o["oi"] * o["gamma"] * spot_price ** 2
                if o["type"] == "C":
                    bmap[b]["gex"]     += contrib
                    bmap[b]["call_oi"] += o["oi"]
                else:
                    bmap[b]["gex"]    -= contrib
                    bmap[b]["put_oi"] += o["oi"]
        return sorted(
            [{"strike": round(k),
              "gex":    round(v["gex"] / 1e6, 3),
              "callOi": round(v["call_oi"]),
              "putOi":  round(v["put_oi"])}
             for k, v in bmap.items()],
            key=lambda x: x["strike"]
        )

    gex_bars = build_gex_bars(options)

    # ── Zone analysis: put/call OI above & below spot ────────────────────
    def zone_stats(opts_subset):
        c_oi = sum(o["oi"] for o in opts_subset if o["type"] == "C")
        p_oi = sum(o["oi"] for o in opts_subset if o["type"] == "P")
        net  = sum(
            o["oi"] * o["gamma"] * spot_price ** 2 * (1 if o["type"] == "C" else -1)
            for o in opts_subset
        )
        total = c_oi + p_oi
        return {
            "callOi":    round(c_oi),
            "putOi":     round(p_oi),
            "callPct":   round(c_oi / total * 100, 1) if total else 0,
            "putPct":    round(p_oi / total * 100, 1) if total else 0,
            "netGex":    round(net / 1e6, 1),
            "dominant":  "puts" if p_oi > c_oi else "calls",
        }

    in_range = [o for o in options if spot_price * 0.85 <= o["strike"] <= spot_price * 1.15]
    flip_ref = gamma_flip if gamma_flip else spot_price
    zone_analysis = {
        "belowFlip":  zone_stats([o for o in in_range if o["strike"] < flip_ref]),
        "aboveFlip":  zone_stats([o for o in in_range if o["strike"] >= flip_ref]),
        "flipLevel":  flip_ref,
    }

    # ── GEX bars per expiry ──────────────────────────────────────────────
    future_expiries_all = sorted(set(o["expiry"] for o in options))
    gex_by_expiry: dict = {}
    expiry_list = []
    for exp in future_expiries_all:
        exp_str = exp.strftime("%d %b").lstrip("0") if hasattr(exp, "strftime") else str(exp)
        exp_date = exp.date() if hasattr(exp, "date") else exp
        dte = (exp_date - datetime.now(timezone.utc).date()).days
        subset = [o for o in options if o["expiry"] == exp]
        gex_by_expiry[exp_str] = build_gex_bars(subset)
        expiry_list.append({"label": exp_str, "dte": dte})

    # ── Max pain for nearest expiry ──────────────────────────────────────
    max_pain = None
    nearest_expiry_str = None
    if future_expiries_all:
        nearest = future_expiries_all[0]
        nearest_expiry_str = nearest.strftime("%d %b").lstrip("0") if hasattr(nearest, "strftime") else str(nearest)
        nearest_opts = [o for o in options if o["expiry"] == nearest]
        strikes_near = sorted(set(o["strike"] for o in nearest_opts))
        min_pain = float("inf")
        for test in strikes_near:
            pain = sum(
                max(0, test - o["strike"]) * o["oi"] if o["type"] == "C"
                else max(0, o["strike"] - test) * o["oi"]
                for o in nearest_opts
            )
            if pain < min_pain:
                min_pain = pain
                max_pain = test

    # ── Price position ───────────────────────────────────────────────────
    flip_position = None
    if gamma_flip is not None:
        flip_position = "above" if spot_price > gamma_flip else "below"

    # ── Distance to key levels ───────────────────────────────────────────
    def pct_dist(level):
        return round((level - spot_price) / spot_price * 100, 2) if level else None

    return {
        "gammaFlip":      gamma_flip,
        "flipPosition":   flip_position,
        "totalGex":       round(total_gex / 1e6, 2),
        "callWall":       call_wall,
        "putWall":        put_wall,
        "callWallDist":   pct_dist(call_wall),
        "putWallDist":    pct_dist(put_wall),
        "maxPain":        max_pain,
        "maxPainDist":    pct_dist(max_pain),
        "nearestExpiry":  nearest_expiry_str,
        "gexByStrike":    gex_bars,
        "gexByExpiry":    gex_by_expiry,
        "expiryList":     expiry_list,
        "zoneAnalysis":   zone_analysis,
        "spotPrice":      spot_price,
    }


def find_walls(clustered: list, threshold_multiplier: float = 3.0) -> list:
    """Find significant liquidity walls (levels with qty >> average)."""
    if not clustered or len(clustered) < 3:
        return []
    avg_qty = sum(c["qty"] for c in clustered) / len(clustered)
    threshold = avg_qty * threshold_multiplier
    return [c for c in clustered if c["qty"] >= threshold]


async def fetch_json(client: httpx.AsyncClient, url: str, params: dict = None) -> Optional[dict]:
    """Fetch JSON from URL with error handling."""
    try:
        r = await client.get(url, params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"Failed to fetch {url}: {e}")
        return None


async def post_json(client: httpx.AsyncClient, url: str, body: dict) -> Optional[dict]:
    """POST JSON and return response."""
    try:
        r = await client.post(url, json=body, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"Failed to POST {url}: {e}")
        return None


async def fetch_depth() -> dict:
    """Fetch and process order book depth."""
    global current_depth, current_depth_ts, last_depth_summary_ts

    now = time.time()
    if current_depth and (now - current_depth_ts) < DEPTH_INTERVAL:
        return current_depth

    async with httpx.AsyncClient() as client:
        raw = await fetch_json(
            client,
            f"{BINANCE_FAPI}/fapi/v1/depth",
            {"symbol": "ETHUSDT", "limit": 1000}
        )

    if not raw or "bids" not in raw:
        return current_depth or {}

    bids_clustered = cluster_levels(raw["bids"], DEPTH_CLUSTER_SIZE, "bid")
    asks_clustered = cluster_levels(raw["asks"], DEPTH_CLUSTER_SIZE, "ask")

    bid_walls = find_walls(bids_clustered)
    ask_walls = find_walls(asks_clustered)

    best_bid = float(raw["bids"][0][0]) if raw["bids"] else 0
    best_ask = float(raw["asks"][0][0]) if raw["asks"] else 0
    mid_price = (best_bid + best_ask) / 2

    range_pct = 0.005  # ±0.5% — 1000 levels with $0.01 tick cover ~$10 range
    low_bound = mid_price * (1 - range_pct)
    high_bound = mid_price * (1 + range_pct)

    bids_filtered = [c for c in bids_clustered if c["price"] >= low_bound]
    asks_filtered = [c for c in asks_clustered if c["price"] <= high_bound]

    total_bid_qty = sum(c["qty"] for c in bids_filtered)
    total_ask_qty = sum(c["qty"] for c in asks_filtered)
    bid_ask_imbalance = None
    if total_bid_qty + total_ask_qty > 0:
        bid_ask_imbalance = (total_bid_qty - total_ask_qty) / (total_bid_qty + total_ask_qty)

    current_depth = {
        "ts": int(now * 1000),
        "midPrice": round(mid_price, 2),
        "spread": round(best_ask - best_bid, 4),
        "bids": bids_filtered[:40],
        "asks": asks_filtered[:40],
        "bidWalls": bid_walls[:10],
        "askWalls": ask_walls[:10],
        "totalBidQty": round(total_bid_qty, 1),
        "totalAskQty": round(total_ask_qty, 1),
        "bidAskImbalance": round(bid_ask_imbalance, 4) if bid_ask_imbalance is not None else None,
    }
    current_depth_ts = now

    if now - last_depth_summary_ts >= DEPTH_SUMMARY_INTERVAL:
        summary = {
            "ts": int(now * 1000),
            "midPrice": current_depth["midPrice"],
            "bidWalls": bid_walls[:5],
            "askWalls": ask_walls[:5],
            "totalBidQty": current_depth["totalBidQty"],
            "totalAskQty": current_depth["totalAskQty"],
            "bidAskImbalance": current_depth["bidAskImbalance"],
        }
        depth_history.append(summary)
        last_depth_summary_ts = now
        logger.info(f"Depth summary saved. History size: {len(depth_history)}")

    return current_depth


async def fetch_all_data() -> dict:
    """Fetch all positioning data from Binance + OKX."""
    global cache, cache_ts

    now = time.time()
    if cache and (now - cache_ts) < CACHE_TTL:
        return cache

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            fetch_json(client, f"{BINANCE_FAPI}/fapi/v1/ticker/24hr", {"symbol": "ETHUSDT"}),
            fetch_json(client, f"{BINANCE_FAPI}/fapi/v1/premiumIndex", {"symbol": "ETHUSDT"}),
            fetch_json(client, f"{BINANCE_FAPI}/fapi/v1/openInterest", {"symbol": "ETHUSDT"}),
            fetch_json(client, f"{BINANCE_FAPI}/futures/data/openInterestHist",
                       {"symbol": "ETHUSDT", "period": "1h", "limit": 48}),
            fetch_json(client, f"{BINANCE_FAPI}/futures/data/globalLongShortAccountRatio",
                       {"symbol": "ETHUSDT", "period": "1h", "limit": 24}),
            fetch_json(client, f"{BINANCE_FAPI}/futures/data/topLongShortPositionRatio",
                       {"symbol": "ETHUSDT", "period": "1h", "limit": 24}),
            fetch_json(client, f"{BINANCE_FAPI}/futures/data/takerlongshortRatio",
                       {"symbol": "ETHUSDT", "period": "1h", "limit": 24}),
            fetch_json(client, f"{BINANCE_FAPI}/fapi/v1/fundingRate",
                       {"symbol": "ETHUSDT", "limit": 30}),
            fetch_json(client, f"{BINANCE_FAPI}/futures/data/takerlongshortRatio",
                       {"symbol": "ETHUSDT", "period": "5m", "limit": 48}),
            fetch_json(client, f"{BINANCE_FAPI}/futures/data/globalLongShortAccountRatio",
                       {"symbol": "ETHUSDT", "period": "5m", "limit": 48}),
            # L/S multi-period: 15m, 4h, 1d
            fetch_json(client, f"{BINANCE_FAPI}/futures/data/globalLongShortAccountRatio",
                       {"symbol": "ETHUSDT", "period": "15m", "limit": 48}),
            fetch_json(client, f"{BINANCE_FAPI}/futures/data/globalLongShortAccountRatio",
                       {"symbol": "ETHUSDT", "period": "4h", "limit": 30}),
            fetch_json(client, f"{BINANCE_FAPI}/futures/data/globalLongShortAccountRatio",
                       {"symbol": "ETHUSDT", "period": "1d", "limit": 30}),
            fetch_json(client, f"{BINANCE_FAPI}/futures/data/topLongShortPositionRatio",
                       {"symbol": "ETHUSDT", "period": "15m", "limit": 48}),
            fetch_json(client, f"{BINANCE_FAPI}/futures/data/topLongShortPositionRatio",
                       {"symbol": "ETHUSDT", "period": "4h", "limit": 30}),
            fetch_json(client, f"{BINANCE_FAPI}/futures/data/topLongShortPositionRatio",
                       {"symbol": "ETHUSDT", "period": "1d", "limit": 30}),
            fetch_json(client, f"{OKX_API}/api/v5/public/funding-rate",
                       {"instId": "ETH-USDT-SWAP"}),
            fetch_json(client, f"{OKX_API}/api/v5/public/open-interest",
                       {"instType": "SWAP", "instId": "ETH-USDT-SWAP"}),
            fetch_json(client, f"{OKX_API}/api/v5/rubik/stat/contracts/long-short-account-ratio",
                       {"ccy": "ETH", "period": "1H"}),
            # 1h klines: 720 for volatility (30 days), 200 for volume profile (8d default)
            fetch_json(client, f"{BINANCE_FAPI}/fapi/v1/klines",
                       {"symbol": "ETHUSDT", "interval": "1h", "limit": 720}),
            fetch_json(client, f"{BINANCE_FAPI}/fapi/v1/klines",
                       {"symbol": "ETHUSDT", "interval": "1h", "limit": 200}),
            # Extra klines for multi-period volume profile (90d)
            fetch_json(client, f"{BINANCE_FAPI}/fapi/v1/klines",
                       {"symbol": "ETHUSDT", "interval": "1h", "limit": 1080}),
            # Spot klines for spot taker flow (5m history + 1h history)
            fetch_json(client, f"{BINANCE_SPOT}/api/v3/klines",
                       {"symbol": "ETHUSDT", "interval": "5m", "limit": 48}),
            fetch_json(client, f"{BINANCE_SPOT}/api/v3/klines",
                       {"symbol": "ETHUSDT", "interval": "1h", "limit": 24}),
            # Perp 5m klines for cumulative flow (24h = 288 candles)
            fetch_json(client, f"{BINANCE_FAPI}/fapi/v1/klines",
                       {"symbol": "ETHUSDT", "interval": "5m", "limit": 288}),
            # Spot 5m klines for cumulative flow (24h = 288 candles)
            fetch_json(client, f"{BINANCE_SPOT}/api/v3/klines",
                       {"symbol": "ETHUSDT", "interval": "5m", "limit": 288}),
            # Deribit ETH options — all active instruments within 60 days
            fetch_json(client, f"{DERIBIT_API}/api/v2/public/get_book_summary_by_currency",
                       {"currency": "ETH", "kind": "option"}),
            # Bybit ETH options (has greeks + OI)
            fetch_json(client, "https://api.bybit.com/v5/market/tickers",
                       {"category": "option", "baseCoin": "ETH"}),
            # OKX ETH options OI
            fetch_json(client, f"{OKX_API}/api/v5/public/open-interest",
                       {"instType": "OPTION", "instFamily": "ETH-USD"}),
            # OKX ETH options IV/greeks
            fetch_json(client, f"{OKX_API}/api/v5/public/opt-summary",
                       {"instFamily": "ETH-USD"}),
            # Extra volume: Binance spot, Bybit perp, OKX perp
            fetch_json(client, f"{BINANCE_SPOT}/api/v3/ticker/24hr",
                       {"symbol": "ETHUSDT"}),
            fetch_json(client, f"{BYBIT_API}/v5/market/tickers",
                       {"category": "linear", "symbol": "ETHUSDT"}),
            fetch_json(client, f"{OKX_API}/api/v5/market/ticker",
                       {"instId": "ETH-USDT-SWAP"}),
            # Bybit funding history
            fetch_json(client, f"{BYBIT_API}/v5/market/funding/history",
                       {"category": "linear", "symbol": "ETHUSDT", "limit": 30}),
            # Hyperliquid: funding, OI, volume
            post_json(client, f"{HYPERLIQUID_API}/info",
                      {"type": "metaAndAssetCtxs"}),
            # ETH/BTC relative strength
            fetch_json(client, f"{BINANCE_SPOT}/api/v3/ticker/24hr",
                       {"symbol": "ETHBTC"}),
            # Bybit OI for liq map
            fetch_json(client, f"{BYBIT_API}/v5/market/open-interest",
                       {"category": "linear", "symbol": "ETHUSDT", "intervalTime": "1h", "limit": 1}),
            # Klines for multi-timeframe stochastics (need ≥450 bars for slow 400,40,10)
            fetch_json(client, f"{BINANCE_FAPI}/fapi/v1/klines",
                       {"symbol": "ETHUSDT", "interval": "1m", "limit": 500}),
            fetch_json(client, f"{BINANCE_FAPI}/fapi/v1/klines",
                       {"symbol": "ETHUSDT", "interval": "15m", "limit": 500}),
            fetch_json(client, f"{BINANCE_FAPI}/fapi/v1/klines",
                       {"symbol": "ETHUSDT", "interval": "4h", "limit": 500}),
            fetch_json(client, f"{BINANCE_FAPI}/fapi/v1/klines",
                       {"symbol": "ETHUSDT", "interval": "5m", "limit": 500}),
            return_exceptions=True,
        )

    (
        bn_ticker, bn_premium, bn_oi, bn_oi_hist,          # 0-3
        bn_ls, bn_top_ls, bn_taker, bn_fund_hist,          # 4-7
        bn_taker_hist, bn_ls_hist,                          # 8-9
        bn_ls_15m, bn_ls_4h, bn_ls_1d,                     # 10-12
        bn_top_ls_15m, bn_top_ls_4h, bn_top_ls_1d,         # 13-15
        okx_fund, okx_oi, okx_ls,                           # 16-18
        bn_klines_vol, bn_klines_vp,                        # 19-20
        bn_klines_90d,                                       # 21
        spot_klines_5m, spot_klines_1h,                     # 22-23
        perp_klines_5m_flow, spot_klines_5m_flow,           # 23-24
        deribit_options_raw,                                 # 25
        bybit_options_raw, okx_options_oi_raw, okx_options_iv_raw,  # 26-28
        bn_spot_ticker, bybit_perp_ticker, okx_perp_ticker, # 29-31
        bybit_funding_hist_raw, hyperliquid_raw,            # 32-33
        ethbtc_ticker, bybit_oi_raw,                         # 34-35
        kl_1m, kl_15m, kl_4h_stoch, kl_5m_stoch,             # 36-39
    ) = results

    def safe_float(obj, key, default=None):
        try:
            return float(obj[key])
        except (TypeError, KeyError, ValueError):
            return default

    def safe_list(obj):
        return obj if isinstance(obj, list) else []

    oi_hist = []
    for d in safe_list(bn_oi_hist):
        try:
            oi_hist.append({
                "ts": d["timestamp"],
                "value": float(d["sumOpenInterestValue"]),
                "oi": float(d["sumOpenInterest"]),
            })
        except (KeyError, ValueError):
            continue

    fund_hist = []
    for d in safe_list(bn_fund_hist):
        try:
            fund_hist.append({"ts": d["fundingTime"], "rate": float(d["fundingRate"])})
        except (KeyError, ValueError):
            continue

    ls_hist = []
    for d in safe_list(bn_ls_hist):
        try:
            ls_hist.append({
                "ts": d["timestamp"],
                "ratio": float(d["longShortRatio"]),
                "longPct": float(d["longAccount"]),
                "shortPct": float(d["shortAccount"]),
            })
        except (KeyError, ValueError):
            continue

    taker_hist = []
    for d in safe_list(bn_taker_hist):
        try:
            taker_hist.append({"ts": d["timestamp"], "ratio": float(d["buySellRatio"])})
        except (KeyError, ValueError):
            continue

    bn_ls_list = safe_list(bn_ls)
    bn_top_ls_list = safe_list(bn_top_ls)
    bn_taker_list = safe_list(bn_taker)

    latest_ls = bn_ls_list[-1] if bn_ls_list else {}
    latest_top_ls = bn_top_ls_list[-1] if bn_top_ls_list else {}
    latest_taker = bn_taker_list[-1] if bn_taker_list else {}

    # Multi-period L/S ratio helper
    def parse_ls_series(raw_data):
        """Parse L/S ratio list into [{ts, ratio, longPct, shortPct}]."""
        out = []
        if not isinstance(raw_data, list):
            return out
        for d in raw_data:
            try:
                if not isinstance(d, dict):
                    continue
                out.append({
                    "ts": d["timestamp"],
                    "ratio": float(d["longShortRatio"]),
                    "longPct": float(d["longAccount"]),
                    "shortPct": float(d["shortAccount"]),
                })
            except (KeyError, ValueError, TypeError):
                continue
        return out

    ls_by_period = {
        "5m": parse_ls_series(bn_ls_hist),   # bn_ls_hist = 5m, 48 candles
        "15m": parse_ls_series(bn_ls_15m),
        "1h": parse_ls_series(bn_ls),
        "4h": parse_ls_series(bn_ls_4h),
        "1d": parse_ls_series(bn_ls_1d),
    }
    top_ls_by_period = {
        "1h": parse_ls_series(bn_top_ls),
        "15m": parse_ls_series(bn_top_ls_15m),
        "4h": parse_ls_series(bn_top_ls_4h),
        "1d": parse_ls_series(bn_top_ls_1d),
    }

    okx_fund_data = okx_fund.get("data", [{}])[0] if isinstance(okx_fund, dict) else {}
    okx_oi_data = okx_oi.get("data", [{}])[0] if isinstance(okx_oi, dict) else {}
    okx_ls_data = okx_ls.get("data", []) if isinstance(okx_ls, dict) else []

    # Bybit funding
    bybit_fund_rate = None
    bybit_fund_hist = []
    if isinstance(bybit_perp_ticker, dict):
        bybit_ticker_list = (bybit_perp_ticker.get("result", {}) or {}).get("list", [])
        if bybit_ticker_list:
            bybit_fund_rate = safe_float(bybit_ticker_list[0], "fundingRate")
    if isinstance(bybit_funding_hist_raw, dict):
        for item in (bybit_funding_hist_raw.get("result", {}) or {}).get("list", []):
            try:
                bybit_fund_hist.append({
                    "ts": int(item["fundingRateTimestamp"]),
                    "rate": float(item["fundingRate"]),
                })
            except (KeyError, ValueError):
                continue
        bybit_fund_hist.reverse()  # oldest first

    # Hyperliquid
    hl_funding = None
    hl_oi = None
    hl_volume = None
    if isinstance(hyperliquid_raw, list) and len(hyperliquid_raw) == 2:
        meta, asset_ctxs = hyperliquid_raw
        # Find ETH by name in meta universe instead of hardcoding index
        eth_idx = None
        if isinstance(meta, dict):
            universe = meta.get("universe", [])
            for i, coin in enumerate(universe):
                if isinstance(coin, dict) and coin.get("name") == "ETH":
                    eth_idx = i
                    break
        if eth_idx is None:
            eth_idx = 1  # fallback
        if isinstance(asset_ctxs, list) and len(asset_ctxs) > eth_idx:
            eth_ctx = asset_ctxs[eth_idx]
            hl_funding = safe_float(eth_ctx, "funding")
            hl_oi_eth = safe_float(eth_ctx, "openInterest")
            hl_mark = safe_float(eth_ctx, "markPx")
            hl_oi = round(hl_oi_eth * hl_mark) if hl_oi_eth and hl_mark else None
            hl_volume = safe_float(eth_ctx, "dayNtlVlm")

    oi_change = None
    if len(oi_hist) >= 2:
        oi_change = ((oi_hist[-1]["value"] - oi_hist[0]["value"]) / oi_hist[0]["value"]) * 100

    retail_ratio = safe_float(latest_ls, "longShortRatio")
    top_ratio = safe_float(latest_top_ls, "longShortRatio")
    ls_divergence = None
    if retail_ratio is not None and top_ratio is not None:
        ls_divergence = round(retail_ratio - top_ratio, 4)

    # Spot taker buy/sell from klines (field[9]=taker_buy_base, field[5]=total_volume)
    def spot_taker_from_klines(klines):
        result = []
        for k in (klines if isinstance(klines, list) else []):
            try:
                total = float(k[5])
                buy   = float(k[9])
                if total <= 0:
                    continue
                sell  = total - buy
                ratio = round(buy / sell, 4) if sell > 0 else None
                result.append({"ts": int(k[0]), "ratio": ratio, "buy": round(buy, 2), "sell": round(sell, 2)})
            except (IndexError, ValueError):
                continue
        return result

    def cumulative_flow(klines, n_candles):
        """Aggregate taker buy/sell from last n_candles. k[1]=open, k[4]=close, k[5]=total_vol, k[9]=taker_buy_base."""
        subset = klines[-n_candles:] if isinstance(klines, list) and len(klines) >= n_candles else (klines or [])
        buy = 0.0
        sell = 0.0
        total_vol = 0.0
        price_open = None
        price_close = None
        for k in subset:
            try:
                total = float(k[5])
                b = float(k[9])
                buy  += b
                sell += max(total - b, 0)
                total_vol += total
                if price_open is None:
                    price_open = float(k[1])
                price_close = float(k[4])
            except (IndexError, ValueError):
                continue
        ratio = round(buy / sell, 4) if sell > 0 else None
        delta = round(buy - sell, 2)
        # Price change and divergence metrics
        price_chg = None
        price_chg_pct = None
        delta_vs_vol = None
        if price_open and price_close:
            price_chg = round(price_close - price_open, 2)
            price_chg_pct = round((price_close - price_open) / price_open * 100, 3)
        if total_vol > 0:
            delta_vs_vol = round(delta / total_vol * 100, 2)  # delta as % of total volume
        return {
            "buy": round(buy, 2), "sell": round(sell, 2), "delta": delta, "ratio": ratio,
            "totalVol": round(total_vol, 2),
            "priceOpen": price_open, "priceClose": price_close,
            "priceChg": price_chg, "priceChgPct": price_chg_pct,
            "deltaVsVol": delta_vs_vol,
        }

    # Windows: 1h=12, 4h=48, 12h=144, 24h=288 candles of 5min
    flow_windows = {"1h": 12, "4h": 48, "12h": 144, "24h": 288}
    perp_flow = {w: cumulative_flow(perp_klines_5m_flow, n) for w, n in flow_windows.items()}
    spot_flow = {w: cumulative_flow(spot_klines_5m_flow, n) for w, n in flow_windows.items()}

    spot_taker_hist_5m = spot_taker_from_klines(spot_klines_5m)
    spot_taker_hist_1h = spot_taker_from_klines(spot_klines_1h)
    # Use last 12 x 5min closed candles (= 1h complete) for spot ratio — avoids partial 1h candle
    latest_spot_taker  = spot_flow.get("1h", {})

    # Volume profile bar chart (±10% range) — multi-period
    current_price = safe_float(bn_ticker, "lastPrice")
    vol_profile = []
    vol_profile_by_period = {}
    if current_price:
        klines_vp_safe = bn_klines_vp if isinstance(bn_klines_vp, list) else []
        klines_vol_safe_bp = bn_klines_vol if isinstance(bn_klines_vol, list) else []
        perp_5m_safe_bp = perp_klines_5m_flow if isinstance(perp_klines_5m_flow, list) else []
        klines_90d_safe_bp = bn_klines_90d if isinstance(bn_klines_90d, list) else []
        # Default 8d chart
        if klines_vp_safe:
            vol_profile = build_volume_profile(klines_vp_safe, cluster_size=5.0, price_center=current_price, range_pct=0.10)
        # Multi-period charts
        period_configs = {
            "4h":  (perp_5m_safe_bp[-48:], 2.0),
            "12h": (perp_5m_safe_bp[-144:], 3.0),
            "24h": (klines_vol_safe_bp[-24:], 5.0),
            "7d":  (klines_vol_safe_bp[-168:], 5.0),
            "30d": (klines_vol_safe_bp, 5.0),
            "45d": (klines_90d_safe_bp, 5.0),
        }
        for p, (kl, cs) in period_configs.items():
            if kl:
                vol_profile_by_period[p] = build_volume_profile(kl, cluster_size=cs, price_center=current_price, range_pct=0.10)

    # Volatility metrics (30 days of 1h klines)
    volatility = calculate_volatility(bn_klines_vol if isinstance(bn_klines_vol, list) else [])

    # Structured volume profile: POC, Value Area, HVN, LVN — multi-period
    klines_vol_safe = bn_klines_vol if isinstance(bn_klines_vol, list) else []
    klines_vp_safe = bn_klines_vp if isinstance(bn_klines_vp, list) else []
    klines_90d_safe = bn_klines_90d if isinstance(bn_klines_90d, list) else []
    perp_5m_safe = perp_klines_5m_flow if isinstance(perp_klines_5m_flow, list) else []
    volume_profile_by_period = {
        "4h": calculate_volume_profile(perp_5m_safe[-48:] if len(perp_5m_safe) >= 48 else perp_5m_safe, cluster_size=2.0),
        "12h": calculate_volume_profile(perp_5m_safe[-144:] if len(perp_5m_safe) >= 144 else perp_5m_safe, cluster_size=3.0),
        "24h": calculate_volume_profile(klines_vol_safe[-24:] if len(klines_vol_safe) >= 24 else klines_vol_safe),
        "7d": calculate_volume_profile(klines_vol_safe[-168:] if len(klines_vol_safe) >= 168 else klines_vol_safe),
        "30d": calculate_volume_profile(klines_vol_safe),  # 720 klines = 30d
        "45d": calculate_volume_profile(klines_90d_safe),  # 1080 klines = 45d (max available)
    }
    # Default (8d) for backward compat
    volume_profile = calculate_volume_profile(klines_vp_safe)

    # ── Merge options from Deribit + Bybit + OKX ────────────────────────
    spot = current_price or 0
    all_instruments = []

    # Deribit
    if isinstance(deribit_options_raw, dict):
        all_instruments.extend(deribit_options_raw.get("result", []) or [])

    # Bybit — normalize to Deribit format
    if isinstance(bybit_options_raw, dict):
        bybit_list = (bybit_options_raw.get("result", {}) or {}).get("list", [])
        for item in bybit_list:
            try:
                sym = item.get("symbol", "")  # ETH-25SEP26-2100-P-USDT
                parts = sym.split("-")
                if len(parts) < 4:
                    continue
                # Reformat to Deribit style: ETH-25SEP26-2100-P
                expiry_str = parts[1]  # 25SEP26 -> need DDMMMYY
                # Bybit format is DDMONYY already (e.g. 25SEP26)
                deribit_name = f"ETH-{expiry_str}-{parts[2]}-{parts[3]}"
                oi = float(item.get("openInterest") or 0)
                mark_iv = float(item.get("markIv") or 0)  # Bybit returns decimal (0.68 = 68%)
                if oi <= 0 or mark_iv <= 0:
                    continue
                all_instruments.append({
                    "instrument_name": deribit_name,
                    "open_interest": oi,
                    "mark_iv": mark_iv * 100,  # convert to Deribit % format for unified processing
                })
            except (ValueError, KeyError, IndexError):
                continue

    # OKX — merge OI + IV from two endpoints
    okx_oi_map = {}
    if isinstance(okx_options_oi_raw, dict):
        for item in (okx_options_oi_raw.get("data", []) or []):
            inst_id = item.get("instId", "")
            oi_val = float(item.get("oi") or 0)
            if oi_val > 0:
                okx_oi_map[inst_id] = oi_val

    if isinstance(okx_options_iv_raw, dict):
        for item in (okx_options_iv_raw.get("data", []) or []):
            try:
                inst_id = item.get("instId", "")  # ETH-USD-260409-1975-P
                if inst_id not in okx_oi_map:
                    continue
                parts = inst_id.split("-")
                if len(parts) < 5:
                    continue
                # Parse: ETH-USD-YYMMDD-STRIKE-TYPE
                date_str = parts[2]  # "260409" = YYMMDD
                expiry_dt = datetime.strptime(date_str, "%y%m%d").replace(tzinfo=timezone.utc)
                # Convert to Deribit format DDMMMYY
                deribit_exp = expiry_dt.strftime("%d%b%y").upper()  # "09APR26"
                strike = parts[3]
                opt_type = parts[4]  # C or P
                deribit_name = f"ETH-{deribit_exp}-{strike}-{opt_type}"
                mark_iv = float(item.get("markVol") or 0)  # OKX returns decimal (0.71)
                if mark_iv <= 0:
                    continue
                all_instruments.append({
                    "instrument_name": deribit_name,
                    "open_interest": okx_oi_map[inst_id],
                    "mark_iv": mark_iv * 100,  # convert to % format
                })
            except (ValueError, KeyError, IndexError):
                continue

    logger.info(f"Options merged: Deribit={len(deribit_options_raw.get('result', []) if isinstance(deribit_options_raw, dict) else [])}, Bybit={len((bybit_options_raw.get('result', {}) or {}).get('list', []) if isinstance(bybit_options_raw, dict) else [])}, OKX={len(okx_oi_map)}, Total={len(all_instruments)}")
    options_analytics = calculate_options_analytics(all_instruments, spot)
    iv_term_structure = calculate_iv_term_structure(all_instruments, spot)

    # ── Combined volume across exchanges ──────────────────────────────
    vol_bn_perp = safe_float(bn_ticker, "quoteVolume") or 0
    vol_bn_spot = safe_float(bn_spot_ticker, "quoteVolume") or 0

    vol_bybit_perp = 0
    if isinstance(bybit_perp_ticker, dict):
        bybit_list_perp = (bybit_perp_ticker.get("result", {}) or {}).get("list", [])
        if bybit_list_perp:
            vol_bybit_perp = float(bybit_list_perp[0].get("turnover24h") or 0)

    vol_okx_perp = 0
    if isinstance(okx_perp_ticker, dict):
        okx_data = (okx_perp_ticker.get("data", []) or [])
        if okx_data:
            # OKX volCcy24h is in ETH, multiply by price
            okx_vol_eth = float(okx_data[0].get("volCcy24h") or 0)
            vol_okx_perp = okx_vol_eth * (spot if spot else 1)

    combined_volume = vol_bn_perp + vol_bn_spot + vol_bybit_perp + vol_okx_perp

    # ── ETH/BTC relative strength ────────────────────────────────────
    ethbtc = {}
    if isinstance(ethbtc_ticker, dict):
        ethbtc = {
            "price": safe_float(ethbtc_ticker, "lastPrice"),
            "change24h": safe_float(ethbtc_ticker, "priceChangePercent"),
            "high24h": safe_float(ethbtc_ticker, "highPrice"),
            "low24h": safe_float(ethbtc_ticker, "lowPrice"),
        }

    # ── Funding spread between exchanges ─────────────────────────────
    bn_fund_rate = safe_float(bn_premium, "lastFundingRate")
    okx_fund_rate = safe_float(okx_fund_data, "fundingRate")
    funding_spread = {}
    rates = {}
    if bn_fund_rate is not None:
        rates["binance"] = bn_fund_rate
    if okx_fund_rate is not None:
        rates["okx"] = okx_fund_rate
    if bybit_fund_rate is not None:
        rates["bybit"] = bybit_fund_rate
    if hl_funding is not None:
        rates["hyperliquid"] = hl_funding
    if len(rates) >= 2:
        vals = list(rates.values())
        funding_spread = {
            "rates": {k: round(v * 100, 6) for k, v in rates.items()},  # as percentage
            "maxSpread": round((max(vals) - min(vals)) * 100, 6),
            "maxExchange": max(rates, key=rates.get),
            "minExchange": min(rates, key=rates.get),
            "mean": round(sum(vals) / len(vals) * 100, 6),
        }

    # ── IV vs RV spread ──────────────────────────────────────────────
    iv_rv_spread = {}
    if iv_term_structure and volatility.get("rv24h") is not None:
        # Find ATM IV from nearest-term options (7-14 DTE ideal)
        short_term_ivs = [x for x in iv_term_structure if 3 <= x["dte"] <= 21]
        if short_term_ivs:
            atm_iv = short_term_ivs[0]["iv"]  # nearest DTE
            rv24h = volatility["rv24h"]
            spread = round(atm_iv - rv24h, 2)
            ratio_iv_rv = round(atm_iv / rv24h, 3) if rv24h > 0 else None
            iv_rv_spread = {
                "impliedVol": atm_iv,
                "realizedVol": rv24h,
                "spread": spread,
                "ratio": ratio_iv_rv,
                "dte": short_term_ivs[0]["dte"],
            }

    # ── Liquidation map — all exchanges ──────────────────────────────
    # Aggregate OI from all exchanges
    bn_oi_val = (safe_float(bn_oi, "openInterest") or 0) * (spot or 1)
    okx_oi_val = (safe_float(okx_oi_data, "oiCcy") or 0) * (spot or 1)
    bybit_oi_val = 0
    if isinstance(bybit_oi_raw, dict):
        oi_list = (bybit_oi_raw.get("result", {}) or {}).get("list", [])
        if oi_list:
            bybit_oi_val = float(oi_list[0].get("openInterest") or 0) * (spot or 1)
    hl_oi_val = hl_oi or 0
    total_oi_usd = bn_oi_val + okx_oi_val + bybit_oi_val + hl_oi_val

    # ── Stochastics multi-timeframe ──────────────────────────────────
    stochastics_data = compute_stochastics_multi({
        "1m":  kl_1m if isinstance(kl_1m, list) else [],
        "5m":  kl_5m_stoch if isinstance(kl_5m_stoch, list) else [],
        "15m": kl_15m if isinstance(kl_15m, list) else [],
        "1h":  bn_klines_vol if isinstance(bn_klines_vol, list) else [],
        "4h":  kl_4h_stoch if isinstance(kl_4h_stoch, list) else [],
    })

    liq_map = estimate_liquidation_map(
        total_oi_usd, spot or 0,
        funding_rate=bn_fund_rate or 0
    )
    liq_map_data = {
        "clusters": liq_map,
        "totalOiUsd": round(total_oi_usd),
        "oiByExchange": {
            "binance": round(bn_oi_val),
            "okx": round(okx_oi_val),
            "bybit": round(bybit_oi_val),
            "hyperliquid": round(hl_oi_val),
        },
        "spotPrice": spot,
    }

    data = {
        "timestamp": int(time.time() * 1000),
        "binance": {
            "price": safe_float(bn_ticker, "lastPrice"),
            "priceChange24h": safe_float(bn_ticker, "priceChangePercent"),
            "volume24h": safe_float(bn_ticker, "quoteVolume"),
            "markPrice": safe_float(bn_premium, "markPrice"),
            "indexPrice": safe_float(bn_premium, "indexPrice"),
            "funding": {
                "rate": safe_float(bn_premium, "lastFundingRate"),
                "nextTime": safe_float(bn_premium, "nextFundingTime"),
                "history": fund_hist,
            },
            "openInterest": {
                "current": safe_float(bn_oi, "openInterest"),
                "history": oi_hist,
                "change48h": oi_change,
            },
            "longShort": {
                "globalRatio": retail_ratio,
                "globalLongPct": safe_float(latest_ls, "longAccount"),
                "globalShortPct": safe_float(latest_ls, "shortAccount"),
                "topTradersRatio": top_ratio,
                "topLongPct": safe_float(latest_top_ls, "longAccount"),
                "topShortPct": safe_float(latest_top_ls, "shortAccount"),
                "divergence": ls_divergence,
                "history": ls_hist,
                "byPeriod": ls_by_period,
                "topByPeriod": top_ls_by_period,
            },
            "takerBuySell": {
                "ratio": safe_float(latest_taker, "buySellRatio"),
                "history": taker_hist,
                "spotRatio": latest_spot_taker.get("ratio"),
                "spotBuy": latest_spot_taker.get("buy"),
                "spotSell": latest_spot_taker.get("sell"),
                "spotDelta": latest_spot_taker.get("delta"),
                "spotHistory5m": [{"ts": d["ts"], "ratio": d["ratio"]} for d in spot_taker_hist_5m],
                "spotHistory1h": [{"ts": d["ts"], "ratio": d["ratio"]} for d in spot_taker_hist_1h],
                "flow": {"perp": perp_flow, "spot": spot_flow},
            },
            "volumeProfile": vol_profile,
            "volumeProfileByPeriod": vol_profile_by_period,
        },
        "okx": {
            "funding": {
                "rate": safe_float(okx_fund_data, "fundingRate"),
                "nextTime": safe_float(okx_fund_data, "nextFundingTime"),
            },
            "openInterest": {
                "oi": safe_float(okx_oi_data, "oi"),
                "oiCcy": safe_float(okx_oi_data, "oiCcy"),
            },
            "longShort": {
                "history": [
                    {"ts": int(d[0]), "ratio": float(d[1])}
                    for d in okx_ls_data[:24]
                ] if okx_ls_data else [],
            },
        },
        "bybit": {
            "funding": {
                "rate": bybit_fund_rate,
                "history": bybit_fund_hist[-30:],
            },
            "openInterest": {
                "oi": safe_float(((bybit_perp_ticker or {}).get("result", {}) or {}).get("list", [{}])[0], "openInterest"),
                "oiValue": safe_float(((bybit_perp_ticker or {}).get("result", {}) or {}).get("list", [{}])[0], "openInterestValue"),
            },
        },
        "hyperliquid": {
            "funding": {
                "rate": hl_funding,
            },
            "openInterest": hl_oi,
            "volume24h": hl_volume,
        },
        "volatility": volatility,
        "volumeProfile": volume_profile,
        "volumeProfileByPeriod": volume_profile_by_period,
        "options": options_analytics,
        "marketVolume": {
            "combined24h": combined_volume + (hl_volume or 0),
            "breakdown": {
                "binancePerp": round(vol_bn_perp),
                "binanceSpot": round(vol_bn_spot),
                "bybitPerp":   round(vol_bybit_perp),
                "okxPerp":     round(vol_okx_perp),
                "hyperliquid": round(hl_volume or 0),
            }
        },
        "ethBtc": ethbtc,
        "fundingSpread": funding_spread,
        "ivRvSpread": iv_rv_spread,
        "ivTermStructure": iv_term_structure,
        "liquidationMap": liq_map_data,
        "stochastics": stochastics_data,
    }

    cache = data
    cache_ts = time.time()
    logger.info("Data refreshed successfully")
    return data


# ── Background task for depth snapshots ───────────────────────────────
async def depth_collector():
    """Background task that collects order book depth periodically."""
    while True:
        try:
            await fetch_depth()
        except Exception as e:
            logger.warning(f"Depth collection error: {e}")
        await asyncio.sleep(DEPTH_INTERVAL)


# ── App ───────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting ETH Positioning Dashboard on port {PORT}")
    try:
        await fetch_all_data()
    except Exception as e:
        logger.warning(f"Cache warm-up failed: {e}")

    task = asyncio.create_task(depth_collector())
    logger.info("Depth collector started")

    yield

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="ETH Positioning Dashboard", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/api/data")
async def get_data():
    return await fetch_all_data()


@app.get("/api/depth")
async def get_depth():
    return current_depth or {}


@app.get("/api/depth/history")
async def get_depth_history():
    return {"snapshots": list(depth_history)}


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "cache_age": round(time.time() - cache_ts, 1) if cache_ts else None,
        "depth_age": round(time.time() - current_depth_ts, 1) if current_depth_ts else None,
        "depth_history_size": len(depth_history),
    }


# Serve frontend static files in production
static_dir = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")
if os.path.isdir(static_dir):
    app.mount("/assets", StaticFiles(directory=os.path.join(static_dir, "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        file_path = os.path.join(static_dir, full_path)
        if os.path.isfile(file_path):
            return FileResponse(file_path)
        return FileResponse(os.path.join(static_dir, "index.html"))
