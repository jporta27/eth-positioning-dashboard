"""Causal regime labelers for event-study conditioning.

Each public function takes `event_ts_ms` (np.int64 array, epoch ms UTC) and
returns a `np.array(dtype='<Uxx')` of regime labels of identical length, one
per event.

CRITICAL — every labeler is causal: at the event timestamp it only uses
information available at that timestamp (`searchsorted(side='right') - 1`).
No look-ahead. If a regime can't be determined for a given event (e.g. macro
data starts after the event), the label is `"unknown"`.

The full registry of labelers is `LABELERS = {name: fn}`. The CLI's
`--conditions` flag takes a comma-separated list of these names.

Available labelers:
    vix_quartile         — VIX rolling 1y quartile at the event (q1=calm…q4=stress).
    etf_7d_sign          — sign of rolling 5d Farside ETF flow at the event.
    stables_30d_sign     — sign of 30d % change in defillama_stables_agg.
    realized_vol_regime  — terciles of trailing-24h realized vol from klines.
    btc_trend_30d        — sign of trailing-30d BTC return from macro.
    rr25_sign            — RR25 30d sign if Deribit backfill exists, else "unknown".
"""

from __future__ import annotations

from typing import Dict, Callable, Optional
import numpy as np
import pyarrow as pa

from . import load


# ── Internal helpers ────────────────────────────────────────────────────
def _causal_lookup_close(
    series_ts: np.ndarray,
    series_val: np.ndarray,
    event_ts_ms: np.ndarray,
) -> np.ndarray:
    """For each event, return the most-recent value of `series` at-or-before
    the event timestamp. Result NaN where no past data exists.

    series_ts must be ascending. Uses searchsorted(side='right')-1 to ensure
    same-bar values are usable (e.g. event at midnight uses that day's macro
    close, which is set at end of session — safe assumption for daily series).
    """
    out = np.full(len(event_ts_ms), np.nan, dtype=np.float64)
    if len(series_ts) == 0:
        return out
    idx = np.searchsorted(series_ts, event_ts_ms, side="right") - 1
    valid = idx >= 0
    out[valid] = series_val[idx[valid]]
    return out


def _rolling_quantile_at(
    series_ts: np.ndarray,
    series_val: np.ndarray,
    event_ts_ms: np.ndarray,
    window_days: int,
    q: float,
) -> np.ndarray:
    """For each event, return the q-th quantile of `series` over the trailing
    `window_days` (strictly past, exclusive of future). NaN if fewer than 30
    samples in the window.

    Used by vix_quartile to compute the 25/50/75 thresholds at each event.
    Vectorisation is unnecessary — N events × N threshold lookups, the cost
    is dominated by extracting the slice each time, and we only call this
    function for at most a few hundred events per backtest.
    """
    out = np.full(len(event_ts_ms), np.nan, dtype=np.float64)
    if len(series_ts) == 0:
        return out
    win_ms = window_days * 86_400_000
    for i, ts in enumerate(event_ts_ms):
        lo = ts - win_ms
        # strictly past: include rows with series_ts < ts
        lo_idx = np.searchsorted(series_ts, lo, side="left")
        hi_idx = np.searchsorted(series_ts, ts, side="left")
        if hi_idx - lo_idx < 30:
            continue
        out[i] = float(np.quantile(series_val[lo_idx:hi_idx], q))
    return out


# ── Public labelers ─────────────────────────────────────────────────────
def vix_quartile(event_ts_ms: np.ndarray) -> np.ndarray:
    """Bucket each event by where the VIX close that day sits inside its
    rolling 1-year distribution at the event time.

    Returns one of: "q1_calm", "q2", "q3", "q4_stress", "unknown".

    Uses macro.parquet label="VIX". Daily cadence, so for an intra-day event
    we read the most recent prior daily close (no look-ahead).
    """
    n = len(event_ts_ms)
    out = np.full(n, "unknown", dtype="<U10")
    t = load.load_macro(label="VIX")
    if t is None or t.num_rows == 0:
        return out
    ts = t.column("ts_utc_ms").to_numpy().astype(np.int64)
    val = t.column("close").to_numpy().astype(np.float64)
    order = np.argsort(ts, kind="stable")
    ts, val = ts[order], val[order]

    spot = _causal_lookup_close(ts, val, event_ts_ms)
    q25 = _rolling_quantile_at(ts, val, event_ts_ms, window_days=365, q=0.25)
    q50 = _rolling_quantile_at(ts, val, event_ts_ms, window_days=365, q=0.50)
    q75 = _rolling_quantile_at(ts, val, event_ts_ms, window_days=365, q=0.75)

    for i in range(n):
        if not (np.isfinite(spot[i]) and np.isfinite(q25[i]) and np.isfinite(q50[i]) and np.isfinite(q75[i])):
            continue
        if spot[i] <= q25[i]:
            out[i] = "q1_calm"
        elif spot[i] <= q50[i]:
            out[i] = "q2"
        elif spot[i] <= q75[i]:
            out[i] = "q3"
        else:
            out[i] = "q4_stress"
    return out


def etf_7d_sign(event_ts_ms: np.ndarray, threshold_musd: float = 50.0) -> np.ndarray:
    """Sign of the trailing 5-day Farside ETF flow at the event time.

    Returns "inflow" if rolling 5d sum >= +threshold_musd,
            "outflow" if <= -threshold_musd,
            "neutral" otherwise,
            "unknown" if event predates ETF launch (2024-07-23) or backfill missing.
    """
    n = len(event_ts_ms)
    out = np.full(n, "unknown", dtype="<U10")
    t = load.load_etf()
    if t is None or t.num_rows == 0:
        return out
    ts = t.column("ts_utc_ms").to_numpy().astype(np.int64)
    flow = t.column("flow_total").to_numpy().astype(np.float64)
    order = np.argsort(ts, kind="stable")
    ts, flow = ts[order], flow[order]

    # Build the 5d-rolling-sum series, then causal lookup at each event.
    n_days = len(ts)
    r5 = np.full(n_days, np.nan, dtype=np.float64)
    for i in range(4, n_days):
        w = flow[i - 4:i + 1]
        if np.all(np.isfinite(w)):
            r5[i] = w.sum()

    spot = _causal_lookup_close(ts, r5, event_ts_ms)
    for i in range(n):
        v = spot[i]
        if not np.isfinite(v):
            continue
        if v >= threshold_musd:
            out[i] = "inflow"
        elif v <= -threshold_musd:
            out[i] = "outflow"
        else:
            out[i] = "neutral"
    return out


def stables_30d_sign(event_ts_ms: np.ndarray, threshold_pct: float = 1.0) -> np.ndarray:
    """Sign of the trailing 30-day percent change in aggregate stablecoin supply.

    Returns "expanding" if Δ30d >= +threshold_pct,
            "contracting" if Δ30d <= -threshold_pct,
            "flat" otherwise,
            "unknown" if no past 30d window is available.
    """
    n = len(event_ts_ms)
    out = np.full(n, "unknown", dtype="<U12")
    t = load.load_stables(source="defillama_stables_agg")
    if t is None or t.num_rows == 0:
        return out
    ts = t.column("ts_utc_ms").to_numpy().astype(np.int64)
    val = t.column("total_usd").to_numpy().astype(np.float64)
    order = np.argsort(ts, kind="stable")
    ts, val = ts[order], val[order]

    win_ms = 30 * 86_400_000
    spot = _causal_lookup_close(ts, val, event_ts_ms)
    past = _causal_lookup_close(ts, val, event_ts_ms - win_ms)
    for i in range(n):
        if not (np.isfinite(spot[i]) and np.isfinite(past[i])) or past[i] <= 0:
            continue
        delta_pct = (spot[i] / past[i] - 1.0) * 100.0
        if delta_pct >= threshold_pct:
            out[i] = "expanding"
        elif delta_pct <= -threshold_pct:
            out[i] = "contracting"
        else:
            out[i] = "flat"
    return out


def realized_vol_regime(event_ts_ms: np.ndarray) -> np.ndarray:
    """Bucket events by trailing-24h realized log-return vol (stdev × sqrt(24)).

    Terciles of the rolling 1y distribution at each event:
        "rv_low"  → bottom third
        "rv_mid"  → middle
        "rv_high" → top
        "unknown" → window has < 30 prior bars
    """
    n = len(event_ts_ms)
    out = np.full(n, "unknown", dtype="<U10")
    k = load.load_klines_1h()
    if k is None or k.num_rows < 30:
        return out
    ts = k.column("ts_utc_ms").to_numpy().astype(np.int64)
    close = k.column("close").to_numpy().astype(np.float64)
    order = np.argsort(ts, kind="stable")
    ts, close = ts[order], close[order]

    log_r = np.full(len(close), np.nan, dtype=np.float64)
    log_r[1:] = np.log(close[1:] / close[:-1])

    # Trailing-24-bar stdev
    rv24 = np.full(len(close), np.nan, dtype=np.float64)
    for i in range(24, len(close)):
        w = log_r[i - 23:i + 1]
        w = w[np.isfinite(w)]
        if len(w) >= 12:
            rv24[i] = w.std(ddof=1) * np.sqrt(24)

    spot = _causal_lookup_close(ts, rv24, event_ts_ms)
    q33 = _rolling_quantile_at(ts, rv24, event_ts_ms, window_days=365, q=1 / 3)
    q67 = _rolling_quantile_at(ts, rv24, event_ts_ms, window_days=365, q=2 / 3)
    for i in range(n):
        if not (np.isfinite(spot[i]) and np.isfinite(q33[i]) and np.isfinite(q67[i])):
            continue
        if spot[i] <= q33[i]:
            out[i] = "rv_low"
        elif spot[i] <= q67[i]:
            out[i] = "rv_mid"
        else:
            out[i] = "rv_high"
    return out


def btc_trend_30d(event_ts_ms: np.ndarray, threshold_pct: float = 5.0) -> np.ndarray:
    """Sign of trailing-30d BTC return.

    Returns "btc_up" if 30d return >= +threshold_pct,
            "btc_down" if <= -threshold_pct,
            "btc_flat" otherwise,
            "unknown" if backfill too short.
    """
    n = len(event_ts_ms)
    out = np.full(n, "unknown", dtype="<U10")
    t = load.load_macro(label="BTC")
    if t is None or t.num_rows == 0:
        return out
    ts = t.column("ts_utc_ms").to_numpy().astype(np.int64)
    val = t.column("close").to_numpy().astype(np.float64)
    order = np.argsort(ts, kind="stable")
    ts, val = ts[order], val[order]

    win_ms = 30 * 86_400_000
    spot = _causal_lookup_close(ts, val, event_ts_ms)
    past = _causal_lookup_close(ts, val, event_ts_ms - win_ms)
    for i in range(n):
        if not (np.isfinite(spot[i]) and np.isfinite(past[i])) or past[i] <= 0:
            continue
        ret_pct = (spot[i] / past[i] - 1.0) * 100.0
        if ret_pct >= threshold_pct:
            out[i] = "btc_up"
        elif ret_pct <= -threshold_pct:
            out[i] = "btc_down"
        else:
            out[i] = "btc_flat"
    return out


def rr25_sign(event_ts_ms: np.ndarray) -> np.ndarray:
    """RR25 30d trailing sign — placeholder until Deribit options trade backfill exists.

    Returns "unknown" for every event, with an inline note. When the Deribit
    backfill lands, this will compute the 30d-trailing average RR25 (ATM call
    minus put implied vol at 25-delta) and bucket it as positive/negative/flat.

    Documented limitation per AUDIT_POST_BACKFILL.md.
    """
    n = len(event_ts_ms)
    return np.full(n, "unknown", dtype="<U10")


# ── Registry + helpers ──────────────────────────────────────────────────
LABELERS: Dict[str, Callable[[np.ndarray], np.ndarray]] = {
    "vix_quartile":        vix_quartile,
    "etf_7d_sign":         etf_7d_sign,
    "stables_30d_sign":    stables_30d_sign,
    "realized_vol_regime": realized_vol_regime,
    "btc_trend_30d":       btc_trend_30d,
    "rr25_sign":           rr25_sign,
}


def label_events(
    event_ts_ms: np.ndarray,
    names: list,
) -> Dict[str, np.ndarray]:
    """Build the dict expected by run_with_regimes from a list of labeler names."""
    event_ts_ms = np.asarray(event_ts_ms, dtype=np.int64)
    out: Dict[str, np.ndarray] = {}
    for n in names:
        if n not in LABELERS:
            raise KeyError(f"unknown regime labeler: {n}. Available: {list(LABELERS)}")
        out[n] = LABELERS[n](event_ts_ms)
    return out
