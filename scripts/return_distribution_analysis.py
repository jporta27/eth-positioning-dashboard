"""
Return distribution analysis for ETH — the quant 'know your asset' pass.

Before modelling anything (mean-reversion, score, regime), you characterise the
return distribution. This script answers:

  1. Moments — mean, std, skew, excess kurtosis at multiple horizons
  2. Normality — Jarque-Bera test (is it Gaussian? spoiler: no)
  3. Fat tails — observed vs Gaussian-expected frequency of 3σ/4σ/5σ moves,
     and left-vs-right tail asymmetry (crash risk)
  4. Volatility clustering — autocorrelation of |returns| (ARCH effect)
  5. Return autocorrelation — is there mean-reversion or momentum at the bar level?
  6. CONDITIONAL on HMM K=4 regime — does the distribution change by regime?
     (CRASH/STRESS/CHOP/UP) — this is where it gets actionable for the score.

Why it matters for THIS project:
  - Mean-reversion edge depends on negative return autocorrelation. Test 5 measures it.
  - Fat tails (test 3) explain why fixed stops get hit and why the score's
    Gaussian-ish z-score thresholds mis-calibrate in the tails.
  - Regime-conditional moments (test 6) justify (or kill) regime-conditioning
    the score thresholds — the P5 idea from the quant review.

Run:
    python scripts/return_distribution_analysis.py
"""

from __future__ import annotations

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

# Horizons in hours
HORIZONS = [1, 4, 24]


def load_close_1h() -> pd.DataFrame:
    t = pq.read_table(KLINES_PATH)
    df = t.to_pandas()[["ts_utc_ms", "close"]].sort_values("ts_utc_ms")
    df["ts"] = pd.to_datetime(df["ts_utc_ms"], unit="ms", utc=True)
    return df.set_index("ts")[["close"]]


def log_returns(close: pd.Series, h: int) -> np.ndarray:
    """h-bar log returns (non-overlapping not enforced — we use all bars)."""
    r = np.log(close / close.shift(h)).dropna().values
    return r


def describe_moments(r: np.ndarray, label: str) -> dict:
    return {
        "label": label, "n": len(r),
        "mean_bp": r.mean() * 1e4,         # basis points
        "std_pct": r.std() * 100,
        "skew": stats.skew(r),
        "exkurt": stats.kurtosis(r),       # excess (normal = 0)
        "min_pct": r.min() * 100, "max_pct": r.max() * 100,
    }


def tail_analysis(r: np.ndarray) -> dict:
    """Observed vs Gaussian-expected frequency of extreme moves, both tails."""
    z = (r - r.mean()) / r.std()
    out = {}
    for k in (3, 4, 5):
        gauss_expected = 2 * stats.norm.sf(k)        # two-tailed P(|Z|>k) under normal
        obs = (np.abs(z) > k).mean()
        out[f"{k}sigma_obs"] = obs
        out[f"{k}sigma_exp"] = gauss_expected
        out[f"{k}sigma_ratio"] = obs / gauss_expected if gauss_expected > 0 else np.inf
    # Tail asymmetry: how much fatter is the left tail (crashes) vs right?
    left = (z < -3).mean()
    right = (z > 3).mean()
    out["left_3s"] = left
    out["right_3s"] = right
    out["left_right_ratio"] = left / right if right > 0 else np.inf
    return out


def main():
    print("=" * 76)
    print("ETH RETURN DISTRIBUTION ANALYSIS")
    print("=" * 76)
    close = load_close_1h()["close"]
    print(f"\nklines: {len(close):,} 1h bars  {close.index.min()} → {close.index.max()}")

    # ── 1. Moments by horizon ───────────────────────────────────────
    print("\n" + "─" * 76)
    print("1. MOMENTS by horizon")
    print("─" * 76)
    print(f"  {'horizon':>8} {'n':>7} {'mean(bp)':>9} {'std(%)':>8} {'skew':>7} {'exkurt':>8} {'min%':>8} {'max%':>8}")
    rets_by_h = {}
    for h in HORIZONS:
        r = log_returns(close, h)
        rets_by_h[h] = r
        m = describe_moments(r, f"{h}h")
        print(f"  {m['label']:>8} {m['n']:>7} {m['mean_bp']:>9.2f} {m['std_pct']:>8.3f} "
              f"{m['skew']:>7.2f} {m['exkurt']:>8.2f} {m['min_pct']:>8.2f} {m['max_pct']:>8.2f}")
    print("  → exkurt >> 0 = fat tails (normal=0). skew<0 = left tail (crash) heavier.")

    # ── 2. Normality ────────────────────────────────────────────────
    print("\n" + "─" * 76)
    print("2. NORMALITY (Jarque-Bera)")
    print("─" * 76)
    for h in HORIZONS:
        jb, p = stats.jarque_bera(rets_by_h[h])
        verdict = "REJECT normal" if p < 0.01 else "cannot reject"
        print(f"  {h:>3}h  JB={jb:>14,.0f}  p={p:.2e}  → {verdict}")
    print("  → p<0.01 means returns are NOT Gaussian. Expected for crypto.")

    # ── 3. Fat tails ────────────────────────────────────────────────
    print("\n" + "─" * 76)
    print("3. FAT TAILS — observed vs Gaussian-expected extreme moves")
    print("─" * 76)
    for h in HORIZONS:
        t = tail_analysis(rets_by_h[h])
        print(f"\n  Horizon {h}h:")
        for k in (3, 4, 5):
            obs = t[f"{k}sigma_obs"]; exp = t[f"{k}sigma_exp"]; ratio = t[f"{k}sigma_ratio"]
            # expected count in this sample
            exp_n = exp * len(rets_by_h[h])
            obs_n = obs * len(rets_by_h[h])
            print(f"    >{k}σ:  observed {obs*100:>6.3f}% ({obs_n:>5.0f} events)  "
                  f"vs normal {exp*100:>6.4f}% ({exp_n:>4.1f})  → {ratio:>5.1f}× more frequent")
        print(f"    left/right 3σ asymmetry: {t['left_3s']*100:.3f}% vs {t['right_3s']*100:.3f}% "
              f"→ left tail {t['left_right_ratio']:.2f}× the right")
    print("\n  → ratio >> 1 = fat tails. This is WHY fixed-σ stops get hit and why")
    print("    Gaussian z-score thresholds in the score under-estimate tail risk.")

    # ── 4. Volatility clustering ────────────────────────────────────
    print("\n" + "─" * 76)
    print("4. VOLATILITY CLUSTERING — autocorr of |returns| (ARCH effect)")
    print("─" * 76)
    r1 = rets_by_h[1]
    abs_r = np.abs(r1)
    print(f"  {'lag':>5} {'acf(|r|)':>10} {'acf(r)':>10}")
    for lag in (1, 2, 4, 8, 24, 48):
        acf_abs = pd.Series(abs_r).autocorr(lag)
        acf_r = pd.Series(r1).autocorr(lag)
        print(f"  {lag:>5} {acf_abs:>10.4f} {acf_r:>10.4f}")
    print("  → acf(|r|) > 0 and decaying slowly = vol clustering (calm follows calm,")
    print("    storms follow storms). acf(r) = return autocorrelation (next section).")

    # ── 5. Return autocorrelation (mean-reversion vs momentum) ──────
    print("\n" + "─" * 76)
    print("5. RETURN AUTOCORRELATION — mean-reversion or momentum at bar level?")
    print("─" * 76)
    print(f"  {'horizon':>8} {'n':>6} {'lag-1 acf':>10} {'reading':>30}")
    for h in HORIZONS:
        # NON-OVERLAPPING returns — overlapping ones give spurious positive acf
        # (a 4h return built on 1h bars shares 3h with the next → fake momentum).
        r_nonoverlap = np.log(close / close.shift(h)).dropna().values[::h]
        acf1 = pd.Series(r_nonoverlap).autocorr(1)
        if acf1 < -0.03:
            reading = "mean-reversion (neg autocorr)"
        elif acf1 > 0.03:
            reading = "momentum (pos autocorr)"
        else:
            reading = "~random walk"
        print(f"  {h:>7}h  {len(r_nonoverlap):>6} {acf1:>10.4f}  {reading:>30}")
    print("  → non-overlapping returns (no spurious overlap autocorr).")
    print("    negative = bar tends to reverse (helps mean-rev). near zero = random walk")
    print("    (mean-rev needs a real trigger, not just 'price went down').")

    # ── 6. Regime-conditional distribution (HMM K=4) ────────────────
    print("\n" + "─" * 76)
    print("6. CONDITIONAL ON HMM REGIME — does the distribution change by regime?")
    print("─" * 76)
    try:
        sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
        from run_regime_classifier import build_full_features
        sys.path.insert(0, REPO_ROOT)
        from backtest.regime_classifier import RegimeClassifier, LABELS
        model_path = os.path.join(REPO_ROOT, "data", "regime", "model.pkl")
        if not os.path.exists(model_path):
            print("  (no fitted regime model — run scripts/run_regime_classifier.py --refit)")
            return
        clf = RegimeClassifier.load(model_path)
        F = build_full_features()
        # Standardise with the model's stored stats + predict regime per bar
        X = (F.values - clf.artifact.feature_means) / clf.artifact.feature_stds
        states = clf.artifact.hmm_model.predict(X)
        idx_by_label = {v: k for k, v in clf.artifact.labels_by_idx.items()}
        # Map each 4h feature bar to a regime label
        regime_series = pd.Series(
            [clf.artifact.labels_by_idx[s] for s in states], index=F.index)
        # 4h forward returns aligned to regime bars
        close_4h = close.resample("4h").last()
        ret_4h = np.log(close_4h / close_4h.shift(1)).shift(-1)  # NEXT-bar return per regime
        joined = pd.DataFrame({"regime": regime_series, "ret": ret_4h.reindex(F.index)}).dropna()
        print(f"  Next-4h-bar return distribution by regime (n={len(joined):,}):\n")
        print(f"  {'regime':>8} {'n':>6} {'mean(bp)':>9} {'std(%)':>8} {'skew':>7} {'exkurt':>8} {'P(up)':>7}")
        for lab in LABELS:
            sub = joined[joined["regime"] == lab]["ret"].values
            if len(sub) < 10:
                print(f"  {lab:>8} {len(sub):>6}  (too few)")
                continue
            print(f"  {lab:>8} {len(sub):>6} {sub.mean()*1e4:>9.2f} {sub.std()*100:>8.3f} "
                  f"{stats.skew(sub):>7.2f} {stats.kurtosis(sub):>8.2f} {(sub>0).mean()*100:>6.1f}%")
        print("\n  → if mean/std/skew differ sharply by regime, the score SHOULD condition")
        print("    its thresholds on regime (P5 of the quant review). If they're similar,")
        print("    the regime label adds little to the score and simplicity wins.")
    except Exception as e:
        print(f"  (regime conditioning skipped: {e})")

    print("\n" + "=" * 76)
    print("IMPLICATIONS")
    print("=" * 76)
    print("  • Fat tails → the score's Gaussian z-thresholds under-state tail risk.")
    print("    Consider empirical percentiles instead of σ-multiples for magnitude.")
    print("  • Vol clustering → realized vol is forecastable; the vol amplifier in the")
    print("    score has a real basis. Regime persistence (HMM) rides on this.")
    print("  • Return autocorrelation near zero → mean-reversion needs a genuine trigger")
    print("    (stoch + flow), not just 'price went down'. Matches the event-study finding.")
    print("  • Regime-conditional moments → decide whether to regime-condition the score.")


if __name__ == "__main__":
    main()
