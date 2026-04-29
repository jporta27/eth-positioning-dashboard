"""Backtest CLI — run a preset signal through the event-study framework.

Usage:
    python -m backtest.cli --audit
    python -m backtest.cli --list
    python -m backtest.cli --signal funding_extreme_inflow --horizons 1h,4h,1d,3d,7d
    python -m backtest.cli --signal stables_expanding --output reports/stables_exp.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Optional

import numpy as np

from . import load
from .features import SIGNAL_REGISTRY
from .eventstudy import run_event_study


def _git_sha() -> Optional[str]:
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return sha
    except Exception:
        return None


def cmd_audit(args):
    a = load.audit()
    print(json.dumps(a, indent=2))


def cmd_list(args):
    print("Available signals:")
    for name in SIGNAL_REGISTRY:
        print(f"  {name}")


def cmd_run(args):
    horizons = [h.strip() for h in args.horizons.split(",") if h.strip()]
    if args.signal not in SIGNAL_REGISTRY:
        print(f"Unknown signal '{args.signal}'. Use --list.", file=sys.stderr)
        sys.exit(2)

    print(f"Extracting events for {args.signal} ...")
    t0 = time.time()
    try:
        ts_ms, sig_vals = SIGNAL_REGISTRY[args.signal]()
    except RuntimeError as e:
        print(f"SIGNAL FAILED: {e}", file=sys.stderr)
        sys.exit(1)
    dt = time.time() - t0
    if len(ts_ms) < 30:
        print(f"Only {len(ts_ms)} events — event study skipped (needs ≥30 for stable stats).", file=sys.stderr)
        sys.exit(1)
    print(f"  {len(ts_ms)} events, signal range [{sig_vals.min():.3f}, {sig_vals.max():.3f}]  ({dt:.1f}s)")

    # Apply date filter if requested
    if args.start:
        start_ms = int(_to_epoch_ms(args.start))
        mask = ts_ms >= start_ms
        ts_ms, sig_vals = ts_ms[mask], sig_vals[mask]
    if args.end:
        end_ms = int(_to_epoch_ms(args.end))
        mask = ts_ms <= end_ms
        ts_ms, sig_vals = ts_ms[mask], sig_vals[mask]
    if len(ts_ms) < 30:
        print(f"After date filter: only {len(ts_ms)} events. Skipped.", file=sys.stderr)
        sys.exit(1)

    print(f"Running event study over {len(ts_ms)} events, horizons={horizons} ...")
    t0 = time.time()
    res = run_event_study(
        event_ts_ms=ts_ms,
        signal_values=sig_vals,
        horizons=horizons,
        bootstrap_iter=args.bootstrap_iter,
        n_trials=args.n_trials,
        rng_seed=args.seed,
    )
    dt = time.time() - t0
    print(f"  done in {dt:.1f}s")

    payload = {
        "signal": {
            "name":   args.signal,
            "preset": True,
            "start":  args.start,
            "end":    args.end,
            "n_trials_for_dsr": args.n_trials,
            "bootstrap_iter":   args.bootstrap_iter,
        },
        "n_observations": int(len(ts_ms)),
        "horizons":       res["horizons"],
        "generated_at":   int(time.time() * 1000),
        "code_version":   _git_sha(),
    }

    # Pretty summary
    print("\n=== RESULTS ===")
    for h, hh in res["horizons"].items():
        if hh.get("note"):
            print(f"  {h}: {hh['note']} (n_valid={hh.get('n_valid')})")
            continue
        # Sharpe printed is the operationally-correct event-frequency variant.
        # The horizon-frequency variant lives in `sharpe_annualized_by_horizon_freq`
        # for backwards compatibility with older reports.
        print(f"  {h}: n={hh['n_valid']:>4}  "
              f"mean={hh['mean_return']:+.4f} "
              f"CI95=[{hh['ci95_bootstrap'][0]:+.4f}, {hh['ci95_bootstrap'][1]:+.4f}]  "
              f"IC={hh['ic_spearman']:+.3f} p={hh['ic_pvalue_bootstrap']:.3f}  "
              f"hit={hh['hit_rate']:.3f} (base {hh['hit_rate_baseline_random']:.3f})  "
              f"sharpe_evf={hh['sharpe_annualized_by_event_freq']:+.2f}  "
              f"sharpe/ev={hh['sharpe_per_event']:+.3f}  "
              f"DSR={hh['deflated_sharpe']:.3f}  MDD={hh['max_drawdown']:.3f}")

    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
        print(f"\nWrote {args.output}")


def _to_epoch_ms(s: str) -> int:
    from datetime import datetime, timezone
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return int(datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            continue
    raise ValueError(f"can't parse date {s}")


def main():
    p = argparse.ArgumentParser(prog="backtest", description="ETH event-study framework")
    p.add_argument("--audit", action="store_true", help="Print data availability summary")
    p.add_argument("--list",  action="store_true", help="Print available signal presets")
    p.add_argument("--signal", type=str, help="Signal preset name (see --list)")
    p.add_argument("--horizons", type=str, default="1h,4h,1d,3d,7d")
    p.add_argument("--start", type=str, default=None, help="YYYY-MM-DD filter")
    p.add_argument("--end",   type=str, default=None, help="YYYY-MM-DD filter")
    p.add_argument("--bootstrap-iter", type=int, default=5000)
    p.add_argument("--n-trials", type=int, default=1, help="Number of strategies tested (for DSR)")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--output", type=str, default=None, help="Write JSON report to path")
    args = p.parse_args()

    if args.audit:
        return cmd_audit(args)
    if args.list:
        return cmd_list(args)
    if not args.signal:
        p.print_help()
        sys.exit(2)
    return cmd_run(args)


if __name__ == "__main__":
    main()
