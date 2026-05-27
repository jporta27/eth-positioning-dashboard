"""
Robustness test suite for the K=4 HMM regime classifier.

Goal: BEFORE productionizing the classifier, prove that:
  1. The fitted model is identified (not chasing local minima)
  2. The 4 states discovered are stable across time windows (same identities)
  3. K=4 is the right model complexity (not over- or underfitting)
  4. OOS state assignment makes sense
  5. We know how much the transition matrix drifts
  6. The model adds value at some horizon (not just the trivial 1-bar case)

Each test outputs PASS / FAIL / INSPECT. Final block: overall verdict.

Run:
    python scripts/regime_robustness_tests.py

Approx runtime: 10–15 min (most spent on rolling re-fit in Test 2).
"""

from __future__ import annotations

import os
import sys
import time
import warnings
from collections import defaultdict

import numpy as np
import pandas as pd
from hmmlearn import hmm
from scipy.stats import chi2 as chi2_dist

sys.stdout.reconfigure(encoding="utf-8")
warnings.filterwarnings("ignore", category=RuntimeWarning)
warnings.filterwarnings("ignore", category=UserWarning, module="hmmlearn")
warnings.filterwarnings("ignore", category=DeprecationWarning)

# Import the shared loader & feature pipeline from the backtest script
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from markov_hmm_backtest import (  # noqa: E402
    load_klines_4h, load_funding_4h, load_macro_4h,
    build_features, standardize_features,
    fit_hmm, label_hmm_states,
    bic, count_transitions, smooth_transition,
    TRAIN_END, VAL_END,
    COV_TYPE, ROLL_WIN_24, ROLL_WIN_20, STANDARDIZE_WIN, DIRICHLET_ALPHA,
)

# ── Config ───────────────────────────────────────────────────────────
K = 4
LABELS = ["CRASH", "STRESS", "CHOP", "UP"]
FEATURE_NAMES = [
    "log_return_4h", "realized_vol_24", "parkinson_vol_24",
    "taker_buy_imbalance", "return_skew_20", "funding_zscore",
    "eth_btc_relret_20", "vix_level_z",
]

# Test 1
N_SEED_FITS = 30
SEED_LOGL_TOLERANCE = 0.05   # within 5% of best logL
SEED_MEAN_TOLERANCE = 0.30   # max state-mean std across top fits, in z-units

# Test 2
ROLLING_WINDOW_MONTHS = 18
ROLLING_STEP_MONTHS = 3
RESTARTS_PER_WINDOW = 8       # fewer than the main fit to keep runtime sane
IDENTITY_MEAN_STD_TOLERANCE = 0.50  # max std across windows per (label, feature)

# Test 3
K_CANDIDATES = [2, 3, 4, 5]
K5_NEW_STATE_MEAN_ABS_MAX = 0.30  # if K=5's "new" state has all |means| < this → noise

# Test 4
OOS_TOP_RUNS_PER_STATE = 5

# Test 5
DIAGONAL_DRIFT_TOLERANCE = 0.03
OFFDIAGONAL_DRIFT_TOLERANCE = 0.08

# Test 6
HORIZONS = [1, 4, 8, 24, 48, 96]


# ── Helpers ─────────────────────────────────────────────────────────
def _signature_means(model, X, train_z, k):
    """For a fitted model, return dict {label: 8-vector of feature means in z-space}."""
    states = model.predict(X)
    df = pd.DataFrame(X, columns=train_z.columns)
    df["state"] = states
    raw_means = df.groupby("state").mean()
    if k == K:
        labels_by_idx = label_hmm_states(model, X, train_z)
        out = {}
        for raw_idx, label in labels_by_idx.items():
            out[label] = raw_means.loc[raw_idx].values
        return out
    # For other K, just return ordered raw means without canonical labels
    sorted_states = raw_means.sort_values("realized_vol_24", ascending=False)
    return {f"S{i}": sorted_states.iloc[i].values for i in range(k)}


def _print_test_header(test_n, name):
    print(f"\n{'═' * 76}")
    print(f"TEST {test_n} — {name}")
    print('═' * 76)


def _verdict(passed):
    return "✓ PASS" if passed else "✗ FAIL"


# ── Test 1: Random seed stability ───────────────────────────────────
def test_1_seed_stability(X_train, train_z):
    _print_test_header(1, "Random seed stability")
    print(f"Fitting K={K} HMM {N_SEED_FITS} times with different seeds ...")

    logLs = []
    sigs_by_label = defaultdict(list)
    t0 = time.time()
    for seed in range(N_SEED_FITS):
        m, ll, _ = fit_hmm(X_train, K, n_restarts=1, rng_seed=seed * 7 + 1)
        if ll == -np.inf:
            continue
        logLs.append(ll)
        sig = _signature_means(m, X_train, train_z, K)
        for label, means in sig.items():
            sigs_by_label[label].append(means)
    dt = time.time() - t0

    if not logLs:
        print("  ⚠ No fits converged.")
        return False

    best_ll = max(logLs)
    within_tol = sum(1 for ll in logLs if abs(ll - best_ll) / abs(best_ll) < SEED_LOGL_TOLERANCE)
    print(f"  Completed {len(logLs)}/{N_SEED_FITS} fits in {dt:.1f}s")
    print(f"  Best logL: {best_ll:.1f}   Worst: {min(logLs):.1f}   Δ: {best_ll - min(logLs):.1f}")
    print(f"  Within ±{SEED_LOGL_TOLERANCE * 100:.0f}% of best: {within_tol}/{len(logLs)}")

    # Check state-mean stability across top fits
    max_std = 0.0
    label_max_stds = {}
    for label, means_list in sigs_by_label.items():
        if len(means_list) < 2:
            continue
        arr = np.array(means_list)
        stds = arr.std(axis=0)
        label_max_stds[label] = stds.max()
        max_std = max(max_std, stds.max())

    print(f"  Max state-mean std across fits per label:")
    for label in LABELS:
        if label in label_max_stds:
            n_occ = len(sigs_by_label[label])
            print(f"    {label:<7} appeared in {n_occ:>2}/{len(logLs)} fits   max_std = {label_max_stds[label]:.3f}σ")
        else:
            print(f"    {label:<7} NEVER labeled in any fit ✗")

    seed_pass = (within_tol / len(logLs) >= 0.80) and (max_std < SEED_MEAN_TOLERANCE)
    extra = ""
    if within_tol / len(logLs) < 0.80:
        extra += f"  only {within_tol/len(logLs)*100:.0f}% within tolerance"
    if max_std >= SEED_MEAN_TOLERANCE:
        extra += f"  max_std {max_std:.3f}σ ≥ {SEED_MEAN_TOLERANCE}"
    print(f"\n  {_verdict(seed_pass)}{extra}")
    return seed_pass


# ── Test 2: State identity stability across rolling windows ─────────
def test_2_state_identity_rolling(F):
    _print_test_header(2, "State identity across rolling windows")
    print(f"Rolling 18-month windows, step 3 months, K={K}, restarts={RESTARTS_PER_WINDOW} per window")

    # Build windows from F.index
    start = F.index.min().to_pydatetime()
    end = F.index.max().to_pydatetime()
    window_starts = []
    cursor = start
    while True:
        w_end = cursor + pd.DateOffset(months=ROLLING_WINDOW_MONTHS)
        if w_end > end:
            break
        window_starts.append(cursor)
        cursor = cursor + pd.DateOffset(months=ROLLING_STEP_MONTHS)
    print(f"  → {len(window_starts)} windows from {start.date()} to {end.date()}")

    sigs_per_window = []  # list of {label: 8-vector}
    transitions_per_window = []  # list of P̂ ordered by canonical labels
    t0 = time.time()
    for i, w_start in enumerate(window_starts):
        w_end = w_start + pd.DateOffset(months=ROLLING_WINDOW_MONTHS)
        sub = F[(F.index >= w_start) & (F.index < w_end)]
        if len(sub) < 1000:  # safety
            print(f"  ⚠ Window {i}: only {len(sub)} bars, skipping")
            continue
        # Standardize within window
        mu = sub.mean()
        sd = sub.std().replace(0, 1.0)
        Xw = ((sub - mu) / sd).values
        m, ll, _ = fit_hmm(Xw, K, n_restarts=RESTARTS_PER_WINDOW, rng_seed=42 + i)
        sig = _signature_means(m, Xw, sub, K)
        sigs_per_window.append(sig)
        # Canonical-labeled transition matrix
        labels_by_idx = label_hmm_states(m, Xw, sub)
        states = m.predict(Xw)
        C = count_transitions(states, K)
        P = smooth_transition(C, DIRICHLET_ALPHA, K)
        # Reorder rows/cols to canonical [CRASH, STRESS, CHOP, UP]
        idx_by_label = {v: k for k, v in labels_by_idx.items()}
        order = [idx_by_label[s] for s in LABELS]
        P_canon = P[np.ix_(order, order)]
        transitions_per_window.append(P_canon)
        ll_str = f"{ll:>9.1f}" if ll > -np.inf else "FAIL"
        print(f"  Win {i + 1}/{len(window_starts)}: {w_start.date()} → {w_end.date()}  logL={ll_str}  ({len(sub)} bars)")
    dt = time.time() - t0
    print(f"  All windows fit in {dt:.1f}s")

    # For each label, gather means across windows
    print(f"\n  State signature std across windows (max per feature):")
    label_present = {l: 0 for l in LABELS}
    label_max_feature_std = {}
    overall_max_std = 0.0
    for label in LABELS:
        means_list = [sig.get(label) for sig in sigs_per_window if label in sig]
        label_present[label] = len(means_list)
        if not means_list:
            label_max_feature_std[label] = None
            continue
        arr = np.array(means_list)
        per_feature_std = arr.std(axis=0)
        label_max_feature_std[label] = per_feature_std
        max_std = per_feature_std.max()
        overall_max_std = max(overall_max_std, max_std)
        worst_feature = FEATURE_NAMES[per_feature_std.argmax()]
        print(f"    {label:<7}  present {label_present[label]:>2}/{len(sigs_per_window)} windows   max_std = {max_std:.3f}σ on '{worst_feature}'")

    n_windows = len(sigs_per_window)
    presence_ok = all(label_present[l] >= max(1, n_windows - 1) for l in LABELS)
    std_ok = overall_max_std < IDENTITY_MEAN_STD_TOLERANCE
    test_2_pass = presence_ok and std_ok

    if not presence_ok:
        absent = [l for l, c in label_present.items() if c < max(1, n_windows - 1)]
        print(f"  ⚠ Labels absent or near-absent: {absent}")
    if not std_ok:
        print(f"  ⚠ Overall max std {overall_max_std:.3f}σ ≥ tolerance {IDENTITY_MEAN_STD_TOLERANCE}σ")

    print(f"\n  {_verdict(test_2_pass)}")
    return test_2_pass, transitions_per_window, sigs_per_window


# ── Test 3: K sensitivity ───────────────────────────────────────────
def test_3_k_sensitivity(X_train, train_z):
    _print_test_header(3, "K sensitivity (BIC + interpretability)")
    print(f"Fitting K ∈ {K_CANDIDATES} with 10 restarts each ...")
    n_features = X_train.shape[1]
    n_obs = len(X_train)
    bic_by_k = {}
    aic_by_k = {}
    model_by_k = {}
    t0 = time.time()
    for k in K_CANDIDATES:
        m, ll, _ = fit_hmm(X_train, k, n_restarts=10, rng_seed=42)
        b = bic(m, X_train, k, n_features)
        n_params = (k - 1) + k * (k - 1) + k * n_features + k * n_features
        a = -2 * ll + 2 * n_params
        bic_by_k[k] = b
        aic_by_k[k] = a
        model_by_k[k] = m
        print(f"  K={k}: logL={ll:>10.1f}  BIC={b:>12.1f}  AIC={a:>12.1f}  n_params={n_params}")
    dt = time.time() - t0
    best_bic_k = min(bic_by_k, key=bic_by_k.get)
    print(f"  Fit in {dt:.1f}s. BIC minimum at K={best_bic_k}")

    # K=5 interpretability check: of the 5 states, is there one with all means ≈ 0?
    k5 = model_by_k.get(5)
    k5_has_noise_state = False
    if k5 is not None:
        states_k5 = k5.predict(X_train)
        df_k5 = pd.DataFrame(X_train, columns=train_z.columns)
        df_k5["state"] = states_k5
        means_k5 = df_k5.groupby("state").mean()
        max_abs_per_state = means_k5.abs().max(axis=1)
        print(f"\n  K=5 state means (max |mean| across features per state):")
        for sidx, m_abs in max_abs_per_state.sort_values().items():
            tag = "  ← potential noise state" if m_abs < K5_NEW_STATE_MEAN_ABS_MAX else ""
            print(f"    state {sidx}: max|mean| = {m_abs:.3f}{tag}")
        k5_has_noise_state = (max_abs_per_state.min() < K5_NEW_STATE_MEAN_ABS_MAX)

    test_3_pass = (best_bic_k == K) and (k5_has_noise_state or (5 not in K_CANDIDATES))
    if best_bic_k != K:
        print(f"  ⚠ BIC favors K={best_bic_k}, not K={K}")
    if not k5_has_noise_state:
        print(f"  ⚠ K=5 doesn't show a noise state — could be that K=5 is genuinely better")

    print(f"\n  {_verdict(test_3_pass)}")
    return test_3_pass


# ── Test 4: OOS sanity ──────────────────────────────────────────────
def test_4_oos_sanity(F, X_train, train_z, val_z):
    _print_test_header(4, "OOS sanity — val period state assignments")
    # Use a single best-of-25 fit for the main model
    print(f"  Refitting K={K} on train (25 restarts) ...")
    m, ll, _ = fit_hmm(X_train, K, n_restarts=25, rng_seed=42)
    labels_by_idx = label_hmm_states(m, X_train, train_z)
    X_val = val_z.values
    val_states = m.predict(X_val)
    val_state_labels = np.array([labels_by_idx[s] for s in val_states])

    # Find contiguous runs per state
    print(f"\n  Top {OOS_TOP_RUNS_PER_STATE} longest contiguous runs per state in val:")
    for label in LABELS:
        is_label = (val_state_labels == label)
        if not is_label.any():
            print(f"\n  {label}: 0 bars in val")
            continue
        # Detect runs
        runs = []
        i = 0
        while i < len(is_label):
            if is_label[i]:
                j = i
                while j < len(is_label) and is_label[j]:
                    j += 1
                runs.append((i, j - 1, j - i))
                i = j
            else:
                i += 1
        runs.sort(key=lambda r: r[2], reverse=True)
        print(f"\n  {label}:")
        for r_start, r_end, r_len in runs[:OOS_TOP_RUNS_PER_STATE]:
            ts_start = val_z.index[r_start]
            ts_end = val_z.index[r_end]
            ret = val_z.iloc[r_start:r_end + 1, 0].mean()
            vol = val_z.iloc[r_start:r_end + 1, 1].mean()
            vix = val_z.iloc[r_start:r_end + 1, 7].mean()
            print(f"    {ts_start.strftime('%Y-%m-%d %H:%M')} → {ts_end.strftime('%Y-%m-%d %H:%M')}  "
                  f"({r_len * 4:>3}h)  log_ret_z={ret:+.2f}  rv_z={vol:+.2f}  vix_z={vix:+.2f}")

    print(f"\n  INSPECT — verify the CRASH/STRESS runs above coincide with known events")
    return None  # semi-qualitative


# ── Test 5: Transition drift magnitude ──────────────────────────────
def test_5_transition_drift(transitions_per_window):
    _print_test_header(5, "Transition matrix drift across rolling windows")
    if len(transitions_per_window) < 2:
        print("  Not enough windows from Test 2; skipping.")
        return None
    arr = np.array(transitions_per_window)  # (n_windows, K, K)
    mean_P = arr.mean(axis=0)
    std_P = arr.std(axis=0)
    min_P = arr.min(axis=0)
    max_P = arr.max(axis=0)

    print(f"  Across {arr.shape[0]} windows. Cell format: mean ± std (min..max)\n")
    header = "          " + "".join(f"{l:>22s}" for l in LABELS)
    print(header)
    for i, ri in enumerate(LABELS):
        row = f"{ri:<10s}"
        for j, rj in enumerate(LABELS):
            row += f"  {mean_P[i, j]:.3f}±{std_P[i, j]:.3f}({min_P[i, j]:.2f}..{max_P[i, j]:.2f})"
        print(row)

    diag_max_std = float(np.diag(std_P).max())
    off_diag = std_P - np.diag(np.diag(std_P))
    off_diag_max_std = float(off_diag.max())
    print(f"\n  Max diagonal std:     {diag_max_std:.4f}   (tolerance {DIAGONAL_DRIFT_TOLERANCE})")
    print(f"  Max off-diagonal std: {off_diag_max_std:.4f}   (tolerance {OFFDIAGONAL_DRIFT_TOLERANCE})")
    test_5_pass = diag_max_std < DIAGONAL_DRIFT_TOLERANCE and off_diag_max_std < OFFDIAGONAL_DRIFT_TOLERANCE
    print(f"\n  {_verdict(test_5_pass)}")
    return test_5_pass


# ── Test 6: Multi-step horizon ──────────────────────────────────────
def test_6_horizon(X_train, train_z, X_val, val_z):
    _print_test_header(6, "Multi-step horizon: Markov vs persistence")
    m, _, _ = fit_hmm(X_train, K, n_restarts=15, rng_seed=42)
    train_states = m.predict(X_train)
    val_states = m.predict(X_val)
    C = count_transitions(train_states, K)
    P = smooth_transition(C, DIRICHLET_ALPHA, K)

    print(f"\n  Horizon  Persistence  Markov   Δ (pp)  Markov-prediction-mode")
    print(f"  -------  -----------  ------   ------  ----------------------")
    diverges_at = None
    for h in HORIZONS:
        if h >= len(val_states):
            continue
        Ph = np.linalg.matrix_power(P, h)
        pred_persist = val_states[:-h]
        pred_markov = Ph[pred_persist].argmax(axis=1)
        actual = val_states[h:]
        acc_p = (pred_persist == actual).mean()
        acc_m = (pred_markov == actual).mean()
        delta = (acc_m - acc_p) * 100
        # Are Markov predictions all "same state"?
        markov_eq_persist = (pred_markov == pred_persist).all()
        mode = "all=persist" if markov_eq_persist else f"differs in {(pred_markov != pred_persist).sum()}/{len(actual)} cases"
        if not markov_eq_persist and diverges_at is None:
            diverges_at = h
        print(f"  {h:>4} bar  {acc_p*100:>9.2f}%  {acc_m*100:>5.2f}%  {delta:>+5.2f}pp   {mode}")

    if diverges_at:
        print(f"\n  → Markov diverges from persistence at horizon ≥ {diverges_at} bars ({diverges_at * 4}h ≈ {diverges_at * 4 / 24:.1f} days)")
    else:
        print(f"\n  → Markov never diverges from persistence within tested horizons.")
        print(f"     Reasonable: diagonal dominance ≈0.96 → P^h diagonal stays > off-diag for many h.")

    return diverges_at


# ── Main ────────────────────────────────────────────────────────────
def main():
    print("=" * 76)
    print("REGIME CLASSIFIER ROBUSTNESS TEST SUITE")
    print("=" * 76)
    t_start = time.time()

    # Load + features
    print("\n[Setup] Loading data and building features ...")
    klines = load_klines_4h()
    funding = load_funding_4h()
    macro = load_macro_4h()
    F = build_features(klines, funding, macro)
    print(f"  feature matrix: {F.shape}")

    train = F[F.index <= TRAIN_END]
    val = F[(F.index > TRAIN_END) & (F.index <= VAL_END)]
    train_z, val_z = standardize_features(train, val)
    X_train, X_val = train_z.values, val_z.values
    print(f"  train: {len(train):,} bars   val: {len(val):,} bars")

    results = {}

    # Tests
    results[1] = test_1_seed_stability(X_train, train_z)
    test_2_pass, transitions_per_window, _ = test_2_state_identity_rolling(F)
    results[2] = test_2_pass
    results[3] = test_3_k_sensitivity(X_train, train_z)
    results[4] = test_4_oos_sanity(F, X_train, train_z, val_z)  # INSPECT (None)
    results[5] = test_5_transition_drift(transitions_per_window)
    results[6] = test_6_horizon(X_train, train_z, X_val, val_z)  # info (horizon int or None)

    # Summary
    print("\n" + "═" * 76)
    print("SUMMARY")
    print("═" * 76)

    def fmt(r):
        if r is True:  return "✓ PASS"
        if r is False: return "✗ FAIL"
        if r is None:  return "— INSPECT (qualitative)"
        return f"INFO: diverges at {r} bars"

    print(f"  Test 1  Seed stability ................. {fmt(results[1])}")
    print(f"  Test 2  State identity stability ....... {fmt(results[2])}")
    print(f"  Test 3  K sensitivity .................. {fmt(results[3])}")
    print(f"  Test 4  OOS sanity (val period) ........ {fmt(results[4])}")
    print(f"  Test 5  Transition drift magnitude ..... {fmt(results[5])}")
    print(f"  Test 6  Multi-step horizon ............. {fmt(results[6])}")
    quant = [r for r in (results[1], results[2], results[3], results[5]) if r is not None]
    n_pass = sum(1 for r in quant if r is True)
    n_total = len(quant)
    print(f"\n  Quantitative tests: {n_pass}/{n_total} PASS")
    print(f"  Total runtime: {time.time() - t_start:.1f}s")

    if n_pass == n_total:
        print(f"\n  ✓ APPROACH IS ROBUST. Safe to productionize the classifier.")
    elif n_pass >= n_total - 1:
        print(f"\n  ⚠ APPROACH IS MOSTLY ROBUST. Address the failed test before relying in production.")
    else:
        print(f"\n  ✗ MULTIPLE ROBUSTNESS ISSUES. Reconsider the modeling choices before continuing.")


if __name__ == "__main__":
    main()
