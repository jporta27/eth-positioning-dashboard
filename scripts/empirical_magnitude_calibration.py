"""
Empirical, regime-aware magnitude calibration for ETH moves.

Implements P-of-the-quant-review: replace Gaussian σ-multiple thresholds with
EMPIRICAL percentiles, conditioned on the HMM K=4 regime. Two findings from
return_distribution_analysis.py motivate this:
  1. Fat tails: a |z|=2 move ("EXTREME" under normality) happens FAR more often
     than the 2.3% a Gaussian predicts → σ-thresholds cry wolf.
  2. Regime predicts VOLATILITY (CRASH 2.11% vs CHOP 0.98% std, 2.1×): the same
     ±2% move is routine in CRASH but a 2σ event in CHOP. A fixed threshold
     mislabels both.

What this produces:
  - Per-regime, per-horizon empirical percentiles of |return| (p50/p90/p95/p99)
  - Empirical magnitude cuts: NOISE <p50, NORMAL p50-p90, ELEVATED p90-p99,
    EXTREME ≥p99
  - The Gaussian σ-multiple that WOULD correspond to each empirical percentile,
    showing how badly the normal assumption misprices the tail
  - A JSON calibration table (data/regime/magnitude_calibration.json) the
    backend can load to label moves empirically instead of by σ

Run:
    python scripts/empirical_magnitude_calibration.py
"""

from __future__ import annotations

import json
import os
import sys
import warnings

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from scipy import stats

sys.stdout.reconfigure(encoding="utf-8")
warnings.filterwarnings("ignore", category=RuntimeWarning)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KLINES_PATH = os.path.join(REPO_ROOT, "data", "backfill", "binance_klines_1h.parquet")
OUT_JSON = os.path.join(REPO_ROOT, "data", "regime", "magnitude_calibration.json")

HORIZONS = [1, 4, 24]
PERCENTILES = [50, 75, 90, 95, 99]


def load_close_1h() -> pd.Series:
    t = pq.read_table(KLINES_PATH).to_pandas()[["ts_utc_ms", "close"]].sort_values("ts_utc_ms")
    t["ts"] = pd.to_datetime(t["ts_utc_ms"], unit="ms", utc=True)
    return t.set_index("ts")["close"]


def gaussian_sigma_for_percentile(p: float) -> float:
    """The σ-multiple a Gaussian would assign to the p-th percentile of |x|.
    For |x|, percentile p maps to the (1+p/100)/2 quantile of the standard normal."""
    return stats.norm.ppf((1 + p / 100) / 2)


def main():
    print("=" * 78)
    print("EMPIRICAL REGIME-AWARE MAGNITUDE CALIBRATION")
    print("=" * 78)

    close = load_close_1h()
    print(f"\nklines: {len(close):,} 1h bars  {close.index.min().date()} → {close.index.max().date()}")

    # ── Assign regime to each 1h bar via the fitted HMM ─────────────
    sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
    sys.path.insert(0, REPO_ROOT)
    from run_regime_classifier import build_full_features
    from backtest.regime_classifier import RegimeClassifier, LABELS
    model_path = os.path.join(REPO_ROOT, "data", "regime", "model.pkl")
    if not os.path.exists(model_path):
        print("No regime model — run scripts/run_regime_classifier.py --refit first")
        return
    clf = RegimeClassifier.load(model_path)
    F = build_full_features()
    X = (F.values - clf.artifact.feature_means) / clf.artifact.feature_stds
    states = clf.artifact.hmm_model.predict(X)
    regime_4h = pd.Series([clf.artifact.labels_by_idx[s] for s in states], index=F.index)
    # Forward-fill the 4h regime onto the 1h grid (causal: regime known at 4h close)
    regime_1h = regime_4h.reindex(close.index, method="ffill")
    print(f"regime labels assigned to {regime_1h.notna().sum():,} 1h bars")

    calibration = {"horizons": {}, "meta": {
        "generated_from_bars": int(len(close)),
        "regimes": LABELS,
        "note": "Empirical |return| percentiles by regime. EXTREME=p99, ELEVATED=p90-99, "
                "NORMAL=p50-90, NOISE<p50. Use abs-return vs these cuts instead of Gaussian σ.",
    }}

    for h in HORIZONS:
        ret = np.log(close / close.shift(h))
        abs_ret = ret.abs()
        df = pd.DataFrame({"abs_ret": abs_ret, "regime": regime_1h}).dropna()

        print("\n" + "─" * 78)
        print(f"HORIZON {h}h — empirical |return| percentiles by regime")
        print("─" * 78)
        header = f"  {'regime':>8} {'n':>6} " + " ".join(f"p{p}(%)".rjust(9) for p in PERCENTILES)
        print(header)
        calibration["horizons"][f"{h}h"] = {}
        for lab in LABELS + ["ALL"]:
            sub = df["abs_ret"].values if lab == "ALL" else df[df["regime"] == lab]["abs_ret"].values
            if len(sub) < 50:
                print(f"  {lab:>8} {len(sub):>6}  (too few)")
                continue
            pcts = {p: float(np.percentile(sub, p)) for p in PERCENTILES}
            cells = " ".join(f"{pcts[p]*100:>8.3f}" for p in PERCENTILES)
            print(f"  {lab:>8} {len(sub):>6} {cells}")
            calibration["horizons"][f"{h}h"][lab] = {
                "n": int(len(sub)),
                "p50": pcts[50], "p75": pcts[75], "p90": pcts[90],
                "p95": pcts[95], "p99": pcts[99],
                "std": float(np.std(sub)),
            }

        # ── The headline comparison: Gaussian vs empirical at the EXTREME cut ──
        print(f"\n  GAUSSIAN MISPRICING at horizon {h}h:")
        print(f"  A σ-threshold assumes p99 ≈ 2.58σ. What σ does the EMPIRICAL p99 sit at?")
        print(f"  {'regime':>8} {'emp p99(%)':>11} {'regime σ(%)':>12} {'p99 in σ':>9} {'vs 2.58σ':>9}")
        for lab in LABELS + ["ALL"]:
            c = calibration["horizons"][f"{h}h"].get(lab)
            if not c:
                continue
            sigma = c["std"]
            p99_in_sigma = c["p99"] / sigma if sigma > 0 else float("nan")
            # If the empirical p99 sits at, say, 3.5σ, then a "2.58σ = p99" rule
            # would fire ~3× too often (label too many moves EXTREME).
            verdict = "σ-rule cries wolf" if p99_in_sigma > 2.8 else "ok"
            print(f"  {lab:>8} {c['p99']*100:>10.3f}% {sigma*100:>11.3f}% "
                  f"{p99_in_sigma:>8.2f}σ {('+' if p99_in_sigma>2.58 else '')+f'{p99_in_sigma-2.58:.2f}σ':>9}")

    # ── Persist ─────────────────────────────────────────────────────
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(calibration, f, indent=2)
    print("\n" + "=" * 78)
    print(f"Calibration table → {OUT_JSON}")
    print("=" * 78)
    print("  How the backend uses it: to label a move's magnitude, compare |return|")
    print("  against THIS regime's empirical cuts, not a fixed σ-multiple. A −2% move")
    print("  in CHOP may be p95 (ELEVATED) while the same −2% in CRASH is p60 (NORMAL).")
    print("\n  Same idea applies to the CEX-netflow z-score magnitude (|z|≥2=EXTREME):")
    print("  replace the Gaussian cut with the empirical percentile of the netflow")
    print("  distribution once enough netflow history is logged.")


if __name__ == "__main__":
    main()
