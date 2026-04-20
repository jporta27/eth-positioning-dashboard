"""Signal extractors.

Each function returns (event_ts_ms, signal_values) arrays ready for
run_event_study(). Signals are built from the parquet data that is actually
in data/backfill/ + data/YYYY-MM-DD/ (call backtest.load.audit() to check).

Available today (2026-Q2):
    funding_extreme_z  — funding rate z-score over rolling window
    stables_supply_delta_extreme — stables aggregate supply delta 30d extreme
    etf_5d_topdecile   — Farside ETF rolling 5d top/bottom decile (if backfill present)

Blocked until more persister time or offline reconstruction:
    moneyQuality_label (needs OI history — Binance API only gives 30d)
    cex_netflow_zscore (Dune query only retains ~3 weeks)
    ls_top_vs_retail   (Binance L/S APIs only retain ~30 days)
    rr25_canonical     (Deribit trade history not backfilled yet)
"""

from __future__ import annotations

from typing import Optional
import numpy as np
import pyarrow as pa

from .load import load_funding, load_stables, load_etf


def _zscore(x: np.ndarray, window: int) -> np.ndarray:
    """Causal z-score: for each i, mean/std over x[max(0,i-window):i+1] (exclusive of future)."""
    n = len(x)
    out = np.full(n, np.nan, dtype=np.float64)
    for i in range(window, n):
        w = x[i - window:i]  # strictly past
        mu = w.mean()
        sd = w.std(ddof=1)
        if sd > 0:
            out[i] = (x[i] - mu) / sd
    return out


def funding_extreme_z(
    z_threshold: float = 2.0,
    side: str = "inflow",
    window: int = 90,  # ~30 days of 8h periods
    min_gap_hours: int = 16,  # dedup clusters
) -> tuple:
    """Binance funding rate extreme z-score events.

    side='inflow'  — funding z >= +threshold (longs paying shorts — market skewed long)
    side='outflow' — funding z <= -threshold (shorts paying longs — market skewed short)
    side='abs'     — |z| >= threshold (either direction; signal value is the z)

    Returns (event_ts_ms, signal_values).
      For 'inflow'/'outflow': signal_values = |z| (magnitude).
      For 'abs': signal_values = z (signed).
    """
    t = load_funding()
    if t is None or t.num_rows < window + 10:
        raise RuntimeError("funding backfill missing or too short; run scripts/backfill.py --sources binance_funding")

    ts = t.column("ts_utc_ms").to_numpy().astype(np.int64)
    fr = t.column("funding_rate").to_numpy().astype(np.float64)
    order = np.argsort(ts, kind="stable")
    ts, fr = ts[order], fr[order]

    z = _zscore(fr, window=window)

    if side == "inflow":
        mask = z >= z_threshold
        signal = np.where(mask, np.abs(z), np.nan)
    elif side == "outflow":
        mask = z <= -z_threshold
        signal = np.where(mask, np.abs(z), np.nan)
    elif side == "abs":
        mask = np.abs(z) >= z_threshold
        signal = np.where(mask, z, np.nan)
    else:
        raise ValueError(f"bad side {side}")

    idxs = np.where(mask & np.isfinite(signal))[0]

    # Dedup: events within min_gap_hours of each other collapse to the first.
    gap_ms = int(min_gap_hours * 3_600_000)
    keep = []
    last_kept_ts = -10**18
    for i in idxs:
        if ts[i] - last_kept_ts >= gap_ms:
            keep.append(i)
            last_kept_ts = ts[i]
    keep_arr = np.array(keep, dtype=np.int64)
    return ts[keep_arr], signal[keep_arr]


def stables_supply_delta_extreme(
    source: str = "defillama_stables_agg",
    window_days: int = 30,
    percentile: float = 90.0,
    side: str = "expanding",
    min_gap_days: int = 5,
) -> tuple:
    """Stablecoin supply delta extreme events.

    Event: delta over `window_days` reaches the top/bottom `percentile` of its
    rolling 1y distribution. 'expanding' = new inflow surge (≥ top decile),
    'contracting' = net outflow (≤ bottom decile).

    signal = the delta value (signed).
    """
    t = load_stables(source=source)
    if t is None or t.num_rows < window_days + 365:
        raise RuntimeError(f"stables backfill too short for source={source}")

    ts = t.column("ts_utc_ms").to_numpy().astype(np.int64)
    usd = t.column("total_usd").to_numpy().astype(np.float64)
    order = np.argsort(ts, kind="stable")
    ts, usd = ts[order], usd[order]

    # Compute N-day delta as % change
    # data is ~daily; find pairs N days apart using ts
    day_ms = 86_400_000
    delta_pct = np.full(len(ts), np.nan, dtype=np.float64)
    for i in range(len(ts)):
        target_ts = ts[i] - window_days * day_ms
        j = np.searchsorted(ts, target_ts, side="right") - 1
        if 0 <= j < i and usd[j] > 0:
            delta_pct[i] = (usd[i] / usd[j] - 1.0) * 100.0

    # Rolling 1y percentile (causal)
    year_window = 365
    threshold = np.full(len(ts), np.nan, dtype=np.float64)
    for i in range(year_window, len(ts)):
        w = delta_pct[i - year_window:i]
        w = w[np.isfinite(w)]
        if len(w) >= 30:
            if side == "expanding":
                threshold[i] = np.percentile(w, percentile)
            elif side == "contracting":
                threshold[i] = np.percentile(w, 100 - percentile)

    if side == "expanding":
        mask = np.isfinite(delta_pct) & np.isfinite(threshold) & (delta_pct >= threshold)
    elif side == "contracting":
        mask = np.isfinite(delta_pct) & np.isfinite(threshold) & (delta_pct <= threshold)
    else:
        raise ValueError(f"bad side {side}")

    idxs = np.where(mask)[0]
    gap_ms = min_gap_days * day_ms
    keep = []
    last = -10**18
    for i in idxs:
        if ts[i] - last >= gap_ms:
            keep.append(i)
            last = ts[i]
    keep_arr = np.array(keep, dtype=np.int64)
    return ts[keep_arr], delta_pct[keep_arr]


def etf_5d_topdecile(
    percentile: float = 90.0,
    side: str = "buying",
    min_gap_days: int = 3,
) -> tuple:
    """Farside ETF 5d-rolling-sum extreme events (top/bottom decile of rolling 10mo window).

    side='buying'  — 5d sum ≥ top `percentile`
    side='selling' — 5d sum ≤ bottom (100 - percentile)

    Note: 10 months ≈ 210 trading days; if the backfill has less, the threshold
    falls back to the percentile of all available history.
    """
    t = load_etf()
    if t is None or t.num_rows < 20:
        raise RuntimeError("ETF backfill missing — Farside 403 blocks backfill (runtime curl fallback works but backfill script does not use it yet)")

    ts = t.column("ts_utc_ms").to_numpy().astype(np.int64)
    if "flow_total" not in t.column_names:
        raise RuntimeError(f"ETF parquet missing flow_total column; cols={t.column_names}")
    flow = t.column("flow_total").to_numpy().astype(np.float64)
    order = np.argsort(ts, kind="stable")
    ts, flow = ts[order], flow[order]

    # 5d rolling sum (past-inclusive, causal)
    n = len(ts)
    r5 = np.full(n, np.nan, dtype=np.float64)
    for i in range(4, n):
        w = flow[i - 4:i + 1]
        if np.all(np.isfinite(w)):
            r5[i] = w.sum()

    ten_mo = 210
    thr = np.full(n, np.nan, dtype=np.float64)
    for i in range(ten_mo, n):
        w = r5[i - ten_mo:i]
        w = w[np.isfinite(w)]
        if len(w) >= 50:
            if side == "buying":
                thr[i] = np.percentile(w, percentile)
            else:
                thr[i] = np.percentile(w, 100 - percentile)

    if side == "buying":
        mask = np.isfinite(r5) & np.isfinite(thr) & (r5 >= thr)
    else:
        mask = np.isfinite(r5) & np.isfinite(thr) & (r5 <= thr)

    idxs = np.where(mask)[0]
    gap_ms = min_gap_days * 86_400_000
    keep = []
    last = -10**18
    for i in idxs:
        if ts[i] - last >= gap_ms:
            keep.append(i)
            last = ts[i]
    keep_arr = np.array(keep, dtype=np.int64)
    return ts[keep_arr], r5[keep_arr]


SIGNAL_REGISTRY = {
    # z=2 threshold — ~28 inflow events in 2y, 50 outflow events; binary-style
    "funding_extreme_inflow":  lambda: funding_extreme_z(side="inflow", z_threshold=2.0),
    "funding_extreme_outflow": lambda: funding_extreme_z(side="outflow", z_threshold=2.0),
    "funding_extreme_abs":     lambda: funding_extreme_z(side="abs", z_threshold=2.0),
    # z=1.5 threshold — ~100 events per side in 2y; better for IC/bootstrap stability
    "funding_hot_inflow":      lambda: funding_extreme_z(side="inflow", z_threshold=1.5),
    "funding_hot_outflow":     lambda: funding_extreme_z(side="outflow", z_threshold=1.5),
    "funding_hot_abs":         lambda: funding_extreme_z(side="abs", z_threshold=1.5),
    "stables_expanding":       lambda: stables_supply_delta_extreme(side="expanding"),
    "stables_contracting":     lambda: stables_supply_delta_extreme(side="contracting"),
    "etf_buying":              lambda: etf_5d_topdecile(side="buying"),
    "etf_selling":             lambda: etf_5d_topdecile(side="selling"),
}
