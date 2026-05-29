"""
Event study: stochastic mean-reversion triggers, filtered by confluence.

This is the CORRECT test of the Market State Score, replacing the flawed
panel-regression in validate_score_magnitude.py. See the session log
'Session 2026-05 — Whale tracking, HMM regime, Score validation.md' for why
the old approach was wrong.

THE USER'S ACTUAL OPERATIVA:
  - The stochastic gives the TRIGGER (mean-reversion: oversold→long, overbought→short)
  - The rest of the factors are a CONFLUENCE FILTER — mean-reversion blind loses
    in trends, so you only take the trigger when context says the bounce is likely.

So the score was never a per-bar direction generator. It's a filter on triggers.
Measuring it on all 1441 bars (90% of which have no trigger) averaged the real
signal with contrarian noise → spurious negative IC. This script fixes that.

THREE CHAINED MEASUREMENTS:
  1. BASELINE   — do the triggers alone have edge? (all triggers, return in trade
                  direction). If the raw trigger already wins, the filter is moot.
                  If it loses, the filter is where the edge must come from.
  2. ATTRIBUTION — for each confluence factor, split triggers into "confirms" vs
                  "contradicts" the trade direction. If confirms >> contradicts,
                  that factor is a good filter. Tells us which filter adds value.
  3. CONFLUENCE — do triggers where MORE factors confirm out-perform triggers where
                  few confirm? Validates the confluence idea itself.

Trigger definition (mirrors factor_stoch in Dashboard.jsx / backtest_score_historical.py):
  Long  (OS):  fast %K crosses ABOVE fast %D, fast %K < 30, slow %K < 40
  Short (OB):  fast %K crosses BELOW fast %D, fast %K > 70, slow %K > 60

Causality: trigger at bar t uses data through close[t]. Forward return is t+h.
No lookahead. Dedup avoids counting the same bounce multiple times.

CAVEAT: only the 5 historically-reconstructable confluence factors are tested
(funding, taker, vol, ETH/BTC, VP-proxy). The other 6 (OI, L/S, options, gamma,
MQ, setup) need persistence we never had. The setup detector (factor 12) is the
real-world trigger refinement and is NOT reconstructable — so this measures a
SIMPLIFIED trigger (raw stoch cross) filtered by SIMPLIFIED confluence. Findings
are directional, not the final word on the live 12-factor system.

Run:
    python scripts/event_study_stoch_triggers.py --tf 4h --horizons 4,12,24,48 --dedup-h 12
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import warnings

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

sys.stdout.reconfigure(encoding="utf-8")
warnings.filterwarnings("ignore", category=RuntimeWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

# Reuse the EXACT factor computations + loaders from the historical backtest
from backtest_score_historical import (  # noqa: E402
    load_klines, load_funding, load_macro_btc,
    compute_stoch_series,
    factor_funding, factor_taker, factor_vol_amplifier, factor_ethbtc, factor_vp,
    W,
)

# Stoch trigger thresholds (mirror Dashboard.jsx factor_stoch)
OS_FAST_MAX = 30      # fast %K must be below this for a long (oversold) trigger
OS_SLOW_MAX = 40      # slow %K must be below this (régimen agrees)
OB_FAST_MIN = 70      # fast %K above this for a short (overbought) trigger
OB_SLOW_MIN = 60      # slow %K above this


def resample_klines(klines_1h: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Resample 1h klines to the trade timeframe. tf in {'1h','4h','12h','1d'}."""
    if tf == "1h":
        out = klines_1h.copy()
    else:
        g = klines_1h.resample(tf)
        out = pd.DataFrame({
            "open": g["open"].first(), "high": g["high"].max(),
            "low": g["low"].min(), "close": g["close"].last(),
            "volume": g["volume"].sum(), "taker_buy_base": g["taker_buy_base"].sum(),
        }).dropna()
    return out


def bars_for_hours(h: int, tf: str) -> int:
    """Convert a horizon in hours to a number of bars of the given TF."""
    tf_hours = {"1h": 1, "4h": 4, "12h": 12, "1d": 24}[tf]
    return max(1, round(h / tf_hours))


def build_factor_series(klines: pd.DataFrame, tf: str) -> dict:
    """Compute each confluence factor's RAW input series, causally, on the TF grid."""
    close = klines["close"].values
    high = klines["high"].values
    low = klines["low"].values
    volume = klines["volume"].values
    taker_buy = klines["taker_buy_base"].values

    # Funding + BTC aligned to this index
    funding = load_funding(klines.index).values
    btc = load_macro_btc(klines.index).values

    # Realized vol ratio (short vs long) for the vol amplifier
    log_ret = np.concatenate([[np.nan], np.log(close[1:] / close[:-1])])
    rv_long = pd.Series(log_ret).rolling(24).std().values
    rv_short = pd.Series(log_ret).rolling(2).std().values
    vol_ratio = np.where(rv_long > 0, rv_short / rv_long, np.nan)

    # Taker ratio (buy/sell)
    sell = volume - taker_buy
    taker_ratio = np.where(sell > 0, taker_buy / sell, np.nan)

    # ETH/BTC 24h change (in bars)
    eth_btc = close / btc
    bars_24h = bars_for_hours(24, tf)
    eth_btc_chg = pd.Series(eth_btc).pct_change(bars_24h).values * 100.0

    # VWAP-based VP proxy (24h window in bars)
    typ = (high + low + close) / 3.0
    pv = typ * volume
    win = bars_24h
    vwap = (pd.Series(pv).rolling(win).sum().values /
            pd.Series(volume).rolling(win).sum().values)
    vwap_std = pd.Series(close).rolling(win).std().values

    return {
        "close": close, "funding": funding, "taker_ratio": taker_ratio,
        "vol_ratio": vol_ratio, "eth_btc_chg": eth_btc_chg,
        "vwap": vwap, "vwap_std": vwap_std,
    }


def identify_triggers(klines: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Find all mean-reversion stoch triggers. Returns DataFrame with columns:
    pos (int index), ts, side ('long'/'short')."""
    high = klines["high"].values
    low = klines["low"].values
    close = klines["close"].values
    slow_k, slow_d = compute_stoch_series(high, low, close, 400, 10, 40)
    fast_k, fast_d = compute_stoch_series(high, low, close, 100, 4, 10)

    triggers = []
    n = len(close)
    for i in range(1, n):
        sk, fk, fd = slow_k[i], fast_k[i], fast_d[i]
        fk_prev, fd_prev = fast_k[i - 1], fast_d[i - 1]
        if any(np.isnan(x) for x in (sk, fk, fd, fk_prev, fd_prev)):
            continue
        # Long: fast crosses up, both fast & slow in OS zone
        cross_up = fk > fd and fk_prev <= fd_prev
        cross_dn = fk < fd and fk_prev >= fd_prev
        if cross_up and fk < OS_FAST_MAX and sk < OS_SLOW_MAX:
            triggers.append({"pos": i, "ts": klines.index[i], "side": "long"})
        elif cross_dn and fk > OB_FAST_MIN and sk > OB_SLOW_MIN:
            triggers.append({"pos": i, "ts": klines.index[i], "side": "short"})
    return pd.DataFrame(triggers)


def dedup_triggers(triggers: pd.DataFrame, dedup_bars: int) -> pd.DataFrame:
    """Drop triggers of the SAME side within `dedup_bars` of a kept one — so the
    same bounce isn't counted multiple times. Long and short have separate timers."""
    if triggers.empty:
        return triggers
    kept = []
    last_pos = {"long": -10**9, "short": -10**9}
    for _, row in triggers.iterrows():
        if row["pos"] - last_pos[row["side"]] >= dedup_bars:
            kept.append(row)
            last_pos[row["side"]] = row["pos"]
    return pd.DataFrame(kept).reset_index(drop=True)


def forward_return(close: np.ndarray, pos: int, h_bars: int, side: str) -> float:
    """Return in the DIRECTION OF THE TRADE. Positive = trade won."""
    if pos + h_bars >= len(close):
        return np.nan
    raw = close[pos + h_bars] / close[pos] - 1.0
    return raw if side == "long" else -raw


def factor_confluence_sign(factor_name: str, pos: int, F: dict, tf: str) -> int:
    """Signed contribution of a confluence factor at the trigger bar.
    +1 = factor leans bullish, -1 = bearish, 0 = neutral. We use a weight of 1
    and read the SIGN (the magnitude/weighting is the score's job; here we only
    ask 'does this factor confirm the trade direction?')."""
    if factor_name == "funding":
        c = factor_funding(F["funding"][pos], 1)
    elif factor_name == "taker":
        c = factor_taker(F["taker_ratio"][pos], 1, with_modulator=False)
    elif factor_name == "ethbtc":
        c = factor_ethbtc(F["eth_btc_chg"][pos], 1)
    elif factor_name == "vp":
        c, _ = factor_vp(F["close"][pos], F["vwap"][pos], F["vwap_std"][pos], 1)
    elif factor_name == "vol":
        # vol is an amplifier, not directional on its own — skip in attribution
        return 0
    else:
        return 0
    return int(np.sign(c))


CONFLUENCE_FACTORS = ["funding", "taker", "ethbtc", "vp"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="4h", choices=["1h", "4h", "12h", "1d"])
    ap.add_argument("--horizons", default="4,12,24,48", help="Forward horizons in hours")
    ap.add_argument("--dedup-h", type=int, default=12, help="Min hours between same-side triggers")
    ap.add_argument("--days", type=int, default=0, help="0 = all history; else last N days")
    args = ap.parse_args()
    horizons_h = [int(x) for x in args.horizons.split(",")]

    print("=" * 78)
    print(f"EVENT STUDY — stoch mean-reversion triggers + confluence filter (TF={args.tf})")
    print("=" * 78)

    klines_1h = load_klines()
    klines = resample_klines(klines_1h, args.tf)
    if args.days > 0:
        cutoff = klines.index.max() - pd.Timedelta(days=args.days)
        klines = klines[klines.index >= cutoff]
    print(f"\nklines {args.tf}: {len(klines):,} bars  {klines.index.min()} → {klines.index.max()}")

    F = build_factor_series(klines, args.tf)
    close = F["close"]

    triggers = identify_triggers(klines, args.tf)
    dedup_bars = bars_for_hours(args.dedup_h, args.tf)
    triggers = dedup_triggers(triggers, dedup_bars)
    n_long = (triggers["side"] == "long").sum() if not triggers.empty else 0
    n_short = (triggers["side"] == "short").sum() if not triggers.empty else 0
    print(f"Triggers (after dedup {args.dedup_h}h): {len(triggers)}  "
          f"({n_long} long, {n_short} short)")
    if len(triggers) < 20:
        print("⚠ Too few triggers for meaningful stats. Try a shorter TF or looser zones.")
        if triggers.empty:
            return

    # Pre-compute confluence signs per trigger (factor → list of signs)
    conf_signs = {f: [] for f in CONFLUENCE_FACTORS}
    for _, row in triggers.iterrows():
        for f in CONFLUENCE_FACTORS:
            conf_signs[f].append(factor_confluence_sign(f, row["pos"], F, args.tf))
    for f in CONFLUENCE_FACTORS:
        triggers[f"conf_{f}"] = conf_signs[f]

    # dir_sign: +1 for long, -1 for short — a factor "confirms" if its sign matches
    triggers["dir_sign"] = np.where(triggers["side"] == "long", 1, -1)

    # ── 1. BASELINE ─────────────────────────────────────────────────
    print("\n" + "─" * 78)
    print("1. BASELINE — do the raw triggers have edge? (return in trade direction)")
    print("─" * 78)
    print(f"  {'horizon':>8} {'n':>5} {'mean_ret':>10} {'median':>9} {'hit_rate':>9} {'long_hit':>9} {'short_hit':>10}")
    baseline_rets = {}
    for h in horizons_h:
        hb = bars_for_hours(h, args.tf)
        rets, sides = [], []
        for _, row in triggers.iterrows():
            r = forward_return(close, row["pos"], hb, row["side"])
            if not np.isnan(r):
                rets.append(r); sides.append(row["side"])
        rets = np.array(rets); sides = np.array(sides)
        baseline_rets[h] = (rets, sides)
        if len(rets) == 0:
            continue
        hit = (rets > 0).mean()
        long_hit = (rets[sides == "long"] > 0).mean() if (sides == "long").any() else np.nan
        short_hit = (rets[sides == "short"] > 0).mean() if (sides == "short").any() else np.nan
        print(f"  {h:>6}h {len(rets):>5} {rets.mean()*100:>9.3f}% {np.median(rets)*100:>8.3f}% "
              f"{hit*100:>8.1f}% {long_hit*100:>8.1f}% {short_hit*100:>9.1f}%")
    print("  → hit_rate > 50% & mean_ret > 0 = raw mean-reversion trigger has edge.")
    print("    If ~50% or negative, the edge must come from the confluence filter.")

    # ── 2. ATTRIBUTION ──────────────────────────────────────────────
    print("\n" + "─" * 78)
    print("2. ATTRIBUTION — does each confluence factor improve the trigger?")
    print("─" * 78)
    print("   For each factor: mean return when it CONFIRMS the trade direction vs CONTRADICTS.")
    primary_h = horizons_h[len(horizons_h) // 2]  # middle horizon as the headline
    hb = bars_for_hours(primary_h, args.tf)
    print(f"   (headline horizon = {primary_h}h)\n")
    print(f"  {'factor':>10} {'confirm_n':>10} {'confirm_ret':>12} {'contra_n':>9} {'contra_ret':>11} {'edge_pp':>9}")
    rets_primary, _ = baseline_rets.get(primary_h, (np.array([]), None))
    # recompute aligned rets with row mask
    row_rets = np.array([forward_return(close, r["pos"], hb, r["side"])
                         for _, r in triggers.iterrows()])
    valid = ~np.isnan(row_rets)
    for f in CONFLUENCE_FACTORS:
        sign = triggers[f"conf_{f}"].values
        confirms = valid & (sign == triggers["dir_sign"].values) & (sign != 0)
        contra = valid & (sign == -triggers["dir_sign"].values) & (sign != 0)
        cn, kn = int(confirms.sum()), int(contra.sum())
        cret = row_rets[confirms].mean() * 100 if cn > 0 else np.nan
        kret = row_rets[contra].mean() * 100 if kn > 0 else np.nan
        edge = (cret - kret) if (cn > 0 and kn > 0) else np.nan
        cret_s = f"{cret:>11.3f}%" if not np.isnan(cret) else f"{'—':>12}"
        kret_s = f"{kret:>10.3f}%" if not np.isnan(kret) else f"{'—':>11}"
        edge_s = f"{edge:>+8.2f}" if not np.isnan(edge) else f"{'—':>9}"
        print(f"  {f:>10} {cn:>10} {cret_s} {kn:>9} {kret_s} {edge_s}")
    print("\n  → edge_pp > 0 = factor's confirmation improves the trigger. The bigger,")
    print("    the better a filter it is. ≤ 0 = that factor is noise (or inverted).")

    # ── 3. CONFLUENCE COUNT ─────────────────────────────────────────
    print("\n" + "─" * 78)
    print("3. CONFLUENCE — more factors confirming = better edge?")
    print("─" * 78)
    # n_confirm per trigger = how many factors confirm the direction
    n_confirm = np.zeros(len(triggers), dtype=int)
    for f in CONFLUENCE_FACTORS:
        sign = triggers[f"conf_{f}"].values
        n_confirm += (sign == triggers["dir_sign"].values) & (sign != 0)
    triggers["n_confirm"] = n_confirm
    print(f"   (headline horizon = {primary_h}h, {len(CONFLUENCE_FACTORS)} factors max)\n")
    print(f"  {'n_confirm':>10} {'n_trig':>7} {'mean_ret':>10} {'hit_rate':>9}")
    for nc in range(0, len(CONFLUENCE_FACTORS) + 1):
        mask = valid & (triggers["n_confirm"].values == nc)
        if mask.sum() == 0:
            print(f"  {nc:>10} {0:>7} {'—':>10} {'—':>9}")
            continue
        r = row_rets[mask]
        print(f"  {nc:>10} {mask.sum():>7} {r.mean()*100:>9.3f}% {(r > 0).mean()*100:>8.1f}%")
    print("\n  → if mean_ret/hit_rate rise with n_confirm, the confluence filter works:")
    print("    taking only high-confluence triggers beats taking all of them.")

    # ── 4. VOLUME / EFFECTIVE-VOLUME CONTEXT ────────────────────────
    # The user's confluence centres on FLOW normalised by volume. On-chain
    # netflow has no history, but volumen operado (total) and volumen efectivo
    # (taker delta = directional volume) DO. Question: does the volume context
    # at the trigger bar improve the mean-reversion outcome?
    #   vol_rel    = volume[t] / median(volume, 90 bars)   → climax vs drift
    #   taker_imb  = (2*taker_buy − volume) / volume        → effective/total ratio
    print("\n" + "─" * 78)
    print("4. VOLUME CONTEXT — does volume at the trigger improve mean-reversion?")
    print("─" * 78)
    volume = klines["volume"].values
    taker_buy = klines["taker_buy_base"].values
    vol_med = pd.Series(volume).rolling(90).median().values
    vol_rel = np.where(vol_med > 0, volume / vol_med, np.nan)
    taker_imb = np.where(volume > 0, (2 * taker_buy - volume) / volume, np.nan)

    pos_arr = triggers["pos"].values
    side_arr = triggers["side"].values
    vr_at = vol_rel[pos_arr]
    ti_at = taker_imb[pos_arr]
    # effective-volume confirmation: for a long (oversold), aggressive SELLING
    # at the trigger (taker_imb < 0) = sellers still dominant. Does climax-selling
    # (high vol + strong negative imb on a long) rebound better, or worse?
    dir_sign = triggers["dir_sign"].values

    print(f"   (headline horizon = {primary_h}h)\n")
    print("  A) By volume relative to 90-bar median (climax vs drift):")
    print(f"  {'vol_rel bucket':>16} {'n':>5} {'mean_ret':>10} {'hit_rate':>9}")
    for lab, lo, hi in [("low <0.8", 0, 0.8), ("normal 0.8-1.5", 0.8, 1.5),
                         ("high 1.5-3", 1.5, 3.0), ("climax >3", 3.0, 1e9)]:
        mask = valid & (vr_at >= lo) & (vr_at < hi)
        if mask.sum() == 0:
            print(f"  {lab:>16} {0:>5} {'—':>10} {'—':>9}"); continue
        r = row_rets[mask]
        print(f"  {lab:>16} {mask.sum():>5} {r.mean()*100:>9.3f}% {(r > 0).mean()*100:>8.1f}%")

    print("\n  B) By taker imbalance AGAINST the trade at the trigger")
    print("     (long: sellers still aggressive = imb<0; short: buyers aggressive = imb>0):")
    print(f"  {'imb vs trade':>16} {'n':>5} {'mean_ret':>10} {'hit_rate':>9}")
    # imb_against = taker imbalance opposing the trade direction (capitulation proxy)
    imb_against = -ti_at * dir_sign  # >0 means flow still pushing against the trade
    # Buckets on the real scale of perp taker imbalance (typically ±0.02-0.08)
    for lab, lo, hi in [("aligned <0", -1e9, 0), ("mild 0-0.03", 0, 0.03),
                        ("strong 0.03-0.06", 0.03, 0.06), ("extreme >0.06", 0.06, 1e9)]:
        mask = valid & (imb_against >= lo) & (imb_against < hi)
        if mask.sum() == 0:
            print(f"  {lab:>16} {0:>5} {'—':>10} {'—':>9}"); continue
        r = row_rets[mask]
        print(f"  {lab:>16} {mask.sum():>5} {r.mean()*100:>9.3f}% {(r > 0).mean()*100:>8.1f}%")
    print("\n  → if 'climax' / 'extreme against' rows rebound better, the user's")
    print("    intuition (enter mean-rev on exhaustion, filtered by volume) holds")
    print("    for the RECONSTRUCTABLE part. The on-chain netflow is still untested")
    print("    (no history) — only the logger captures it going forward.")

    # ── Verdict ─────────────────────────────────────────────────────
    print("\n" + "=" * 78)
    print("NOTES")
    print("=" * 78)
    print(f"  • Horizons ≥ dedup ({args.dedup_h}h) have OVERLAPPING forward windows →")
    print(f"    observations not independent, p-values would be optimistic. Treat")
    print(f"    mean_ret as descriptive, not inferential, at long horizons.")
    print(f"  • Only {len(CONFLUENCE_FACTORS)} reconstructable confluence factors tested")
    print(f"    (funding, taker, ethbtc, vp). Live system has 6 more incl. the cut-")
    print(f"    anchored MQ setup filter — the real-world trigger refinement.")
    print(f"  • Trigger here = raw stoch cross. Live = setup detector (stoch + MQ).")
    print(f"    So this is a LOWER BOUND on what the full filtered system can do.")


if __name__ == "__main__":
    main()
