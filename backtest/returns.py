"""Forward returns computed from binance_klines_1h.parquet.

Convention:
    A signal observed at ts_sig is joined to the kline whose CLOSE_TIME is the
    most recent <= ts_sig + epsilon, and the forward return horizon h hours ahead
    uses the close at ts_sig + h*3600000 vs that anchor close.

    Return definition: log(close[t+h] / close[t]) — additive-friendly, symmetric.

No pandas. numpy + pyarrow only.
"""

from __future__ import annotations

from typing import Optional
import numpy as np
import pyarrow as pa

from .load import load_klines_1h


def _anchor_closes(klines: pa.Table) -> tuple:
    """Return (ts_ms_np, close_np) sorted ascending by ts.
    ts_utc_ms in klines is the OPEN time; add 3600000 to get close time anchor."""
    ts = klines.column("ts_utc_ms").to_numpy().astype(np.int64)
    close = klines.column("close").to_numpy().astype(np.float64)
    # Sort by ts (should already be sorted but be defensive)
    order = np.argsort(ts, kind="stable")
    return ts[order], close[order]


def forward_log_returns(
    event_ts_ms: np.ndarray,
    horizons_hours: list,
    klines_table: Optional[pa.Table] = None,
) -> np.ndarray:
    """Vectorised forward log returns for an array of event timestamps.

    Parameters
    ----------
    event_ts_ms : (N,) int64 array of event timestamps (epoch ms UTC).
    horizons_hours : list of horizons in hours (e.g. [1, 4, 24, 72, 168]).
    klines_table : pyarrow Table with ts_utc_ms + close. If None, loaded from backfill.

    Returns
    -------
    (N, H) float64 array — log(close[t+h]/close[t]). NaN where either endpoint
    is outside the available kline range.
    """
    if klines_table is None:
        klines_table = load_klines_1h()
        if klines_table is None:
            raise RuntimeError("load_klines_1h() returned None — run scripts/backfill.py --sources binance_klines_1h")

    kts, kclose = _anchor_closes(klines_table)
    event_ts_ms = np.asarray(event_ts_ms, dtype=np.int64)
    n = len(event_ts_ms)
    h_ms = np.array([int(h * 3_600_000) for h in horizons_hours], dtype=np.int64)
    out = np.full((n, len(horizons_hours)), np.nan, dtype=np.float64)

    # Anchor: for each event ts, find the kline whose open_time <= event_ts, closest.
    # searchsorted on sorted kts for right insertion → index-1 gives anchor.
    anchor_idx = np.searchsorted(kts, event_ts_ms, side="right") - 1
    # Events before the first kline or invalid: leave NaN
    valid_anchor = (anchor_idx >= 0) & (anchor_idx < len(kts))

    # For each horizon, compute target kline index
    for j, hms in enumerate(h_ms):
        target_ts = event_ts_ms + hms
        target_idx = np.searchsorted(kts, target_ts, side="right") - 1
        valid = valid_anchor & (target_idx >= 0) & (target_idx < len(kts))
        # Avoid log(0) or negative
        anchor_close = np.where(valid_anchor, kclose[np.clip(anchor_idx, 0, len(kclose) - 1)], np.nan)
        target_close = np.where(valid, kclose[np.clip(target_idx, 0, len(kclose) - 1)], np.nan)
        with np.errstate(divide="ignore", invalid="ignore"):
            out[:, j] = np.where(
                (anchor_close > 0) & (target_close > 0),
                np.log(target_close / anchor_close),
                np.nan,
            )
    return out


def klines_coverage(klines_table: Optional[pa.Table] = None) -> tuple:
    """Return (ts_min, ts_max) of the klines data, for preflight range checks."""
    if klines_table is None:
        klines_table = load_klines_1h()
    if klines_table is None or klines_table.num_rows == 0:
        return (None, None)
    ts = klines_table.column("ts_utc_ms").to_numpy()
    return (int(ts.min()), int(ts.max()))
