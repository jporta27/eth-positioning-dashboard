"""
ETH Positioning Dashboard — Backend API
Fetches data from Binance & OKX public APIs and serves unified endpoints.
Includes order book depth snapshots with rolling history.
Designed for Railway deployment.
"""

import os
import re
import csv
import io
import json
import time
import math
import shutil
import statistics
import asyncio
import logging
import subprocess
from typing import Optional
from contextlib import asynccontextmanager
from collections import deque
from datetime import datetime, timezone, timedelta
from urllib.parse import quote as urlquote

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# Farside parser split out to its own module so scripts/backfill.py can reuse
# the same implementation (QW2). Aliased to preserve internal call sites.
from farside_parse import (
    parse_farside_csv as _parse_farside_csv,
    parse_farside_html as _parse_farside_html,
    parse_sosovalue as _parse_sosovalue,
    parse_farside_number as _parse_farside_number,
    looks_like_date as _looks_like_date,
    sort_etf_rows_ascending as _sort_etf_rows_ascending,
    parse_farside_date_to_epoch as _parse_farside_date_to_epoch,
)

# Minimal .env loader (no extra dep). Looks for .env next to this file.
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
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
CACHE_TTL = 10  # seconds
DEPTH_INTERVAL = 5  # seconds between depth snapshots
DEPTH_HISTORY_MINUTES = 240  # 4 hours of history
DEPTH_SUMMARY_INTERVAL = 300  # save summary every 5 min (seconds)
DEPTH_CLUSTER_SIZE = 0.25  # group price levels into $0.25 buckets
VOLATILITY_LOOKBACK_DAYS = 30  # for percentile calculation
PORT = int(os.getenv("PORT", 8000))

# Dune Analytics — ETH CEX netflows
# Each (key, query_id) pair is tried in order; the first that returns data wins.
# Fallback's query is typically a fork of the primary's, owned by the fallback
# account, so when the primary's quota is exhausted the fallback can read AND
# trigger fresh executions. If you only set the fallback key but not its query_id,
# we'll try the primary query with the fallback key — that only works if the
# primary query is public, otherwise Dune returns 404.
DUNE_API_KEY = os.getenv("DUNE_API_KEY", "")
DUNE_API_KEY_FALLBACK = os.getenv("DUNE_API_KEY_FALLBACK", "")
DUNE_QUERY_ID = os.getenv("DUNE_QUERY_ID", "6984181")
DUNE_QUERY_ID_FALLBACK = os.getenv("DUNE_QUERY_ID_FALLBACK", "")
DUNE_KEY_QUERY_PAIRS = [
    (k, q) for k, q in [
        (DUNE_API_KEY, DUNE_QUERY_ID),
        (DUNE_API_KEY_FALLBACK, DUNE_QUERY_ID_FALLBACK or DUNE_QUERY_ID),
    ] if k and q
]
DUNE_CACHE_TTL = 300  # 5 min — short so we pick up Dune-side re-executions promptly
DUNE_MAX_AGE_HOURS = 1  # trigger fresh execution if Dune's last result is older than 1h
DUNE_POLL_MAX_ATTEMPTS = 6  # 30s ceiling (6×5s) — fits Vercel maxDuration=60s budget
DUNE_PAGE_LIMIT = 200  # rows per /results call. 200 × 7 cols = 1400 datapoints, under Dune per-request cap.
DUNE_KEY_EXHAUSTED_BACKOFF_S = 6 * 3600  # remember a 402'd (key, method) for 6h before retrying it
_dune_key_exhausted_until: dict = {}  # (key, method) -> epoch_ts when we'll consider it again

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

# ── New data sources (Fase 1) ─────────────────────────────────────────
# Farside ETF flows (primary) → scrape HTML → SoSoValue (tertiary) → stale
FARSIDE_CSV_URL  = "https://farside.co.uk/wp-content/uploads/ETH.csv"
FARSIDE_HTML_URL = "https://farside.co.uk/eth/"
SOSOVALUE_ETF_URL = "https://api.sosovalue.com/openapi/v2/etf/historicalInflowChart"  # fallback; may require key
ETF_CACHE_TTL = 21600  # 6h — daily cadence, no need to hit source more often

# Stablecoin supply (DefiLlama /stablecoincharts)
STABLES_API_URL = "https://stablecoins.llama.fi/stablecoincharts/all"
STABLES_TRACKED = {"USDT": "Tether", "USDC": "USD Coin"}
STABLES_CACHE_TTL = 1800  # 30 min

# Macro context — Yahoo Finance v8 chart API (no-key, public) + FRED CSV for risk-free
# Stooq required an API key as of 2026 — replaced with Yahoo v8, which remains free.
YAHOO_CHART_BASE = "https://query2.finance.yahoo.com/v8/finance/chart/"
FRED_CSV_BASE    = "https://fred.stlouisfed.org/graph/fredgraph.csv"
MACRO_SYMBOLS = {
    "DXY":   "DX-Y.NYB",
    "SPX":   "^GSPC",      # S&P 500
    "VIX":   "^VIX",
    "US10Y": "^TNX",       # 10Y Treasury yield (x10, divide by 10 for pct)
    "BTC":   "BTC-USD",
}
RISK_FREE_FRED_ID   = "DTB3"   # FRED 3-Month T-bill (secondary fallback)
RISK_FREE_YAHOO_SYM = "^IRX"   # Yahoo CBOE 13-week T-bill yield (primary; same instrument)
MACRO_CACHE_TTL     = 300      # 5 min
RISK_FREE_CACHE_TTL = 86400    # 1d — r is stable intraday
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Deribit futures (for basis + expiry calendar)
DERIBIT_FUTURES_KIND = "future"
DERIBIT_BASIS_CACHE_TTL = 30  # aligned with CACHE_TTL-ish; basis tracks spot

# Schema version for persistence (Fase 2 parquet writer)
SCHEMA_VERSION = 1

# Fase 2 — Persistence (opt-in)
PERSIST_ENABLED = os.getenv("PERSIST_ENABLED", "false").lower() in ("1", "true", "yes", "on")
PERSIST_PATH    = os.getenv("PERSIST_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"))
PERSIST_SNAPSHOT_INTERVAL = 60  # one row per minute
PERSIST_MICRO_INTERVAL    = 5   # one order-book row every 5s

# PyArrow is optional at import time; the persister self-disables if missing
try:
    import pyarrow as _pa
    import pyarrow.parquet as _pq
    PERSIST_AVAILABLE = True
except Exception:
    _pa = None
    _pq = None
    PERSIST_AVAILABLE = False

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dashboard")

# ── Cache ─────────────────────────────────────────────────────────────
cache: dict = {}
cache_ts: float = 0

# Dune cache (longer TTL — hourly query)
dune_cache: dict = {}
dune_cache_ts: float = 0

# DefiLlama reserves cache
llama_cache: dict = {}
llama_cache_ts: float = 0

# New caches (Fase 1) — each has its own TTL, module-level for background refresh access
etf_cache: dict = {}
etf_cache_ts: float = 0

stables_cache: dict = {}
stables_cache_ts: float = 0

macro_cache: dict = {}
macro_cache_ts: float = 0

risk_free_cache: dict = {}
risk_free_cache_ts: float = 0

deribit_basis_cache: dict = {}
deribit_basis_cache_ts: float = 0

deribit_futures_raw: list = []  # shared between basis + expiry calendar
deribit_futures_raw_ts: float = 0

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


def stochastic(klines: list, k_period: int, k_smooth: int, d_smooth: int, history_len: int = 300) -> Optional[dict]:
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


def compute_money_quality(
    oi_hist: list,
    perp_flow: dict,
    funding_rate: Optional[float] = None,
    klines_1h: Optional[list] = None,
) -> dict:
    """
    Classify price movement quality by comparing price change vs OI change per window.
    This distinguishes NEW MONEY entering from SHORT COVERING / ROTATION.

    Short windows (1h/4h/12h/24h) source price change from perp_flow (5m klines).
    Long windows  (3d/7d/14d)       source price change from klines_1h klines[4]=close.
    OI deltas use the hourly openInterestHist (oi_hist), newest last.

    Ratio = |ΔPrice%| / |ΔOI%|
      < 1   → Acumulación real (mucha plata nueva por poco movimiento)
      1-2   → Movimiento balanceado
      2-5   → Covering dominante (rally/drop de baja calidad)
      > 5   → Squeeze puro (sin combustible nuevo)
    """
    if not oi_hist or len(oi_hist) < 2:
        return {}

    # oi_hist is hourly, newest last. Index -1 = now, -(n+1) = n hours ago.
    current_oi = oi_hist[-1].get("oi")
    if current_oi is None:
        return {}

    # Intraday windows sourced from perp_flow, multi-day windows sourced from klines_1h
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

    # Helper: compute priceChgPct for a window from 1h klines list (Binance kline format)
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
        oi_delta     = current_oi - past_oi
        oi_delta_pct = (oi_delta / past_oi) * 100

        if wname in long_windows:
            price_chg_pct = price_chg_from_klines(hours)
            delta_vs_vol  = None
            taker_ratio   = None
        else:
            flow = (perp_flow or {}).get(wname, {}) or {}
            price_chg_pct = flow.get("priceChgPct")
            delta_vs_vol  = flow.get("deltaVsVol")
            taker_ratio   = flow.get("ratio")

        if price_chg_pct is None:
            continue

        # Ratio price/OI (absolute): how much price moved per unit of OI growth
        ratio = None
        if abs(oi_delta_pct) > 0.01:  # avoid division by near-zero
            ratio = abs(price_chg_pct) / abs(oi_delta_pct)

        # Classify
        price_up = price_chg_pct > 0.05
        price_dn = price_chg_pct < -0.05
        oi_up    = oi_delta_pct >  0.10
        oi_dn    = oi_delta_pct < -0.10

        label = "Sin actividad"
        direction = "neutral"   # "bullish" | "bearish" | "neutral"
        quality = "low"          # "high" | "medium" | "low"

        if price_up and oi_up:
            direction = "bullish"
            if ratio is not None and ratio < 1:
                label = "Acumulación real"
                quality = "high"
            elif ratio is not None and ratio < 2:
                label = "Balanceado (longs mixto)"
                quality = "medium"
            elif ratio is not None and ratio < 5:
                label = "Covering dominante"
                quality = "low"
            else:
                label = "Squeeze puro"
                quality = "low"
        elif price_up and not oi_up and not oi_dn:
            label = "Short covering (OI plano)"
            direction = "bullish"
            quality = "low"
        elif price_up and oi_dn:
            label = "Distribución arriba (OI cae)"
            direction = "neutral"
            quality = "low"
        elif price_dn and oi_up:
            direction = "bearish"
            if ratio is not None and ratio < 1:
                label = "Distribución real"
                quality = "high"
            elif ratio is not None and ratio < 2:
                label = "Balanceado (shorts mixto)"
                quality = "medium"
            elif ratio is not None and ratio < 5:
                label = "Liquidation dominante"
                quality = "low"
            else:
                label = "Long squeeze puro"
                quality = "low"
        elif price_dn and not oi_up and not oi_dn:
            label = "Long cerrando (OI plano)"
            direction = "bearish"
            quality = "low"
        elif price_dn and oi_dn:
            label = "Long capitulation"
            direction = "bearish"
            quality = "high"
        elif not price_up and not price_dn:
            if oi_up:
                label = "Build-up (lateral)"
                direction = "neutral"
                quality = "medium"
            elif oi_dn:
                label = "Deleverage (lateral)"
                direction = "neutral"
                quality = "medium"
            else:
                label = "Sin actividad"
                direction = "neutral"
                quality = "low"

        by_window[wname] = {
            "oiDelta":      round(oi_delta, 2),
            "oiDeltaPct":   round(oi_delta_pct, 3),
            "priceChgPct":  round(price_chg_pct, 3),
            "ratio":        round(ratio, 2) if ratio is not None else None,
            "label":        label,
            "direction":    direction,
            "quality":      quality,
            "deltaVsVol":   delta_vs_vol,
            "takerRatio":   taker_ratio,
        }

    # ── Overall verdict weighted by window (4-24h carries timing weight) ──
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
        if info["direction"] == "bullish":
            sign = 1
        elif info["direction"] == "bearish":
            sign = -1
        else:
            sign = 0
        q_mult = {"high": 1.0, "medium": 0.6, "low": 0.3}.get(info["quality"], 0.3)
        score   += sign * ww * q_mult
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

    # Funding context: negative funding + rising price = short squeeze setup
    funding_context = None
    if funding_rate is not None:
        if funding_rate < -0.00005:
            funding_context = "Funding negativo — shorts cargados"
        elif funding_rate > 0.00008:
            funding_context = "Funding positivo — longs cargados"
        else:
            funding_context = "Funding neutro"

    return {
        "byWindow":       by_window,
        "verdict":        verdict,
        "score":          round(norm_score, 3),
        "fundingContext": funding_context,
    }


def compute_cut_anchored_mq(
    klines_by_tf: dict,
    oi_hist_1h: list,
    oi_hist_5m: list,
) -> dict:
    """
    For each stoch timeframe, find when the FAST %K (100,10) most recently crossed
    INTO the extreme zone (≥80 or ≤20) and compute the OI/Price evolution from that
    "cut" anchor up to now.

    This is the *impulse* filter: it tells us whether the current OB/OS phase is
    being powered by NEW MONEY (trend → BLOCK the mean-reversion bet) or by
    COVERING/CAPITULATION (squeeze → UPGRADE the mean-reversion bet).

    Anchor = the bar JUST BEFORE the most recent crossing into the zone.
    OI granularity:
        1m / 5m / 15m  → uses 5min OI history (oi_hist_5m, ~41h coverage)
        1h / 4h        → uses 1h OI history    (oi_hist_1h, ~20d coverage)
    Falls back to 1h OI if 5m series is missing or doesn't reach the anchor.
    """
    tf_minutes = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240}
    K_PERIOD = 100
    K_SMOOTH = 10
    OI_FLAT_THRESHOLD = 0.10  # |ΔOI%| below this is "flat"

    out: dict = {}

    if not klines_by_tf:
        return out

    def lookup_oi_pair(oi_series: list, minutes_back: int, granularity_min: int):
        """Return (anchor_oi, current_oi) approximately `minutes_back` minutes ago."""
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

        # Recompute Fast %K (100,10) over the full klines window
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

        # Determine current zone
        if latest_k >= 80:
            target_dir = "short"
            in_zone = lambda v: v is not None and v >= 80
        elif latest_k <= 20:
            target_dir = "long"
            in_zone = lambda v: v is not None and v <= 20
        else:
            # Not in extreme zone — no cut to anchor
            continue

        # Walk backwards to find the most recent crossing INTO the zone
        anchor_idx = None
        anchor_capped = False
        for i in range(n - 1, 0, -1):
            if not in_zone(smooth_k[i]):
                # We left the zone walking back without finding a cross — bail
                break
            if not in_zone(smooth_k[i - 1]):
                # i is in zone, i-1 is not → i-1 is the anchor (last bar before crossing)
                anchor_idx = i - 1
                break

        if anchor_idx is None:
            # The entire visible history is inside the zone — anchor capped to start
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

        # ── Lookup OI at the anchor in matching granularity ──────────
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

        # Classify per direction
        # SHORT setup (price went UP into OB):
        #   OI up + ratio<1   → acumulación real (longs nuevos pagando arriba) → BLOCK
        #   OI up + ratio 1-2 → balanceado (longs mixto) → NEUTRAL
        #   OI up + ratio 2-5 → covering dominante → UPGRADE A+
        #   OI up + ratio >5  → squeeze puro → UPGRADE A++
        #   OI flat           → short covering puro → UPGRADE A+
        #   OI down           → distribución arriba → UPGRADE A++
        # LONG setup (price went DOWN into OS):
        #   OI up + ratio<1   → distribución real (shorts nuevos vendiendo abajo) → BLOCK
        #   OI up + ratio 1-2 → balanceado (shorts mixto) → NEUTRAL
        #   OI up + ratio 2-5 → liquidation dominante → UPGRADE A+
        #   OI up + ratio >5  → long squeeze puro → UPGRADE A++
        #   OI flat           → long cerrando → UPGRADE A+
        #   OI down           → long capitulation → UPGRADE A++
        label = "Sin clasificar"
        quality = "neutral"  # 'block' | 'neutral' | 'upgrade-mid' | 'upgrade-high'

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
            else:  # oi_dn
                label, quality = "Distribución arriba (OI cae)", "upgrade-high"
        else:  # long
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
            else:  # oi_dn
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


async def _dune_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    key: str,
    *,
    timeout: float = 30.0,
) -> Optional[httpx.Response]:
    """One key, one HTTP call. Returns Response on success, None on 402 (after
    marking the (key, method) pair as backed-off for DUNE_KEY_EXHAUSTED_BACKOFF_S).
    Raises on other HTTP errors so the caller can decide whether to swallow.

    Backoff is scoped to (key, method): a key that 402'd on POST /execute
    (write quota) might still serve GET /results (free read of cached data).
    """
    if not key:
        return None
    now = time.time()
    backoff_key = (key, method.upper())
    if _dune_key_exhausted_until.get(backoff_key, 0) > now:
        return None
    resp = await client.request(
        method, url,
        headers={"X-DUNE-API-KEY": key},
        timeout=timeout,
    )
    if resp.status_code == 402:
        _dune_key_exhausted_until[backoff_key] = now + DUNE_KEY_EXHAUSTED_BACKOFF_S
        logger.warning(
            f"Dune key …{key[-6:]} 402 on {method} {url.split('?')[0]}; "
            f"backed-off ({method}) for {DUNE_KEY_EXHAUSTED_BACKOFF_S//3600}h"
        )
        return None
    resp.raise_for_status()
    return resp


async def _dune_trigger_and_poll(
    client: httpx.AsyncClient, key: str, query_id: str,
) -> Optional[dict]:
    """Trigger a fresh Dune execution using (key, query_id) and poll until
    complete (max ~30s). Returns None on any failure (402, 404, timeout, bad
    state). Caller should fall back to cached/paginated data or try next pair.
    """
    # Step 1: trigger execution
    try:
        exec_resp = await _dune_request(
            client, "POST",
            f"https://api.dune.com/api/v1/query/{query_id}/execute",
            key, timeout=15.0,
        )
    except Exception as e:
        logger.warning(f"Dune execute trigger failed (key …{key[-6:]} q={query_id}): {e}")
        return None
    if exec_resp is None:
        return None
    execution_id = exec_resp.json().get("execution_id")
    if not execution_id:
        return None
    logger.info(f"Dune execution triggered (key …{key[-6:]} q={query_id}): {execution_id}")

    # Step 2: poll status (capped by DUNE_POLL_MAX_ATTEMPTS, 5s intervals)
    for _ in range(DUNE_POLL_MAX_ATTEMPTS):
        await asyncio.sleep(5)
        try:
            status_resp = await _dune_request(
                client, "GET",
                f"https://api.dune.com/api/v1/execution/{execution_id}/status",
                key, timeout=10.0,
            )
        except Exception as e:
            logger.warning(f"Dune status poll failed: {e}")
            return None
        if status_resp is None:
            return None
        state = status_resp.json().get("state", "")
        if state == "QUERY_STATE_COMPLETED":
            break
        if state in ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED", "QUERY_STATE_EXPIRED"):
            logger.warning(f"Dune execution {execution_id} ended state={state}")
            return None
    else:
        logger.warning(f"Dune execution {execution_id} did not finish within budget")
        return None

    # Step 3: fetch results
    try:
        res_resp = await _dune_request(
            client, "GET",
            f"https://api.dune.com/api/v1/execution/{execution_id}/results",
            key, timeout=30.0,
        )
    except Exception as e:
        logger.warning(f"Dune fresh-results fetch failed: {e}")
        return None
    if res_resp is None:
        return None
    data = res_resp.json()
    logger.info(f"Dune fresh execution ok: {data.get('result', {}).get('metadata', {}).get('row_count', 0)} rows")
    return data


async def _fetch_dune_all_pages(
    client: httpx.AsyncClient, key: str, query_id: str,
) -> Optional[dict]:
    """GET /query/{id}/results paginated using a single (key, query_id) pair.
    Returns full payload with merged rows or None if the key can't access this
    query (404) or 402'd on read.
    """
    url = f"https://api.dune.com/api/v1/query/{query_id}/results?limit={DUNE_PAGE_LIMIT}"
    resp = await _dune_request(client, "GET", url, key, timeout=30.0)
    if resp is None:
        return None
    data = resp.json()

    exec_id = data.get("execution_id")
    meta = (data.get("result") or {}).get("metadata") or {}
    total_rows = meta.get("total_row_count", 0)
    rows = list((data.get("result") or {}).get("rows") or [])

    if exec_id and total_rows > len(rows):
        offsets = list(range(len(rows), total_rows, DUNE_PAGE_LIMIT))
        async def _fetch_page(off: int) -> list:
            pg_url = f"https://api.dune.com/api/v1/execution/{exec_id}/results?limit={DUNE_PAGE_LIMIT}&offset={off}"
            try:
                pg_resp = await _dune_request(client, "GET", pg_url, key, timeout=30.0)
            except Exception:
                return []
            if pg_resp is None:
                return []
            return ((pg_resp.json().get("result") or {}).get("rows") or [])
        extra = await asyncio.gather(*[_fetch_page(o) for o in offsets], return_exceptions=True)
        for chunk in extra:
            if isinstance(chunk, list):
                rows.extend(chunk)

    data["result"]["rows"] = rows
    data["result"]["metadata"]["row_count"] = len(rows)
    return data


async def fetch_dune_cex_netflows(client: httpx.AsyncClient) -> Optional[dict]:
    """Fetch ETH CEX netflows from Dune Analytics (paginated to dodge per-request 402).

    Strategy:
      1. Return in-memory cache if fresh (< DUNE_CACHE_TTL).
      2. Page through Dune's cached results (instant once Dune has them).
      3. If Dune's cached results are older than DUNE_MAX_AGE_HOURS,
         trigger a fresh execution via POST /execute, poll until done,
         then re-page those results. Falls back to stale data on timeout.
    """
    global dune_cache, dune_cache_ts
    if not DUNE_KEY_QUERY_PAIRS:
        return None
    now = time.time()
    if dune_cache and (now - dune_cache_ts) < DUNE_CACHE_TTL:
        return dune_cache

    # Iterate (key, query_id) pairs. The first pair that returns data wins;
    # we still try to refresh that pair's cached data with a fresh execution
    # before returning. If a pair can't read its query (404/402 on GET), we
    # move on to the next pair.
    for key, query_id in DUNE_KEY_QUERY_PAIRS:
        try:
            data = await _fetch_dune_all_pages(client, key, query_id)
        except Exception as e:
            logger.warning(f"Dune read failed (key …{key[-6:]} q={query_id}): {e}")
            continue
        if not data:
            continue  # try next pair

        exec_ended = data.get("execution_ended_at", "")
        is_stale = False
        if exec_ended:
            try:
                ended_dt = datetime.fromisoformat(exec_ended.replace("Z", "+00:00"))
                age_hours = (datetime.now(timezone.utc) - ended_dt).total_seconds() / 3600
                logger.info(f"Dune cached result age: {age_hours:.1f}h (max {DUNE_MAX_AGE_HOURS}h, key …{key[-6:]})")
                if age_hours > DUNE_MAX_AGE_HOURS:
                    is_stale = True
            except Exception:
                is_stale = True
        else:
            is_stale = True

        if is_stale:
            logger.info(f"Dune results stale — triggering fresh execution (key …{key[-6:]} q={query_id})")
            fresh = await _dune_trigger_and_poll(client, key, query_id)
            if fresh:
                paged = await _fetch_dune_all_pages(client, key, query_id)
                if paged:
                    data = paged
            else:
                logger.info("Fresh execution unavailable — using stale results")

        dune_cache = data
        dune_cache_ts = now
        row_count = (data.get("result") or {}).get("metadata", {}).get("row_count", 0)
        logger.info(f"Dune fetch ok via key …{key[-6:]} q={query_id}: {row_count} rows")
        return data

    logger.warning("Dune: all (key, query_id) pairs failed — no data this cycle")
    return None


async def fetch_defillama_reserves(client: httpx.AsyncClient) -> Optional[dict]:
    """Fetch current ETH reserves (absolute stock) + historical averages from DefiLlama.

    Returns dict with:
      - totalEth: aggregate ETH across all tracked exchanges (current)
      - byExchange: [{name, ethReserve}, ...]
      - exchangeCount: how many responded
      - history: { "7d": {avg, delta_pct}, "30d": {...}, "90d": {...} }
    Cached for DEFILLAMA_CACHE_TTL (1h).
    """
    global llama_cache, llama_cache_ts
    now_ts = time.time()
    if llama_cache and (now_ts - llama_cache_ts) < DEFILLAMA_CACHE_TTL:
        return llama_cache

    now_dt = datetime.now(timezone.utc)

    async def _fetch_one(name: str, slug: str) -> Optional[dict]:
        """Returns current reserve + daily history for last 90 days."""
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

            # Latest snapshot
            latest_tokens = tokens_arr[-1].get("tokens", {})
            current_eth = sum(
                v for k, v in latest_tokens.items()
                if k.upper() in DEFILLAMA_ETH_KEYS
            )
            current_stable = sum(
                v for k, v in latest_tokens.items()
                if k.upper() in DEFILLAMA_STABLE_KEYS
            )

            # Historical daily ETH + stablecoins for last 90 days
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

            return {
                "name": name,
                "ethReserve": round(current_eth, 2),
                "stableReserve": round(current_stable, 2),
                "daily": daily,
            }
        except Exception as e:
            logger.warning(f"DefiLlama fetch failed for {name}: {e}")
        return None

    tasks = [_fetch_one(name, slug) for name, slug in DEFILLAMA_CEX_SLUGS.items()]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)
    exchange_results = [r for r in raw_results if isinstance(r, dict)]

    if not exchange_results:
        return None

    # Current totals
    by_exchange = [
        {"name": r["name"], "ethReserve": r["ethReserve"], "stableReserve": r.get("stableReserve", 0)}
        for r in exchange_results
    ]
    total_eth = sum(ex["ethReserve"] for ex in by_exchange)
    total_stable = sum(ex["stableReserve"] for ex in by_exchange)

    # Build aggregate daily series: sum all exchanges per calendar date.
    # Each exchange's tokens array has entries at slightly different
    # unix timestamps, so we normalize to YYYY-MM-DD to ensure each day
    # aggregates ALL exchanges that reported for that date.
    n_exchanges = len(exchange_results)
    day_sums: dict = {}  # "YYYY-MM-DD" -> {total_eth, total_stable, n_exchanges}
    for exr in exchange_results:
        seen_dates: set = set()
        for d in exr.get("daily", []):
            date_str = datetime.fromtimestamp(d["date"], tz=timezone.utc).strftime("%Y-%m-%d")
            if date_str in seen_dates:
                continue  # skip duplicate entries for same exchange on same day
            seen_dates.add(date_str)
            if date_str not in day_sums:
                day_sums[date_str] = {"eth": 0.0, "stable": 0.0, "n": 0}
            day_sums[date_str]["eth"] += d["eth"]
            day_sums[date_str]["stable"] += d.get("stable", 0)
            day_sums[date_str]["n"] += 1

    # Only keep days where ALL (or most) exchanges reported, to avoid
    # partial-day artifacts dragging the average down
    min_exchanges = max(1, n_exchanges - 1)  # allow 1 missing
    complete_days = sorted(
        [(dt_str, info["eth"], info["stable"]) for dt_str, info in day_sums.items()
         if info["n"] >= min_exchanges]
    )

    # Compute averages for each window (ETH + stablecoins)
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
    """Fetch ETHBTC spot taker buy/sell ratio from klines.

    Taker buy ratio > 0.5 = BTC holders aggressively buying ETH (rotation into ETH).
    Taker buy ratio < 0.5 = ETH holders selling for BTC (rotation out of ETH).
    """
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
            total_vol = float(k[5])   # base asset volume (ETH)
            taker_buy = float(k[9])   # taker buy base asset volume
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

        # Price change over available history
        price_chg = (hours[-1]["close"] - hours[0]["close"]) / hours[0]["close"] * 100 if hours[0]["close"] > 0 else 0

        # Signal based on sustained deviation from 0.5
        if avg_24h > 0.54:
            signal = "BTC_TO_ETH"
        elif avg_24h < 0.46:
            signal = "ETH_TO_BTC"
        else:
            signal = "BALANCED"

        # Volume stats for materiality assessment
        vol_24h_eth = sum(h["volume"] for h in last_24)

        return {
            "currentRatio": round(current, 4),
            "avg24h": round(avg_24h, 4),
            "avg7d": round(avg_7d, 4),
            "priceChange7dPct": round(price_chg, 2),
            "currentPrice": hours[-1]["close"],
            "signal": signal,
            "volume24hEth": round(vol_24h_eth, 2),
            "hourly": hours[-48:],  # last 48h for spark chart
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
DEFI_CACHE_TTL = 3600  # 1h

defi_cache: dict = {}
defi_cache_ts: float = 0


async def fetch_defi_eth_map(client: httpx.AsyncClient) -> Optional[dict]:
    """Fetch ETH locked in major DeFi protocols via DefiLlama.

    Groups by category: liquid_staking, restaking, lending, cdp.
    Uses same DEFILLAMA_ETH_KEYS to extract ETH-equivalent tokens.
    """
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

            # Try tokens array first (most accurate)
            tokens_arr = data.get("tokens", [])
            if tokens_arr:
                latest_tokens = tokens_arr[-1].get("tokens", {})
                eth_amount = sum(
                    v for k, v in latest_tokens.items()
                    if k.upper() in DEFILLAMA_ETH_KEYS
                )
                if eth_amount > 0:
                    return {
                        "name": name,
                        "category": info["category"],
                        "ethAmount": round(eth_amount, 2),
                    }

            # Fallback: derive from chain TVL on Ethereum + current ETH price
            chain_tvls = data.get("currentChainTvls", {})
            eth_chain_tvl = chain_tvls.get("Ethereum", 0)
            if eth_chain_tvl > 0:
                return {
                    "name": name,
                    "category": info["category"],
                    "ethAmount": 0,  # can't determine ETH-specific amount
                    "tvlUsd": round(eth_chain_tvl, 0),
                }

            return None
        except Exception as e:
            logger.warning(f"DeFi protocol fetch failed for {name}: {e}")
            return None

    tasks = [_fetch_protocol(name, info) for name, info in DEFI_ETH_PROTOCOLS.items()]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)
    protocols = [r for r in raw_results if isinstance(r, dict)]

    if not protocols:
        return None

    # Group by category
    categories: dict = {}
    for p in protocols:
        cat = p["category"]
        if cat not in categories:
            categories[cat] = {"totalEth": 0.0, "totalUsd": 0.0, "protocols": []}
        categories[cat]["totalEth"] += p.get("ethAmount", 0)
        categories[cat]["totalUsd"] += p.get("tvlUsd", 0)
        categories[cat]["protocols"].append({
            "name": p["name"],
            "ethAmount": p.get("ethAmount", 0),
            "tvlUsd": p.get("tvlUsd"),
        })

    # Round totals
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


# ╔════════════════════════════════════════════════════════════════════════╗
# ║ NEW DATA SOURCES — Fase 1                                              ║
# ║ ETF flows · Stablecoin supply · Deribit futures basis · Perp basis     ║
# ║ Macro cross-asset · Risk-free rate · Options skew · Options expiries   ║
# ║                                                                        ║
# ║ TODO fase 2: CME settlement scrape (tradfi segmentation), daily cadence║
# ║ TODO fase 2: replicate fetchers to api/index.py serverless mirror      ║
# ╚════════════════════════════════════════════════════════════════════════╝


# ── Shared helpers: Yahoo v8 chart + FRED CSV (macro + risk-free) ─────
async def _fetch_yahoo_daily(client: httpx.AsyncClient, symbol: str, range_: str = "3mo") -> Optional[list]:
    """Fetch daily OHLCV from Yahoo v8 chart API. No key required.

    Returns list of {date, open, high, low, close, volume} sorted ascending, or None.
    """
    try:
        url = f"{YAHOO_CHART_BASE}{urlquote(symbol, safe='')}"
        r = await client.get(
            url,
            params={"interval": "1d", "range": range_},
            timeout=15.0,
            headers={
                "User-Agent": BROWSER_UA,
                "Accept": "application/json, text/plain, */*",
            },
        )
        r.raise_for_status()
        payload = r.json()
        chart = (payload.get("chart") or {}).get("result")
        if not chart or not isinstance(chart, list):
            return None
        result = chart[0]
        timestamps = result.get("timestamp") or []
        quote = (result.get("indicators", {}).get("quote") or [{}])[0]
        opens   = quote.get("open")   or []
        highs   = quote.get("high")   or []
        lows    = quote.get("low")    or []
        closes  = quote.get("close")  or []
        volumes = quote.get("volume") or []

        rows = []
        for i, ts in enumerate(timestamps):
            if ts is None:
                continue
            def _safe(arr, idx):
                if idx < len(arr):
                    v = arr[idx]
                    return None if v is None else float(v)
                return None
            c = _safe(closes, i)
            if c is None:
                continue  # Yahoo occasionally nulls some cells; skip
            rows.append({
                "date":   datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"),
                "open":   _safe(opens, i),
                "high":   _safe(highs, i),
                "low":    _safe(lows, i),
                "close":  c,
                "volume": _safe(volumes, i),
            })
        return rows if rows else None
    except Exception as e:
        logger.warning(f"Yahoo fetch failed for {symbol}: {e}")
        return None


async def _fetch_fred_csv(client: httpx.AsyncClient, series_id: str) -> Optional[list]:
    """Fetch a FRED series via the public fredgraph CSV endpoint (no key needed)."""
    try:
        # FRED endpoint is slow; generous timeout
        r = await client.get(
            FRED_CSV_BASE,
            params={"id": series_id},
            timeout=45.0,
            headers={"User-Agent": BROWSER_UA},
        )
        r.raise_for_status()
        text = r.text
        if not text or "<html" in text.lower()[:200]:
            return None
        reader = csv.reader(io.StringIO(text))
        header = next(reader, None)
        if not header or len(header) < 2:
            return None
        rows = []
        for row in reader:
            if len(row) < 2:
                continue
            date_str, val = row[0].strip(), row[1].strip()
            if not date_str or val in ("", ".", "-"):
                continue
            try:
                rows.append({"date": date_str, "close": float(val)})
            except ValueError:
                continue
        return rows if rows else None
    except Exception as e:
        logger.warning(f"FRED fetch failed for {series_id}: {e}")
        return None


def _series_change_pct(rows: list, days: int) -> Optional[float]:
    """% change over last N trading days."""
    closes = [r["close"] for r in rows if r.get("close") is not None]
    if len(closes) < days + 1:
        return None
    latest, past = closes[-1], closes[-(days + 1)]
    if past <= 0:
        return None
    return round((latest / past - 1) * 100, 3)


def _series_realized_vol_20d(rows: list) -> Optional[float]:
    """Annualized realized vol (log returns, 20-day window, 252-day year)."""
    closes = [r["close"] for r in rows if r.get("close") is not None]
    if len(closes) < 21:
        return None
    log_rets = []
    for i in range(len(closes) - 20, len(closes)):
        c0, c1 = closes[i - 1], closes[i]
        if c0 > 0 and c1 > 0:
            log_rets.append(math.log(c1 / c0))
    if len(log_rets) < 10:
        return None
    try:
        return round(statistics.stdev(log_rets) * math.sqrt(252), 5)
    except statistics.StatisticsError:
        return None


# ── Risk-free rate (US 3M T-bill via Yahoo ^IRX; FRED DTB3 fallback) ──
def _sanity_bound_yield_pct(v: Optional[float], max_pct: float, label: str = "yield") -> Optional[float]:
    """Guard against provider-convention drift (e.g. Yahoo sometimes quotes yields as pct×10).

    If the raw value is above `max_pct` but `v/10` is within bounds, the source is
    almost certainly using the ×10 convention — divide. If even /10 is out of range,
    return None (caller treats as missing rather than propagating a bad number).
    T-bill/Treasury yields have never exceeded ~17% historically (1981 peak),
    so max_pct=20 for short-dated and 25 for long-dated is safely conservative.
    """
    if v is None:
        return None
    if v > max_pct:
        scaled = v / 10.0
        if 0 < scaled <= max_pct:
            logger.warning(f"Sanity bound triggered for {label}: raw={v:.3f} → using /10 = {scaled:.3f}")
            return scaled
        logger.error(f"Sanity bound FAILED for {label}: raw={v:.3f} (even /10 = {scaled:.3f} out of range). Dropping.")
        return None
    if v < 0:
        logger.warning(f"Sanity bound: negative {label}={v:.3f}; dropping")
        return None
    return v


async def fetch_risk_free_rate(client: httpx.AsyncClient) -> Optional[dict]:
    """US 3-month T-bill yield as proxy for risk-free rate. Cached 1 day.

    Primary source: Yahoo Finance ^IRX (CBOE 13-week T-bill; same infra as /macro).
    Fallback: FRED DTB3 CSV (reachable from most environments, but not all).

    Sanity bounds applied (TAREA 1):
      - Post-fetch: values > 20% are treated as ×10 convention and divided;
        values that remain out-of-band after /10 are dropped as corrupt.
      - Cross-check at warmup: if Yahoo value diverges > 50% from FRED,
        log ERROR and prefer FRED. Silently skipped if FRED unreachable.
    """
    global risk_free_cache, risk_free_cache_ts
    now_ts = time.time()
    if risk_free_cache and (now_ts - risk_free_cache_ts) < RISK_FREE_CACHE_TTL:
        return risk_free_cache

    is_warmup = not risk_free_cache_ts

    # Primary: Yahoo
    rows = await _fetch_yahoo_daily(client, RISK_FREE_YAHOO_SYM, range_="1mo")
    source, series = "yahoo", RISK_FREE_YAHOO_SYM

    # Fallback: FRED
    if not rows:
        rows = await _fetch_fred_csv(client, RISK_FREE_FRED_ID)
        if rows:
            source, series = "fred", RISK_FREE_FRED_ID

    if not rows:
        return risk_free_cache or None
    last = rows[-1].get("close")
    last = _sanity_bound_yield_pct(last, max_pct=20.0, label="3M T-bill")
    if last is None:
        return risk_free_cache or None

    # Cross-check Yahoo vs FRED at warmup — catches silent convention drift
    if source == "yahoo" and is_warmup:
        try:
            fred_rows = await _fetch_fred_csv(client, RISK_FREE_FRED_ID)
            if fred_rows:
                fred_last = _sanity_bound_yield_pct(fred_rows[-1].get("close"), 20.0, "FRED DTB3")
                if fred_last and fred_last > 0:
                    divergence = abs(last - fred_last) / fred_last
                    if divergence > 0.5:
                        logger.error(
                            f"Risk-free CROSS-CHECK FAILED: yahoo={last:.3f}% fred={fred_last:.3f}% "
                            f"(divergence {divergence*100:.1f}% > 50%). Preferring FRED."
                        )
                        last = fred_last
                        source, series = "fred", RISK_FREE_FRED_ID
                    else:
                        logger.info(
                            f"Risk-free cross-check ok: yahoo={last:.3f}% fred={fred_last:.3f}% "
                            f"(divergence {divergence*100:.1f}%)"
                        )
        except Exception as e:
            # FRED unreachable is common; skip cross-check silently
            logger.debug(f"FRED cross-check skipped: {e}")

    r_dec = last / 100.0
    result = {
        "rate":      round(r_dec, 5),
        "ratePct":   round(last, 3),
        "source":    source,
        "series":    series,
        "date":      rows[-1]["date"],
        "fetchedAt": int(now_ts * 1000),
    }
    risk_free_cache = result
    risk_free_cache_ts = now_ts
    logger.info(f"Risk-free rate ok ({source} {series}): {last:.3f}%")
    return result


def get_risk_free_rate_value(fallback: float = 0.04) -> float:
    """Return cached r as decimal, or fallback if cache empty."""
    if risk_free_cache and "rate" in risk_free_cache:
        return risk_free_cache["rate"]
    return fallback


# ── Curl fallback for TLS-fingerprinted sites (Farside/Cloudflare) ────
async def _curl_get(url: str, headers: dict, timeout: int = 25) -> Optional[str]:
    """Fetch a URL via subprocess curl. Used when httpx is blocked by TLS fingerprinting.

    curl uses the OS-level TLS stack, which matches real browsers better than
    Python's httpx/httpcore. Returns response body as text, or None on failure.
    """
    curl_bin = shutil.which("curl")
    if not curl_bin:
        logger.warning("_curl_get: curl binary not found in PATH")
        return None
    args = [curl_bin, "-s", "-L", "--max-time", str(timeout), url]
    for k, v in headers.items():
        args.extend(["-H", f"{k}: {v}"])
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout + 5)
        if proc.returncode != 0:
            logger.warning(f"_curl_get: curl rc={proc.returncode} err={err[:200]!r}")
            return None
        text = out.decode("utf-8", errors="replace")
        return text if text else None
    except Exception as e:
        logger.warning(f"_curl_get subprocess failed: {e}")
        return None


# ── ETF flows (Farside CSV → Farside HTML → SoSoValue → stale) ────────
async def fetch_etf_flows(client: httpx.AsyncClient) -> Optional[dict]:
    """Spot ETH ETF flows. Tries CSV → scrape HTML → SoSoValue, falls back to stale.

    Returns daily per-issuer flows + total + rolling 5d/20d aggregates.
    Cached 6h (daily cadence data).
    """
    global etf_cache, etf_cache_ts
    now_ts = time.time()
    if etf_cache and (now_ts - etf_cache_ts) < ETF_CACHE_TTL:
        return etf_cache

    data = None

    browser_headers = {
        "User-Agent":      BROWSER_UA,
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    # 1) Farside CSV (sometimes published, often 404)
    try:
        r = await client.get(FARSIDE_CSV_URL, timeout=15.0, headers=browser_headers)
        if r.status_code == 200 and r.text.strip() and not r.text.lstrip().startswith("<"):
            data = _parse_farside_csv(r.text)
            if data:
                data["source"] = "farside_csv"
    except Exception as e:
        logger.warning(f"Farside CSV fetch failed: {e}")

    # 2) Farside HTML table scrape via httpx; if 403 (TLS fingerprint),
    #    retry once via curl subprocess which mimics a real browser handshake.
    if not data:
        try:
            r = await client.get(FARSIDE_HTML_URL, timeout=20.0, headers=browser_headers)
            if r.status_code == 200 and r.text:
                data = _parse_farside_html(r.text)
                if data:
                    data["source"] = "farside_html"
            elif r.status_code == 403:
                logger.info("Farside 403 via httpx; falling back to curl subprocess")
                html = await _curl_get(FARSIDE_HTML_URL, browser_headers)
                if html:
                    data = _parse_farside_html(html)
                    if data:
                        data["source"] = "farside_html_curl"
        except Exception as e:
            logger.warning(f"Farside HTML fetch failed: {e}")
            html = await _curl_get(FARSIDE_HTML_URL, browser_headers)
            if html:
                data = _parse_farside_html(html)
                if data:
                    data["source"] = "farside_html_curl"

    # 3) SoSoValue fallback (may require key — best-effort)
    if not data:
        try:
            r = await client.get(
                SOSOVALUE_ETF_URL,
                params={"type": "us-eth-spot"},
                timeout=15.0,
                headers={"User-Agent": BROWSER_UA},
            )
            if r.status_code == 200:
                data = _parse_sosovalue(r.json())
                if data:
                    data["source"] = "sosovalue"
        except Exception as e:
            logger.warning(f"SoSoValue ETF fetch failed: {e}")

    # 4) Stale cache
    if not data:
        if etf_cache:
            logger.warning("ETF flows: all sources failed, serving stale cache")
            return etf_cache
        return None

    daily = data.get("daily", [])
    data["rolling5d"]  = _etf_rolling(daily, 5)
    data["rolling20d"] = _etf_rolling(daily, 20)
    data["fetchedAt"]  = int(now_ts * 1000)
    etf_cache = data
    etf_cache_ts = now_ts
    last = daily[-1] if daily else None
    logger.info(f"ETF flows ok ({data['source']}): {len(daily)} days · latest total = {last.get('total') if last else '—'} M$")
    return data


def _etf_rolling(daily: list, window: int) -> Optional[dict]:
    """Rolling aggregate over the last `window` daily rows.

    QW4: if the window contains any None totals (Farside omits a column or
    the cell is blank), set hasGaps=True and report nullCount so the frontend
    can show a data-quality flag. The sum/avg still use only the non-null
    values, but hasGaps tells consumers the rolling isn't a full window.
    """
    if len(daily) < window:
        return None
    daily_window = daily[-window:]
    totals = [r.get("total") for r in daily_window]
    null_count = sum(1 for t in totals if t is None)
    valid = [t for t in totals if t is not None]
    if not valid:
        return {
            "sum":           None,
            "avg":           None,
            "positiveDays":  0,
            "negativeDays":  0,
            "window":        window,
            "hasGaps":       True,
            "nullCount":     null_count,
        }
    return {
        "sum":           round(sum(valid), 2),
        "avg":           round(sum(valid) / len(valid), 2),
        "positiveDays":  sum(1 for v in valid if v > 0),
        "negativeDays":  sum(1 for v in valid if v < 0),
        "window":        window,
        "hasGaps":       null_count > 0,
        "nullCount":     null_count,
    }


# ── Stablecoin supply (DefiLlama /stablecoincharts) ───────────────────
async def fetch_stables_supply(client: httpx.AsyncClient) -> Optional[dict]:
    """Aggregate stablecoin supply + per-stablecoin deltas (1d/7d/30d). Cached 30 min."""
    global stables_cache, stables_cache_ts
    now_ts = time.time()
    if stables_cache and (now_ts - stables_cache_ts) < STABLES_CACHE_TTL:
        return stables_cache

    try:
        agg_resp = await client.get(STABLES_API_URL, timeout=30.0)
        agg_resp.raise_for_status()
        agg_chart = agg_resp.json()
        list_resp = await client.get(
            "https://stablecoins.llama.fi/stablecoins",
            params={"includePrices": "false"},
            timeout=30.0,
        )
        list_resp.raise_for_status()
        stables_list = list_resp.json()
    except Exception as e:
        logger.warning(f"Stables supply fetch failed: {e}")
        return stables_cache or None

    def _to_series(chart):
        out = []
        if not isinstance(chart, list):
            return out
        for pt in chart:
            try:
                ts_s = int(pt.get("date"))
                tc = pt.get("totalCirculating", {})
                usd = tc.get("peggedUSD") if isinstance(tc, dict) else tc
                usd_val = float(usd) if usd is not None else None
                if usd_val:
                    out.append({"ts": ts_s, "usd": usd_val})
            except (TypeError, ValueError):
                continue
        return out

    def _delta_abs(series, days):
        if not series or len(series) < days + 1:
            return None
        return round(series[-1]["usd"] - series[-(days + 1)]["usd"], 0)

    def _delta_pct(series, days):
        if not series or len(series) < days + 1:
            return None
        past = series[-(days + 1)]["usd"]
        if past <= 0:
            return None
        return round((series[-1]["usd"] / past - 1) * 100, 4)

    agg_series = _to_series(agg_chart)
    agg_out = {
        "currentUsd":  agg_series[-1]["usd"] if agg_series else None,
        "delta1dUsd":  _delta_abs(agg_series, 1),
        "delta7dUsd":  _delta_abs(agg_series, 7),
        "delta30dUsd": _delta_abs(agg_series, 30),
        "delta1dPct":  _delta_pct(agg_series, 1),
        "delta7dPct":  _delta_pct(agg_series, 7),
        "delta30dPct": _delta_pct(agg_series, 30),
        "series": [
            {"ts": p["ts"] * 1000, "usd": p["usd"]}
            for p in agg_series[-90:]
        ],
    }

    # Resolve IDs for tracked stables from /stablecoins listing
    by_stable, id_map = {}, {}
    if isinstance(stables_list, dict):
        for item in stables_list.get("peggedAssets", []):
            symbol = item.get("symbol")
            if symbol not in STABLES_TRACKED:
                continue
            circ = item.get("circulating", {})
            usd = circ.get("peggedUSD") if isinstance(circ, dict) else circ
            try:
                usd_val = float(usd) if usd is not None else None
            except (TypeError, ValueError):
                usd_val = None
            by_stable[symbol] = {
                "currentUsd": usd_val,
                "name":       STABLES_TRACKED[symbol],
                "id":         item.get("id"),
            }
            id_map[symbol] = item.get("id")

    # TAREA 2: verified 2026-04 — /stablecoincharts/all?stablecoin=<id> returns
    # a per-stable series (3062 points for USDT) distinct from aggregate.
    # If the endpoint ever regresses and returns the aggregate again, this
    # guard compares head values and marks the entry as stale rather than
    # propagating duplicate numbers.
    async def _single(symbol, sid):
        if sid is None:
            by_stable[symbol]["note"] = "per-stable id missing"
            return
        try:
            r = await client.get(
                STABLES_API_URL,
                params={"stablecoin": str(sid)},
                timeout=20.0,
            )
            r.raise_for_status()
            series = _to_series(r.json())
        except Exception as e:
            logger.warning(f"Stables per-stable chart fetch failed for {symbol} (id={sid}): {e}")
            by_stable[symbol]["note"] = "per-stable series unavailable"
            return
        if not series:
            logger.warning(f"Stables per-stable chart empty for {symbol} (id={sid})")
            by_stable[symbol]["note"] = "per-stable series unavailable"
            return
        # Guard: if per-stable latest ≈ aggregate latest, the endpoint is regressed
        # (it's returning the aggregate ignoring the stablecoin param).
        if agg_series and series:
            agg_latest = agg_series[-1]["usd"]
            our_latest = series[-1]["usd"]
            if agg_latest and our_latest and abs(our_latest / agg_latest - 1.0) < 0.02:
                logger.error(
                    f"Stables per-stable endpoint regressed: {symbol} latest={our_latest/1e9:.1f}B "
                    f"matches aggregate={agg_latest/1e9:.1f}B within 2%. Dropping per-stable values."
                )
                by_stable[symbol]["note"] = "per-stable endpoint returning aggregate — dropped"
                return
        by_stable[symbol].update({
            "delta1dUsd":  _delta_abs(series, 1),
            "delta7dUsd":  _delta_abs(series, 7),
            "delta30dUsd": _delta_abs(series, 30),
            "delta1dPct":  _delta_pct(series, 1),
            "delta7dPct":  _delta_pct(series, 7),
            "delta30dPct": _delta_pct(series, 30),
            "series": [
                {"ts": p["ts"] * 1000, "usd": p["usd"]}
                for p in series[-90:]
            ],
        })

    await asyncio.gather(*(_single(s, sid) for s, sid in id_map.items()), return_exceptions=True)

    # Coverage diagnostic: what % of aggregate do the tracked stables cover?
    if agg_out.get("currentUsd") and by_stable:
        tracked_sum = sum((v.get("currentUsd") or 0) for v in by_stable.values())
        coverage_pct = 100 * tracked_sum / agg_out["currentUsd"]
        if coverage_pct < 50:
            logger.warning(
                f"Stables tracked coverage low: {coverage_pct:.1f}% "
                f"(tracked=${tracked_sum/1e9:.1f}B vs aggregate=${agg_out['currentUsd']/1e9:.1f}B). "
                f"Tracked list: {list(STABLES_TRACKED.keys())}"
            )

    result = {
        "aggregate":    agg_out,
        "byStablecoin": by_stable,
        "tracked":      list(STABLES_TRACKED.keys()),
        "source":       "defillama",
        "fetchedAt":    int(now_ts * 1000),
    }
    stables_cache    = result
    stables_cache_ts = now_ts
    if agg_out["currentUsd"]:
        logger.info(f"Stables supply ok: aggregate = ${agg_out['currentUsd']/1e9:,.1f}B")
    return result


# ── Deribit futures basis (dated contracts) ──────────────────────────
async def fetch_deribit_basis(client: httpx.AsyncClient, spot: Optional[float] = None) -> Optional[dict]:
    """Deribit ETH dated-futures basis per expiry. Cached 30s. Persists raw for expiry calendar."""
    global deribit_basis_cache, deribit_basis_cache_ts
    global deribit_futures_raw, deribit_futures_raw_ts
    now_ts = time.time()
    if deribit_basis_cache and (now_ts - deribit_basis_cache_ts) < DERIBIT_BASIS_CACHE_TTL:
        return deribit_basis_cache

    try:
        r = await client.get(
            f"{DERIBIT_API}/api/v2/public/get_book_summary_by_currency",
            params={"currency": "ETH", "kind": DERIBIT_FUTURES_KIND},
            timeout=15.0,
        )
        r.raise_for_status()
        summaries = r.json().get("result", [])
    except Exception as e:
        logger.warning(f"Deribit futures fetch failed: {e}")
        return deribit_basis_cache or None

    deribit_futures_raw    = summaries
    deribit_futures_raw_ts = now_ts

    # Infer spot from PERPETUAL mark if not provided
    if spot is None or spot <= 0:
        for s in summaries:
            if s.get("instrument_name") == "ETH-PERPETUAL":
                try:
                    spot = float(s.get("mark_price") or s.get("last") or 0)
                except (TypeError, ValueError):
                    spot = None
                break
    if spot is None or spot <= 0:
        return None

    now_dt = datetime.now(timezone.utc)
    by_expiry = []
    for s in summaries:
        name = s.get("instrument_name", "")
        if "PERPETUAL" in name or "_" in name:
            continue
        parsed = _parse_deribit_future_expiry(name)
        if not parsed:
            continue
        exp_dt = parsed
        dte = (exp_dt - now_dt).total_seconds() / 86400
        if dte <= 0:
            continue
        mark = s.get("mark_price") or s.get("last")
        try:
            mark = float(mark) if mark is not None else None
        except (TypeError, ValueError):
            mark = None
        if not mark:
            continue
        basis_abs = mark - spot
        basis_pct = basis_abs / spot
        basis_ann = basis_pct * (365.0 / dte) if dte > 0 else None
        by_expiry.append({
            "instrument":  name,
            "expiry":      exp_dt.strftime("%Y-%m-%d"),
            "expiryTs":    int(exp_dt.timestamp() * 1000),
            "dte":         round(dte, 2),
            "mark":        round(mark, 2),
            "spot":        round(spot, 2),
            "basisAbs":    round(basis_abs, 2),
            "basisPct":    round(basis_pct * 100, 4),
            "basisAnnualizedPct": round(basis_ann * 100, 2) if basis_ann is not None else None,
            "openInterest": s.get("open_interest"),
            "volume24h":    s.get("volume"),
        })
    by_expiry.sort(key=lambda x: x["dte"])

    front = by_expiry[0] if by_expiry else None
    term_mid = None
    if len(by_expiry) >= 3:
        vals = [b["basisAnnualizedPct"] for b in by_expiry[:3] if b["basisAnnualizedPct"] is not None]
        if vals:
            term_mid = round(sum(vals) / len(vals), 2)

    result = {
        "spot":                 round(spot, 2),
        "byExpiry":             by_expiry,
        "front":                front,
        "termAvgAnnualizedPct": term_mid,
        "source":               "deribit",
        "fetchedAt":            int(now_ts * 1000),
    }
    deribit_basis_cache    = result
    deribit_basis_cache_ts = now_ts
    if front:
        logger.info(f"Deribit basis ok: front {front['expiry']} dte={front['dte']:.1f}d "
                    f"basis_ann={front['basisAnnualizedPct']}%")
    return result


def _parse_deribit_future_expiry(name: str) -> Optional[datetime]:
    """'ETH-29DEC24' → datetime at 08:00 UTC (Deribit expiry time)."""
    m = re.match(r"^ETH-(\d{1,2})([A-Z]{3})(\d{2})$", name)
    if not m:
        return None
    day, mon, yr = m.groups()
    months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
              "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
    if mon not in months:
        return None
    try:
        return datetime(2000 + int(yr), months[mon], int(day), 8, 0, tzinfo=timezone.utc)
    except ValueError:
        return None


# ── Spot-perp basis (pure compute, reuses cached prices) ──────────────
def compute_perp_basis(
    spot_price: Optional[float],
    perp_prices: dict,
    front_future_data: Optional[dict] = None,
) -> Optional[dict]:
    """Compute spot-perp spread and perp-quarterly basis. No network."""
    if not spot_price or spot_price <= 0:
        return None
    spot_perp = {}
    for venue, perp in perp_prices.items():
        try:
            perp_val = float(perp) if perp is not None else None
        except (TypeError, ValueError):
            perp_val = None
        if not perp_val or perp_val <= 0:
            continue
        spread    = perp_val - spot_price
        basis_pct = (spread / spot_price) * 100
        spot_perp[venue] = {
            "perp":     round(perp_val, 2),
            "spread":   round(spread, 4),
            "basisPct": round(basis_pct, 4),
        }

    perp_quarterly = None
    if front_future_data and spot_perp:
        perp_values = [v["perp"] for v in spot_perp.values()]
        if perp_values:
            perp_avg = sum(perp_values) / len(perp_values)
            future   = front_future_data.get("mark")
            dte      = front_future_data.get("dte")
            if future and dte and dte > 0:
                spread_q    = future - perp_avg
                basis_pct_q = (spread_q / perp_avg) * 100
                basis_ann_q = basis_pct_q * (365.0 / dte)
                perp_quarterly = {
                    "perpAvg":   round(perp_avg, 2),
                    "quarterly": round(future, 2),
                    "dte":       round(dte, 2),
                    "spread":    round(spread_q, 2),
                    "basisPct":  round(basis_pct_q, 4),
                    "basisAnnualizedPct": round(basis_ann_q, 2),
                }

    max_spread = max((v["spread"] for v in spot_perp.values()), default=None)
    min_spread = min((v["spread"] for v in spot_perp.values()), default=None)

    return {
        "spot":          round(spot_price, 2),
        "spotPerp":      spot_perp,
        "perpQuarterly": perp_quarterly,
        "spreadRange":   {
            "max": round(max_spread, 4) if max_spread is not None else None,
            "min": round(min_spread, 4) if min_spread is not None else None,
        },
        "fetchedAt":     int(time.time() * 1000),
    }


# ── Macro cross-asset (Yahoo Finance v8 chart API) ────────────────────
async def fetch_macro(client: httpx.AsyncClient) -> Optional[dict]:
    """DXY, SPX, VIX, US10Y, BTC: spot + 1d/7d/30d change + 20d realized vol. Cached 5 min."""
    global macro_cache, macro_cache_ts
    now_ts = time.time()
    if macro_cache and (now_ts - macro_cache_ts) < MACRO_CACHE_TTL:
        return macro_cache

    async def _one(label, symbol):
        rows = await _fetch_yahoo_daily(client, symbol, range_="3mo")
        if not rows:
            return label, None
        close = rows[-1].get("close")
        if close is None:
            return label, None
        # Sanity bound for Treasury yields (Yahoo has historically switched between
        # % and ×10 conventions). 25% is a safely conservative cap — 1981 peak was ~17%.
        if label == "US10Y":
            close = _sanity_bound_yield_pct(close, max_pct=25.0, label=label)
            if close is None:
                return label, None
            # Also normalize the series we pass to _series_change_pct / vol so pct
            # changes remain comparable across convention switches:
            for r in rows:
                if r.get("close") is not None:
                    bounded = _sanity_bound_yield_pct(r["close"], max_pct=25.0, label=label)
                    r["close"] = bounded
        return label, {
            "symbol":         symbol,
            "value":          close,
            "date":           rows[-1]["date"],
            "change1dPct":    _series_change_pct(rows, 1),
            "change7dPct":    _series_change_pct(rows, 5),
            "change30dPct":   _series_change_pct(rows, 21),
            "realizedVol20d": _series_realized_vol_20d(rows),
        }

    pairs = list(MACRO_SYMBOLS.items())
    results = await asyncio.gather(*(_one(l, s) for l, s in pairs), return_exceptions=True)
    by_asset = {}
    for item in results:
        if isinstance(item, tuple) and len(item) == 2:
            lab, val = item
            if val is not None:
                by_asset[lab] = val

    if not by_asset:
        return macro_cache or None

    result = {
        "byAsset":   by_asset,
        "source":    "yahoo",
        "fetchedAt": int(now_ts * 1000),
    }
    macro_cache    = result
    macro_cache_ts = now_ts
    logger.info(f"Macro ok: {len(by_asset)}/{len(MACRO_SYMBOLS)} assets fetched")
    return result


# ── Black-Scholes delta helpers (for options skew) ────────────────────
def _bs_delta(S: float, K: float, T: float, r: float, sigma: float, is_call: bool) -> float:
    """Black-Scholes delta. T in years, sigma as decimal."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    cdf = 0.5 * (1 + math.erf(d1 / math.sqrt(2)))
    return cdf if is_call else cdf - 1.0


def _parse_deribit_option_instrument(name: str):
    """'ETH-29DEC24-3000-C' → (expiry_dt, strike, is_call) or None."""
    m = re.match(r"^ETH-(\d{1,2})([A-Z]{3})(\d{2})-(\d+(?:\.\d+)?)-([CP])$", name)
    if not m:
        return None
    day, mon, yr, strike, cp = m.groups()
    months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
              "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
    if mon not in months:
        return None
    try:
        exp_dt = datetime(2000 + int(yr), months[mon], int(day), 8, 0, tzinfo=timezone.utc)
        return (exp_dt, float(strike), cp == "C")
    except ValueError:
        return None


def _interp_iv_at_delta(opts_sorted_by_strike: list, target_delta: float) -> Optional[float]:
    """Linear interp of IV between two strikes bracketing the target delta."""
    if not opts_sorted_by_strike:
        return None
    opts = opts_sorted_by_strike
    for i in range(len(opts) - 1):
        d0, d1 = opts[i]["delta"], opts[i + 1]["delta"]
        if (d0 >= target_delta >= d1) or (d0 <= target_delta <= d1):
            if d1 == d0:
                return opts[i]["iv"]
            w = (target_delta - d0) / (d1 - d0)
            return opts[i]["iv"] + w * (opts[i + 1]["iv"] - opts[i]["iv"])
    nearest = min(opts, key=lambda x: abs(x["delta"] - target_delta))
    if abs(nearest["delta"] - target_delta) > 0.12:
        return None  # too far, unreliable
    return nearest["iv"]


def compute_options_skew(
    deribit_book_summary: list,
    spot: Optional[float],
    r: Optional[float] = None,
    strike_filter_pct: float = 0.30,
) -> Optional[dict]:
    """RR25 and BF25 per expiry from Deribit book_summary.

    - r: risk-free rate (decimal). If None, read from cached ^irx (fallback 0.04).
    - strike_filter_pct: skip strikes outside ±30% of spot (fast + reliable for interp).
    """
    if not deribit_book_summary or not spot or spot <= 0:
        return None
    if r is None:
        r = get_risk_free_rate_value(0.04)

    low_bound  = spot * (1 - strike_filter_pct)
    high_bound = spot * (1 + strike_filter_pct)

    now_dt = datetime.now(timezone.utc)
    by_expiry: dict = {}
    for s in deribit_book_summary:
        name = s.get("instrument_name", "")
        parsed = _parse_deribit_option_instrument(name)
        if not parsed:
            continue
        exp_dt, strike, is_call = parsed
        if strike < low_bound or strike > high_bound:
            continue
        iv_pct = s.get("mark_iv")
        try:
            iv = float(iv_pct) / 100.0 if iv_pct is not None else None
        except (TypeError, ValueError):
            iv = None
        if not iv or iv <= 0:
            continue
        T = (exp_dt - now_dt).total_seconds() / (365.25 * 86400)
        if T <= 0:
            continue
        by_expiry.setdefault(exp_dt, {"calls": [], "puts": []})
        entry = {"strike": strike, "iv": iv, "T": T}
        (by_expiry[exp_dt]["calls"] if is_call else by_expiry[exp_dt]["puts"]).append(entry)

    term = []
    for exp_dt, groups in by_expiry.items():
        calls = sorted(groups["calls"], key=lambda x: x["strike"])
        puts  = sorted(groups["puts"],  key=lambda x: x["strike"])
        if not calls or not puts:
            continue
        for c in calls:
            c["delta"] = _bs_delta(spot, c["strike"], c["T"], r, c["iv"], True)
        for p in puts:
            p["delta"] = _bs_delta(spot, p["strike"], p["T"], r, p["iv"], False)
        iv_call25 = _interp_iv_at_delta(calls, target_delta=0.25)
        iv_put25  = _interp_iv_at_delta(puts,  target_delta=-0.25)

        all_strikes = calls + puts
        atm = min(all_strikes, key=lambda x: abs(x["strike"] - spot))
        iv_atm = atm["iv"]

        rr25 = bf25 = None
        if iv_call25 is not None and iv_put25 is not None:
            rr25 = iv_call25 - iv_put25
            bf25 = 0.5 * (iv_call25 + iv_put25) - iv_atm

        dte_days = (exp_dt - now_dt).total_seconds() / 86400
        term.append({
            "expiry":        exp_dt.strftime("%Y-%m-%d"),
            "expiryTs":      int(exp_dt.timestamp() * 1000),
            "dte":           round(dte_days, 2),
            "ivAtm":         round(iv_atm, 5),
            "ivCall25":      round(iv_call25, 5) if iv_call25 is not None else None,
            "ivPut25":       round(iv_put25, 5)  if iv_put25  is not None else None,
            "rr25":          round(rr25, 5) if rr25 is not None else None,
            "bf25":          round(bf25, 5) if bf25 is not None else None,
            "strikesInScope": len(all_strikes),
        })

    term.sort(key=lambda x: x["dte"])

    def _pick(target):
        return min(term, key=lambda x: abs(x["dte"] - target)) if term else None

    return {
        "spot":             round(spot, 2),
        "riskFreeRate":     round(r, 5),
        "strikeFilterPct":  strike_filter_pct,
        "term":             term,
        "canonical": {
            "t7d":  _pick(7),
            "t30d": _pick(30),
            "t60d": _pick(60),
            "t90d": _pick(90),
        },
        "source":    "deribit",
        "fetchedAt": int(time.time() * 1000),
    }


# ── Options expiry calendar (notional + put/call + pin risk) ──────────
def compute_options_expiries(
    deribit_book_summary: list,
    spot: Optional[float],
    max_expiries: int = 8,
    pin_strike_range_pct: float = 0.02,
) -> Optional[dict]:
    """Next N expiries with notional USD, put/call ratio, pin risk (OI in ±2%/DTE)."""
    if not deribit_book_summary or not spot or spot <= 0:
        return None

    now_dt = datetime.now(timezone.utc)
    by_expiry: dict = {}
    for s in deribit_book_summary:
        name = s.get("instrument_name", "")
        parsed = _parse_deribit_option_instrument(name)
        if not parsed:
            continue
        exp_dt, strike, is_call = parsed
        dte = (exp_dt - now_dt).total_seconds() / 86400
        if dte <= 0:
            continue
        oi = s.get("open_interest")
        try:
            oi_val = float(oi) if oi is not None else 0.0
        except (TypeError, ValueError):
            oi_val = 0.0
        slot = by_expiry.setdefault(exp_dt, {"callOi": 0.0, "putOi": 0.0, "strikes": [], "dte": dte})
        if is_call:
            slot["callOi"] += oi_val
        else:
            slot["putOi"]  += oi_val
        slot["strikes"].append({"strike": strike, "is_call": is_call, "oi": oi_val})

    pin_low  = spot * (1 - pin_strike_range_pct)
    pin_high = spot * (1 + pin_strike_range_pct)

    upcoming = []
    for exp_dt, info in by_expiry.items():
        total_oi = info["callOi"] + info["putOi"]
        if total_oi <= 0:
            continue
        notional_usd = total_oi * spot
        pc = (info["putOi"] / info["callOi"]) if info["callOi"] > 0 else None
        pin_oi = sum(x["oi"] for x in info["strikes"]
                     if pin_low <= x["strike"] <= pin_high)
        dte_clamped = max(info["dte"], 0.25)
        # Gamma scales as 1/√T, not 1/T — under a linear 1/T weighting, a weekly
        # (DTE≈7) with 10k OI got the same score as a monthly (DTE≈30) with 4.3k OI,
        # which massively over-weighted ultra-short expiries. With √T the
        # weekly/monthly ratio matches physical gamma exposure (≈1.9× instead of ~4×).
        pin_risk = pin_oi / math.sqrt(dte_clamped)
        upcoming.append({
            "expiry":        exp_dt.strftime("%Y-%m-%d"),
            "expiryTs":      int(exp_dt.timestamp() * 1000),
            "dte":           round(info["dte"], 2),
            "callOi":        round(info["callOi"], 2),
            "putOi":         round(info["putOi"], 2),
            "totalOi":       round(total_oi, 2),
            "notionalUsd":   round(notional_usd, 0),
            "putCallRatio":  round(pc, 4) if pc is not None else None,
            "pinOi":         round(pin_oi, 2),
            "pinRisk":       round(pin_risk, 2),
        })

    upcoming.sort(key=lambda x: x["dte"])
    upcoming = upcoming[:max_expiries]

    total_notional = sum(x["notionalUsd"] for x in upcoming)
    total_call_oi  = sum(x["callOi"] for x in upcoming)
    total_put_oi   = sum(x["putOi"]  for x in upcoming)
    agg_pc = (total_put_oi / total_call_oi) if total_call_oi > 0 else None
    largest = max(upcoming, key=lambda x: x["notionalUsd"]) if upcoming else None

    return {
        "spot":                    round(spot, 2),
        "upcoming":                upcoming,
        "totalNotionalUsd":        total_notional,
        "aggregatePutCallRatio":   round(agg_pc, 4) if agg_pc is not None else None,
        "largestExpiry":           largest,
        "pinStrikeRangePct":       pin_strike_range_pct,
        "source":                  "deribit",
        "fetchedAt":               int(time.time() * 1000),
    }


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

    # Parse rows: convert hour string to UTC timestamp ms
    parsed = []
    for r in rows:
        try:
            hour_str = r.get("hour", "")
            # format: "2026-04-04 21:00:00.000 UTC"
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

    # Aggregate by rolling windows (relative to most recent hour)
    windows = {"1h": 1, "6h": 6, "24h": 24, "7d": 168}
    aggregates = {}
    for label, hours in windows.items():
        cutoff = max_ts - (hours - 1) * HOUR_MS  # inclusive of last `hours` hours
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

    # Per-exchange breakdown over last 24h
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
    by_exchange.sort(key=lambda x: x["netInflowEth"])  # most-negative (bullish) first

    # Hourly series across all exchanges (last ~7d)
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
    # For each hour h, compute the sum of net_eth over hours [h-23 .. h].
    # Each rolling sum is heavily overlapping with its neighbors, but serves as a
    # rough empirical distribution of "typical 24h flows" under the current regime.
    rolling_24h_list: list = []
    n_h = len(hourly_series)
    if n_h >= 24:
        running = sum(h["netInflowEth"] for h in hourly_series[:24])
        rolling_24h_list.append(running)
        for i in range(24, n_h):
            running += hourly_series[i]["netInflowEth"] - hourly_series[i - 24]["netInflowEth"]
            rolling_24h_list.append(running)

    # Exclude the most recent 24h window from the comparison distribution so we're
    # scoring "current vs historical" rather than "current vs including-itself".
    hist_distribution = rolling_24h_list[:-1] if len(rolling_24h_list) > 1 else rolling_24h_list

    mean_24h = statistics.fmean(hist_distribution) if hist_distribution else 0.0
    stdev_24h = statistics.stdev(hist_distribution) if len(hist_distribution) > 1 else 0.0
    z_score = (net_24h_eth - mean_24h) / stdev_24h if stdev_24h > 0 else 0.0

    # Percentile rank of current value within historical distribution
    if hist_distribution:
        rank = sum(1 for v in hist_distribution if v <= net_24h_eth)
        percentile = rank / len(hist_distribution) * 100.0
    else:
        percentile = 50.0

    # Flow-to-volume ratio (|net USD 24h| / spot volume USD 24h)
    flow_vol_ratio_pct = None
    if spot_volume_usd_24h and spot_volume_usd_24h > 0:
        flow_vol_ratio_pct = abs(net_24h_usd) / spot_volume_usd_24h * 100.0

    # Magnitude label from |z-score|
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
    #    This avoids the trap where mean_24h is structurally large (e.g. a 7d
    #    inflow regime) and the noise_band absorbs even clearly elevated flows.
    #  - Otherwise we fall back to absolute-net vs a noise band so tiny flips
    #    near zero don't swing direction.
    if abs_z >= 1.0:
        direction = "BEARISH" if z_score > 0 else "BULLISH"
    else:
        noise_band = max(abs(mean_24h), 500)  # at least ±500 ETH of deadband
        if net_24h_eth < -noise_band:
            direction = "BULLISH"   # withdrawal → potential HODL / buy pressure
        elif net_24h_eth > noise_band:
            direction = "BEARISH"   # deposit → potential sell pressure
        else:
            direction = "NEUTRAL"

    # Flow-price divergence over 24h window
    # Flow dir (bullish=+1, bearish=-1) vs realized price dir
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
            divergence = "FLAT_PRICE"  # flow has direction, price doesn't — accumulation/distribution under cover
        elif flow_sign == price_sign:
            divergence = "CONFIRMED"   # flow direction matches realized move
        else:
            divergence = "DIVERGENT"   # flow direction opposite to realized move — watch for reversal

    # Legacy `bias` field kept for backwards compatibility with older frontend:
    # combines direction + magnitude into a single label.
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
            # Risk-free rate (US 3M T-bill, cached 1d) — needed for options skew BS delta
            fetch_risk_free_rate(client),
            # Deribit futures basis (cached 30s) — also stores raw for expiry calendar
            fetch_deribit_basis(client),
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
        bn_oi_hist_5m,                                       # 40
        dune_cex_raw,                                        # 41
        defillama_reserves_raw,                              # 42
        ethbtc_taker_raw,                                    # 43
        defi_eth_map_raw,                                    # 44
        risk_free_raw,                                       # 45
        deribit_basis_raw,                                   # 46
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

    # 5m OI history (parallel structure to oi_hist)
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
        # Keep "change48h" semantic: use last 48 hourly entries (or full history if shorter)
        oi_48h_start = oi_hist[-49] if len(oi_hist) >= 49 else oi_hist[0]
        if oi_48h_start.get("value"):
            oi_change = ((oi_hist[-1]["value"] - oi_48h_start["value"]) / oi_48h_start["value"]) * 100

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

    # ── Money Quality: plata nueva vs short covering (OI vs Price) ──
    money_quality = compute_money_quality(
        oi_hist,
        perp_flow,
        funding_rate=bn_fund_rate,
        klines_1h=bn_klines_vol if isinstance(bn_klines_vol, list) else None,
    )

    # ── Stochastics multi-timeframe ──────────────────────────────────
    klines_for_stoch = {
        "1m":  kl_1m if isinstance(kl_1m, list) else [],
        "5m":  kl_5m_stoch if isinstance(kl_5m_stoch, list) else [],
        "15m": kl_15m if isinstance(kl_15m, list) else [],
        "1h":  bn_klines_vol if isinstance(bn_klines_vol, list) else [],
        "4h":  kl_4h_stoch if isinstance(kl_4h_stoch, list) else [],
    }
    stochastics_data = compute_stochastics_multi(klines_for_stoch)

    # ── Cut-anchored MQ: OI behaviour from the moment fast %K entered the zone ──
    cut_anchored_mq = compute_cut_anchored_mq(
        klines_for_stoch,
        oi_hist_1h=oi_hist,
        oi_hist_5m=oi_hist_5m,
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

    # ── Fase 1 · Derived basis / skew / expiries (pure compute, no network) ──
    # Extract perp last prices per venue from already-fetched tickers
    try:
        bybit_perp_last = None
        if isinstance(bybit_perp_ticker, dict):
            lst = (bybit_perp_ticker.get("result", {}) or {}).get("list", [])
            if lst:
                bybit_perp_last = safe_float(lst[0], "lastPrice")
    except Exception:
        bybit_perp_last = None
    try:
        okx_perp_last = None
        if isinstance(okx_perp_ticker, dict):
            okx_data = okx_perp_ticker.get("data", []) or []
            if okx_data:
                okx_perp_last = safe_float(okx_data[0], "last")
    except Exception:
        okx_perp_last = None
    # Hyperliquid mark price for ETH
    hl_mark = None
    try:
        if isinstance(hyperliquid_raw, list) and len(hyperliquid_raw) == 2:
            meta, asset_ctxs = hyperliquid_raw
            if isinstance(meta, dict) and isinstance(asset_ctxs, list):
                for i, coin in enumerate(meta.get("universe", [])):
                    if isinstance(coin, dict) and coin.get("name") == "ETH":
                        if i < len(asset_ctxs) and isinstance(asset_ctxs[i], dict):
                            hl_mark = safe_float(asset_ctxs[i], "markPx")
                        break
    except Exception:
        hl_mark = None

    perp_prices_by_venue = {
        "binance":     safe_float(bn_premium, "markPrice"),
        "okx":         okx_perp_last,
        "bybit":       bybit_perp_last,
        "hyperliquid": hl_mark,
    }

    front_future = None
    if isinstance(deribit_basis_raw, dict):
        front_future = deribit_basis_raw.get("front")

    perp_basis_computed = compute_perp_basis(
        spot_price=current_price,
        perp_prices=perp_prices_by_venue,
        front_future_data=front_future,
    )

    # Options skew + expiry calendar from the Deribit book_summary already in memory
    deribit_book_list = []
    if isinstance(deribit_options_raw, dict):
        deribit_book_list = deribit_options_raw.get("result", []) or []

    r_for_skew = risk_free_raw.get("rate") if isinstance(risk_free_raw, dict) else None
    options_skew_computed = compute_options_skew(
        deribit_book_summary=deribit_book_list,
        spot=current_price,
        r=r_for_skew,
    )
    options_expiries_computed = compute_options_expiries(
        deribit_book_summary=deribit_book_list,
        spot=current_price,
    )

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
        # ── Fase 1 · new sources ──────────────────────────────────────
        # Cache-backed (populated by background refresher); read as-is.
        "etfFlows":       etf_cache     or None,
        "stablesSupply":  stables_cache or None,
        "macro":          macro_cache   or None,
        "riskFreeRate":   risk_free_raw if isinstance(risk_free_raw, dict) else (risk_free_cache or None),
        # Fetched inside this gather:
        "deribitBasis":   deribit_basis_raw if isinstance(deribit_basis_raw, dict) else None,
        # Pure-compute derivatives over already-fetched data:
        "perpBasis":       perp_basis_computed,
        "optionsSkew":     options_skew_computed,
        "optionsExpiries": options_expiries_computed,
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


# ── Fase 2 · Parquet persister (append-only daily partitions) ────────
# Layout:
#   {PERSIST_PATH}/YYYY-MM-DD/snapshot.parquet       (1 row per minute, wide flat schema)
#   {PERSIST_PATH}/YYYY-MM-DD/microstructure.parquet (1 row per 5s, order book + CVD)
#
# Snapshot schema rules (hybrid):
#   • ts_utc_ms      (BIGINT)  — primary key, epoch ms UTC
#   • schema_version (INT)     — bump when flat columns change
#   • scalars        (DOUBLE/INT/STRING/BOOL as appropriate) — filterable
#   • fixed-TF dicts → expanded flat cols (e.g. mq_1h_ratio, stochastics_4h_k)
#   • arrays/histories → JSON string columns (hydrate on demand)

_FLAT_TIMEFRAMES = {"1m", "5m", "15m", "30m", "1h", "4h", "12h", "1d", "3d", "7d", "14d", "30d", "90d"}


def _flatten_snapshot(snap: dict) -> dict:
    """Flatten a cache dict to single-level columns. Fixed-TF keys stay expanded as
    separate scalar columns; arrays / deep nesting become JSON string columns."""
    ts_ms = snap.get("timestamp") or int(time.time() * 1000)
    out: dict = {
        "ts_utc_ms":      int(ts_ms),
        "schema_version": SCHEMA_VERSION,
    }

    def _as_json(v):
        try:
            return json.dumps(v, default=str, separators=(",", ":"))
        except Exception:
            return None

    def _walk(prefix: str, obj, depth: int = 0):
        if obj is None:
            out[prefix] = None
            return
        if isinstance(obj, bool):
            out[prefix] = 1 if obj else 0
            return
        if isinstance(obj, (int, float)):
            out[prefix] = obj
            return
        if isinstance(obj, str):
            out[prefix] = obj
            return
        if isinstance(obj, list):
            # Arrays → JSON column (histories, deciles, per-strike, expiries, etc.)
            out[prefix] = _as_json(obj)
            return
        if isinstance(obj, dict):
            # Allow expansion for up to 2 levels; deeper → JSON
            if depth >= 2:
                out[prefix] = _as_json(obj)
                return
            # Special case: fixed-TF dict → keep all timeframes as flat columns
            # (already handled by normal expansion; this comment documents intent)
            for k, v in obj.items():
                key = f"{prefix}_{k}" if prefix else str(k)
                _walk(key, v, depth + 1)
            return
        out[prefix] = str(obj)

    for k, v in snap.items():
        _walk(str(k), v)
    return out


def _write_parquet_append(path: str, row: dict) -> None:
    """Append a single row to a parquet file by read → concat → atomic rewrite.

    Schema evolution (QW3): when padding a side with nulls for a missing column,
    the null array is built with the OTHER side's concrete type. Otherwise
    pyarrow creates `null`-typed arrays that fail to unify with int64/float64
    columns on concat, silently dropping the new value in some edge cases.
    Atomic: write to path.tmp then rename.
    """
    if not PERSIST_AVAILABLE:
        return
    new_table = _pa.Table.from_pydict({k: [v] for k, v in row.items()})
    if os.path.isfile(path):
        try:
            existing = _pq.read_table(path)
            # Pad new_table with existing columns it lacks, typed to existing schema.
            for col in existing.column_names:
                if col not in new_table.column_names:
                    target_type = existing.schema.field(col).type
                    null_arr = _pa.array([None], type=target_type)
                    new_table = new_table.append_column(col, null_arr)
            # Pad existing with new columns, typed to new_table schema.
            for col in new_table.column_names:
                if col not in existing.column_names:
                    target_type = new_table.schema.field(col).type
                    null_arr = _pa.array([None] * len(existing), type=target_type)
                    existing = existing.append_column(col, null_arr)
            new_table = new_table.select(existing.column_names)
            combined = _pa.concat_tables([existing, new_table], promote_options="default")
        except Exception as e:
            logger.warning(f"Parquet read failed for {path}, overwriting: {e}")
            combined = new_table
    else:
        combined = new_table
    tmp = path + ".tmp"
    _pq.write_table(combined, tmp, compression="zstd")
    os.replace(tmp, path)


_last_snapshot_minute: Optional[int] = None
_last_micro_write_ts: float = 0


async def snapshot_persister():
    """Append flattened cache snapshot once per minute to YYYY-MM-DD/snapshot.parquet."""
    global _last_snapshot_minute
    if not PERSIST_ENABLED:
        logger.info("Snapshot persister disabled (PERSIST_ENABLED != true)")
        return
    if not PERSIST_AVAILABLE:
        logger.warning("pyarrow not available — snapshot persister disabled")
        return
    try:
        os.makedirs(PERSIST_PATH, exist_ok=True)
    except Exception as e:
        logger.warning(f"Persister: cannot create {PERSIST_PATH}: {e}")
        return
    logger.info(f"Snapshot persister started at {PERSIST_PATH}")
    while True:
        try:
            await asyncio.sleep(15)
            if not cache:
                continue
            now = datetime.now(timezone.utc)
            current_minute = int(now.timestamp() // 60)
            if _last_snapshot_minute == current_minute:
                continue
            flat = _flatten_snapshot(cache)
            day_dir = os.path.join(PERSIST_PATH, now.strftime("%Y-%m-%d"))
            os.makedirs(day_dir, exist_ok=True)
            await asyncio.to_thread(
                _write_parquet_append,
                os.path.join(day_dir, "snapshot.parquet"),
                flat,
            )
            _last_snapshot_minute = current_minute
        except Exception as e:
            logger.warning(f"Snapshot persist error: {e}")
            await asyncio.sleep(30)


async def microstructure_persister():
    """Append order book + derived microstructure snapshot every 5s."""
    global _last_micro_write_ts
    if not PERSIST_ENABLED or not PERSIST_AVAILABLE:
        return
    logger.info(f"Microstructure persister started at {PERSIST_PATH}")
    while True:
        try:
            await asyncio.sleep(PERSIST_MICRO_INTERVAL)
            if not current_depth:
                continue
            now_s = time.time()
            if now_s - _last_micro_write_ts < PERSIST_MICRO_INTERVAL * 0.9:
                continue
            now = datetime.now(timezone.utc)
            row = {
                "ts_utc_ms":       int(now.timestamp() * 1000),
                "schema_version":  SCHEMA_VERSION,
                "midPrice":        current_depth.get("midPrice"),
                "spread":          current_depth.get("spread"),
                "bidAskImbalance": current_depth.get("bidAskImbalance"),
                "totalBidQty":     current_depth.get("totalBidQty"),
                "totalAskQty":     current_depth.get("totalAskQty"),
                # JSON columns (arrays)
                "bids_json":     json.dumps(current_depth.get("bids", [])[:30],     default=str),
                "asks_json":     json.dumps(current_depth.get("asks", [])[:30],     default=str),
                "bidWalls_json": json.dumps(current_depth.get("bidWalls", []),      default=str),
                "askWalls_json": json.dumps(current_depth.get("askWalls", []),      default=str),
            }
            day_dir = os.path.join(PERSIST_PATH, now.strftime("%Y-%m-%d"))
            os.makedirs(day_dir, exist_ok=True)
            await asyncio.to_thread(
                _write_parquet_append,
                os.path.join(day_dir, "microstructure.parquet"),
                row,
            )
            _last_micro_write_ts = now_s
        except Exception as e:
            logger.warning(f"Microstructure persist error: {e}")
            await asyncio.sleep(10)


# ── Background task for slow data sources (non-blocking warmup) ───────
async def slow_data_refresher():
    """Background task that keeps Farside / Stooq / DefiLlama stables caches fresh.

    Each fetch_* function has its own TTL check so this loop is a thin tick;
    work only happens when a cache expires. Kept out of fetch_all_data so
    Farside HTML scrapes can't block user requests.
    """
    # First pass to warm caches (won't block lifespan; runs concurrently)
    try:
        async with httpx.AsyncClient() as client:
            await asyncio.gather(
                fetch_risk_free_rate(client),
                fetch_etf_flows(client),
                fetch_stables_supply(client),
                fetch_macro(client),
                return_exceptions=True,
            )
        logger.info("Slow data caches warmed")
    except Exception as e:
        logger.warning(f"Slow data initial warmup error: {e}")

    while True:
        await asyncio.sleep(60)  # 1-min tick; per-fetcher TTL gates real work
        try:
            async with httpx.AsyncClient() as client:
                await asyncio.gather(
                    fetch_risk_free_rate(client),
                    fetch_etf_flows(client),
                    fetch_stables_supply(client),
                    fetch_macro(client),
                    return_exceptions=True,
                )
        except Exception as e:
            logger.warning(f"Slow data refresh error: {e}")


# ── App ───────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting ETH Positioning Dashboard on port {PORT}")
    try:
        await fetch_all_data()
    except Exception as e:
        logger.warning(f"Cache warm-up failed: {e}")

    task_depth = asyncio.create_task(depth_collector())
    logger.info("Depth collector started")

    task_slow = asyncio.create_task(slow_data_refresher())
    logger.info("Slow data refresher started (non-blocking)")

    tasks = [task_depth, task_slow]
    if PERSIST_ENABLED:
        tasks.append(asyncio.create_task(snapshot_persister()))
        tasks.append(asyncio.create_task(microstructure_persister()))
        logger.info(f"Persistence tasks started (path={PERSIST_PATH})")
    else:
        logger.info("Persistence disabled (PERSIST_ENABLED=false)")

    yield

    for t in tasks:
        t.cancel()
        try:
            await t
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
    now = time.time()
    def _age(ts):
        return round(now - ts, 1) if ts else None
    return {
        "status": "ok",
        "cache_age":           _age(cache_ts),
        "depth_age":           _age(current_depth_ts),
        "depth_history_size":  len(depth_history),
        # Fase 1 cache ages (None if warming)
        "etf_age":             _age(etf_cache_ts),
        "stables_age":         _age(stables_cache_ts),
        "macro_age":           _age(macro_cache_ts),
        "risk_free_age":       _age(risk_free_cache_ts),
        "deribit_basis_age":   _age(deribit_basis_cache_ts),
        "dune_age":            _age(dune_cache_ts),
        "llama_age":           _age(llama_cache_ts),
        "defi_age":            _age(defi_cache_ts),
    }


# ── Fase 1 · Dedicated endpoints (thin readers over cache) ────────────
# All return {"status": "warming"} if the cache hasn't been populated yet.

@app.get("/api/etf/flows")
async def get_etf_flows():
    if not etf_cache:
        return {"status": "warming"}
    return etf_cache


@app.get("/api/stables/supply")
async def get_stables_supply():
    if not stables_cache:
        return {"status": "warming"}
    return stables_cache


@app.get("/api/basis/deribit")
async def get_deribit_basis():
    """Deribit dated-futures basis. Triggers a fetch if cache cold (TTL 30s)."""
    if not deribit_basis_cache:
        async with httpx.AsyncClient() as client:
            await fetch_deribit_basis(client)
    if not deribit_basis_cache:
        return {"status": "warming"}
    return deribit_basis_cache


@app.get("/api/basis/cme")
async def get_cme_basis():
    """TODO fase 2: scrape CME ETH settlement for TradFi-segmented basis.

    Rationale: CME basis reflects institutional USA positioning during RTH,
    which is economically distinct from crypto-native futures like Deribit.
    Daily cadence; 10-min delay of the public settlement page is fine.
    """
    return {
        "status":     "not_implemented",
        "todo":       "CME ETH settlement scrape — fase 2",
        "fallback":   "/api/basis/deribit",
    }


@app.get("/api/basis/perp")
async def get_perp_basis():
    """Spot-perp and perp-quarterly basis. Derived inside fetch_all_data (TTL 10s)."""
    if not cache or cache.get("perpBasis") is None:
        return {"status": "warming"}
    return cache["perpBasis"]


@app.get("/api/options/skew")
async def get_options_skew():
    if not cache or cache.get("optionsSkew") is None:
        return {"status": "warming"}
    return cache["optionsSkew"]


@app.get("/api/options/expiries")
async def get_options_expiries():
    if not cache or cache.get("optionsExpiries") is None:
        return {"status": "warming"}
    return cache["optionsExpiries"]


@app.get("/api/macro")
async def get_macro():
    if not macro_cache:
        return {"status": "warming"}
    return macro_cache


@app.get("/api/riskfree")
async def get_risk_free():
    """US 3M T-bill yield used as risk-free rate for BS delta. Cached 1d."""
    if not risk_free_cache:
        return {"status": "warming"}
    return risk_free_cache


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
