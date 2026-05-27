"""
Quick check: is K=5 just adding a noise state or genuine extra structure?

Per the robustness test, BIC favored K=5 over K=4 (Δ=5,427 better fit) but
one of the K=5 states had max|mean| = 0.156 — borderline noise.

This script fits K=5 with full restarts and dumps the per-state feature means
+ transition matrix + dwell times so we can decide:
  - K=5 wins: each state is operationally distinct, no near-zero-mean noise state
  - K=4 wins: K=5 splits a state without adding interpretive value, or has a
    "centered" state that's just an EM-induced artifact

Either way, we get a defensible answer to the K choice question.

Run:
    python scripts/check_k5_states.py
"""

from __future__ import annotations

import os
import sys
import warnings

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
warnings.filterwarnings("ignore", category=RuntimeWarning)
warnings.filterwarnings("ignore", category=UserWarning, module="hmmlearn")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from markov_hmm_backtest import (  # noqa: E402
    load_klines_4h, load_funding_4h, load_macro_4h,
    build_features, standardize_features,
    fit_hmm, count_transitions, smooth_transition,
    stationary_distribution,
    TRAIN_END, VAL_END, DIRICHLET_ALPHA,
)

FEATURE_NAMES = [
    "log_return_4h", "realized_vol_24", "parkinson_vol_24",
    "taker_buy_imbalance", "return_skew_20", "funding_zscore",
    "eth_btc_relret_20", "vix_level_z",
]


def main():
    print("=" * 76)
    print("K=5 interpretability check")
    print("=" * 76)

    klines = load_klines_4h()
    funding = load_funding_4h()
    macro = load_macro_4h()
    F = build_features(klines, funding, macro)
    train = F[F.index <= TRAIN_END]
    val = F[(F.index > TRAIN_END) & (F.index <= VAL_END)]
    train_z, val_z = standardize_features(train, val)
    X_train, X_val = train_z.values, val_z.values
    print(f"train: {len(X_train):,} bars   val: {len(X_val):,} bars")

    print(f"\nFitting K=5 with 25 restarts ...")
    model, best_ll, _ = fit_hmm(X_train, 5, n_restarts=25, rng_seed=42)
    print(f"best logL = {best_ll:.1f}")

    # Per-state feature means
    states = model.predict(X_train)
    df = pd.DataFrame(X_train, columns=train_z.columns)
    df["state"] = states
    means = df.groupby("state").mean()
    counts = df.groupby("state").size()
    pct = counts / len(states) * 100

    # Sort states by realized_vol_24 desc — primary axis
    sort_order = means["realized_vol_24"].sort_values(ascending=False).index
    means_sorted = means.loc[sort_order]
    counts_sorted = counts.loc[sort_order]
    pct_sorted = pct.loc[sort_order]

    # Suggest a label per state based on (vol, return) signature
    def suggest_label(row):
        vol = row["realized_vol_24"]
        ret = row["log_return_4h"]
        if vol > 1.0:
            return "STRESS" if ret > 0.0 else "CRASH"
        if vol > 0.0:
            return "MID_BEAR" if ret < -0.005 else "MID_NEUTRAL" if abs(ret) <= 0.005 else "MID_BULL"
        return "CHOP" if abs(ret) < 0.005 else ("UP" if ret > 0 else "DOWN")

    print(f"\n=== K=5 state means (sorted by realized_vol_24 desc) ===\n")
    header = "  state " + "".join(f"{f[:8]:>9s}" for f in FEATURE_NAMES) + "   n bars   %    suggested"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for s_idx in sort_order:
        row = means_sorted.loc[s_idx]
        cells = "".join(f"  {row[f]:+6.2f}" for f in FEATURE_NAMES)
        n = counts_sorted.loc[s_idx]
        p = pct_sorted.loc[s_idx]
        label = suggest_label(row)
        max_abs = row.abs().max()
        flag = "  ← NOISE?" if max_abs < 0.25 else ""
        print(f"  s={s_idx}  {cells}   {n:>5d}  {p:>5.2f}%   {label}{flag}")

    # Transition matrix on sorted order
    print(f"\n=== Transition matrix (smoothed, sorted by vol desc) ===\n")
    # Reorder states by sort_order
    state_order = list(sort_order)
    state_idx_to_pos = {orig: pos for pos, orig in enumerate(state_order)}
    states_remapped = np.array([state_idx_to_pos[s] for s in states])
    C = count_transitions(states_remapped, 5)
    P = smooth_transition(C, DIRICHLET_ALPHA, 5)

    # Print P
    labels_short = [f"s{i}" for i in range(5)]
    print("        " + "".join(f"{l:>9s}" for l in labels_short))
    for i in range(5):
        print(f"  {labels_short[i]:<6s}" + "".join(f"  {P[i, j]:.4f}" for j in range(5)))

    # Dwell times
    dwell = 1.0 / (1.0 - np.diag(P))
    pi = stationary_distribution(P)
    print(f"\n=== Per-state dynamics ===\n")
    print(f"  {'orig':>4s}  {'pos':>3s}  {'sugg':>15s}  {'dwell (bars)':>14s}   {'dwell (hours)':>15s}   {'π %':>7s}")
    for pos, orig in enumerate(state_order):
        d = dwell[pos]
        suggested = suggest_label(means_sorted.loc[orig])
        print(f"  {orig:>4d}  {pos:>3d}  {suggested:>15s}  {d:>14.2f}   {d * 4:>15.2f}   {pi[pos] * 100:>6.2f}%")

    # Decision logic
    print(f"\n=== Decision: K=5 vs K=4 ===\n")
    means_per_state_max_abs = means_sorted.abs().max(axis=1)
    weakest_state_max_abs = means_per_state_max_abs.min()
    weakest_idx = means_per_state_max_abs.idxmin()
    weakest_pos = state_idx_to_pos[weakest_idx]
    weakest_dwell = dwell[weakest_pos]
    weakest_pct = pct_sorted.loc[weakest_idx]

    print(f"Weakest state (orig {weakest_idx}, sorted pos {weakest_pos}):")
    print(f"  max |mean| across features: {weakest_state_max_abs:.3f}")
    print(f"  dwell time: {weakest_dwell:.1f} bars ({weakest_dwell * 4:.1f}h)")
    print(f"  share of bars: {weakest_pct:.2f}%")
    print()

    is_noise = weakest_state_max_abs < 0.25
    is_rare = weakest_pct < 5.0
    is_unstable = weakest_dwell < 8  # < 32h dwell time = state flicker

    if is_noise or is_rare or is_unstable:
        reasons = []
        if is_noise: reasons.append(f"means too close to 0 (max |mean|={weakest_state_max_abs:.3f} < 0.25)")
        if is_rare: reasons.append(f"too rare ({weakest_pct:.2f}% of bars < 5%)")
        if is_unstable: reasons.append(f"unstable (dwell {weakest_dwell:.1f} bars < 8 bars)")
        print(f"✗ K=5 weakest state looks like artifact: {'; '.join(reasons)}")
        print(f"  → K=4 is the parsimonious choice. Productionize K=4 with rolling re-fit.")
    else:
        print(f"✓ K=5 weakest state is operationally meaningful:")
        sig_str = ", ".join(f"{f}={means_sorted.loc[weakest_idx][f]:+.2f}" for f in FEATURE_NAMES
                            if abs(means_sorted.loc[weakest_idx][f]) > 0.20)
        print(f"  signature: {sig_str}")
        print(f"  → K=5 wins. Re-validate stability with rolling re-fit at K=5.")


if __name__ == "__main__":
    main()
