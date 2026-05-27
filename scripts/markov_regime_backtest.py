"""
Markov regime backtest for ETH 4h returns.

Self-contained, single file. Runs the F1–F6 pipeline from the regime detection
working doc (see ETH_Regimen_HMM_Documento_de_Trabajo.md) for K=3 states
(UP / DOWN / CHOP) before committing to anything heavier (HMM with covariance,
hidden state inference, etc).

F1. Bright-line states from train-set return terciles (no tunable thresholds)
F2. Dirichlet-smoothed transition matrix + block bootstrap CI per cell
F3. Homogeneity test across 3 train sub-periods (is the matrix stationary?)
F4. Persistence baseline accuracy vs Markov on validation
F5. Stationary distribution + average dwell time per state
F6. Out-of-sample validation (no peeking at test, only train→val)

Reads `data/backfill/binance_klines_1h.parquet`, aggregates to 4h, runs the
pipeline, prints a report to stdout. No commit-time side effects.

Run:
    python scripts/markov_regime_backtest.py
"""

from __future__ import annotations

import sys
import os
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

sys.stdout.reconfigure(encoding="utf-8")

# ── Config ───────────────────────────────────────────────────────────
PARQUET_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..",
    "data", "backfill", "binance_klines_1h.parquet",
)
STATES = ["DOWN", "CHOP", "UP"]   # order matters: indexes into matrices
K = len(STATES)
DIRICHLET_ALPHA = 1.0              # Laplace smoothing (uniform prior)
BLOCK_SIZE = 20                    # 4h bars per block for bootstrap (~3.3 days)
N_BOOTSTRAP = 1000
TRAIN_END = "2024-12-31"           # boundary 1: train cutoff
VAL_END   = "2025-06-30"           # boundary 2: validation cutoff
# Test set is everything after VAL_END — we do NOT touch it in this run.


# ── Data loading + state labeling ────────────────────────────────────
def load_4h_returns() -> pd.DataFrame:
    """Load 1h klines, aggregate to 4h close-to-close log returns."""
    t = pq.read_table(PARQUET_PATH)
    df = t.to_pandas()[["ts_utc_ms", "close"]].sort_values("ts_utc_ms")
    df["ts"] = pd.to_datetime(df["ts_utc_ms"], unit="ms", utc=True)
    # Resample to 4h on bar START (closing price of the 4th hour)
    df = df.set_index("ts")
    close_4h = df["close"].resample("4h").last().dropna()
    ret_4h = np.log(close_4h).diff().dropna()
    return pd.DataFrame({"close": close_4h, "ret": ret_4h}).dropna()


def label_states(returns: pd.Series, lo: float, hi: float) -> pd.Series:
    """Tercile-based bright-line labeling. lo/hi are the train-set terciles."""
    out = pd.Series(index=returns.index, dtype="object")
    out[returns <= lo] = "DOWN"
    out[(returns > lo) & (returns < hi)] = "CHOP"
    out[returns >= hi] = "UP"
    return out


def split_train_val_test(df: pd.DataFrame):
    """Chronological 3-way split. Train < TRAIN_END, val [TRAIN_END, VAL_END],
    test > VAL_END. Returns 3 DataFrames."""
    train = df[df.index <= TRAIN_END]
    val = df[(df.index > TRAIN_END) & (df.index <= VAL_END)]
    test = df[df.index > VAL_END]
    return train, val, test


# ── Transition matrix estimation ─────────────────────────────────────
def state_to_idx(s: pd.Series) -> np.ndarray:
    """Map state labels to integer indices via STATES order."""
    idx_map = {s: i for i, s in enumerate(STATES)}
    return np.array([idx_map[x] for x in s])


def count_transitions(state_idx: np.ndarray) -> np.ndarray:
    """Count C[i,j] = number of times state went from i to j."""
    C = np.zeros((K, K), dtype=np.int64)
    for a, b in zip(state_idx[:-1], state_idx[1:]):
        C[a, b] += 1
    return C


def smooth_transition(C: np.ndarray, alpha: float = DIRICHLET_ALPHA) -> np.ndarray:
    """Dirichlet-smoothed row-stochastic matrix:
       P[i,j] = (C[i,j] + alpha) / (sum_j(C[i,j]) + K * alpha)
    """
    P = (C + alpha) / (C.sum(axis=1, keepdims=True) + K * alpha)
    return P


# ── Bootstrap CIs ────────────────────────────────────────────────────
def block_bootstrap_ci(state_idx: np.ndarray, block_size: int = BLOCK_SIZE,
                       B: int = N_BOOTSTRAP, alpha: float = DIRICHLET_ALPHA,
                       rng_seed: int = 42):
    """Block bootstrap on the state sequence. Returns (lo, mid, hi) matrices
    at 2.5 / 50 / 97.5 percentiles per cell."""
    rng = np.random.default_rng(rng_seed)
    n = len(state_idx)
    n_blocks = n // block_size
    results = np.empty((B, K, K), dtype=np.float64)
    for b in range(B):
        idx_start = rng.integers(0, n - block_size, size=n_blocks)
        sample = np.concatenate([state_idx[i:i + block_size] for i in idx_start])
        C_b = count_transitions(sample)
        results[b] = smooth_transition(C_b, alpha=alpha)
    lo = np.percentile(results, 2.5, axis=0)
    md = np.percentile(results, 50.0, axis=0)
    hi = np.percentile(results, 97.5, axis=0)
    return lo, md, hi


# ── Homogeneity test ─────────────────────────────────────────────────
def chi2_homogeneity_G(state_idx: np.ndarray, n_periods: int = 3) -> tuple:
    """Likelihood-ratio chi-square (G²) test for Markov chain homogeneity
    across n_periods sub-periods. Compares per-period transition counts to
    the pooled MLE.

    H0: same transition matrix in all sub-periods.
    Returns: (G2, dof, p_value_approx)

    Approximation: chi-square with dof = (n_periods - 1) * K * (K - 1).
    """
    from scipy.stats import chi2 as chi2_dist
    n = len(state_idx)
    cuts = np.linspace(0, n, n_periods + 1, dtype=int)
    sub_C = [count_transitions(state_idx[cuts[k]:cuts[k + 1]])
             for k in range(n_periods)]
    pooled_C = sum(sub_C)
    pooled_row_sums = pooled_C.sum(axis=1, keepdims=True)
    pooled_P = np.where(pooled_row_sums > 0, pooled_C / pooled_row_sums, 0.0)

    G2 = 0.0
    for C_k in sub_C:
        for i in range(K):
            for j in range(K):
                if C_k[i, j] > 0 and pooled_P[i, j] > 0:
                    expected = C_k[i, :].sum() * pooled_P[i, j]
                    if expected > 0:
                        G2 += 2 * C_k[i, j] * np.log(C_k[i, j] / expected)
    dof = (n_periods - 1) * K * (K - 1)
    p = 1.0 - chi2_dist.cdf(G2, dof)
    return G2, dof, p


# ── Stationary distribution ──────────────────────────────────────────
def stationary_distribution(P: np.ndarray, max_iter: int = 10000,
                            tol: float = 1e-12) -> np.ndarray:
    """Power-iteration stationary distribution. π = π P."""
    pi = np.ones(K) / K
    for _ in range(max_iter):
        pi_new = pi @ P
        if np.max(np.abs(pi_new - pi)) < tol:
            return pi_new
        pi = pi_new
    return pi


# ── Persistence baseline + Markov accuracy on val ────────────────────
def accuracy_markov_vs_persistence(state_idx: np.ndarray, P: np.ndarray) -> dict:
    """For each t > 0, persistence predicts s_t = s_{t-1}; Markov predicts
    s_t = argmax_j P[s_{t-1}, j]. Compares to true s_t."""
    if len(state_idx) < 2:
        return {"persistence_acc": None, "markov_acc": None, "n": 0}
    prev = state_idx[:-1]
    actual = state_idx[1:]
    pred_persistence = prev
    pred_markov = P[prev].argmax(axis=1)
    return {
        "persistence_acc": float((pred_persistence == actual).mean()),
        "markov_acc": float((pred_markov == actual).mean()),
        "n": int(len(actual)),
        "markov_predicts_same_as_persistence_pct": float(
            (pred_markov == pred_persistence).mean()
        ),
    }


# ── Report ───────────────────────────────────────────────────────────
def fmt_matrix(M: np.ndarray, fmt: str = "{:>8.4f}") -> str:
    rows = []
    header = "        " + "".join(f"{s:>10s}" for s in STATES)
    rows.append(header)
    for i, s in enumerate(STATES):
        row = f"{s:<8s}" + "".join(fmt.format(M[i, j]) for j in range(K)) + "  "
        rows.append(row)
    return "\n".join(rows)


def fmt_count_matrix(C: np.ndarray) -> str:
    rows = ["        " + "".join(f"{s:>10s}" for s in STATES)]
    for i, s in enumerate(STATES):
        row = f"{s:<8s}" + "".join(f"{C[i, j]:>10d}" for j in range(K))
        rows.append(row)
    return "\n".join(rows)


def main() -> None:
    print("=" * 70)
    print("Markov Regime Backtest — ETH 4h, K=3 (DOWN/CHOP/UP)")
    print("=" * 70)

    # ── Load + split ─────────────────────────────────────────────────
    print("\n[Load] Reading 1h klines, aggregating to 4h log-returns …")
    df = load_4h_returns()
    train, val, test = split_train_val_test(df)
    print(f"  total 4h bars: {len(df):,}")
    print(f"  train : {train.index.min().date()} → {train.index.max().date()}  ({len(train):,} bars)")
    print(f"  val   : {val.index.min().date()} → {val.index.max().date()}  ({len(val):,} bars)")
    print(f"  test  : {test.index.min().date()} → {test.index.max().date()}  ({len(test):,} bars)  [NOT USED]")

    # ── F1: state definition from train terciles ─────────────────────
    lo, hi = train["ret"].quantile([1 / 3, 2 / 3]).tolist()
    print(f"\n[F1] Bright-line state thresholds from TRAIN terciles:")
    print(f"  DOWN: ret ≤ {lo:.6f}   ({lo * 100:+.3f}% per 4h)")
    print(f"  CHOP: {lo:.6f} < ret < {hi:.6f}")
    print(f"  UP  : ret ≥ {hi:.6f}   ({hi * 100:+.3f}% per 4h)")

    train["state"] = label_states(train["ret"], lo, hi)
    val["state"] = label_states(val["ret"], lo, hi)

    # State distribution
    train_dist = train["state"].value_counts(normalize=True).reindex(STATES)
    val_dist = val["state"].value_counts(normalize=True).reindex(STATES)
    print(f"\n[F1] State distribution (% of bars):")
    print(f"  {'state':<8}{'train':>10}{'val':>10}")
    for s in STATES:
        print(f"  {s:<8}{train_dist[s] * 100:>9.2f}%{val_dist[s] * 100:>9.2f}%")

    train_idx = state_to_idx(train["state"])
    val_idx = state_to_idx(val["state"])

    # ── F2: smoothed transition matrix + bootstrap CI ────────────────
    C = count_transitions(train_idx)
    print(f"\n[F2] Train transition counts (raw, n={C.sum()} transitions):")
    print(fmt_count_matrix(C))
    min_count = C.min()
    print(f"  min cell count: {min_count}   (rule of thumb: ≥30 per cell)")

    P = smooth_transition(C, alpha=DIRICHLET_ALPHA)
    print(f"\n[F2] Smoothed transition matrix P̂ (Dirichlet α={DIRICHLET_ALPHA}):")
    print(fmt_matrix(P))

    print(f"\n[F2] Block bootstrap 95% CI per cell (B={N_BOOTSTRAP}, block_size={BLOCK_SIZE} bars):")
    lo_ci, mid_ci, hi_ci = block_bootstrap_ci(train_idx)
    print(f"\n  Lower (2.5%):")
    print(fmt_matrix(lo_ci))
    print(f"\n  Upper (97.5%):")
    print(fmt_matrix(hi_ci))
    print(f"\n  CI width (hi − lo):")
    print(fmt_matrix(hi_ci - lo_ci))

    # Flag wide CIs
    wide = (hi_ci - lo_ci) > 0.10
    if wide.any():
        print(f"  ⚠️  {wide.sum()} cell(s) have CI width > 0.10 — treat as low-confidence")
    else:
        print(f"  ✓  All cells have CI width ≤ 0.10")

    # ── F3: homogeneity test ─────────────────────────────────────────
    print(f"\n[F3] Homogeneity test across 3 train sub-periods (G² likelihood ratio):")
    G2, dof, p = chi2_homogeneity_G(train_idx, n_periods=3)
    print(f"  G² = {G2:.2f}   dof = {dof}   p ≈ {p:.4f}")
    if p < 0.05:
        print(f"  ⚠️  Reject H0 (homogeneous Markov) at α=0.05")
        print(f"      → matrix is NOT stationary across train. Need rolling-window re-fit.")
    else:
        print(f"  ✓  Cannot reject homogeneity. Stationary matrix is defensible.")

    # ── F4: persistence baseline ─────────────────────────────────────
    print(f"\n[F4] Persistence baseline vs Markov (on TRAIN set, in-sample):")
    train_acc = accuracy_markov_vs_persistence(train_idx, P)
    print(f"  n = {train_acc['n']:,} predictions")
    print(f"  Persistence: {train_acc['persistence_acc'] * 100:.2f}%")
    print(f"  Markov     : {train_acc['markov_acc'] * 100:.2f}%")
    print(f"  Δ (Markov − Persistence): {(train_acc['markov_acc'] - train_acc['persistence_acc']) * 100:+.2f}pp")
    print(f"  Markov predicts same state as persistence: {train_acc['markov_predicts_same_as_persistence_pct'] * 100:.1f}% of the time")

    # ── F5: stationary distribution + dwell time ─────────────────────
    pi = stationary_distribution(P)
    dwell = 1.0 / (1.0 - np.diag(P))  # expected dwell time = 1 / (1 - P_ii)
    print(f"\n[F5] Stationary distribution π (long-run % time in each state):")
    print(f"  {'state':<8}{'π':>10}{'dwell (bars)':>15}{'dwell (hours)':>15}")
    for i, s in enumerate(STATES):
        print(f"  {s:<8}{pi[i] * 100:>9.2f}%{dwell[i]:>14.2f}{dwell[i] * 4:>14.1f}")
    print(f"  Sanity: π sums to {pi.sum():.6f}")

    # ── F6: validation set ───────────────────────────────────────────
    print(f"\n[F6] Out-of-sample validation (val set, single pass):")
    val_acc = accuracy_markov_vs_persistence(val_idx, P)
    print(f"  n = {val_acc['n']:,} predictions")
    print(f"  Persistence: {val_acc['persistence_acc'] * 100:.2f}%")
    print(f"  Markov     : {val_acc['markov_acc'] * 100:.2f}%")
    delta_val = (val_acc['markov_acc'] - val_acc['persistence_acc']) * 100
    print(f"  Δ (Markov − Persistence): {delta_val:+.2f}pp")

    # Verdict
    print("\n" + "=" * 70)
    print("VERDICT")
    print("=" * 70)
    if delta_val < 1.0:
        print("  ❌ Markov does NOT meaningfully beat persistence out-of-sample.")
        print("     Δ < 1pp suggests no exploitable Markovian structure at K=3 / 4h.")
        print("     Recommendation: don't use this as a filter. Consider higher K,")
        print("     different features (vol-conditioned states), or different TF.")
    elif delta_val < 3.0:
        print(f"  ⚠️  Marginal edge ({delta_val:+.2f}pp on val).")
        print("     Borderline. Test on the holdout test set ONCE to confirm.")
    else:
        print(f"  ✓  Markov beats persistence by {delta_val:+.2f}pp on val.")
        print("     Worth proceeding to HMM (latent states + emission features).")

    if p < 0.05:
        print(f"  ⚠️  Reminder: homogeneity rejected. Stationary matrix is approximate.")
        print(f"     Even if accuracy is OK, rolling-window re-fit is needed in production.")


if __name__ == "__main__":
    main()
