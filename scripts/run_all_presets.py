"""Re-run all 10 SIGNAL_REGISTRY presets with the PROMPT-A fixes + extended data.

Writes one JSON per signal to reports/<name>.json. Sequential to keep parquet
read traffic predictable. Per-signal timing reported at the end.
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
HORIZONS = "1h,4h,1d,3d,7d"
BOOTSTRAP = "5000"
REPORTS_DIR = "reports"


def main():
    os.makedirs(REPORTS_DIR, exist_ok=True)
    timings = []
    for sig in PRESETS:
        out = os.path.join(REPORTS_DIR, f"{sig}.json")
        print(f"\n=== {sig} -> {out} ===", flush=True)
        t0 = time.time()
        rc = subprocess.call(
            [sys.executable, "-m", "backtest.cli",
             "--signal", sig,
             "--horizons", HORIZONS,
             "--bootstrap-iter", BOOTSTRAP,
             "--output", out],
        )
        dt = time.time() - t0
        timings.append((sig, rc, dt))
        print(f"  rc={rc}  elapsed={dt:.1f}s", flush=True)

    print("\n=== Summary ===")
    for sig, rc, dt in timings:
        status = "OK" if rc == 0 else f"FAIL(rc={rc})"
        print(f"  {sig:30s}  {status:10s}  {dt:6.1f}s")
    failures = [s for s, rc, _ in timings if rc != 0]
    if failures:
        print(f"\n{len(failures)} failed: {failures}")
        sys.exit(1)


if __name__ == "__main__":
    main()
