"""Conditioned event-study runs for every preset × every regime subset.

Six conditioning subsets (chosen to keep combinatorics manageable — full
cross-product across 6 labelers would be 4×3×2×3×3=216 combos × 10 signals,
which is both too noisy and too long to compute):

  S1: {vix_quartile}                  4  buckets
  S2: {etf_7d_sign}                   3  buckets (ETF-era events only)
  S3: {stables_30d_sign}              3  buckets
  S4: {realized_vol_regime}           3  buckets
  S5: {btc_trend_30d}                 3  buckets
  S6: {vix_quartile, etf_7d_sign}     up to 12 combos

= 6 conditioning runs × 10 signals = 60 reports written under
  reports/conditioned/<signal>__<subset_name>.json

Subsets that include a labeler with mostly-unknown labels for a given signal
(e.g. ETF-era labels for pre-2024 funding events) will produce many
'unknown' / insufficient combos — that's expected and surfaced in the
summary.
"""
import os
import subprocess
import sys
import time

PRESETS = [
    "funding_hot_inflow",
    "funding_hot_outflow",
    "funding_hot_abs",
    "funding_extreme_inflow",
    "funding_extreme_outflow",
    "funding_extreme_abs",
    "stables_expanding",
    "stables_contracting",
    "etf_buying",
    "etf_selling",
]
SUBSETS = [
    ("vix",        ["vix_quartile"]),
    ("etf",        ["etf_7d_sign"]),
    ("stables",    ["stables_30d_sign"]),
    ("rv",         ["realized_vol_regime"]),
    ("btc",        ["btc_trend_30d"]),
    ("vix_etf",    ["vix_quartile", "etf_7d_sign"]),
]
HORIZONS = "1h,4h,1d,3d,7d"
BOOTSTRAP = "5000"
OUT_DIR = "reports/conditioned"


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    rows = []
    for sig in PRESETS:
        for subset_name, labelers in SUBSETS:
            out = os.path.join(OUT_DIR, f"{sig}__{subset_name}.json")
            print(f"\n=== {sig}  +  {','.join(labelers)} -> {out} ===", flush=True)
            t0 = time.time()
            cmd = [sys.executable, "-m", "backtest.cli",
                   "--signal", sig,
                   "--horizons", HORIZONS,
                   "--bootstrap-iter", BOOTSTRAP,
                   "--conditions", ",".join(labelers),
                   "--output", out]
            rc = subprocess.call(cmd)
            dt = time.time() - t0
            rows.append((sig, subset_name, rc, dt))
            print(f"  rc={rc}  {dt:.1f}s", flush=True)

    print("\n=== Summary ===")
    fails = []
    for sig, subset, rc, dt in rows:
        status = "OK" if rc == 0 else f"FAIL(rc={rc})"
        print(f"  {sig:30s} + {subset:12s}  {status:10s} {dt:6.1f}s")
        if rc != 0:
            fails.append((sig, subset))
    if fails:
        print(f"\n{len(fails)} failed:")
        for sig, subset in fails:
            print(f"  {sig} + {subset}")
        # Failures are usually 'insufficient base sample' (e.g. ETF signals with
        # n=14/19) — non-blocking for the summary script, which handles missing
        # files gracefully.


if __name__ == "__main__":
    main()
