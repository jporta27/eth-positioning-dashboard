"""
CLI for the HMM K=4 regime classifier.

Usage:
    python scripts/run_regime_classifier.py --refit           # full retrain, save model
    python scripts/run_regime_classifier.py                   # classify latest, print JSON
    python scripts/run_regime_classifier.py --refit-if-stale  # smart: refit only if model > 7 days old

Reads:
    data/backfill/binance_klines_1h.parquet (resampled to 4h)
    data/backfill/binance_funding.parquet   (ffilled to 4h)
    data/backfill/macro.parquet             (BTC + VIX, ffilled to 4h)

Writes:
    data/regime/model.pkl  — the fitted HMM artifact

Refit schedule: weekly cron or manual. See methodology MD in
docs/obsidian/03-metrics/Regime classifier (HMM K=4).md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
from backtest.regime_classifier import (  # noqa: E402
    RegimeClassifier, build_features, REFIT_EVERY_DAYS,
)

KLINES_PATH = os.path.join(REPO_ROOT, "data", "backfill", "binance_klines_1h.parquet")
FUNDING_PATH = os.path.join(REPO_ROOT, "data", "backfill", "binance_funding.parquet")
MACRO_PATH = os.path.join(REPO_ROOT, "data", "backfill", "macro.parquet")
MODEL_PATH = os.path.join(REPO_ROOT, "data", "regime", "model.pkl")
SNAPSHOT_PATH = os.path.join(REPO_ROOT, "data", "regime", "latest.json")


def load_klines_4h() -> pd.DataFrame:
    t = pq.read_table(KLINES_PATH)
    df = t.to_pandas()[["ts_utc_ms", "open", "high", "low", "close", "volume",
                        "taker_buy_base"]].sort_values("ts_utc_ms")
    df["ts"] = pd.to_datetime(df["ts_utc_ms"], unit="ms", utc=True)
    df = df.set_index("ts")
    g = df.resample("4h")
    out = pd.DataFrame({
        "open": g["open"].first(), "high": g["high"].max(),
        "low": g["low"].min(), "close": g["close"].last(),
        "volume": g["volume"].sum(), "taker_buy_base": g["taker_buy_base"].sum(),
    }).dropna()
    out["log_return"] = np.log(out["close"]).diff()
    out["taker_buy_imbalance"] = (
        out["taker_buy_base"] - (out["volume"] - out["taker_buy_base"])
    ) / out["volume"]
    return out


def load_funding_4h() -> pd.Series:
    t = pq.read_table(FUNDING_PATH)
    df = t.to_pandas()[["ts_utc_ms", "funding_rate"]].sort_values("ts_utc_ms")
    df["ts"] = pd.to_datetime(df["ts_utc_ms"], unit="ms", utc=True)
    df = df.set_index("ts")
    return df["funding_rate"].resample("4h").ffill()


def load_macro_4h() -> pd.DataFrame:
    t = pq.read_table(MACRO_PATH)
    df = t.to_pandas()[["ts_utc_ms", "label", "close"]].sort_values("ts_utc_ms")
    df["ts"] = pd.to_datetime(df["ts_utc_ms"], unit="ms", utc=True)
    btc = df[df["label"] == "BTC"].set_index("ts")["close"].rename("btc_close")
    vix = df[df["label"] == "VIX"].set_index("ts")["close"].rename("vix_close")
    return pd.concat([btc.resample("4h").ffill(), vix.resample("4h").ffill()], axis=1)


def build_full_features() -> pd.DataFrame:
    return build_features(load_klines_4h(), load_funding_4h(), load_macro_4h())


def cmd_refit() -> None:
    print(f"[refit] Loading data + building features ...", file=sys.stderr)
    F = build_full_features()
    print(f"[refit] Features: {F.shape}  range {F.index.min()} → {F.index.max()}", file=sys.stderr)
    print(f"[refit] Fitting K=4 HMM with 20 random restarts ...", file=sys.stderr)
    clf = RegimeClassifier.fit(F)
    clf.save(MODEL_PATH)
    print(f"[refit] Saved to {MODEL_PATH}", file=sys.stderr)
    print(f"[refit] train_n_obs={clf.artifact.train_n_obs}  train_logL={clf.artifact.train_logL:.1f}", file=sys.stderr)
    print(f"[refit] train_start={clf.artifact.train_start}  train_end={clf.artifact.train_end}", file=sys.stderr)
    print(f"[refit] labels_by_idx={clf.artifact.labels_by_idx}", file=sys.stderr)


def cmd_classify(refit_if_stale: bool = False, save_json: bool = True,
                 quiet: bool = False) -> dict:
    if not os.path.exists(MODEL_PATH):
        if not refit_if_stale:
            raise SystemExit(f"Model not found at {MODEL_PATH}. Run --refit first.")
        print(f"[classify] No model on disk; refitting ...", file=sys.stderr)
        cmd_refit()
    clf = RegimeClassifier.load(MODEL_PATH)
    if refit_if_stale and clf.needs_refit(REFIT_EVERY_DAYS):
        print(f"[classify] Model is stale (> {REFIT_EVERY_DAYS}d); refitting ...", file=sys.stderr)
        cmd_refit()
        clf = RegimeClassifier.load(MODEL_PATH)
    F = build_full_features()
    # Pass the last 1 year of features for the forward-backward to have
    # enough context. 1 year ≈ 2190 4h bars.
    recent = F.iloc[-2190:]
    out = clf.classify(recent)
    if save_json:
        os.makedirs(os.path.dirname(SNAPSHOT_PATH), exist_ok=True)
        with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
        if not quiet:
            print(f"[classify] Snapshot written to {SNAPSHOT_PATH}", file=sys.stderr)
    if not quiet:
        print(json.dumps(out, indent=2))
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--refit", action="store_true", help="Full retrain on latest window")
    p.add_argument("--refit-if-stale", action="store_true",
                   help="Refit only if model is older than REFIT_EVERY_DAYS")
    p.add_argument("--no-save", action="store_true",
                   help="Don't write the snapshot JSON (default: write to data/regime/latest.json)")
    p.add_argument("--quiet", action="store_true", help="Suppress JSON stdout (still writes file)")
    args = p.parse_args()
    if args.refit:
        cmd_refit()
        # Always re-classify after a refit so the snapshot reflects the new model
        cmd_classify(save_json=not args.no_save, quiet=args.quiet)
    else:
        cmd_classify(refit_if_stale=args.refit_if_stale,
                     save_json=not args.no_save,
                     quiet=args.quiet)


if __name__ == "__main__":
    main()
