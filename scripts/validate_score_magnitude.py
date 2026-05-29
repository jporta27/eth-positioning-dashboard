"""
Market State Score — magnitude validation pipeline (P3 of the quant review).

Reads:
  data/state_log/*.jsonl           — snapshots logged by the dashboard
  data/backfill/binance_klines_1h.parquet — price for forward returns

Reports:
  - IC (Spearman) between score and forward return at multiple horizons
  - Sign-accuracy CONDITIONED on |score|: does accuracy rise with magnitude?
    This is THE test that decides whether the score→state mapping is meaningful
    (per the quant review, section 2.2). If accuracy is flat across |score|,
    the boundaries (+3 → ALCISTA, etc) need recalibration or the magnitude is
    decorative.
  - IC decay by horizon — how fast does the signal lose information?
  - Lift over baseline — does the 12-factor score beat sign(last_return)?
  - Pre/post modulator comparison — did P1's volume modulator help?

This script is safe to run with no log data (it will report what's missing
and exit cleanly). It becomes meaningful with ~2-4 weeks of logged data.

Run:
    python scripts/validate_score_magnitude.py
    python scripts/validate_score_magnitude.py --min-horizon 1 --max-horizon 48
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import warnings
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from scipy.stats import spearmanr

sys.stdout.reconfigure(encoding="utf-8")
warnings.filterwarnings("ignore", category=RuntimeWarning)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_LOG_DIR = os.path.join(REPO_ROOT, "data", "state_log")
KLINES_PATH = os.path.join(REPO_ROOT, "data", "backfill", "binance_klines_1h.parquet")

# Horizons (in hours) at which to measure forward return vs score
DEFAULT_HORIZONS = [1, 2, 4, 8, 16, 24, 48]


# ── Loading ─────────────────────────────────────────────────────────
def load_log() -> pd.DataFrame:
    """Concatenate all JSONL snapshots into a single DataFrame.
    Tolerates malformed lines (skips them with a warning)."""
    if not os.path.isdir(STATE_LOG_DIR):
        return pd.DataFrame()
    files = sorted(glob.glob(os.path.join(STATE_LOG_DIR, "*.jsonl")))
    if not files:
        return pd.DataFrame()
    rows = []
    skipped = 0
    for fp in files:
        with open(fp, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    skipped += 1
    if skipped:
        print(f"  ⚠ Skipped {skipped} malformed lines", file=sys.stderr)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.sort_values("ts").reset_index(drop=True)
    return df


def load_klines_1h() -> pd.DataFrame:
    if not os.path.exists(KLINES_PATH):
        raise FileNotFoundError(KLINES_PATH)
    t = pq.read_table(KLINES_PATH)
    df = t.to_pandas()[["ts_utc_ms", "close"]].sort_values("ts_utc_ms")
    df["ts"] = pd.to_datetime(df["ts_utc_ms"], unit="ms", utc=True)
    return df.set_index("ts")[["close"]]


# ── Forward returns ─────────────────────────────────────────────────
def attach_forward_returns(log: pd.DataFrame, klines: pd.DataFrame,
                            horizons_h: list) -> pd.DataFrame:
    """For each snapshot in `log`, find the most-recent at-or-before close at
    `ts` and at `ts + h`, then compute forward return.

    Uses numpy.searchsorted for robustness — pd.merge_asof was finicky about
    null timestamps and timezone alignment."""
    out = log.copy()
    # Drop rows without a valid timestamp first
    out = out[out["ts"].notna()].reset_index(drop=True)
    kline_ts = klines.index.values.astype("datetime64[ns]")
    kline_closes = klines["close"].values

    def _close_at(target_ts_ns: np.ndarray) -> np.ndarray:
        """For each target ts, return the most-recent close at-or-before."""
        # searchsorted with side='right' gives insertion point AFTER duplicates;
        # subtract 1 to get the at-or-before index.
        idx = np.searchsorted(kline_ts, target_ts_ns, side="right") - 1
        result = np.full(len(target_ts_ns), np.nan, dtype=float)
        valid = (idx >= 0) & (idx < len(kline_closes))
        result[valid] = kline_closes[idx[valid]]
        return result

    log_ts_ns = out["ts"].values.astype("datetime64[ns]")
    spot_at_t = _close_at(log_ts_ns)
    for h in horizons_h:
        target = log_ts_ns + np.timedelta64(h * 3600, "s")
        spot_at_t_h = _close_at(target)
        with np.errstate(divide="ignore", invalid="ignore"):
            out[f"ret_{h}h"] = np.where(
                (spot_at_t > 0) & ~np.isnan(spot_at_t_h),
                spot_at_t_h / spot_at_t - 1.0,
                np.nan,
            )
    return out


# ── Metrics ─────────────────────────────────────────────────────────
def metric_ic(scores: np.ndarray, returns: np.ndarray) -> tuple:
    """Spearman IC + p-value. NaN-safe."""
    mask = ~(np.isnan(scores) | np.isnan(returns))
    if mask.sum() < 10:
        return np.nan, np.nan, int(mask.sum())
    rho, p = spearmanr(scores[mask], returns[mask])
    return rho, p, int(mask.sum())


def metric_sign_accuracy(scores: np.ndarray, returns: np.ndarray) -> dict:
    """Accuracy(sign(score) == sign(return)) overall."""
    mask = ~(np.isnan(scores) | np.isnan(returns)) & (scores != 0) & (returns != 0)
    if mask.sum() == 0:
        return {"n": 0, "accuracy": np.nan}
    matches = np.sign(scores[mask]) == np.sign(returns[mask])
    return {"n": int(mask.sum()), "accuracy": float(matches.mean())}


def metric_sign_acc_conditional(scores: np.ndarray, returns: np.ndarray,
                                  bins=((1, 1), (2, 2), (3, 4), (5, 7), (8, 10))) -> list:
    """Sign accuracy conditioned on |score| bin. THE key test of the quant
    review section 2.2: if accuracy does NOT rise with magnitude, the boundary
    mapping (+3 → ALCISTA etc) is decorative."""
    out = []
    mask = ~(np.isnan(scores) | np.isnan(returns)) & (returns != 0)
    abs_s = np.abs(scores)
    for lo, hi in bins:
        in_bin = mask & (abs_s >= lo) & (abs_s <= hi) & (scores != 0)
        n = int(in_bin.sum())
        if n == 0:
            out.append({"range": f"|s|∈[{lo},{hi}]", "n": 0, "accuracy": np.nan})
            continue
        matches = np.sign(scores[in_bin]) == np.sign(returns[in_bin])
        out.append({"range": f"|s|∈[{lo},{hi}]", "n": n, "accuracy": float(matches.mean())})
    return out


def metric_baseline_lift(scores: np.ndarray, returns: np.ndarray,
                          prev_returns: np.ndarray) -> dict:
    """Compare score's sign accuracy vs the trivial baseline:
    predict next return's sign = sign(previous return). The 12-factor score
    has to beat this to justify its complexity."""
    mask = (~(np.isnan(scores) | np.isnan(returns) | np.isnan(prev_returns))
            & (scores != 0) & (returns != 0) & (prev_returns != 0))
    if mask.sum() == 0:
        return {"n": 0, "score_acc": np.nan, "baseline_acc": np.nan, "lift_pp": np.nan}
    score_acc = (np.sign(scores[mask]) == np.sign(returns[mask])).mean()
    base_acc = (np.sign(prev_returns[mask]) == np.sign(returns[mask])).mean()
    return {"n": int(mask.sum()), "score_acc": float(score_acc),
            "baseline_acc": float(base_acc),
            "lift_pp": float((score_acc - base_acc) * 100)}


# ── Report ──────────────────────────────────────────────────────────
def _fmt(v, fmt=".4f"):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    return format(v, fmt)


def report(log: pd.DataFrame, horizons_h: list) -> None:
    print("═" * 76)
    print("Market State Score — Magnitude Validation Report")
    print("═" * 76)
    print(f"Log snapshots: {len(log):,}")
    if len(log):
        print(f"  Date range: {log['ts'].min()} → {log['ts'].max()}")
        days_span = (log["ts"].max() - log["ts"].min()).total_seconds() / 86400
        print(f"  Span: {days_span:.1f} days")

    if len(log) < 50:
        print()
        print("⚠ Too few snapshots for meaningful statistics.")
        print(f"  Need at least 50 (currently {len(log)}). Wait for ~2-4 weeks of usage")
        print(f"  with a varied score distribution before drawing conclusions.")
        return

    # ── IC by horizon ─────────────────────────────────────────────
    print()
    print("── 1. Information Coefficient (Spearman, score vs forward return) ──")
    print(f"{'horizon':<10s}{'IC':>10s}{'p-value':>12s}{'n':>10s}")
    for h in horizons_h:
        col = f"ret_{h}h"
        if col not in log.columns:
            continue
        rho, p, n = metric_ic(log["score"].values, log[col].values)
        print(f"{h:>4}h     {_fmt(rho, '+.4f'):>10s}  {_fmt(p, '.4f'):>10s}  {n:>10,}")
    print("  Reading: |IC| ≥ 0.03 stable across regimes is genuinely exploitable.")
    print("  |IC| > 0.10 sustained → suspect leakage; review.")

    # ── Sign accuracy conditional on |score| ─────────────────────
    print()
    print("── 2. Sign accuracy CONDITIONED on |score| (the key test) ──")
    print("    Hypothesis: accuracy should RISE monotonically with |score|.")
    print()
    for h in horizons_h:
        col = f"ret_{h}h"
        if col not in log.columns:
            continue
        bins = metric_sign_acc_conditional(log["score"].values, log[col].values)
        print(f"  Horizon {h}h:")
        for b in bins:
            acc_str = _fmt(b["accuracy"], ".3f")
            bar_n = int(b["accuracy"] * 30) if not np.isnan(b["accuracy"]) else 0
            bar = "█" * bar_n + "░" * (30 - bar_n) if bar_n else ""
            print(f"    {b['range']:<10s}  n={b['n']:>5}  acc={acc_str}  {bar}")
        # Verdict
        accs = [b["accuracy"] for b in bins if not np.isnan(b["accuracy"]) and b["n"] >= 5]
        if len(accs) >= 2:
            monotonic = all(accs[i+1] >= accs[i] for i in range(len(accs)-1))
            if monotonic:
                print(f"    ✓ Accuracy rises monotonically with |score| → magnitude IS informative")
            else:
                print(f"    ⚠ Not monotonic → magnitude semantics may need recalibration")
        print()

    # ── IC decay ─────────────────────────────────────────────────
    print("── 3. IC decay by horizon ──")
    ics = []
    for h in horizons_h:
        col = f"ret_{h}h"
        if col not in log.columns:
            continue
        rho, _, n = metric_ic(log["score"].values, log[col].values)
        ics.append((h, rho, n))
    if ics:
        ic_at_1 = ics[0][1] if not np.isnan(ics[0][1]) else None
        for h, ic, n in ics:
            half_str = ""
            if ic_at_1 and ic and abs(ic) < abs(ic_at_1) * 0.5:
                half_str = "  ← signal half-life"
            print(f"  {h:>4}h  IC = {_fmt(ic, '+.4f'):>10s}{half_str}")
    print("  Reading: IC decay → signal lives short. Persistent → structural.")

    # ── Baseline lift ────────────────────────────────────────────
    print()
    print("── 4. Lift over baseline (sign of previous bar's return) ──")
    base_return_col = f"ret_1h"  # the previous bar = ret over -1h
    if "score" in log.columns and base_return_col in log.columns:
        prev_returns = log[base_return_col].shift(1).values
        for h in horizons_h:
            col = f"ret_{h}h"
            if col not in log.columns:
                continue
            r = metric_baseline_lift(log["score"].values, log[col].values, prev_returns)
            print(f"  {h:>4}h  n={r['n']:>5}  score={_fmt(r['score_acc'], '.3f')}  "
                  f"baseline={_fmt(r['baseline_acc'], '.3f')}  "
                  f"lift={_fmt(r['lift_pp'], '+.2f'):>7s}pp")
    print("  Reading: lift > 0pp → score adds value over the trivial predictor.")

    # ── Modulator A/B test (pre vs post) ─────────────────────────
    if "scorePreModulator" in log.columns and "scorePostModulator" in log.columns:
        print()
        print("── 5. Pre vs Post modulator (P1 A/B test) ──")
        pre = log["scorePreModulator"].values
        post = log["scorePostModulator"].values
        for h in horizons_h:
            col = f"ret_{h}h"
            if col not in log.columns:
                continue
            ic_pre, _, n = metric_ic(pre, log[col].values)
            ic_post, _, _ = metric_ic(post, log[col].values)
            delta = ic_post - ic_pre if not (np.isnan(ic_post) or np.isnan(ic_pre)) else np.nan
            print(f"  {h:>4}h  IC pre={_fmt(ic_pre, '+.4f'):>10s}  "
                  f"post={_fmt(ic_post, '+.4f'):>10s}  Δ={_fmt(delta, '+.4f'):>10s}")
        print("  Reading: Δ > 0 → modulator HELPED. < 0 → it hurt; investigate or roll back.")


# ── Main ────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizons", type=str, default=",".join(str(h) for h in DEFAULT_HORIZONS),
                    help="Comma-separated forward-return horizons in hours")
    ap.add_argument("--invert", action="store_true",
                    help="Negate the score before measuring. Tests the 'score is "
                         "contrarian' hypothesis — if the score is anti-predictive "
                         "in normal mode, --invert should produce positive IC.")
    ap.add_argument("--source", choices=["all", "live", "historical"], default="all",
                    help="Filter log source. 'historical' = only _historical_backtest "
                         "snapshots, 'live' = real-time logged snapshots, 'all' = both.")
    args = ap.parse_args()
    horizons_h = [int(h) for h in args.horizons.split(",") if h.strip()]

    log = load_log()
    if log.empty:
        print("No state log data found.")
        print(f"  Expected JSONL files in: {STATE_LOG_DIR}")
        print("  Run the local backend + open the dashboard for at least a few hours")
        print("  to accumulate snapshots, then re-run this script.")
        return

    # Source filter: split historical vs live by the `_historical` flag
    if args.source != "all":
        if "_historical" not in log.columns:
            log["_historical"] = False
        if args.source == "historical":
            log = log[log["_historical"] == True].reset_index(drop=True)
        else:  # live
            log = log[log["_historical"].isna() | (log["_historical"] == False)].reset_index(drop=True)
        print(f"  [filter] source={args.source} → {len(log)} snapshots", file=sys.stderr)

    if args.invert:
        # Negate all score columns. Tests the contrarian hypothesis: if the score
        # is anti-predictive in the forward direction, inverting it should
        # produce positive IC. This is a falsifiable test of "interpretation A"
        # (semantic mapping is reversed) from the empirical findings doc.
        for col in ("score", "scorePreModulator", "scorePostModulator"):
            if col in log.columns:
                log[col] = -log[col]
        print(f"  [invert] Score sign flipped — testing contrarian hypothesis", file=sys.stderr)

    print(f"Loading klines for forward returns ...", file=sys.stderr)
    klines = load_klines_1h()
    print(f"  klines range: {klines.index.min()} → {klines.index.max()}", file=sys.stderr)

    log = attach_forward_returns(log, klines, horizons_h)
    report(log, horizons_h)


if __name__ == "__main__":
    main()
