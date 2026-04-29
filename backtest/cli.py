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
from . import regime as regime_mod
from .features import SIGNAL_REGISTRY
from .eventstudy import run_event_study, run_with_regimes


STRATEGY_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "strategy_log.json")


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


def _load_strategy_log() -> list:
    """Read backtest/strategy_log.json. Returns [] if missing/malformed."""
    if not os.path.exists(STRATEGY_LOG_PATH):
        return []
    try:
        with open(STRATEGY_LOG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return list(data.get("strategies_tested", []))
    except (json.JSONDecodeError, OSError):
        return []


def _record_strategy_tested(name: str) -> list:
    """Append `name` to strategy_log.json if not already present, then return
    the updated list. The list length is the operationally-correct n_trials
    for DSR — every distinct signal we have ever evaluated counts as one trial,
    so DSR can correctly deflate for the search we actually performed.
    """
    log = _load_strategy_log()
    if name not in log:
        log.append(name)
        os.makedirs(os.path.dirname(STRATEGY_LOG_PATH), exist_ok=True)
        with open(STRATEGY_LOG_PATH, "w", encoding="utf-8") as f:
            json.dump({"strategies_tested": log}, f, indent=2)
    return log


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

    # n_trials for DSR: auto-track every distinct signal we have ever evaluated
    # via backtest/strategy_log.json. The current run's signal is appended now
    # so DSR for *this* report includes itself in the trial count. --n-trials
    # on the command line still overrides the auto-tracked value.
    log = _record_strategy_tested(args.signal)
    auto_n_trials = len(log)
    if args.n_trials is None:
        n_trials = auto_n_trials
        n_trials_source = "auto-tracked"
        print(f"DSR using n_trials={n_trials} (auto-tracked from strategy_log.json, "
              f"override with --n-trials)")
    else:
        n_trials = int(args.n_trials)
        n_trials_source = "manual override"
        print(f"DSR using n_trials={n_trials} (manual override; auto-tracked would be "
              f"{auto_n_trials})")

    # Optional regime conditioning
    conditions = [c.strip() for c in (args.conditions or "").split(",") if c.strip()]

    if conditions:
        # Validate names early
        unknown = [c for c in conditions if c not in regime_mod.LABELERS]
        if unknown:
            print(f"Unknown regime labelers: {unknown}. Available: {list(regime_mod.LABELERS)}",
                  file=sys.stderr)
            sys.exit(2)

        print(f"Conditioning on: {conditions}")
        labels = regime_mod.label_events(ts_ms, conditions)
        for cname, arr in labels.items():
            unique, counts = np.unique(arr, return_counts=True)
            dist = ", ".join(f"{u}:{c}" for u, c in zip(unique, counts))
            print(f"  {cname}: {dist}")

        print(f"Running conditioned event study over {len(ts_ms)} events, "
              f"horizons={horizons} ...")
        t0 = time.time()
        regimes_out = run_with_regimes(
            event_ts_ms=ts_ms,
            signal_values=sig_vals,
            regime_labels=labels,
            horizons=horizons,
            bootstrap_iter=args.bootstrap_iter,
            n_trials=n_trials,
            rng_seed=args.seed,
        )
        # n_trials is passed through so per-combo DSR deflates against the same
        # global search width as the unconditioned baseline.
        dt = time.time() - t0
        print(f"  done in {dt:.1f}s ({len(regimes_out)} regime combinations)")
        # Also compute the unconditioned baseline so the report can compare.
        baseline_res = run_event_study(
            event_ts_ms=ts_ms,
            signal_values=sig_vals,
            horizons=horizons,
            bootstrap_iter=args.bootstrap_iter,
            n_trials=n_trials,
            rng_seed=args.seed,
        )
        res = {"horizons": baseline_res["horizons"], "regimes": regimes_out,
               "n_events_pre_purge":       baseline_res.get("n_events_pre_purge"),
               "n_events_purged":          baseline_res.get("n_events_purged"),
               "purge_applied":            baseline_res.get("purge_applied"),
               "years_in_sample":          baseline_res.get("years_in_sample"),
               "event_frequency_per_year": baseline_res.get("event_frequency_per_year")}
    else:
        print(f"Running event study over {len(ts_ms)} events, horizons={horizons} ...")
        t0 = time.time()
        res = run_event_study(
            event_ts_ms=ts_ms,
            signal_values=sig_vals,
            horizons=horizons,
            bootstrap_iter=args.bootstrap_iter,
            n_trials=n_trials,
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
            "n_trials_for_dsr":         n_trials,
            "n_trials_source":          n_trials_source,
            "n_trials_auto_tracked":    auto_n_trials,
            "strategies_tested_known":  list(log),
            "bootstrap_iter":           args.bootstrap_iter,
            "conditions":               conditions,
        },
        "n_observations":           int(len(ts_ms)),
        "n_events_pre_purge":       res.get("n_events_pre_purge"),
        "n_events_purged":          res.get("n_events_purged"),
        "purge_applied":            res.get("purge_applied"),
        "years_in_sample":          res.get("years_in_sample"),
        "event_frequency_per_year": res.get("event_frequency_per_year"),
        "horizons":                 res["horizons"],
        "regimes":                  res.get("regimes"),
        "generated_at":             int(time.time() * 1000),
        "code_version":             _git_sha(),
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
    p.add_argument("--n-trials", type=int, default=None,
                   help="Number of strategies tested (for DSR). Default: auto-tracked "
                        "from backtest/strategy_log.json — every --signal run appends to it.")
    p.add_argument("--conditions", type=str, default=None,
                   help="Comma-separated regime labelers from backtest/regime.py "
                        "(e.g. vix_quartile,etf_7d_sign). When set, a per-regime-combo "
                        "event study runs alongside the unconditioned baseline; combos "
                        "with <30 events are marked insufficient.")
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
