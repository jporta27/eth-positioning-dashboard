"""Event study core.

Given:
  - event_ts_ms: array of timestamps at which the signal fires (or values thereof)
  - signal_values: scalar per event (for continuous signals — IC needs ordering)
  - horizons_hours: list of forward horizons to evaluate

Produces a structured report with bootstrap CI, IC, hit rate, Sharpe, max DD,
equity curve, return distribution, and (when regimes provided) conditioned views.

Purged train/test: for a given test split, all events within
`purge_hours = max(horizon_hours)` of the test boundary are dropped to avoid
look-ahead leakage when the signal is sampled at sub-horizon frequency.
"""

from __future__ import annotations

from typing import Optional, Dict, List, Any
import numpy as np
import pyarrow as pa

from . import metrics as M
from .returns import forward_log_returns
from .load import load_klines_1h


HORIZON_PERIODS_PER_YEAR = {
    "1h":  24 * 365,
    "4h":  6 * 365,
    "1d":  365,
    "3d":  365 / 3,
    "7d":  365 / 7,
}


def _horizon_to_hours(h: str) -> float:
    s = h.strip().lower()
    if s.endswith("h"):
        return float(s[:-1])
    if s.endswith("d"):
        return float(s[:-1]) * 24
    raise ValueError(f"bad horizon {h}")


def run_event_study(
    event_ts_ms: np.ndarray,
    signal_values: np.ndarray,
    horizons: List[str],
    klines_table: Optional[pa.Table] = None,
    bootstrap_iter: int = 5000,
    n_trials: int = 1,
    rng_seed: int = 42,
) -> dict:
    """Core event-study runner over one unconditioned sample.

    Parameters
    ----------
    event_ts_ms : (N,) int64 event timestamps in epoch ms UTC.
    signal_values : (N,) float — one scalar per event. For binary signals pass 0/1.
    horizons : list of strings like ["1h","4h","1d","3d","7d"].
    klines_table : preloaded table; None → loads backfill.
    bootstrap_iter : iterations for CI and permutation p-value.
    n_trials : number of strategies searched (for Deflated Sharpe).
    rng_seed : reproducibility.

    Returns
    -------
    dict with per-horizon statistics. Shape matches the CLI contract.
    """
    if klines_table is None:
        klines_table = load_klines_1h()
    hours = [_horizon_to_hours(h) for h in horizons]
    rng = np.random.default_rng(rng_seed)

    ret_matrix = forward_log_returns(event_ts_ms, hours, klines_table=klines_table)
    out: Dict[str, Any] = {"n_events": int(len(event_ts_ms)), "horizons": {}}

    for j, h in enumerate(horizons):
        col = ret_matrix[:, j]
        valid = np.isfinite(col)
        n_valid = int(valid.sum())
        if n_valid < 10:
            out["horizons"][h] = {"n_valid": n_valid, "note": "insufficient"}
            continue

        ret = col[valid]
        sig = signal_values[valid] if signal_values is not None else None

        # Core stats
        ic = M.spearman_ic(sig, ret) if sig is not None else None
        ic_p = M.bootstrap_ic_pvalue(sig, ret, n_iter=bootstrap_iter, rng=rng) if sig is not None else None
        mean_r = float(ret.mean())
        median_r = float(np.median(ret))
        ci_lo, ci_hi = M.bootstrap_mean_ci(ret, n_iter=bootstrap_iter, rng=rng)
        hit_pos = M.hit_rate(ret, direction=+1)
        baseline_hit = _baseline_hit_rate(klines_table, hours[j])

        # Sharpe — horizon-frequency → annualized
        ppy = HORIZON_PERIODS_PER_YEAR.get(h, 365)
        sharpe = M.annualized_sharpe(ret, periods_per_year=ppy)

        # Equity + DD
        equity = M.equity_curve(ret, side="long")
        mdd = M.max_drawdown(equity)

        # DSR
        skew = _skew(ret)
        kurt = _excess_kurt(ret)
        dsr = M.deflated_sharpe(sharpe, n_valid, n_trials, skew=skew, kurt_excess=kurt)

        percentiles = M.percentiles(ret)

        out["horizons"][h] = {
            "n_valid":                  n_valid,
            "mean_return":              mean_r,
            "median_return":            median_r,
            "ci95_bootstrap":           [ci_lo, ci_hi],
            "ic_spearman":              ic,
            "ic_pvalue_bootstrap":      ic_p,
            "hit_rate":                 hit_pos,
            "hit_rate_baseline_random": baseline_hit,
            "sharpe_annualized":        sharpe,
            "max_drawdown":             mdd,
            "deflated_sharpe":          dsr,
            "percentiles":              percentiles,
            "equity_curve_pts":         [float(v) for v in equity[::max(1, len(equity) // 200)]],
        }

    return out


def run_with_regimes(
    event_ts_ms: np.ndarray,
    signal_values: np.ndarray,
    regime_labels: Dict[str, np.ndarray],
    horizons: List[str],
    klines_table: Optional[pa.Table] = None,
    bootstrap_iter: int = 5000,
    rng_seed: int = 42,
) -> dict:
    """Like run_event_study but split by regime combinations.

    regime_labels : dict of regime_name → array of string labels per event (same length as event_ts_ms).
    Returns a dict keyed by 'regime1=a & regime2=b' strings.
    """
    assert all(len(v) == len(event_ts_ms) for v in regime_labels.values())
    names = list(regime_labels.keys())
    arrs = [regime_labels[n] for n in names]
    combos: Dict[str, list] = {}
    for i in range(len(event_ts_ms)):
        key = " & ".join(f"{n}={a[i]}" for n, a in zip(names, arrs))
        combos.setdefault(key, []).append(i)

    out = {}
    for key, idxs in combos.items():
        if len(idxs) < 30:
            out[key] = {"n_events": len(idxs), "note": "insufficient for conditioning"}
            continue
        idxs_arr = np.array(idxs, dtype=np.int64)
        sub_ts = event_ts_ms[idxs_arr]
        sub_sig = signal_values[idxs_arr]
        out[key] = run_event_study(
            event_ts_ms=sub_ts,
            signal_values=sub_sig,
            horizons=horizons,
            klines_table=klines_table,
            bootstrap_iter=bootstrap_iter,
            rng_seed=rng_seed,
        )
    return out


def purged_split_indices(
    event_ts_ms: np.ndarray,
    test_start_ms: int,
    test_end_ms: int,
    horizon_hours_max: float,
) -> tuple:
    """Return (train_idx, test_idx) arrays with a purge band of horizon_hours_max
    around the test window. Any training event whose forward horizon overlaps
    the test window, or whose timestamp falls within purge of the test boundary,
    is dropped from training."""
    purge_ms = int(horizon_hours_max * 3_600_000)
    ts = np.asarray(event_ts_ms, dtype=np.int64)
    in_test = (ts >= test_start_ms) & (ts < test_end_ms)
    # Train = outside test AND outside the purge band on either side. Without
    # the outer parens, `&`/`|` precedence makes the right-side clause apply
    # regardless of in_test — currently harmless given the test-window invariant,
    # but breaks if purge becomes asymmetric or test windows are redefined.
    train_mask = (~in_test) & (
        (ts + purge_ms < test_start_ms) | (ts > test_end_ms + purge_ms)
    )
    return np.where(train_mask)[0], np.where(in_test)[0]


# ── Internal helpers ─────────────────────────────────────────────────
def _baseline_hit_rate(klines_table: Optional[pa.Table], horizon_hours: float) -> float:
    """Baseline: fraction of all hourly-rolling returns of size horizon_hours that
    were > 0 over the full kline history. This is the benchmark an 'always long'
    signal would hit randomly."""
    if klines_table is None:
        klines_table = load_klines_1h()
    if klines_table is None:
        return float("nan")
    close = klines_table.column("close").to_numpy().astype(np.float64)
    shift = int(round(horizon_hours))
    if shift <= 0 or shift >= len(close):
        return float("nan")
    log_r = np.log(close[shift:] / close[:-shift])
    log_r = log_r[np.isfinite(log_r)]
    if len(log_r) == 0:
        return float("nan")
    return float((log_r > 0).mean())


def _skew(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if len(x) < 3:
        return 0.0
    m = x.mean()
    s = x.std(ddof=1)
    if s == 0:
        return 0.0
    return float(((x - m) ** 3).mean() / (s ** 3))


def _excess_kurt(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if len(x) < 4:
        return 0.0
    m = x.mean()
    s = x.std(ddof=1)
    if s == 0:
        return 0.0
    return float(((x - m) ** 4).mean() / (s ** 4) - 3.0)
