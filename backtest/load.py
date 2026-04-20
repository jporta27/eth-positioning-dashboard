"""Unified parquet loaders for the backfill + live-snapshot layout.

All data is keyed by `ts_utc_ms` (int64, epoch milliseconds UTC). No pandas
in the hot path; pyarrow + numpy only.

Expected layout (relative to repo root):
    data/backfill/
        binance_klines_1h.parquet   (ts_utc_ms, open, high, low, close, volume, ...)
        binance_funding.parquet     (ts_utc_ms, funding_rate)
        defillama_stables.parquet   (ts_utc_ms, source, total_usd)
        farside_etf.parquet         (ts_utc_ms, flow_total, flow_<ISSUER>, ...)
        macro.parquet               (ts_utc_ms, symbol, label, close)
    data/YYYY-MM-DD/
        snapshot.parquet            (1 row/min, 300+ flat columns)
        microstructure.parquet      (1 row/5s, order book)
"""

from __future__ import annotations

import os
import glob
from typing import Optional, Iterable

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq


DATA_ROOT_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")


def _dr(data_root: Optional[str]) -> str:
    return os.path.abspath(data_root or DATA_ROOT_DEFAULT)


def load_backfill(name: str, data_root: Optional[str] = None) -> Optional[pa.Table]:
    """Load a single backfill parquet, e.g. 'binance_klines_1h'. Returns None if missing."""
    path = os.path.join(_dr(data_root), "backfill", f"{name}.parquet")
    if not os.path.isfile(path):
        return None
    return pq.read_table(path)


def load_snapshots(
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    data_root: Optional[str] = None,
    columns: Optional[Iterable[str]] = None,
) -> Optional[pa.Table]:
    """Read all data/YYYY-MM-DD/snapshot.parquet, filter by ts range, return concatenated.

    Returns None if no snapshots exist in the window.
    `columns`: if provided, only these columns are read (massive speedup; the live
    snapshot has 300+ columns).
    """
    root = _dr(data_root)
    tables = []
    cols = list(columns) if columns else None
    for day_dir in sorted(glob.glob(os.path.join(root, "20??-??-??"))):
        path = os.path.join(day_dir, "snapshot.parquet")
        if not os.path.isfile(path):
            continue
        try:
            t = pq.read_table(path, columns=cols) if cols else pq.read_table(path)
            if start_ms or end_ms:
                ts = t.column("ts_utc_ms").to_numpy()
                mask = np.ones(len(ts), dtype=bool)
                if start_ms:
                    mask &= ts >= start_ms
                if end_ms:
                    mask &= ts < end_ms
                if mask.any():
                    t = t.filter(pa.array(mask))
                else:
                    continue
            if t.num_rows > 0:
                tables.append(t)
        except Exception:
            continue
    if not tables:
        return None
    # Unify schemas (promote_options handles new/old columns)
    return pa.concat_tables(tables, promote_options="default")


def load_klines_1h(data_root: Optional[str] = None) -> Optional[pa.Table]:
    """Load the 2y Binance 1h perp klines (from backfill)."""
    return load_backfill("binance_klines_1h", data_root)


def load_funding(data_root: Optional[str] = None) -> Optional[pa.Table]:
    """Load 2y Binance funding-rate history (8h cadence)."""
    return load_backfill("binance_funding", data_root)


def load_macro(data_root: Optional[str] = None, label: Optional[str] = None) -> Optional[pa.Table]:
    """Load macro backfill (Yahoo 5y). Optionally filter by label (DXY/SPX/VIX/US10Y/BTC/RISK_FREE)."""
    t = load_backfill("macro", data_root)
    if t is None or label is None:
        return t
    mask = np.array([x == label for x in t.column("label").to_pylist()])
    return t.filter(pa.array(mask))


def load_stables(data_root: Optional[str] = None, source: str = "defillama_stables_agg") -> Optional[pa.Table]:
    """Load stablecoin supply series. `source` filter:
      - 'defillama_stables_agg' (aggregate across all stables)
      - 'defillama_stables_usdt'
      - 'defillama_stables_usdc'
    """
    t = load_backfill("defillama_stables", data_root)
    if t is None:
        return None
    mask = np.array([x == source for x in t.column("source").to_pylist()])
    return t.filter(pa.array(mask))


def load_etf(data_root: Optional[str] = None) -> Optional[pa.Table]:
    """Load Farside ETF flows (daily, since Jul 2024). May be None if backfill
    failed (Farside blocks non-browser fingerprints; see backend/farside_parse.py
    and scripts/backfill.py)."""
    return load_backfill("farside_etf", data_root)


def audit(data_root: Optional[str] = None) -> dict:
    """Return a summary of what's available. Useful for event-study CLI preflight."""
    root = _dr(data_root)
    backfill_dir = os.path.join(root, "backfill")
    snapshots_root = root

    bf = {}
    if os.path.isdir(backfill_dir):
        for f in sorted(os.listdir(backfill_dir)):
            if not f.endswith(".parquet"):
                continue
            path = os.path.join(backfill_dir, f)
            try:
                t = pq.read_table(path, columns=["ts_utc_ms"])
                ts = t.column("ts_utc_ms").to_numpy()
                if len(ts):
                    bf[f[:-8]] = {
                        "rows": t.num_rows,
                        "ts_min": int(ts.min()),
                        "ts_max": int(ts.max()),
                    }
            except Exception as e:
                bf[f[:-8]] = {"error": str(e)}

    snaps = []
    for day_dir in sorted(glob.glob(os.path.join(snapshots_root, "20??-??-??"))):
        s = {"day": os.path.basename(day_dir)}
        sp = os.path.join(day_dir, "snapshot.parquet")
        if os.path.isfile(sp):
            try:
                m = pq.read_metadata(sp)
                s["snapshot_rows"] = m.num_rows
            except Exception as e:
                s["snapshot_err"] = str(e)
        snaps.append(s)

    return {
        "data_root":     root,
        "backfill":      bf,
        "snapshot_days": snaps,
    }
