"""Event-study metrics: IC, hit rate, Sharpe, max DD, deflated Sharpe, bootstrap CI.

All functions take numpy arrays. No pandas. Vectorised where possible.
"""

from __future__ import annotations

import math
from typing import Optional
import numpy as np


def _rankdata(x: np.ndarray) -> np.ndarray:
    """Average-rank (handles ties) — equivalent to scipy.stats.rankdata(x, method='average')."""
    n = len(x)
    order = np.argsort(x, kind="mergesort")
    ranks = np.empty(n, dtype=np.float64)
    ranks[order] = np.arange(1, n + 1, dtype=np.float64)
    # Average rank for ties
    _, inv, counts = np.unique(x, return_inverse=True, return_counts=True)
    if counts.max() > 1:
        # Sum of ranks per group / count
        sum_ranks = np.zeros_like(counts, dtype=np.float64)
        np.add.at(sum_ranks, inv, ranks)
        mean_ranks = sum_ranks / counts
        ranks = mean_ranks[inv]
    return ranks


def spearman_ic(signal: np.ndarray, forward_ret: np.ndarray) -> float:
    """Spearman rank correlation between signal value and forward return.

    Returns 0.0 on empty/constant inputs (conservative). NaN values are dropped
    pairwise before computing.
    """
    s = np.asarray(signal, dtype=np.float64)
    r = np.asarray(forward_ret, dtype=np.float64)
    mask = np.isfinite(s) & np.isfinite(r)
    s, r = s[mask], r[mask]
    if len(s) < 10 or np.ptp(s) == 0 or np.ptp(r) == 0:
        return 0.0
    rs = _rankdata(s)
    rr = _rankdata(r)
    rs_dev = rs - rs.mean()
    rr_dev = rr - rr.mean()
    denom = math.sqrt((rs_dev @ rs_dev) * (rr_dev @ rr_dev))
    if denom == 0:
        return 0.0
    return float((rs_dev @ rr_dev) / denom)


def hit_rate(forward_ret: np.ndarray, direction: int = 1) -> float:
    """Fraction of forward returns in the expected direction.

    direction = +1 → fraction of returns > 0
    direction = -1 → fraction of returns < 0
    Returns NaN if empty.
    """
    r = np.asarray(forward_ret, dtype=np.float64)
    r = r[np.isfinite(r)]
    if len(r) == 0:
        return float("nan")
    if direction > 0:
        return float((r > 0).mean())
    return float((r < 0).mean())


def annualized_sharpe(forward_ret: np.ndarray, periods_per_year: float) -> float:
    """Simple Sharpe (no risk-free adjustment — returns are already excess for
    a crypto perp). periods_per_year converts to annual scale. Returns NaN on
    insufficient data."""
    r = np.asarray(forward_ret, dtype=np.float64)
    r = r[np.isfinite(r)]
    if len(r) < 10 or r.std(ddof=1) == 0:
        return float("nan")
    return float(r.mean() / r.std(ddof=1) * math.sqrt(periods_per_year))


def max_drawdown(equity: np.ndarray) -> float:
    """Maximum drawdown of an equity curve (cumulative). Returns value in [-1, 0]."""
    e = np.asarray(equity, dtype=np.float64)
    if len(e) < 2:
        return 0.0
    running_max = np.maximum.accumulate(e)
    dd = (e - running_max) / running_max
    return float(dd.min())


def equity_curve(forward_ret: np.ndarray, side: str = "long") -> np.ndarray:
    """Cumulative equity from sequentially compounding returns (assuming 1 unit
    per event). side='long' → returns as-is; 'short' → negated; 'long_short'
    → abs value with sign from returns (trivially same as long if all positive,
    but useful when signals carry direction)."""
    r = np.asarray(forward_ret, dtype=np.float64)
    r = r[np.isfinite(r)]
    if side == "short":
        r = -r
    elif side == "long_short":
        pass  # expects signed returns already
    if len(r) == 0:
        return np.array([1.0])
    # Compound via log: equity[k] = exp(sum(r[:k+1]))
    # Since r is log-return, cumulative equity = exp(cumsum(r))
    return np.exp(np.cumsum(r))


def deflated_sharpe(
    sharpe: float,
    n_obs: int,
    n_trials: int,
    skew: float = 0.0,
    kurt_excess: float = 0.0,
) -> float:
    """Deflated Sharpe ratio (Bailey & López de Prado, 2014).

    Adjusts the observed Sharpe for multiple-testing (n_trials independent
    strategies searched) and non-normality of returns (skew/excess-kurt).
    Returns a probability that the true Sharpe is > 0 given the observation.

    n_trials: number of strategies tried (set = 1 for a single-hypothesis test).
    """
    if n_obs < 10 or n_trials < 1:
        return float("nan")
    # Expected maximum of N(0,1) random variables (Gumbel approx)
    emc = 0.5772156649  # Euler–Mascheroni
    max_z = (1 - emc) * _norm_ppf(1 - 1 / n_trials) + emc * _norm_ppf(1 - 1 / (n_trials * math.e))
    # Variance of Sharpe estimator under non-normal returns (LdP eq 7)
    var_sr = (1 - skew * sharpe + ((kurt_excess - 1) / 4) * sharpe * sharpe) / (n_obs - 1)
    sr_std = math.sqrt(max(var_sr, 1e-12))
    if sr_std == 0:
        return float("nan")
    dsr_z = (sharpe - max_z) / sr_std
    return float(_norm_cdf(dsr_z))


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _norm_ppf(p: float) -> float:
    """Inverse CDF via Beasley-Springer-Moro (approx). Good enough for DSR."""
    # Abramowitz & Stegun 26.2.23
    if p <= 0 or p >= 1:
        return 0.0
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
               ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
                ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r + a[1])*r + a[2])*r + a[3])*r + a[4])*r + a[5]) * q / \
           (((((b[0]*r + b[1])*r + b[2])*r + b[3])*r + b[4])*r + 1)


def bootstrap_mean_ci(
    x: np.ndarray,
    n_iter: int = 5000,
    alpha: float = 0.05,
    rng: Optional[np.random.Generator] = None,
) -> tuple:
    """Percentile bootstrap CI for the mean of x. Returns (low, high)."""
    if rng is None:
        rng = np.random.default_rng(42)
    x = np.asarray(x, dtype=np.float64)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 10:
        return (float("nan"), float("nan"))
    means = np.empty(n_iter, dtype=np.float64)
    for i in range(n_iter):
        sample_idx = rng.integers(0, n, size=n)
        means[i] = x[sample_idx].mean()
    lo = float(np.percentile(means, 100 * alpha / 2))
    hi = float(np.percentile(means, 100 * (1 - alpha / 2)))
    return (lo, hi)


def bootstrap_ic_pvalue(
    signal: np.ndarray,
    forward_ret: np.ndarray,
    n_iter: int = 5000,
    rng: Optional[np.random.Generator] = None,
) -> float:
    """Permutation p-value: P(|IC_shuffled| >= |IC_observed|).

    H0: signal is independent of forward return (no edge).
    Permute the return labels and recompute IC for each shuffle.
    """
    if rng is None:
        rng = np.random.default_rng(42)
    s = np.asarray(signal, dtype=np.float64)
    r = np.asarray(forward_ret, dtype=np.float64)
    mask = np.isfinite(s) & np.isfinite(r)
    s, r = s[mask], r[mask]
    n = len(s)
    if n < 10 or np.ptp(s) == 0:
        return float("nan")
    ic_obs = abs(spearman_ic(s, r))
    # Pre-compute ranks once for speed
    rs = _rankdata(s)
    exceed = 0
    for _ in range(n_iter):
        perm = rng.permutation(r)
        rp = _rankdata(perm)
        rs_dev = rs - rs.mean()
        rp_dev = rp - rp.mean()
        denom = math.sqrt((rs_dev @ rs_dev) * (rp_dev @ rp_dev))
        ic_shuf = abs((rs_dev @ rp_dev) / denom) if denom > 0 else 0.0
        if ic_shuf >= ic_obs:
            exceed += 1
    return (exceed + 1) / (n_iter + 1)


def percentiles(x: np.ndarray) -> dict:
    """Return p5/p25/p50/p75/p95 of the distribution."""
    x = np.asarray(x, dtype=np.float64)
    x = x[np.isfinite(x)]
    if len(x) == 0:
        return {"p5": None, "p25": None, "p50": None, "p75": None, "p95": None}
    p = np.percentile(x, [5, 25, 50, 75, 95])
    return {"p5": float(p[0]), "p25": float(p[1]), "p50": float(p[2]),
            "p75": float(p[3]), "p95": float(p[4])}
