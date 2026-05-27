"""
HMM regime backtest for ETH 4h — full version with 8 emission features.

This is the natural continuation of `markov_regime_backtest.py`, which showed
that tercile-labeled states on raw 4h returns have NO Markovian structure
out-of-sample (Δ +0.09pp vs persistence). The hypothesis there was:
"states defined only by return percentiles don't capture regime context."

This script tests the alternative: latent states inferred by a Gaussian HMM
on a richer feature set, per the working doc ETH_Regimen_HMM_Documento_de_Trabajo.md:

  1. log_return_4h           — direction + magnitude
  2. realized_vol_24         — close-to-close vol, 24-bar window (~4 days)
  3. parkinson_vol_24        — high-low range vol, 24-bar window
  4. taker_buy_imbalance     — (taker_buy_base − sell_base) / volume
  5. return_skew_20          — rolling skewness of returns, 20-bar window
  6. funding_zscore          — rolling z-score of Binance funding rate
  7. eth_btc_relret_20       — ETH log-return − BTC log-return over 20 bars
  8. vix_level_z             — rolling z-score of VIX daily level

All standardization is causal (rolling, NO peeking at future data).

Anti-overfit guards:
  - Diagonal covariance (K * 2d params instead of K * d² + d means)
  - K=3, 20+ random restarts on EM (Baum-Welch local maxima)
  - Train/val/test temporal split, test set NEVER touched in this run
  - BIC reported for K=2,3,4 — informs whether K=3 is the right choice
  - Block bootstrap on Viterbi-decoded states for transition matrix CIs
  - Persistence baseline as the bar to clear

Output: full F2-F6 pipeline on the HMM-decoded states + comparison vs the
naive K=3 baseline from markov_regime_backtest.py.

Run:
    python scripts/markov_hmm_backtest.py

Requires: pip install hmmlearn (already in dev env).
"""

from __future__ import annotations

import os
import sys
import time
import warnings

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from hmmlearn import hmm
from scipy.stats import chi2 as chi2_dist

sys.stdout.reconfigure(encoding="utf-8")
warnings.filterwarnings("ignore", category=RuntimeWarning)
warnings.filterwarnings("ignore", category=UserWarning, module="hmmlearn")

# ── Config ───────────────────────────────────────────────────────────
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
KLINES_PATH  = os.path.join(ROOT, "data", "backfill", "binance_klines_1h.parquet")
FUNDING_PATH = os.path.join(ROOT, "data", "backfill", "binance_funding.parquet")
MACRO_PATH   = os.path.join(ROOT, "data", "backfill", "macro.parquet")

K = 4
N_RESTARTS = 25                # EM random starts
COV_TYPE = "diag"              # diagonal cov keeps params bounded
ROLL_WIN_24 = 24               # 4 days at 4h
ROLL_WIN_20 = 20               # ~3.3 days
STANDARDIZE_WIN = 252          # ~6 weeks for rolling z-score baselines
TRAIN_END = "2024-12-31"
VAL_END   = "2025-06-30"
DIRICHLET_ALPHA = 1.0
BLOCK_SIZE = 20
N_BOOTSTRAP = 500              # smaller B for speed since each call does HMM-free op


# ── Data loading ────────────────────────────────────────────────────
def load_klines_4h() -> pd.DataFrame:
    """1h klines → 4h OHLCV + taker imbalance + log return."""
    t = pq.read_table(KLINES_PATH)
    df = t.to_pandas()[["ts_utc_ms", "open", "high", "low", "close", "volume",
                        "taker_buy_base"]].sort_values("ts_utc_ms")
    df["ts"] = pd.to_datetime(df["ts_utc_ms"], unit="ms", utc=True)
    df = df.set_index("ts")
    g = df.resample("4h")
    out = pd.DataFrame({
        "open":   g["open"].first(),
        "high":   g["high"].max(),
        "low":    g["low"].min(),
        "close":  g["close"].last(),
        "volume": g["volume"].sum(),
        "taker_buy_base": g["taker_buy_base"].sum(),
    }).dropna()
    out["log_return"] = np.log(out["close"]).diff()
    out["taker_buy_imbalance"] = (out["taker_buy_base"] - (out["volume"] - out["taker_buy_base"])) / out["volume"]
    return out


def load_funding_4h() -> pd.Series:
    """Funding rate forward-filled to 4h grid."""
    t = pq.read_table(FUNDING_PATH)
    df = t.to_pandas()[["ts_utc_ms", "funding_rate"]].sort_values("ts_utc_ms")
    df["ts"] = pd.to_datetime(df["ts_utc_ms"], unit="ms", utc=True)
    df = df.set_index("ts")
    # Funding is 8h. Resample to 4h, forward-fill.
    return df["funding_rate"].resample("4h").ffill()


def load_macro_4h() -> pd.DataFrame:
    """BTC daily close + VIX daily close, forward-filled to 4h grid."""
    t = pq.read_table(MACRO_PATH)
    df = t.to_pandas()[["ts_utc_ms", "label", "close"]].sort_values("ts_utc_ms")
    df["ts"] = pd.to_datetime(df["ts_utc_ms"], unit="ms", utc=True)

    btc = df[df["label"] == "BTC"].set_index("ts")["close"].rename("btc_close")
    vix = df[df["label"] == "VIX"].set_index("ts")["close"].rename("vix_close")

    # Resample to 4h, ffill across weekends (VIX has gaps).
    btc_4h = btc.resample("4h").ffill()
    vix_4h = vix.resample("4h").ffill()
    return pd.concat([btc_4h, vix_4h], axis=1)


# ── Feature engineering ─────────────────────────────────────────────
def parkinson_vol(high: pd.Series, low: pd.Series, window: int) -> pd.Series:
    """Parkinson's range-based vol estimator: σ̂ = sqrt(Σ(ln(H/L))² / (4·n·ln 2))"""
    ratio = np.log(high / low) ** 2
    return np.sqrt(ratio.rolling(window).sum() / (4 * window * np.log(2)))


def causal_zscore(s: pd.Series, window: int) -> pd.Series:
    """Rolling z-score using only past data. shift(1) prevents peeking at t in
    the rolling statistics used to standardize t."""
    mu = s.shift(1).rolling(window).mean()
    sd = s.shift(1).rolling(window).std()
    return (s - mu) / sd


def build_features(klines: pd.DataFrame, funding: pd.Series,
                   macro: pd.DataFrame) -> pd.DataFrame:
    """Assemble the 8-feature matrix at 4h cadence, fully causal."""
    f = pd.DataFrame(index=klines.index)
    f["log_return_4h"]      = klines["log_return"]
    f["realized_vol_24"]    = klines["log_return"].rolling(ROLL_WIN_24).std()
    f["parkinson_vol_24"]   = parkinson_vol(klines["high"], klines["low"], ROLL_WIN_24)
    f["taker_buy_imbalance"] = klines["taker_buy_imbalance"]
    f["return_skew_20"]     = klines["log_return"].rolling(ROLL_WIN_20).skew()

    # Funding z-score
    f["funding_zscore"] = causal_zscore(funding.reindex(f.index, method="ffill"), STANDARDIZE_WIN)

    # ETH vs BTC relative return: each 20-bar window
    btc = macro["btc_close"].reindex(f.index, method="ffill")
    btc_logret = np.log(btc).diff()
    eth_20 = klines["log_return"].rolling(ROLL_WIN_20).sum()
    btc_20 = btc_logret.rolling(ROLL_WIN_20).sum()
    f["eth_btc_relret_20"] = eth_20 - btc_20

    # VIX z-score
    vix = macro["vix_close"].reindex(f.index, method="ffill")
    f["vix_level_z"] = causal_zscore(vix, STANDARDIZE_WIN)

    return f.dropna()


def standardize_features(train: pd.DataFrame, *others) -> tuple:
    """Z-score features using TRAIN-set mean and std only. Apply same transform
    to other splits (val, test). No peeking."""
    mu = train.mean()
    sd = train.std().replace(0, 1.0)
    scaled = [(df - mu) / sd for df in (train, *others)]
    return scaled


# ── HMM fit with restarts ───────────────────────────────────────────
def fit_hmm(X: np.ndarray, k: int, n_restarts: int, cov_type: str = COV_TYPE,
            rng_seed: int = 42) -> tuple:
    """Fit Gaussian HMM with multiple random starts. Returns (best_model,
    best_logL, all_logL). Picks the restart with the highest converged log-L."""
    best_model = None
    best_logL = -np.inf
    all_logL = []
    rng = np.random.default_rng(rng_seed)
    for i in range(n_restarts):
        m = hmm.GaussianHMM(
            n_components=k,
            covariance_type=cov_type,
            n_iter=200,
            tol=1e-4,
            random_state=int(rng.integers(0, 2**31 - 1)),
            init_params="stmc",
        )
        try:
            m.fit(X)
            ll = m.score(X)
            all_logL.append(ll)
            if ll > best_logL:
                best_logL = ll
                best_model = m
        except Exception as e:
            all_logL.append(None)
            continue
    return best_model, best_logL, all_logL


def bic(model, X: np.ndarray, k: int, n_features: int) -> float:
    """Bayesian Information Criterion. Penalizes parameter count.
       n_params = (k − 1) initial probs + k(k − 1) transition probs +
                  k * n_features means + k * n_features diag-cov params"""
    n_obs = len(X)
    n_params = (k - 1) + k * (k - 1) + k * n_features + k * n_features
    return -2 * model.score(X) + n_params * np.log(n_obs)


# ── State labeling post-hoc ─────────────────────────────────────────
def label_hmm_states(model, X: np.ndarray, features: pd.DataFrame) -> dict:
    """Assign labels to HMM states 0..k-1 by combining mean log_return and
    mean realized_vol_24 (both in z-space).

    K=3: rank purely by log_return → DOWN / CHOP / UP.
    K=4: rank by log_return AND use vol to separate CRASH from DOWN.
         - 2 lowest log_return states → negative-return cluster.
           Among those, the higher-vol one is CRASH, the other is DOWN.
         - 2 highest log_return states → positive/neutral cluster.
           Lower is CHOP, higher is UP.
    """
    states = model.predict(X)
    state_stats = pd.DataFrame({
        "log_return_4h":   features.iloc[:, 0].values,
        "realized_vol_24": features.iloc[:, 1].values,
        "state":           states,
    }).groupby("state").mean()
    by_ret = state_stats.sort_values("log_return_4h")
    out = {}
    if K == 3:
        out[int(by_ret.index[0])] = "DOWN"
        out[int(by_ret.index[1])] = "CHOP"
        out[int(by_ret.index[2])] = "UP"
    elif K == 4:
        # Empirical observation on ETH 4h: the HMM separates states by vol
        # FIRST and direction SECOND. With K=4 we get two distinct vol regimes
        # (high vs low) each with two direction flavors.
        # Sort by realized_vol_24 desc → top 2 are stressed, bottom 2 calm.
        # Within each pair, higher log_return → bull flavor, lower → bear.
        by_vol = state_stats.sort_values("realized_vol_24", ascending=False)
        high_vol = by_vol.iloc[:2].sort_values("log_return_4h", ascending=False)
        low_vol = by_vol.iloc[2:].sort_values("log_return_4h", ascending=False)
        # High-vol + positive return drift = STRESS (melt-up / FOMO / liquidation cascade)
        # High-vol + negative return drift = CRASH (vol expansion to the downside)
        out[int(high_vol.index[0])] = "STRESS"
        out[int(high_vol.index[1])] = "CRASH"
        # Low-vol + positive drift = UP (calm bull grind)
        # Low-vol + negative-or-flat drift = CHOP (sideways consolidation)
        out[int(low_vol.index[0])] = "UP"
        out[int(low_vol.index[1])] = "CHOP"
    return out


# ── Transition matrix + bootstrap + homogeneity (reused from K=3 baseline) ─
def count_transitions(seq: np.ndarray, k: int) -> np.ndarray:
    C = np.zeros((k, k), dtype=np.int64)
    for a, b in zip(seq[:-1], seq[1:]):
        C[a, b] += 1
    return C


def smooth_transition(C: np.ndarray, alpha: float, k: int) -> np.ndarray:
    return (C + alpha) / (C.sum(axis=1, keepdims=True) + k * alpha)


def block_bootstrap_ci(seq: np.ndarray, k: int, block_size: int = BLOCK_SIZE,
                       B: int = N_BOOTSTRAP, alpha: float = DIRICHLET_ALPHA,
                       rng_seed: int = 42):
    rng = np.random.default_rng(rng_seed)
    n = len(seq)
    n_blocks = n // block_size
    results = np.empty((B, k, k), dtype=np.float64)
    for b in range(B):
        idx_start = rng.integers(0, n - block_size, size=n_blocks)
        sample = np.concatenate([seq[i:i + block_size] for i in idx_start])
        results[b] = smooth_transition(count_transitions(sample, k), alpha, k)
    return (np.percentile(results, 2.5, axis=0),
            np.percentile(results, 50, axis=0),
            np.percentile(results, 97.5, axis=0))


def chi2_homogeneity_G(seq: np.ndarray, k: int, n_periods: int = 3) -> tuple:
    n = len(seq)
    cuts = np.linspace(0, n, n_periods + 1, dtype=int)
    sub_C = [count_transitions(seq[cuts[i]:cuts[i + 1]], k) for i in range(n_periods)]
    pooled_C = sum(sub_C)
    pooled_row_sums = pooled_C.sum(axis=1, keepdims=True)
    pooled_P = np.where(pooled_row_sums > 0, pooled_C / pooled_row_sums, 0.0)
    G2 = 0.0
    for C_k in sub_C:
        for i in range(k):
            for j in range(k):
                if C_k[i, j] > 0 and pooled_P[i, j] > 0:
                    expected = C_k[i, :].sum() * pooled_P[i, j]
                    if expected > 0:
                        G2 += 2 * C_k[i, j] * np.log(C_k[i, j] / expected)
    dof = (n_periods - 1) * k * (k - 1)
    p = 1.0 - chi2_dist.cdf(G2, dof)
    return G2, dof, p


def stationary_distribution(P: np.ndarray, max_iter: int = 10000, tol: float = 1e-12):
    pi = np.ones(P.shape[0]) / P.shape[0]
    for _ in range(max_iter):
        pi_new = pi @ P
        if np.max(np.abs(pi_new - pi)) < tol:
            return pi_new
        pi = pi_new
    return pi


def accuracy_markov_vs_persistence(seq: np.ndarray, P: np.ndarray) -> dict:
    if len(seq) < 2:
        return {"persistence_acc": None, "markov_acc": None, "n": 0}
    prev = seq[:-1]
    actual = seq[1:]
    return {
        "persistence_acc": float((prev == actual).mean()),
        "markov_acc": float((P[prev].argmax(axis=1) == actual).mean()),
        "n": int(len(actual)),
        "markov_same_as_persistence_pct": float(
            (P[prev].argmax(axis=1) == prev).mean()
        ),
    }


# ── Pretty printers ─────────────────────────────────────────────────
LABELS_ORDER = ["CRASH", "STRESS", "CHOP", "UP"] if K == 4 else ["DOWN", "CHOP", "UP"]


def fmt_matrix(M: np.ndarray, labels_by_idx: dict, fmt: str = "{:>10.4f}") -> str:
    """Format with rows/cols reordered to LABELS_ORDER."""
    idx_by_label = {v: k for k, v in labels_by_idx.items()}
    order = [idx_by_label[s] for s in LABELS_ORDER]
    M2 = M[np.ix_(order, order)]
    rows = ["          " + "".join(f"{s:>12s}" for s in LABELS_ORDER)]
    for i, s in enumerate(LABELS_ORDER):
        rows.append(f"{s:<10s}" + "".join(fmt.format(M2[i, j]) for j in range(K)))
    return "\n".join(rows)


# ── Main ────────────────────────────────────────────────────────────
def main() -> None:
    print("=" * 76)
    label_str = "/".join(LABELS_ORDER)
    print(f"HMM Regime Backtest — ETH 4h, 8 features, K={K} ({label_str})")
    print("=" * 76)
    t0 = time.time()

    # ── Load ──────────────────────────────────────────────────────
    print("\n[Load] Reading klines 1h, funding 8h, macro daily ...")
    klines = load_klines_4h()
    funding = load_funding_4h()
    macro = load_macro_4h()
    print(f"  4h klines : {len(klines):,} bars")
    print(f"  funding   : {len(funding):,} pts (ffilled to 4h)")
    print(f"  macro     : {len(macro):,} pts (ffilled to 4h, BTC+VIX)")

    # ── Features ──────────────────────────────────────────────────
    print("\n[Features] Computing 8-feature matrix (causal rolling) ...")
    F = build_features(klines, funding, macro)
    print(f"  feature matrix: {F.shape[0]:,} rows x {F.shape[1]} cols (after dropna)")
    print(f"  feature names: {list(F.columns)}")

    # ── Split ─────────────────────────────────────────────────────
    train = F[F.index <= TRAIN_END]
    val   = F[(F.index > TRAIN_END) & (F.index <= VAL_END)]
    test  = F[F.index > VAL_END]
    print(f"\n[Split] train {len(train):,} | val {len(val):,} | test {len(test):,} (test NOT used)")

    # ── Standardize on train only ─────────────────────────────────
    train_z, val_z, test_z = standardize_features(train, val, test)
    X_train = train_z.values
    X_val = val_z.values

    # ── BIC sweep K=2,3,4 ─────────────────────────────────────────
    print("\n[BIC sweep] Fitting HMM with K=2,3,4 (n_restarts=10 for sweep) ...")
    bic_table = []
    for k_try in (2, 3, 4):
        m_try, ll_try, _ = fit_hmm(X_train, k_try, n_restarts=10)
        bic_val = bic(m_try, X_train, k_try, X_train.shape[1])
        bic_table.append((k_try, ll_try, bic_val))
        print(f"  K={k_try}: logL={ll_try:>10.1f}  BIC={bic_val:>10.1f}")
    best_k = min(bic_table, key=lambda r: r[2])[0]
    print(f"  → BIC favors K={best_k}")
    if best_k != K:
        print(f"  ⚠ This script forces K={K}; report continues with K={K} per the doc.")

    # ── Full fit with K=3 and more restarts ───────────────────────
    print(f"\n[Fit] Final HMM K={K}, cov={COV_TYPE}, n_restarts={N_RESTARTS} ...")
    model, best_logL, all_logL = fit_hmm(X_train, K, n_restarts=N_RESTARTS)
    converged_lls = [ll for ll in all_logL if ll is not None]
    print(f"  converged starts: {len(converged_lls)}/{N_RESTARTS}")
    print(f"  best logL: {best_logL:.2f}")
    print(f"  worst logL: {min(converged_lls):.2f}  (Δ = {best_logL - min(converged_lls):.2f})")
    print(f"  n params (diag cov): K(K-1)+K(2d)+K-1 = {K * (K - 1) + K * 2 * X_train.shape[1] + K - 1}")
    print(f"  n train obs: {len(X_train):,}  ({len(X_train) / (K * (K - 1) + K * 2 * X_train.shape[1] + K - 1):.0f} obs/param)")

    # ── Label states by mean log_return ───────────────────────────
    labels_by_idx = label_hmm_states(model, X_train, train_z)
    print(f"\n[Labeling] HMM raw states mapped post-hoc by mean log_return:")
    for idx in sorted(labels_by_idx):
        mean_ret = train_z.iloc[:, 0].values[model.predict(X_train) == idx].mean()
        print(f"  state {idx} → {labels_by_idx[idx]:<5}  (mean z(log_return) = {mean_ret:+.3f})")

    # ── State means on z-scored features per state ────────────────
    print(f"\n[Inspect] Feature means by HMM state (z-scored space):")
    train_states = model.predict(X_train)
    means_per_state = pd.DataFrame(X_train, columns=train_z.columns)
    means_per_state["state"] = [labels_by_idx[s] for s in train_states]
    summary = means_per_state.groupby("state").mean().reindex(LABELS_ORDER)
    print(summary.to_string(float_format=lambda x: f"{x:+.2f}"))

    # ── Train transition matrix ───────────────────────────────────
    C_train = count_transitions(train_states, K)
    P_train = smooth_transition(C_train, DIRICHLET_ALPHA, K)
    print(f"\n[F2] Train transition counts (raw):")
    print(fmt_matrix(C_train.astype(float), labels_by_idx, fmt="{:>12.0f}"))
    print(f"  min cell: {C_train.min()}   total transitions: {C_train.sum()}")
    print(f"\n[F2] Smoothed transition matrix P̂:")
    print(fmt_matrix(P_train, labels_by_idx))

    # ── Block bootstrap CI ────────────────────────────────────────
    lo_ci, mid_ci, hi_ci = block_bootstrap_ci(train_states, K)
    print(f"\n[F2] CI width (97.5% − 2.5%), B={N_BOOTSTRAP}, block={BLOCK_SIZE}:")
    print(fmt_matrix(hi_ci - lo_ci, labels_by_idx))
    if ((hi_ci - lo_ci) > 0.10).any():
        print(f"  ⚠ Some cells have CI width > 0.10")
    else:
        print(f"  ✓ All cells CI width ≤ 0.10")

    # ── Homogeneity test ──────────────────────────────────────────
    G2, dof, p_homog = chi2_homogeneity_G(train_states, K, n_periods=3)
    print(f"\n[F3] Homogeneity G² across 3 sub-periods: G²={G2:.1f}  dof={dof}  p≈{p_homog:.4f}")
    if p_homog < 0.05:
        print(f"  ⚠ Reject homogeneity. Transition matrix shifts in time.")
    else:
        print(f"  ✓ Cannot reject homogeneity at α=0.05.")

    # ── Stationary distribution ───────────────────────────────────
    pi = stationary_distribution(P_train)
    dwell = 1.0 / (1.0 - np.diag(P_train))
    print(f"\n[F5] Stationary π and avg dwell time:")
    idx_by_label = {v: k for k, v in labels_by_idx.items()}
    for s in LABELS_ORDER:
        i = idx_by_label[s]
        print(f"  {s:<6}  π = {pi[i] * 100:>6.2f}%   dwell ≈ {dwell[i]:>5.2f} bars ({dwell[i] * 4:.1f}h)")

    # ── Train accuracy ────────────────────────────────────────────
    acc_train = accuracy_markov_vs_persistence(train_states, P_train)
    print(f"\n[F4] In-sample accuracy (train, n={acc_train['n']:,}):")
    print(f"  Persistence: {acc_train['persistence_acc'] * 100:.2f}%")
    print(f"  Markov     : {acc_train['markov_acc'] * 100:.2f}%")
    print(f"  Δ          : {(acc_train['markov_acc'] - acc_train['persistence_acc']) * 100:+.2f}pp")

    # ── Validation ────────────────────────────────────────────────
    val_states = model.predict(X_val)
    acc_val = accuracy_markov_vs_persistence(val_states, P_train)
    print(f"\n[F6] Out-of-sample accuracy (VAL, single pass, n={acc_val['n']:,}):")
    print(f"  Persistence: {acc_val['persistence_acc'] * 100:.2f}%")
    print(f"  Markov     : {acc_val['markov_acc'] * 100:.2f}%")
    delta_val = (acc_val['markov_acc'] - acc_val['persistence_acc']) * 100
    print(f"  Δ          : {delta_val:+.2f}pp")

    # ── Val state distribution + Viterbi state usage ──────────────
    val_state_labels = [labels_by_idx[s] for s in val_states]
    val_dist = pd.Series(val_state_labels).value_counts(normalize=True).reindex(LABELS_ORDER)
    train_state_labels = [labels_by_idx[s] for s in train_states]
    train_dist = pd.Series(train_state_labels).value_counts(normalize=True).reindex(LABELS_ORDER)
    print(f"\n[F6] State distribution: train vs val")
    for s in LABELS_ORDER:
        print(f"  {s:<6}  train {train_dist[s] * 100:>6.2f}%   val {val_dist[s] * 100:>6.2f}%")

    # ── Verdict ───────────────────────────────────────────────────
    print("\n" + "=" * 76)
    print("VERDICT")
    print("=" * 76)
    print(f"  Pipeline runtime: {time.time() - t0:.1f}s")
    print(f"  Baseline reminder (raw-return tercile K=3): val Δ = +0.09pp")
    print(f"  Baseline reminder (HMM K=3, 8 features)   : val Δ = +0.00pp")
    print(f"  HMM 8-feature K={K}                         : val Δ = {delta_val:+.2f}pp")
    print()
    if delta_val < 1.0:
        print(f"  ❌ HMM with 8 features does NOT meaningfully beat persistence either.")
        print(f"     Δ < 1pp out-of-sample. Conclusion: at 4h, ETH does not have a")
        print(f"     stable enough Markovian regime structure to forecast next-bar.")
        print(f"     Even with rich features and Viterbi decoding, the persistence")
        print(f"     baseline is the ceiling here.")
        if p_homog < 0.05:
            print(f"     The non-stationarity (G² p≈{p_homog:.3f}) is a structural reason.")
        print()
        print(f"     Honest options now:")
        print(f"       (a) Lower timeframe (1h, 15m) — finer states, more noise")
        print(f"       (b) Higher timeframe (1d) — fewer samples, cleaner regimes")
        print(f"       (c) Rolling-window re-fit instead of fixed train (mitigates")
        print(f"           non-stationarity at the cost of more compute)")
        print(f"       (d) Abandon regime-filter idea and try a different signal")
    elif delta_val < 3.0:
        print(f"  ⚠ Marginal edge ({delta_val:+.2f}pp on val). Borderline.")
        print(f"     If rolling re-fit improves this and homogeneity issue is addressed,")
        print(f"     could be production-grade. Test on test set ONCE to confirm.")
    else:
        print(f"  ✓ HMM beats persistence by {delta_val:+.2f}pp on val.")
        print(f"     Worth productionizing: rolling re-fit, integrate into dashboard")
        print(f"     as advisory panel, then evaluate against the holdout test set.")


if __name__ == "__main__":
    main()
