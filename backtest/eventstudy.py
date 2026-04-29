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

import math
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


def _purge_overlapping_events(
    sorted_event_ts_ms: np.ndarray,
    horizon_max_hours: float,
) -> np.ndarray:
    """Greedy non-overlap filter: walk sorted events, drop event i whenever the
    next event lands inside its forward horizon window.

    Concretely: keep event i iff (event_ts[i+1] - event_ts[i]) > horizon_max_hours
    (the last event is always kept — there is no "next" to overlap with).

    Why we need this: when the same signal fires several times within a horizon,
    their forward-return windows overlap. Treating the overlapping returns as
    independent draws shrinks bootstrap CIs and inflates Sharpe — both reflecting
    serial correlation that is not present in genuinely independent observations.

    Parameters
    ----------
    sorted_event_ts_ms : (N,) int64 — MUST be ascending.
    horizon_max_hours : the largest horizon being evaluated. Smaller horizons
                       are also covered because their windows are subsets.

    Returns
    -------
    keep : (N,) bool mask aligned with input order.
    """
    horizon_ms = int(horizon_max_hours * 3_600_000)
    n = len(sorted_event_ts_ms)
    keep = np.ones(n, dtype=bool)
    if n <= 1:
        return keep
    gaps = np.diff(sorted_event_ts_ms)
    # event i overlaps with i+1 iff gap[i] <= horizon_ms — drop event i.
    keep[:-1] = gaps > horizon_ms
    return keep


def run_event_study(
    event_ts_ms: np.ndarray,
    signal_values: np.ndarray,
    horizons: List[str],
    klines_table: Optional[pa.Table] = None,
    bootstrap_iter: int = 5000,
    n_trials: int = 1,
    rng_seed: int = 42,
    purge_overlapping_events: bool = True,
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
    purge_overlapping_events : if True (default), drop events whose forward
        horizon window overlaps the next event's anchor. Prevents serial-correlation
        leakage that artificially tightens CIs and inflates Sharpe. The number of
        events purged is reported as `n_events_purged` in the output.

    Returns
    -------
    dict with per-horizon statistics. Shape matches the CLI contract. Includes
    `n_events`, `n_events_pre_purge`, `n_events_purged`, and `horizons`.
    """
    if klines_table is None:
        klines_table = load_klines_1h()
    hours = [_horizon_to_hours(h) for h in horizons]
    rng = np.random.default_rng(rng_seed)

    # Purging requires sorted input — sort everything together so signal/ts stay aligned.
    event_ts_ms = np.asarray(event_ts_ms, dtype=np.int64)
    signal_values = np.asarray(signal_values, dtype=np.float64) if signal_values is not None else None
    order = np.argsort(event_ts_ms, kind="stable")
    event_ts_ms = event_ts_ms[order]
    if signal_values is not None:
        signal_values = signal_values[order]

    n_pre_purge = int(len(event_ts_ms))
    if purge_overlapping_events and len(event_ts_ms) > 1:
        h_max = max(hours)
        keep_mask = _purge_overlapping_events(event_ts_ms, h_max)
        event_ts_ms = event_ts_ms[keep_mask]
        if signal_values is not None:
            signal_values = signal_values[keep_mask]
    n_post_purge = int(len(event_ts_ms))
    n_purged = n_pre_purge - n_post_purge

    ret_matrix = forward_log_returns(event_ts_ms, hours, klines_table=klines_table)

    # Annualization frequency from event spacing (operationally correct).
    # The legacy `sharpe_annualized` used HORIZON_PERIODS_PER_YEAR which assumes
    # the strategy could re-fire every horizon period (8760/year for 1h). That's
    # only true for an always-on signal. A signal that fires 77 times in 2 years
    # has 38.5 opportunities per year — annualizing by horizon-frequency multiplies
    # the per-event Sharpe by sqrt(8760/77) ≈ 10.7 vs the operationally honest
    # sqrt(38.5) ≈ 6.2, so the old number was inflated/deflated by ~70%.
    if n_post_purge >= 2:
        years_in_sample = (event_ts_ms.max() - event_ts_ms.min()) / (365.25 * 86400 * 1000)
        event_freq_per_year = (n_post_purge / years_in_sample) if years_in_sample > 0 else float("nan")
    else:
        years_in_sample = float("nan")
        event_freq_per_year = float("nan")

    out: Dict[str, Any] = {
        "n_events":                n_post_purge,
        "n_events_pre_purge":      n_pre_purge,
        "n_events_purged":         n_purged,
        "purge_applied":           bool(purge_overlapping_events),
        "years_in_sample":         years_in_sample,
        "event_frequency_per_year": event_freq_per_year,
        "horizons":                {},
    }

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

        # Sharpe variants:
        #   sharpe_per_event              — raw mean/std of per-event log returns,
        #                                    no annualization. Used for DSR.
        #   sharpe_annualized_by_event_freq — operationally correct: scales by
        #                                    sqrt(events per year). Tells you the
        #                                    Sharpe you'd actually realize trading
        #                                    this signal.
        #   sharpe_annualized_by_horizon_freq — legacy. Scales by sqrt(periods of
        #                                    size `horizon` per year), assuming the
        #                                    signal could re-fire every horizon.
        #                                    Kept for backwards compatibility.
        ret_std = float(ret.std(ddof=1)) if len(ret) > 1 else 0.0
        sharpe_per_event = float(ret.mean() / ret_std) if ret_std > 0 else float("nan")
        sharpe_event_freq = (
            sharpe_per_event * math.sqrt(event_freq_per_year)
            if (event_freq_per_year and event_freq_per_year > 0 and math.isfinite(sharpe_per_event))
            else float("nan")
        )
        ppy = HORIZON_PERIODS_PER_YEAR.get(h, 365)
        sharpe_horizon_freq = M.annualized_sharpe(ret, periods_per_year=ppy)

        # Equity + DD
        equity = M.equity_curve(ret, side="long")
        mdd = M.max_drawdown(equity)

        # DSR — must consume the per-event Sharpe with n_obs = n_valid. The DSR
        # formula's variance term assumes per-period (un-annualized) Sharpe;
        # passing the annualized value (as before) injected a sqrt(ppy) scale
        # error that made DSR pin to ~1.0 on signals with weak per-event edge.
        skew = _skew(ret)
        kurt = _excess_kurt(ret)
        dsr = M.deflated_sharpe(sharpe_per_event, n_valid, n_trials, skew=skew, kurt_excess=kurt)

        percentiles = M.percentiles(ret)

        out["horizons"][h] = {
            "n_valid":                          n_valid,
            "mean_return":                      mean_r,
            "median_return":                    median_r,
            "ci95_bootstrap":                   [ci_lo, ci_hi],
            "ic_spearman":                      ic,
            "ic_pvalue_bootstrap":              ic_p,
            "hit_rate":                         hit_pos,
            "hit_rate_baseline_random":         baseline_hit,
            "sharpe_per_event":                 sharpe_per_event,
            "sharpe_annualized_by_event_freq":  sharpe_event_freq,
            "sharpe_annualized_by_horizon_freq": sharpe_horizon_freq,
            "max_drawdown":                     mdd,
            "deflated_sharpe":                  dsr,
            "percentiles":                      percentiles,
            "equity_curve_pts":                 [float(v) for v in equity[::max(1, len(equity) // 200)]],
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
    purge_overlapping_events: bool = True,
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
            purge_overlapping_events=purge_overlapping_events,
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
