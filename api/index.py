"""
ETH Positioning Dashboard — Vercel Serverless API
Adapted from backend/main.py for stateless serverless execution.
No background tasks — each request fetches fresh data.
"""

import os
import time
import math
import statistics
import asyncio
import logging
from typing import Optional
from datetime import datetime, timezone, timedelta

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Minimal .env loader (for local dev; on Vercel use dashboard env vars)
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if not os.path.exists(_env_path):
    _env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend", ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

# ── Config ────────────────────────────────────────────────────────────
BINANCE_FAPI = "https://fapi.binance.com"
BINANCE_SPOT = "https://api.binance.com"
OKX_API     = "https://www.okx.com"
DERIBIT_API = "https://www.deribit.com"
BYBIT_API   = "https://api.bybit.com"
HYPERLIQUID_API = "https://api.hyperliquid.xyz"
CACHE_TTL = 15  # seconds — slightly longer for serverless warm instances
DEPTH_CLUSTER_SIZE = 0.25

# Dune Analytics — ETH CEX netflows (query 6984181)
DUNE_API_KEY = os.getenv("DUNE_API_KEY", "")
DUNE_API_KEY_FALLBACK = os.getenv("DUNE_API_KEY_FALLBACK", "")
# Keys are tried in order; on HTTP 402 (datapoint cap exhausted) we rotate to
# the next one and remember which one is dry for ~6h so we don't re-spam it.
DUNE_API_KEYS = [k for k in [DUNE_API_KEY, DUNE_API_KEY_FALLBACK] if k]
DUNE_QUERY_ID = os.getenv("DUNE_QUERY_ID", "6984181")
DUNE_CACHE_TTL = 300  # 5 min — short so we pick up Dune-side re-executions promptly
DUNE_MAX_AGE_HOURS = 1  # trigger fresh execution if Dune's last result is older than 1h
DUNE_PAGE_LIMIT = 200  # rows per /results call. 200 × 7 cols = 1400 datapoints, under Dune per-request cap.
DUNE_KEY_EXHAUSTED_BACKOFF_S = 6 * 3600  # remember a 402'd key for 6h before retrying it
# (Vercel uses fire-and-forget for the fresh-execution trigger; see _dune_trigger_fire_and_forget.)
_dune_key_exhausted_until: dict = {}  # key -> epoch_ts when we'll consider it again

# DefiLlama — CEX ETH reserves (absolute stock)
DEFILLAMA_CEX_SLUGS = {
    "Binance":    "binance-cex",
    "OKX":        "okx",
    "Bybit":      "bybit",
    "Bitfinex":   "bitfinex",
    "KuCoin":     "kucoin",
    "Gate.io":    "gate",
    "Crypto.com": "crypto-com",
}
DEFILLAMA_CACHE_TTL = 3600  # 1h — reserves change slowly
DEFILLAMA_ETH_KEYS = {"ETH", "WETH", "STETH", "BETH", "CBETH", "EETH", "WEETH", "RETH"}
DEFILLAMA_STABLE_KEYS = {"USDT", "USDC", "DAI", "FDUSD", "BUSD", "TUSD", "USDD", "PYUSD", "USDE"}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dashboard")

# ── Warm-instance cache (survives between invocations on same lambda) ─
cache: dict = {}
cache_ts: float = 0
depth_cache: dict = {}
depth_cache_ts: float = 0
dune_cache: dict = {}
dune_cache_ts: float = 0
llama_cache: dict = {}
llama_cache_ts: float = 0


def cluster_levels(levels: list, cluster_size: float, side: str = "bid") -> list:
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

    hvn = [{"price": p, "volume": round(v, 0)} for p, v in sorted_levels[:5]]
    lvn = sorted(
        [{"price": p, "volume": round(v, 0)} for p, v in sorted_levels if v < avg_volume * 0.3],
        key=lambda x: x["price"], reverse=True
    )[:5]

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


def stochastic(klines: list, k_period: int, k_smooth: int, d_smooth: int, history_len: int = 300) -> Optional[dict]:
    """Stochastic oscillator with K smoothing. Returns {k, d, kHistory, dHistory} or None."""
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
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        pdf_d1 = math.exp(-0.5 * d1 ** 2) / math.sqrt(2 * math.pi)
        return pdf_d1 / (S * sigma * math.sqrt(T))
    except (ValueError, ZeroDivisionError):
        return 0.0


def calculate_iv_term_structure(instruments: list, spot_price: float) -> list:
    if not instruments or not spot_price:
        return []
    now = datetime.now(timezone.utc)
    dte_iv: dict = {}
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
        moneyness = abs(strike - spot_price) / spot_price
        if moneyness > 0.20:
            continue
        weight = max(1 - moneyness * 5, 0.1) * oi
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
    if not oi_value or not spot_price or spot_price <= 0:
        return []
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
        long_liq_price = round(spot_price * (1 - 0.8 / lev), 2)
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


def compute_money_quality(
    oi_hist: list,
    perp_flow: dict,
    funding_rate: Optional[float] = None,
    klines_1h: Optional[list] = None,
) -> dict:
    """Classify movement quality: new money vs short covering via OI/Price ratio.
    Intraday windows use perp_flow; multi-day windows source price from klines_1h."""
    if not oi_hist or len(oi_hist) < 2:
        return {}
    current_oi = oi_hist[-1].get("oi")
    if current_oi is None:
        return {}
    windows = {
        "1h":  1,
        "4h":  4,
        "12h": 12,
        "24h": 24,
        "3d":  72,
        "7d":  168,
        "14d": 336,
    }
    long_windows = {"3d", "7d", "14d"}

    def price_chg_from_klines(hours: int) -> Optional[float]:
        if not klines_1h or len(klines_1h) <= hours:
            return None
        try:
            price_now  = float(klines_1h[-1][4])
            price_past = float(klines_1h[-1 - hours][4])
            if price_past <= 0:
                return None
            return round((price_now - price_past) / price_past * 100, 3)
        except (IndexError, ValueError, TypeError):
            return None

    by_window: dict = {}
    for wname, hours in windows.items():
        if len(oi_hist) <= hours:
            continue
        past_oi = oi_hist[-1 - hours].get("oi")
        if past_oi is None or past_oi <= 0:
            continue
        oi_delta = current_oi - past_oi
        oi_delta_pct = (oi_delta / past_oi) * 100
        if wname in long_windows:
            price_chg_pct = price_chg_from_klines(hours)
            delta_vs_vol = None
            taker_ratio = None
        else:
            flow = (perp_flow or {}).get(wname, {}) or {}
            price_chg_pct = flow.get("priceChgPct")
            delta_vs_vol = flow.get("deltaVsVol")
            taker_ratio = flow.get("ratio")
        if price_chg_pct is None:
            continue
        ratio = None
        if abs(oi_delta_pct) > 0.01:
            ratio = abs(price_chg_pct) / abs(oi_delta_pct)
        price_up = price_chg_pct > 0.05
        price_dn = price_chg_pct < -0.05
        oi_up = oi_delta_pct > 0.10
        oi_dn = oi_delta_pct < -0.10
        label = "Sin actividad"
        direction = "neutral"
        quality = "low"
        if price_up and oi_up:
            direction = "bullish"
            if ratio is not None and ratio < 1:
                label, quality = "Acumulación real", "high"
            elif ratio is not None and ratio < 2:
                label, quality = "Balanceado (longs mixto)", "medium"
            elif ratio is not None and ratio < 5:
                label, quality = "Covering dominante", "low"
            else:
                label, quality = "Squeeze puro", "low"
        elif price_up and not oi_up and not oi_dn:
            label, direction, quality = "Short covering (OI plano)", "bullish", "low"
        elif price_up and oi_dn:
            label, direction, quality = "Distribución arriba (OI cae)", "neutral", "low"
        elif price_dn and oi_up:
            direction = "bearish"
            if ratio is not None and ratio < 1:
                label, quality = "Distribución real", "high"
            elif ratio is not None and ratio < 2:
                label, quality = "Balanceado (shorts mixto)", "medium"
            elif ratio is not None and ratio < 5:
                label, quality = "Liquidation dominante", "low"
            else:
                label, quality = "Long squeeze puro", "low"
        elif price_dn and not oi_up and not oi_dn:
            label, direction, quality = "Long cerrando (OI plano)", "bearish", "low"
        elif price_dn and oi_dn:
            label, direction, quality = "Long capitulation", "bearish", "high"
        elif not price_up and not price_dn:
            if oi_up:
                label, direction, quality = "Build-up (lateral)", "neutral", "medium"
            elif oi_dn:
                label, direction, quality = "Deleverage (lateral)", "neutral", "medium"
        by_window[wname] = {
            "oiDelta": round(oi_delta, 2),
            "oiDeltaPct": round(oi_delta_pct, 3),
            "priceChgPct": round(price_chg_pct, 3),
            "ratio": round(ratio, 2) if ratio is not None else None,
            "label": label,
            "direction": direction,
            "quality": quality,
            "deltaVsVol": delta_vs_vol,
            "takerRatio": taker_ratio,
        }
    weights = {
        "1h":  1.0,
        "4h":  2.0,
        "12h": 2.0,
        "24h": 1.5,
        "3d":  1.2,
        "7d":  1.0,
        "14d": 0.8,
    }
    score = 0.0
    total_w = 0.0
    for w, info in by_window.items():
        ww = weights.get(w, 1.0)
        sign = 1 if info["direction"] == "bullish" else (-1 if info["direction"] == "bearish" else 0)
        q_mult = {"high": 1.0, "medium": 0.6, "low": 0.3}.get(info["quality"], 0.3)
        score += sign * ww * q_mult
        total_w += ww
    norm_score = score / total_w if total_w > 0 else 0
    if norm_score > 0.4:
        verdict = "ALCISTA con plata nueva"
    elif norm_score > 0.15:
        verdict = "Alcista débil / covering"
    elif norm_score < -0.4:
        verdict = "BAJISTA con plata nueva"
    elif norm_score < -0.15:
        verdict = "Bajista débil / covering"
    else:
        verdict = "Lateral / rotación"
    funding_context = None
    if funding_rate is not None:
        if funding_rate < -0.00005:
            funding_context = "Funding negativo — shorts cargados"
        elif funding_rate > 0.00008:
            funding_context = "Funding positivo — longs cargados"
        else:
            funding_context = "Funding neutro"
    return {
        "byWindow": by_window,
        "verdict": verdict,
        "score": round(norm_score, 3),
        "fundingContext": funding_context,
    }


def compute_cut_anchored_mq(
    klines_by_tf: dict,
    oi_hist_1h: list,
    oi_hist_5m: list,
) -> dict:
    """
    For each stoch TF, find the most recent bar where Fast %K (100,10) crossed
    INTO the OB (≥80) or OS (≤20) zone, and compute OI/Price evolution from
    that anchor to now. Returns dict tf -> {direction, anchorBars, ratio,
    label, quality, ...}.
    """
    tf_minutes = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240}
    K_PERIOD = 100
    K_SMOOTH = 10
    OI_FLAT_THRESHOLD = 0.10
    out: dict = {}
    if not klines_by_tf:
        return out

    def lookup_oi_pair(oi_series: list, minutes_back: int, granularity_min: int):
        if not oi_series or len(oi_series) < 2:
            return None, None
        steps_back = max(0, round(minutes_back / granularity_min))
        now_idx = len(oi_series) - 1
        past_idx = now_idx - steps_back
        if past_idx < 0:
            return None, None
        try:
            return float(oi_series[past_idx]["oi"]), float(oi_series[now_idx]["oi"])
        except (KeyError, ValueError, TypeError):
            return None, None

    for tf, klines in klines_by_tf.items():
        if tf not in tf_minutes or not klines:
            continue
        n = len(klines)
        if n < K_PERIOD + K_SMOOTH:
            continue
        try:
            highs  = [float(k[2]) for k in klines]
            lows   = [float(k[3]) for k in klines]
            closes = [float(k[4]) for k in klines]
            ts     = [int(k[0])   for k in klines]
        except (IndexError, ValueError, TypeError):
            continue

        raw_k = [None] * n
        for i in range(K_PERIOD - 1, n):
            wh = max(highs[i - K_PERIOD + 1: i + 1])
            wl = min(lows[i - K_PERIOD + 1: i + 1])
            rng = wh - wl
            raw_k[i] = 50.0 if rng == 0 else (closes[i] - wl) / rng * 100
        smooth_k: list = [None] * n
        for i in range(n):
            start = i - K_SMOOTH + 1
            if start < 0:
                continue
            window = raw_k[start: i + 1]
            if any(v is None for v in window):
                continue
            smooth_k[i] = sum(window) / K_SMOOTH

        latest_k = smooth_k[-1]
        if latest_k is None:
            continue
        if latest_k >= 80:
            target_dir = "short"
            in_zone = lambda v: v is not None and v >= 80
        elif latest_k <= 20:
            target_dir = "long"
            in_zone = lambda v: v is not None and v <= 20
        else:
            continue

        anchor_idx = None
        anchor_capped = False
        for i in range(n - 1, 0, -1):
            if not in_zone(smooth_k[i]):
                break
            if not in_zone(smooth_k[i - 1]):
                anchor_idx = i - 1
                break
        if anchor_idx is None:
            for i in range(n):
                if smooth_k[i] is not None:
                    anchor_idx = i
                    anchor_capped = True
                    break
            if anchor_idx is None:
                continue

        anchor_bars = (n - 1) - anchor_idx
        anchor_ts = ts[anchor_idx]
        anchor_price = closes[anchor_idx]
        current_price = closes[-1]
        if anchor_price <= 0:
            continue
        price_chg_pct = (current_price - anchor_price) / anchor_price * 100

        anchor_minutes_back = anchor_bars * tf_minutes[tf]
        oi_source = None
        anchor_oi = None
        current_oi = None
        if tf in ("1m", "5m", "15m"):
            anchor_oi, current_oi = lookup_oi_pair(oi_hist_5m, anchor_minutes_back, 5)
            oi_source = "5m"
            if anchor_oi is None:
                anchor_oi, current_oi = lookup_oi_pair(oi_hist_1h, anchor_minutes_back, 60)
                oi_source = "1h-fallback"
        else:
            anchor_oi, current_oi = lookup_oi_pair(oi_hist_1h, anchor_minutes_back, 60)
            oi_source = "1h"

        if anchor_oi is None or current_oi is None or anchor_oi <= 0:
            out[tf] = {
                "direction":      target_dir,
                "anchorBars":     anchor_bars,
                "anchorTs":       anchor_ts,
                "anchorIsCapped": anchor_capped,
                "priceChgPct":    round(price_chg_pct, 3),
                "oiDelta":        None,
                "oiDeltaPct":     None,
                "ratio":          None,
                "label":          "OI no disponible para el corte",
                "quality":        None,
                "oiSource":       oi_source,
            }
            continue

        oi_delta = current_oi - anchor_oi
        oi_delta_pct = (oi_delta / anchor_oi) * 100
        ratio = abs(price_chg_pct) / abs(oi_delta_pct) if abs(oi_delta_pct) > 0.01 else None

        oi_up   = oi_delta_pct >  OI_FLAT_THRESHOLD
        oi_dn   = oi_delta_pct < -OI_FLAT_THRESHOLD
        oi_flat = not oi_up and not oi_dn

        label = "Sin clasificar"
        quality = "neutral"
        if target_dir == "short":
            if oi_up:
                if ratio is not None and ratio < 1:
                    label, quality = "Acumulación real (longs nuevos)", "block"
                elif ratio is not None and ratio < 2:
                    label, quality = "Balanceado (longs mixto)", "neutral"
                elif ratio is not None and ratio < 5:
                    label, quality = "Covering dominante", "upgrade-mid"
                else:
                    label, quality = "Squeeze puro", "upgrade-high"
            elif oi_flat:
                label, quality = "Short covering (OI plano)", "upgrade-mid"
            else:
                label, quality = "Distribución arriba (OI cae)", "upgrade-high"
        else:
            if oi_up:
                if ratio is not None and ratio < 1:
                    label, quality = "Distribución real (shorts nuevos)", "block"
                elif ratio is not None and ratio < 2:
                    label, quality = "Balanceado (shorts mixto)", "neutral"
                elif ratio is not None and ratio < 5:
                    label, quality = "Liquidation dominante", "upgrade-mid"
                else:
                    label, quality = "Long squeeze puro", "upgrade-high"
            elif oi_flat:
                label, quality = "Long cerrando (OI plano)", "upgrade-mid"
            else:
                label, quality = "Long capitulation", "upgrade-high"

        out[tf] = {
            "direction":      target_dir,
            "anchorBars":     anchor_bars,
            "anchorTs":       anchor_ts,
            "anchorIsCapped": anchor_capped,
            "priceChgPct":    round(price_chg_pct, 3),
            "oiDelta":        round(oi_delta, 2),
            "oiDeltaPct":     round(oi_delta_pct, 3),
            "ratio":          round(ratio, 2) if ratio is not None else None,
            "label":          label,
            "quality":        quality,
            "oiSource":       oi_source,
        }

    return out


def calculate_options_analytics(instruments: list, spot_price: float) -> dict:
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
            opt_type = parts[3]
        except (ValueError, IndexError):
            continue
        days_to_expiry = (expiry - now).days
        if days_to_expiry < 0 or days_to_expiry > 60:
            continue
        oi = float(inst.get("open_interest") or 0)
        if oi <= 0:
            continue
        mark_iv = float(inst.get("mark_iv") or 0) / 100.0
        T = days_to_expiry / 365.0
        gamma = bs_gamma(spot_price, strike, T, mark_iv) if mark_iv > 0 else 0.0
        options.append({
            "strike": strike, "type": opt_type, "expiry": expiry,
            "oi": oi, "gamma": gamma, "dte": days_to_expiry,
        })

    if not options:
        return {}

    gex_map: dict = {}
    oi_map:  dict = {}
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

    gamma_flip = None
    sorted_strikes = sorted(gex_map.keys())
    for i in range(1, len(sorted_strikes)):
        prev_s = sorted_strikes[i - 1]
        curr_s = sorted_strikes[i]
        prev_gex = gex_map[prev_s]
        curr_gex = gex_map[curr_s]
        if prev_gex < 0 and curr_gex >= 0:
            span = curr_gex - prev_gex
            if span > 0:
                gamma_flip = round(prev_s + (curr_s - prev_s) * (-prev_gex / span), 0)
            else:
                gamma_flip = curr_s
            break
    if gamma_flip is None:
        if all(gex_map[s] < 0 for s in sorted_strikes):
            gamma_flip = sorted_strikes[-1] if sorted_strikes else None
        elif all(gex_map[s] >= 0 for s in sorted_strikes):
            gamma_flip = sorted_strikes[0] if sorted_strikes else None

    nearby = {s: oi_map[s] for s in gex_map if spot_price * 0.80 <= s <= spot_price * 1.20}
    above  = {s: v for s, v in nearby.items() if s > spot_price}
    below  = {s: v for s, v in nearby.items() if s < spot_price}

    call_wall = max(above, key=lambda s: above[s]["call"]) if above else None
    put_wall  = max(below, key=lambda s: below[s]["put"])  if below else None

    bucket = 50.0
    def build_gex_bars(opts_subset):
        bmap: dict = {}
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

    flip_position = None
    if gamma_flip is not None:
        flip_position = "above" if spot_price > gamma_flip else "below"

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
    if not clustered or len(clustered) < 3:
        return []
    avg_qty = sum(c["qty"] for c in clustered) / len(clustered)
    threshold = avg_qty * threshold_multiplier
    return [c for c in clustered if c["qty"] >= threshold]


async def fetch_json(client: httpx.AsyncClient, url: str, params: dict = None) -> Optional[dict]:
    try:
        r = await client.get(url, params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"Failed to fetch {url}: {e}")
        return None


async def post_json(client: httpx.AsyncClient, url: str, body: dict) -> Optional[dict]:
    try:
        r = await client.post(url, json=body, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"Failed to POST {url}: {e}")
        return None


async def fetch_depth_data() -> dict:
    """Fetch order book depth on-demand (no background collector in serverless)."""
    global depth_cache, depth_cache_ts
    now = time.time()
    if depth_cache and (now - depth_cache_ts) < 5:
        return depth_cache

    async with httpx.AsyncClient() as client:
        raw = await fetch_json(
            client,
            f"{BINANCE_FAPI}/fapi/v1/depth",
            {"symbol": "ETHUSDT", "limit": 1000}
        )

    if not raw or "bids" not in raw:
        return depth_cache or {}

    bids_clustered = cluster_levels(raw["bids"], DEPTH_CLUSTER_SIZE, "bid")
    asks_clustered = cluster_levels(raw["asks"], DEPTH_CLUSTER_SIZE, "ask")
    bid_walls = find_walls(bids_clustered)
    ask_walls = find_walls(asks_clustered)

    best_bid = float(raw["bids"][0][0]) if raw["bids"] else 0
    best_ask = float(raw["asks"][0][0]) if raw["asks"] else 0
    mid_price = (best_bid + best_ask) / 2

    range_pct = 0.005
    low_bound = mid_price * (1 - range_pct)
    high_bound = mid_price * (1 + range_pct)

    bids_filtered = [c for c in bids_clustered if c["price"] >= low_bound]
    asks_filtered = [c for c in asks_clustered if c["price"] <= high_bound]

    total_bid_qty = sum(c["qty"] for c in bids_filtered)
    total_ask_qty = sum(c["qty"] for c in asks_filtered)
    bid_ask_imbalance = None
    if total_bid_qty + total_ask_qty > 0:
        bid_ask_imbalance = (total_bid_qty - total_ask_qty) / (total_bid_qty + total_ask_qty)

    depth_cache = {
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
    depth_cache_ts = now
    return depth_cache


async def _dune_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    timeout: float = 30.0,
) -> Optional[httpx.Response]:
    """HTTP request to Dune with key rotation on HTTP 402.

    Tries each non-empty key in DUNE_API_KEYS in order. If a key is currently
    inside its exhausted-backoff window, it's skipped. On 402, marks the key
    as exhausted (DUNE_KEY_EXHAUSTED_BACKOFF_S) and tries the next.

    Returns the first successful Response, or None if every key is either
    backed-off or returned 402. Non-402 errors are raised so the caller can
    distinguish quota issues from real failures.
    """
    if not DUNE_API_KEYS:
        return None
    # Backoff is scoped to (key, method) — a key that 402'd on POST /execute
    # (write quota) might still serve GET /results (already-cached read).
    now = time.time()
    last_402: Optional[httpx.Response] = None
    for key in DUNE_API_KEYS:
        backoff_key = (key, method.upper())
        if _dune_key_exhausted_until.get(backoff_key, 0) > now:
            continue
        try:
            resp = await client.request(
                method, url,
                headers={"X-DUNE-API-KEY": key},
                timeout=timeout,
            )
        except httpx.HTTPError as e:
            logger.warning(f"Dune {method} {url.split('?')[0]} network error on key …{key[-6:]}: {e}")
            raise
        if resp.status_code == 402:
            _dune_key_exhausted_until[backoff_key] = now + DUNE_KEY_EXHAUSTED_BACKOFF_S
            logger.warning(
                f"Dune key …{key[-6:]} 402 on {method} {url.split('?')[0]}; "
                f"backed-off ({method}) until ts={int(_dune_key_exhausted_until[backoff_key])}"
            )
            last_402 = resp
            continue
        resp.raise_for_status()
        return resp
    if last_402 is not None:
        logger.warning(f"Dune: all keys 402 / in backoff for {method} {url.split('?')[0]}")
    return None


async def _dune_trigger_fire_and_forget(client: httpx.AsyncClient) -> None:
    """Kick off a fresh Dune execution without waiting for it to finish.

    Serverless caveat: polling Dune (30–60s typical run time) blocks the Lambda
    and can push past Vercel's maxDuration budget once you add all the other
    parallel fetches. Instead we POST /execute with a short timeout — Dune
    accepts the trigger in ~1s — and return. The *next* request (within
    DUNE_CACHE_TTL) picks up the new result via GET /results. Trade-off: the
    current request still serves slightly-stale data; freshness catches up one
    cycle later instead of same-request.
    """
    if not DUNE_API_KEYS:
        return
    try:
        resp = await _dune_request(
            client, "POST",
            f"https://api.dune.com/api/v1/query/{DUNE_QUERY_ID}/execute",
            timeout=5.0,
        )
        if resp is None:
            return  # all keys exhausted; quota-warning already logged
        execution_id = resp.json().get("execution_id", "")
        logger.info(f"Dune fresh execution fired (id={execution_id}) — will land in next request")
    except Exception as e:
        logger.warning(f"Dune trigger fire failed: {e}")


async def fetch_dune_cex_netflows(client: httpx.AsyncClient) -> Optional[dict]:
    """Fetch ETH CEX netflows from Dune Analytics, paginating to stay under per-request cap.

    Strategy (serverless):
      1. Return in-memory cache if fresh (< DUNE_CACHE_TTL).
      2. GET /query/{id}/results?limit=DUNE_PAGE_LIMIT — first page also returns
         total_row_count + execution_id. Without `limit`, large queries return 402
         "datapoint limit per billing cycle" because the full payload exceeds the cap.
      3. Fan-out the remaining pages in parallel via /execution/{id}/results?offset=N.
      4. Merge rows into one combined payload with the original metadata shape.
      5. If Dune's cached results are older than DUNE_MAX_AGE_HOURS,
         FIRE (don't await) a POST /execute so the next request lands fresher.
    """
    global dune_cache, dune_cache_ts
    if not DUNE_API_KEYS:
        return None
    now = time.time()
    if dune_cache and (now - dune_cache_ts) < DUNE_CACHE_TTL:
        return dune_cache

    try:
        # Page 1 — gives us execution_id, total_row_count, first DUNE_PAGE_LIMIT rows.
        url = f"https://api.dune.com/api/v1/query/{DUNE_QUERY_ID}/results?limit={DUNE_PAGE_LIMIT}"
        resp = await _dune_request(client, "GET", url, timeout=30.0)
        if resp is None:
            return None  # all keys exhausted
        data = resp.json()

        exec_id = data.get("execution_id")
        meta = (data.get("result") or {}).get("metadata") or {}
        total_rows = meta.get("total_row_count", 0)
        rows = list((data.get("result") or {}).get("rows") or [])

        # Fetch remaining pages in parallel (rotates keys per-request via _dune_request).
        if exec_id and total_rows > len(rows):
            offsets = list(range(len(rows), total_rows, DUNE_PAGE_LIMIT))
            async def _fetch_page(off: int) -> list:
                pg_url = f"https://api.dune.com/api/v1/execution/{exec_id}/results?limit={DUNE_PAGE_LIMIT}&offset={off}"
                pg_resp = await _dune_request(client, "GET", pg_url, timeout=30.0)
                if pg_resp is None:
                    return []
                return ((pg_resp.json().get("result") or {}).get("rows") or [])
            extra = await asyncio.gather(*[_fetch_page(o) for o in offsets], return_exceptions=True)
            for chunk in extra:
                if isinstance(chunk, list):
                    rows.extend(chunk)

        # Reshape so process_dune_netflows sees the full row set under data["result"]["rows"].
        data["result"]["rows"] = rows
        data["result"]["metadata"]["row_count"] = len(rows)

        # Check how old the execution is
        exec_ended = data.get("execution_ended_at", "")
        is_stale = False
        if exec_ended:
            try:
                ended_dt = datetime.fromisoformat(exec_ended.replace("Z", "+00:00"))
                age_hours = (datetime.now(timezone.utc) - ended_dt).total_seconds() / 3600
                logger.info(f"Dune cached result age: {age_hours:.1f}h (max {DUNE_MAX_AGE_HOURS}h)")
                if age_hours > DUNE_MAX_AGE_HOURS:
                    is_stale = True
            except Exception:
                is_stale = True
        else:
            is_stale = True

        if is_stale:
            logger.info("Dune results stale — firing fresh execution (next request will pick it up)")
            await _dune_trigger_fire_and_forget(client)

        dune_cache = data
        dune_cache_ts = now
        logger.info(f"Dune fetch ok: {len(rows)} rows (paginated, total reported {total_rows})")
        return data
    except Exception as e:
        logger.warning(f"Dune fetch failed: {e}")
        return None


async def fetch_defillama_reserves(client: httpx.AsyncClient) -> Optional[dict]:
    """Fetch current ETH reserves + historical averages from DefiLlama."""
    global llama_cache, llama_cache_ts
    now_ts = time.time()
    if llama_cache and (now_ts - llama_cache_ts) < DEFILLAMA_CACHE_TTL:
        return llama_cache

    now_dt = datetime.now(timezone.utc)

    async def _fetch_one(name: str, slug: str) -> Optional[dict]:
        try:
            resp = await client.get(
                f"https://api.llama.fi/protocol/{slug}",
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
            tokens_arr = data.get("tokens", [])
            if not tokens_arr:
                return None

            latest_tokens = tokens_arr[-1].get("tokens", {})
            current_eth = sum(
                v for k, v in latest_tokens.items()
                if k.upper() in DEFILLAMA_ETH_KEYS
            )
            current_stable = sum(
                v for k, v in latest_tokens.items()
                if k.upper() in DEFILLAMA_STABLE_KEYS
            )

            daily: list = []
            for entry in tokens_arr:
                ts = entry.get("date", 0)
                entry_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                age_days = (now_dt - entry_dt).days
                if age_days <= 90:
                    toks = entry.get("tokens", {})
                    eth = sum(v for k, v in toks.items() if k.upper() in DEFILLAMA_ETH_KEYS)
                    stable = sum(v for k, v in toks.items() if k.upper() in DEFILLAMA_STABLE_KEYS)
                    daily.append({"date": ts, "eth": eth, "stable": stable, "age_days": age_days})

            return {"name": name, "ethReserve": round(current_eth, 2), "stableReserve": round(current_stable, 2), "daily": daily}
        except Exception as e:
            logger.warning(f"DefiLlama fetch failed for {name}: {e}")
        return None

    tasks = [_fetch_one(name, slug) for name, slug in DEFILLAMA_CEX_SLUGS.items()]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)
    exchange_results = [r for r in raw_results if isinstance(r, dict)]

    if not exchange_results:
        return None

    by_exchange = [
        {"name": r["name"], "ethReserve": r["ethReserve"], "stableReserve": r.get("stableReserve", 0)}
        for r in exchange_results
    ]
    total_eth = sum(ex["ethReserve"] for ex in by_exchange)
    total_stable = sum(ex["stableReserve"] for ex in by_exchange)

    n_exchanges = len(exchange_results)
    day_sums: dict = {}
    for exr in exchange_results:
        seen_dates: set = set()
        for d in exr.get("daily", []):
            date_str = datetime.fromtimestamp(d["date"], tz=timezone.utc).strftime("%Y-%m-%d")
            if date_str in seen_dates:
                continue
            seen_dates.add(date_str)
            if date_str not in day_sums:
                day_sums[date_str] = {"eth": 0.0, "stable": 0.0, "n": 0}
            day_sums[date_str]["eth"] += d["eth"]
            day_sums[date_str]["stable"] += d.get("stable", 0)
            day_sums[date_str]["n"] += 1

    min_exchanges = max(1, n_exchanges - 1)
    complete_days = sorted(
        [(dt_str, info["eth"], info["stable"]) for dt_str, info in day_sums.items()
         if info["n"] >= min_exchanges]
    )

    history_eth: dict = {}
    history_stable: dict = {}
    for window in (7, 30, 90):
        cutoff_str = (now_dt - timedelta(days=window)).strftime("%Y-%m-%d")
        eth_subset = [eth for dt_str, eth, stb in complete_days if dt_str >= cutoff_str]
        stb_subset = [stb for dt_str, eth, stb in complete_days if dt_str >= cutoff_str]
        if eth_subset:
            avg = sum(eth_subset) / len(eth_subset)
            delta_pct = (total_eth - avg) / avg * 100 if avg > 0 else 0
            history_eth[f"{window}d"] = {
                "avgEth": round(avg, 0),
                "currentVsAvgPct": round(delta_pct, 2),
                "samples": len(eth_subset),
            }
        if stb_subset:
            avg_s = sum(stb_subset) / len(stb_subset)
            delta_pct_s = (total_stable - avg_s) / avg_s * 100 if avg_s > 0 else 0
            history_stable[f"{window}d"] = {
                "avgUsd": round(avg_s, 0),
                "currentVsAvgPct": round(delta_pct_s, 2),
                "samples": len(stb_subset),
            }

    result = {
        "totalEth": round(total_eth, 2),
        "totalStable": round(total_stable, 2),
        "byExchange": sorted(by_exchange, key=lambda x: -x["ethReserve"]),
        "exchangeCount": len(exchange_results),
        "history": history_eth,
        "stableHistory": history_stable,
    }
    llama_cache = result
    llama_cache_ts = now_ts
    logger.info(f"DefiLlama reserves ok: {total_eth:,.0f} ETH + ${total_stable:,.0f} stables across {len(exchange_results)} exchanges")
    return result


# ── ETHBTC Taker Rotation ────────────────────────────────────────────
async def fetch_ethbtc_taker(client: httpx.AsyncClient) -> Optional[dict]:
    """Fetch ETHBTC spot taker buy/sell ratio from klines."""
    try:
        resp = await client.get(
            f"{BINANCE_SPOT}/api/v3/klines",
            params={"symbol": "ETHBTC", "interval": "1h", "limit": 168},
            timeout=10.0,
        )
        resp.raise_for_status()
        klines = resp.json()
        if not klines:
            return None

        hours = []
        for k in klines:
            total_vol = float(k[5])
            taker_buy = float(k[9])
            if total_vol > 0:
                hours.append({
                    "ts": int(k[0]),
                    "ratio": round(taker_buy / total_vol, 4),
                    "volume": round(total_vol, 2),
                    "close": float(k[4]),
                })

        if not hours:
            return None

        current = hours[-1]["ratio"]
        last_24 = hours[-24:] if len(hours) >= 24 else hours
        avg_24h = sum(h["ratio"] for h in last_24) / len(last_24)
        avg_7d = sum(h["ratio"] for h in hours) / len(hours)
        price_chg = (hours[-1]["close"] - hours[0]["close"]) / hours[0]["close"] * 100 if hours[0]["close"] > 0 else 0

        if avg_24h > 0.54:
            signal = "BTC_TO_ETH"
        elif avg_24h < 0.46:
            signal = "ETH_TO_BTC"
        else:
            signal = "BALANCED"

        vol_24h_eth = sum(h["volume"] for h in last_24)

        return {
            "currentRatio": round(current, 4),
            "avg24h": round(avg_24h, 4),
            "avg7d": round(avg_7d, 4),
            "priceChange7dPct": round(price_chg, 2),
            "currentPrice": hours[-1]["close"],
            "signal": signal,
            "volume24hEth": round(vol_24h_eth, 2),
            "hourly": hours[-48:],
        }
    except Exception as e:
        logger.warning(f"ETHBTC taker fetch failed: {e}")
        return None


# ── DeFi ETH Distribution ────────────────────────────────────────────
DEFI_ETH_PROTOCOLS = {
    "Lido":        {"slug": "lido",        "category": "liquid_staking"},
    "Rocket Pool": {"slug": "rocket-pool", "category": "liquid_staking"},
    "EigenLayer":  {"slug": "eigenlayer",  "category": "restaking"},
    "Aave V3":     {"slug": "aave-v3",     "category": "lending"},
    "MakerDAO":    {"slug": "makerdao",    "category": "cdp"},
    "Spark":       {"slug": "spark",       "category": "lending"},
}
DEFI_CACHE_TTL = 3600

defi_cache: dict = {}
defi_cache_ts: float = 0


async def fetch_defi_eth_map(client: httpx.AsyncClient) -> Optional[dict]:
    """Fetch ETH locked in major DeFi protocols via DefiLlama."""
    global defi_cache, defi_cache_ts
    now_ts = time.time()
    if defi_cache and (now_ts - defi_cache_ts) < DEFI_CACHE_TTL:
        return defi_cache

    async def _fetch_protocol(name: str, info: dict) -> Optional[dict]:
        try:
            resp = await client.get(
                f"https://api.llama.fi/protocol/{info['slug']}",
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
            tokens_arr = data.get("tokens", [])
            if tokens_arr:
                latest_tokens = tokens_arr[-1].get("tokens", {})
                eth_amount = sum(
                    v for k, v in latest_tokens.items()
                    if k.upper() in DEFILLAMA_ETH_KEYS
                )
                if eth_amount > 0:
                    return {"name": name, "category": info["category"], "ethAmount": round(eth_amount, 2)}
            chain_tvls = data.get("currentChainTvls", {})
            eth_chain_tvl = chain_tvls.get("Ethereum", 0)
            if eth_chain_tvl > 0:
                return {"name": name, "category": info["category"], "ethAmount": 0, "tvlUsd": round(eth_chain_tvl, 0)}
            return None
        except Exception as e:
            logger.warning(f"DeFi fetch failed for {name}: {e}")
            return None

    tasks = [_fetch_protocol(name, info) for name, info in DEFI_ETH_PROTOCOLS.items()]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)
    protocols = [r for r in raw_results if isinstance(r, dict)]
    if not protocols:
        return None

    categories: dict = {}
    for p in protocols:
        cat = p["category"]
        if cat not in categories:
            categories[cat] = {"totalEth": 0.0, "totalUsd": 0.0, "protocols": []}
        categories[cat]["totalEth"] += p.get("ethAmount", 0)
        categories[cat]["totalUsd"] += p.get("tvlUsd", 0)
        categories[cat]["protocols"].append({
            "name": p["name"], "ethAmount": p.get("ethAmount", 0), "tvlUsd": p.get("tvlUsd"),
        })
    for cat in categories.values():
        cat["totalEth"] = round(cat["totalEth"], 2)
        cat["totalUsd"] = round(cat["totalUsd"], 0)

    total_defi_eth = sum(c["totalEth"] for c in categories.values())
    result = {
        "totalDefiEth": round(total_defi_eth, 2),
        "byCategory": categories,
        "protocolCount": len(protocols),
    }
    defi_cache = result
    defi_cache_ts = now_ts
    logger.info(f"DeFi ETH map ok: {total_defi_eth:,.0f} ETH across {len(protocols)} protocols")
    return result


def process_dune_netflows(
    raw: Optional[dict],
    current_eth_price: Optional[float],
    spot_volume_usd_24h: Optional[float] = None,
    price_change_pct_24h: Optional[float] = None,
    reserves: Optional[dict] = None,
) -> dict:
    """Aggregate Dune CEX flows into 1h/6h/24h/7d windows + per-exchange + hourly series,
    plus relative context (z-score, percentile, flow/vol ratio, flow-price divergence).

    This signal measures changes in the liquid supply of ETH held on CEX — NOT execution.
    Net inflow > 0 = ETH entering exchanges = supply side growing = POTENTIAL sell pressure.
    Net inflow < 0 = ETH leaving exchanges to self-custody = POTENTIAL HODL / buy pressure.

    Relative context distinguishes statistically normal flows from materially impactful ones:
      - z-score vs rolling 24h distribution (is this flow anomalous for the current regime?)
      - percentile vs same distribution (where does it rank?)
      - flow/volume ratio (is the flow large vs actual spot trading?)
      - flow-price divergence (does the flow line up with the realized price move?)

    USD lag: Dune's price join runs ~1h behind, so the freshest hour has null usd.
    Falls back to net_eth × current spot price for those rows.
    """
    if not raw or not isinstance(raw, dict):
        return {}
    rows = (raw.get("result") or {}).get("rows") or []
    if not rows:
        return {}

    px = float(current_eth_price or 0)

    parsed = []
    for r in rows:
        try:
            hour_str = r.get("hour", "")
            hour_clean = hour_str.replace(" UTC", "").split(".")[0]
            dt = datetime.strptime(hour_clean, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            ts_ms = int(dt.timestamp() * 1000)
            in_eth  = float(r.get("inflow_eth")  or 0)
            out_eth = float(r.get("outflow_eth") or 0)
            net_eth = float(r.get("net_inflow_eth") or 0)
            net_usd_raw = r.get("net_inflow_usd")
            if net_usd_raw is None:
                net_usd = net_eth * px  # USD lag fallback
            else:
                net_usd = float(net_usd_raw)
            parsed.append({
                "ts": ts_ms,
                "cex": r.get("cex_name", ""),
                "in_eth": in_eth,
                "out_eth": out_eth,
                "net_eth": net_eth,
                "net_usd": net_usd,
                "tx": int(r.get("tx_count") or 0),
            })
        except (KeyError, ValueError, TypeError):
            continue

    if not parsed:
        return {}

    HOUR_MS = 3600 * 1000
    max_ts = max(p["ts"] for p in parsed)

    # When did Dune actually run the query? Lets the frontend distinguish
    # "freshest bucket is naturally lagged" from "Dune cache hasn't refreshed".
    exec_ended_ms: Optional[int] = None
    exec_ended_str = raw.get("execution_ended_at", "")
    if exec_ended_str:
        try:
            exec_ended_ms = int(datetime.fromisoformat(exec_ended_str.replace("Z", "+00:00")).timestamp() * 1000)
        except (ValueError, TypeError):
            exec_ended_ms = None

    windows = {"1h": 1, "6h": 6, "24h": 24, "7d": 168}
    aggregates = {}
    for label, hours in windows.items():
        cutoff = max_ts - (hours - 1) * HOUR_MS
        subset = [p for p in parsed if p["ts"] >= cutoff]
        in_eth_t  = sum(p["in_eth"]  for p in subset)
        out_eth_t = sum(p["out_eth"] for p in subset)
        net_eth_t = sum(p["net_eth"] for p in subset)
        net_usd_t = sum(p["net_usd"] for p in subset)
        tx_t      = sum(p["tx"] for p in subset)
        aggregates[label] = {
            "inflowEth":    round(in_eth_t,  2),
            "outflowEth":   round(out_eth_t, 2),
            "netInflowEth": round(net_eth_t, 2),
            "netInflowUsd": round(net_usd_t, 2),
            "txCount":      tx_t,
        }

    cutoff_24h = max_ts - 23 * HOUR_MS
    by_cex_dict: dict = {}
    for p in parsed:
        if p["ts"] < cutoff_24h:
            continue
        cex = p["cex"]
        agg = by_cex_dict.setdefault(cex, {"in": 0.0, "out": 0.0, "net_eth": 0.0, "net_usd": 0.0, "tx": 0})
        agg["in"]  += p["in_eth"]
        agg["out"] += p["out_eth"]
        agg["net_eth"] += p["net_eth"]
        agg["net_usd"] += p["net_usd"]
        agg["tx"]  += p["tx"]
    by_exchange = []
    for cex, vals in by_cex_dict.items():
        by_exchange.append({
            "cex": cex,
            "inflowEth":    round(vals["in"],  2),
            "outflowEth":   round(vals["out"], 2),
            "netInflowEth": round(vals["net_eth"], 2),
            "netInflowUsd": round(vals["net_usd"], 2),
            "txCount":      vals["tx"],
        })
    by_exchange.sort(key=lambda x: x["netInflowEth"])

    hourly_dict: dict = {}
    for p in parsed:
        slot = hourly_dict.setdefault(p["ts"], {"ts": p["ts"], "net_eth": 0.0, "net_usd": 0.0, "tx": 0})
        slot["net_eth"] += p["net_eth"]
        slot["net_usd"] += p["net_usd"]
        slot["tx"] += p["tx"]
    hourly = sorted(hourly_dict.values(), key=lambda x: x["ts"])
    hourly_series = [
        {"ts": h["ts"], "netInflowEth": round(h["net_eth"], 2), "netInflowUsd": round(h["net_usd"], 2), "txCount": h["tx"]}
        for h in hourly
    ]

    # ── Relative context ───────────────────────────────────────────────
    net_24h_eth = aggregates["24h"]["netInflowEth"]
    net_24h_usd = aggregates["24h"]["netInflowUsd"]

    # Build rolling 24h netflow distribution across the 7d history.
    rolling_24h_list: list = []
    n_h = len(hourly_series)
    if n_h >= 24:
        running = sum(h["netInflowEth"] for h in hourly_series[:24])
        rolling_24h_list.append(running)
        for i in range(24, n_h):
            running += hourly_series[i]["netInflowEth"] - hourly_series[i - 24]["netInflowEth"]
            rolling_24h_list.append(running)

    # Exclude the most recent 24h window from the comparison distribution.
    hist_distribution = rolling_24h_list[:-1] if len(rolling_24h_list) > 1 else rolling_24h_list

    mean_24h = statistics.fmean(hist_distribution) if hist_distribution else 0.0
    stdev_24h = statistics.stdev(hist_distribution) if len(hist_distribution) > 1 else 0.0
    z_score = (net_24h_eth - mean_24h) / stdev_24h if stdev_24h > 0 else 0.0

    if hist_distribution:
        rank = sum(1 for v in hist_distribution if v <= net_24h_eth)
        percentile = rank / len(hist_distribution) * 100.0
    else:
        percentile = 50.0

    flow_vol_ratio_pct = None
    if spot_volume_usd_24h and spot_volume_usd_24h > 0:
        flow_vol_ratio_pct = abs(net_24h_usd) / spot_volume_usd_24h * 100.0

    abs_z = abs(z_score)
    if abs_z >= 2.0:
        magnitude = "EXTREME"
    elif abs_z >= 1.0:
        magnitude = "ELEVATED"
    elif abs_z >= 0.3:
        magnitude = "NORMAL"
    else:
        magnitude = "NOISE"

    # Direction:
    #  - When the flow is statistically significant (|z|>=1) we trust the z sign:
    #    "more inflow than typical regime" = BEARISH, vice-versa BULLISH.
    #    This avoids the trap where mean_24h is structurally large (e.g. 7d of
    #    inflow regime) and drowns out a clearly-elevated reading.
    #  - Otherwise we fall back to absolute-net vs a noise band so tiny flips
    #    near zero don't swing direction.
    if abs_z >= 1.0:
        direction = "BEARISH" if z_score > 0 else "BULLISH"
    else:
        noise_band = max(abs(mean_24h), 500)
        if net_24h_eth < -noise_band:
            direction = "BULLISH"
        elif net_24h_eth > noise_band:
            direction = "BEARISH"
        else:
            direction = "NEUTRAL"

    divergence = None
    if price_change_pct_24h is not None and direction != "NEUTRAL":
        flow_sign = 1 if direction == "BULLISH" else -1
        if price_change_pct_24h > 0.5:
            price_sign = 1
        elif price_change_pct_24h < -0.5:
            price_sign = -1
        else:
            price_sign = 0
        if price_sign == 0:
            divergence = "FLAT_PRICE"
        elif flow_sign == price_sign:
            divergence = "CONFIRMED"
        else:
            divergence = "DIVERGENT"

    if direction == "NEUTRAL":
        bias = "NEUTRAL"
    elif magnitude in ("EXTREME", "ELEVATED"):
        bias = direction
    else:
        bias = f"{direction}_MILD"

    return {
        "lastUpdate": max_ts,
        "executionEndedAt": exec_ended_ms,
        "exchangeCount": len(set(p["cex"] for p in parsed)),
        "aggregates": aggregates,
        "byExchange24h": by_exchange,
        "hourly": hourly_series,
        "bias": bias,
        "direction": direction,
        "magnitude": magnitude,
        "relativeContext": {
            "mean24hEth":     round(mean_24h, 2),
            "stdev24hEth":    round(stdev_24h, 2),
            "zScore":         round(z_score, 2),
            "percentile":     round(percentile, 1),
            "samplesN":       len(hist_distribution),
            "flowVolRatioPct": round(flow_vol_ratio_pct, 3) if flow_vol_ratio_pct is not None else None,
            "spotVolumeUsd24h": round(spot_volume_usd_24h, 0) if spot_volume_usd_24h else None,
            "priceChangePct24h": round(price_change_pct_24h, 3) if price_change_pct_24h is not None else None,
            "divergence":     divergence,
            # Reserves context (from DefiLlama)
            "reservesTotalEth": reserves.get("totalEth") if reserves else None,
            "reservesTotalStable": reserves.get("totalStable") if reserves else None,
            "reservesExchangeCount": reserves.get("exchangeCount") if reserves else None,
            "flowAsReservesPct": round(abs(net_24h_eth) / reserves["totalEth"] * 100, 4)
                if reserves and reserves.get("totalEth") and reserves["totalEth"] > 0 else None,
            "reservesByExchange": reserves.get("byExchange") if reserves else None,
            "reservesHistory": reserves.get("history") if reserves else None,
            "stableHistory": reserves.get("stableHistory") if reserves else None,
        },
    }


async def fetch_all_data() -> dict:
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
                       {"symbol": "ETHUSDT", "period": "1h", "limit": 500}),
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
            fetch_json(client, f"{BINANCE_FAPI}/fapi/v1/klines",
                       {"symbol": "ETHUSDT", "interval": "1h", "limit": 720}),
            fetch_json(client, f"{BINANCE_FAPI}/fapi/v1/klines",
                       {"symbol": "ETHUSDT", "interval": "1h", "limit": 200}),
            fetch_json(client, f"{BINANCE_FAPI}/fapi/v1/klines",
                       {"symbol": "ETHUSDT", "interval": "1h", "limit": 1080}),
            fetch_json(client, f"{BINANCE_SPOT}/api/v3/klines",
                       {"symbol": "ETHUSDT", "interval": "5m", "limit": 48}),
            fetch_json(client, f"{BINANCE_SPOT}/api/v3/klines",
                       {"symbol": "ETHUSDT", "interval": "1h", "limit": 24}),
            fetch_json(client, f"{BINANCE_FAPI}/fapi/v1/klines",
                       {"symbol": "ETHUSDT", "interval": "5m", "limit": 288}),
            fetch_json(client, f"{BINANCE_SPOT}/api/v3/klines",
                       {"symbol": "ETHUSDT", "interval": "5m", "limit": 288}),
            fetch_json(client, f"{DERIBIT_API}/api/v2/public/get_book_summary_by_currency",
                       {"currency": "ETH", "kind": "option"}),
            fetch_json(client, "https://api.bybit.com/v5/market/tickers",
                       {"category": "option", "baseCoin": "ETH"}),
            fetch_json(client, f"{OKX_API}/api/v5/public/open-interest",
                       {"instType": "OPTION", "instFamily": "ETH-USD"}),
            fetch_json(client, f"{OKX_API}/api/v5/public/opt-summary",
                       {"instFamily": "ETH-USD"}),
            fetch_json(client, f"{BINANCE_SPOT}/api/v3/ticker/24hr",
                       {"symbol": "ETHUSDT"}),
            fetch_json(client, f"{BYBIT_API}/v5/market/tickers",
                       {"category": "linear", "symbol": "ETHUSDT"}),
            fetch_json(client, f"{OKX_API}/api/v5/market/ticker",
                       {"instId": "ETH-USDT-SWAP"}),
            fetch_json(client, f"{BYBIT_API}/v5/market/funding/history",
                       {"category": "linear", "symbol": "ETHUSDT", "limit": 30}),
            post_json(client, f"{HYPERLIQUID_API}/info",
                      {"type": "metaAndAssetCtxs"}),
            fetch_json(client, f"{BINANCE_SPOT}/api/v3/ticker/24hr",
                       {"symbol": "ETHBTC"}),
            fetch_json(client, f"{BYBIT_API}/v5/market/open-interest",
                       {"category": "linear", "symbol": "ETHUSDT", "intervalTime": "1h", "limit": 1}),
            fetch_json(client, f"{BINANCE_FAPI}/fapi/v1/klines",
                       {"symbol": "ETHUSDT", "interval": "1m", "limit": 500}),
            fetch_json(client, f"{BINANCE_FAPI}/fapi/v1/klines",
                       {"symbol": "ETHUSDT", "interval": "15m", "limit": 500}),
            fetch_json(client, f"{BINANCE_FAPI}/fapi/v1/klines",
                       {"symbol": "ETHUSDT", "interval": "4h", "limit": 500}),
            fetch_json(client, f"{BINANCE_FAPI}/fapi/v1/klines",
                       {"symbol": "ETHUSDT", "interval": "5m", "limit": 500}),
            # 5m OI history for cut-anchored MQ on 1m/5m/15m TFs (~41h coverage)
            fetch_json(client, f"{BINANCE_FAPI}/futures/data/openInterestHist",
                       {"symbol": "ETHUSDT", "period": "5m", "limit": 500}),
            # Dune Analytics — ETH CEX netflows (cached every 30 min)
            fetch_dune_cex_netflows(client),
            # DefiLlama — CEX ETH reserves / absolute stock (cached 1h)
            fetch_defillama_reserves(client),
            # ETHBTC taker rotation signal
            fetch_ethbtc_taker(client),
            # DeFi ETH distribution (Lido, Aave, Maker, EigenLayer, etc.)
            fetch_defi_eth_map(client),
            return_exceptions=True,
        )

    (
        bn_ticker, bn_premium, bn_oi, bn_oi_hist,
        bn_ls, bn_top_ls, bn_taker, bn_fund_hist,
        bn_taker_hist, bn_ls_hist,
        bn_ls_15m, bn_ls_4h, bn_ls_1d,
        bn_top_ls_15m, bn_top_ls_4h, bn_top_ls_1d,
        okx_fund, okx_oi, okx_ls,
        bn_klines_vol, bn_klines_vp,
        bn_klines_90d,
        spot_klines_5m, spot_klines_1h,
        perp_klines_5m_flow, spot_klines_5m_flow,
        deribit_options_raw,
        bybit_options_raw, okx_options_oi_raw, okx_options_iv_raw,
        bn_spot_ticker, bybit_perp_ticker, okx_perp_ticker,
        bybit_funding_hist_raw, hyperliquid_raw,
        ethbtc_ticker, bybit_oi_raw,
        kl_1m, kl_15m, kl_4h_stoch, kl_5m_stoch,
        bn_oi_hist_5m,
        dune_cex_raw,
        defillama_reserves_raw,
        ethbtc_taker_raw,
        defi_eth_map_raw,
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

    oi_hist_5m: list = []
    for d in safe_list(bn_oi_hist_5m):
        try:
            oi_hist_5m.append({
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

    def parse_ls_series(raw_data):
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
        "5m": parse_ls_series(bn_ls_hist),
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
        bybit_fund_hist.reverse()

    hl_funding = None
    hl_oi = None
    hl_volume = None
    if isinstance(hyperliquid_raw, list) and len(hyperliquid_raw) == 2:
        meta, asset_ctxs = hyperliquid_raw
        eth_idx = None
        if isinstance(meta, dict):
            universe = meta.get("universe", [])
            for i, coin in enumerate(universe):
                if isinstance(coin, dict) and coin.get("name") == "ETH":
                    eth_idx = i
                    break
        if eth_idx is None:
            eth_idx = 1
        if isinstance(asset_ctxs, list) and len(asset_ctxs) > eth_idx:
            eth_ctx = asset_ctxs[eth_idx]
            hl_funding = safe_float(eth_ctx, "funding")
            hl_oi_eth = safe_float(eth_ctx, "openInterest")
            hl_mark = safe_float(eth_ctx, "markPx")
            hl_oi = round(hl_oi_eth * hl_mark) if hl_oi_eth and hl_mark else None
            hl_volume = safe_float(eth_ctx, "dayNtlVlm")

    oi_change = None
    if len(oi_hist) >= 2:
        oi_48h_start = oi_hist[-49] if len(oi_hist) >= 49 else oi_hist[0]
        if oi_48h_start.get("value"):
            oi_change = ((oi_hist[-1]["value"] - oi_48h_start["value"]) / oi_48h_start["value"]) * 100

    retail_ratio = safe_float(latest_ls, "longShortRatio")
    top_ratio = safe_float(latest_top_ls, "longShortRatio")
    ls_divergence = None
    if retail_ratio is not None and top_ratio is not None:
        ls_divergence = round(retail_ratio - top_ratio, 4)

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
        price_chg = None
        price_chg_pct = None
        delta_vs_vol = None
        if price_open and price_close:
            price_chg = round(price_close - price_open, 2)
            price_chg_pct = round((price_close - price_open) / price_open * 100, 3)
        if total_vol > 0:
            delta_vs_vol = round(delta / total_vol * 100, 2)
        return {
            "buy": round(buy, 2), "sell": round(sell, 2), "delta": delta, "ratio": ratio,
            "totalVol": round(total_vol, 2),
            "priceOpen": price_open, "priceClose": price_close,
            "priceChg": price_chg, "priceChgPct": price_chg_pct,
            "deltaVsVol": delta_vs_vol,
        }

    flow_windows = {"1h": 12, "4h": 48, "12h": 144, "24h": 288}
    perp_flow = {w: cumulative_flow(perp_klines_5m_flow, n) for w, n in flow_windows.items()}
    spot_flow = {w: cumulative_flow(spot_klines_5m_flow, n) for w, n in flow_windows.items()}

    spot_taker_hist_5m = spot_taker_from_klines(spot_klines_5m)
    spot_taker_hist_1h = spot_taker_from_klines(spot_klines_1h)
    latest_spot_taker  = spot_flow.get("1h", {})

    current_price = safe_float(bn_ticker, "lastPrice")
    vol_profile = []
    vol_profile_by_period = {}
    if current_price:
        klines_vp_safe = bn_klines_vp if isinstance(bn_klines_vp, list) else []
        klines_vol_safe_bp = bn_klines_vol if isinstance(bn_klines_vol, list) else []
        perp_5m_safe_bp = perp_klines_5m_flow if isinstance(perp_klines_5m_flow, list) else []
        klines_90d_safe_bp = bn_klines_90d if isinstance(bn_klines_90d, list) else []
        if klines_vp_safe:
            vol_profile = build_volume_profile(klines_vp_safe, cluster_size=5.0, price_center=current_price, range_pct=0.10)
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

    volatility = calculate_volatility(bn_klines_vol if isinstance(bn_klines_vol, list) else [])

    klines_vol_safe = bn_klines_vol if isinstance(bn_klines_vol, list) else []
    klines_vp_safe = bn_klines_vp if isinstance(bn_klines_vp, list) else []
    klines_90d_safe = bn_klines_90d if isinstance(bn_klines_90d, list) else []
    perp_5m_safe = perp_klines_5m_flow if isinstance(perp_klines_5m_flow, list) else []
    volume_profile_by_period = {
        "4h": calculate_volume_profile(perp_5m_safe[-48:] if len(perp_5m_safe) >= 48 else perp_5m_safe, cluster_size=2.0),
        "12h": calculate_volume_profile(perp_5m_safe[-144:] if len(perp_5m_safe) >= 144 else perp_5m_safe, cluster_size=3.0),
        "24h": calculate_volume_profile(klines_vol_safe[-24:] if len(klines_vol_safe) >= 24 else klines_vol_safe),
        "7d": calculate_volume_profile(klines_vol_safe[-168:] if len(klines_vol_safe) >= 168 else klines_vol_safe),
        "30d": calculate_volume_profile(klines_vol_safe),
        "45d": calculate_volume_profile(klines_90d_safe),
    }
    volume_profile = calculate_volume_profile(klines_vp_safe)

    spot = current_price or 0
    all_instruments = []

    if isinstance(deribit_options_raw, dict):
        all_instruments.extend(deribit_options_raw.get("result", []) or [])

    if isinstance(bybit_options_raw, dict):
        bybit_list = (bybit_options_raw.get("result", {}) or {}).get("list", [])
        for item in bybit_list:
            try:
                sym = item.get("symbol", "")
                parts = sym.split("-")
                if len(parts) < 4:
                    continue
                expiry_str = parts[1]
                deribit_name = f"ETH-{expiry_str}-{parts[2]}-{parts[3]}"
                oi = float(item.get("openInterest") or 0)
                mark_iv = float(item.get("markIv") or 0)
                if oi <= 0 or mark_iv <= 0:
                    continue
                all_instruments.append({
                    "instrument_name": deribit_name,
                    "open_interest": oi,
                    "mark_iv": mark_iv * 100,
                })
            except (ValueError, KeyError, IndexError):
                continue

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
                inst_id = item.get("instId", "")
                if inst_id not in okx_oi_map:
                    continue
                parts = inst_id.split("-")
                if len(parts) < 5:
                    continue
                date_str = parts[2]
                expiry_dt = datetime.strptime(date_str, "%y%m%d").replace(tzinfo=timezone.utc)
                deribit_exp = expiry_dt.strftime("%d%b%y").upper()
                strike = parts[3]
                opt_type = parts[4]
                deribit_name = f"ETH-{deribit_exp}-{strike}-{opt_type}"
                mark_iv = float(item.get("markVol") or 0)
                if mark_iv <= 0:
                    continue
                all_instruments.append({
                    "instrument_name": deribit_name,
                    "open_interest": okx_oi_map[inst_id],
                    "mark_iv": mark_iv * 100,
                })
            except (ValueError, KeyError, IndexError):
                continue

    options_analytics = calculate_options_analytics(all_instruments, spot)
    iv_term_structure = calculate_iv_term_structure(all_instruments, spot)

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
            okx_vol_eth = float(okx_data[0].get("volCcy24h") or 0)
            vol_okx_perp = okx_vol_eth * (spot if spot else 1)

    combined_volume = vol_bn_perp + vol_bn_spot + vol_bybit_perp + vol_okx_perp

    ethbtc = {}
    if isinstance(ethbtc_ticker, dict):
        ethbtc = {
            "price": safe_float(ethbtc_ticker, "lastPrice"),
            "change24h": safe_float(ethbtc_ticker, "priceChangePercent"),
            "high24h": safe_float(ethbtc_ticker, "highPrice"),
            "low24h": safe_float(ethbtc_ticker, "lowPrice"),
        }

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
            "rates": {k: round(v * 100, 6) for k, v in rates.items()},
            "maxSpread": round((max(vals) - min(vals)) * 100, 6),
            "maxExchange": max(rates, key=rates.get),
            "minExchange": min(rates, key=rates.get),
            "mean": round(sum(vals) / len(vals) * 100, 6),
        }

    iv_rv_spread = {}
    if iv_term_structure and volatility.get("rv24h") is not None:
        short_term_ivs = [x for x in iv_term_structure if 3 <= x["dte"] <= 21]
        if short_term_ivs:
            atm_iv = short_term_ivs[0]["iv"]
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

    bn_oi_val = (safe_float(bn_oi, "openInterest") or 0) * (spot or 1)
    okx_oi_val = (safe_float(okx_oi_data, "oiCcy") or 0) * (spot or 1)
    bybit_oi_val = 0
    if isinstance(bybit_oi_raw, dict):
        oi_list = (bybit_oi_raw.get("result", {}) or {}).get("list", [])
        if oi_list:
            bybit_oi_val = float(oi_list[0].get("openInterest") or 0) * (spot or 1)
    hl_oi_val = hl_oi or 0
    total_oi_usd = bn_oi_val + okx_oi_val + bybit_oi_val + hl_oi_val

    klines_for_stoch = {
        "1m":  kl_1m if isinstance(kl_1m, list) else [],
        "5m":  kl_5m_stoch if isinstance(kl_5m_stoch, list) else [],
        "15m": kl_15m if isinstance(kl_15m, list) else [],
        "1h":  bn_klines_vol if isinstance(bn_klines_vol, list) else [],
        "4h":  kl_4h_stoch if isinstance(kl_4h_stoch, list) else [],
    }
    stochastics_data = compute_stochastics_multi(klines_for_stoch)

    cut_anchored_mq = compute_cut_anchored_mq(
        klines_for_stoch,
        oi_hist_1h=oi_hist,
        oi_hist_5m=oi_hist_5m,
    )

    money_quality = compute_money_quality(
        oi_hist,
        perp_flow,
        funding_rate=bn_fund_rate,
        klines_1h=bn_klines_vol if isinstance(bn_klines_vol, list) else None,
    )

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
        "moneyQuality": money_quality,
        "cutAnchoredMq": cut_anchored_mq,
        "cexNetflows": process_dune_netflows(
            dune_cex_raw if isinstance(dune_cex_raw, dict) else None,
            current_price,
            spot_volume_usd_24h=vol_bn_spot if vol_bn_spot else None,
            price_change_pct_24h=safe_float(bn_ticker, "priceChangePercent"),
            reserves=defillama_reserves_raw if isinstance(defillama_reserves_raw, dict) else None,
        ),
        "ethBtcRotation": ethbtc_taker_raw if isinstance(ethbtc_taker_raw, dict) else None,
        "defiEthMap": defi_eth_map_raw if isinstance(defi_eth_map_raw, dict) else None,
    }

    cache = data
    cache_ts = time.time()
    return data


# ── FastAPI App ───────────────────────────────────────────────────────
app = FastAPI(title="ETH Positioning Dashboard", version="2.0.0")

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
    return await fetch_depth_data()


@app.get("/api/depth/history")
async def get_depth_history():
    # No persistent history in serverless — return empty
    return {"snapshots": []}


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "runtime": "vercel-serverless",
        "cache_age": round(time.time() - cache_ts, 1) if cache_ts else None,
    }
