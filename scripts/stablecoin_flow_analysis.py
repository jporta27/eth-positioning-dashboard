"""
Stablecoin issuance flow analysis — does USDT 'dry powder' predict ETH?

Validates BEFORE integrating: track net USDT issuance from Tether Treasury to
the market, build a daily series, and test whether it leads ETH returns. If it
has no predictive power, don't bother wiring a panel.

Why this is clean (unlike the aggregate CEX-netflow #1): the Tether Treasury is
ONE known address with ~100 txns/month — no hundreds-of-wallets exclusion
problem. Etherscan tokentx gives its full history for free.

SEMANTICS:
  Treasury ← USDT contract        = fresh mint (Tether printed) → powder created
  Treasury → exchange/market       = issuance (powder activating) → fuel in
  Treasury ← exchange/market       = redemption (USDT returning) → fuel out
  net_to_market = (out to market) − (in from market), excluding contract mints

  net_to_market > 0  → stablecoin flowing into the market = bullish dry powder
  net_to_market < 0  → redemptions = capital leaving

Run:
    python scripts/stablecoin_flow_analysis.py --days 365
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import httpx
from scipy.stats import spearmanr

sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KLINES_PATH = os.path.join(REPO_ROOT, "data", "backfill", "binance_klines_1h.parquet")
ETHERSCAN_API_URL = "https://api.etherscan.io/v2/api"

USDT_CONTRACT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
TETHER_TREASURY = "0x5754284f345afc66a98fbb0a0afe71e0f007b949"
USDT_DECIMALS = 6
PAGE_SIZE = 10000
THROTTLE_S = 0.25


def get_key() -> str:
    env_path = os.path.join(REPO_ROOT, "backend", ".env")
    if os.path.exists(env_path):
        for line in open(env_path):
            if line.startswith("ETHERSCAN_API_KEY="):
                return line.split("=", 1)[1].strip()
    return os.getenv("ETHERSCAN_API_KEY", "")


def fetch_treasury_usdt(api_key: str, oldest_ts: int) -> pd.DataFrame:
    """Paginate tokentx of the Tether Treasury for USDT, back to oldest_ts."""
    rows = []
    end_block = 99_999_999
    while True:
        params = {
            "chainid": 1, "module": "account", "action": "tokentx",
            "address": TETHER_TREASURY, "contractaddress": USDT_CONTRACT,
            "startblock": 0, "endblock": end_block, "page": 1,
            "offset": PAGE_SIZE, "sort": "desc", "apikey": api_key,
        }
        r = httpx.get(ETHERSCAN_API_URL, params=params, timeout=30.0)
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "1":
            break
        page = data.get("result") or []
        if not page:
            break
        for tx in page:
            try:
                rows.append({
                    "ts": int(tx["timeStamp"]),
                    "from": tx["from"].lower(), "to": tx["to"].lower(),
                    "value": int(tx["value"]) / 10 ** USDT_DECIMALS,
                    "block": int(tx["blockNumber"]),
                })
            except (KeyError, ValueError, TypeError):
                continue
        oldest = min(int(t["timeStamp"]) for t in page)
        print(f"  {len(rows):>5} txns, oldest {pd.to_datetime(oldest, unit='s', utc=True).date()}",
              file=sys.stderr)
        if oldest <= oldest_ts or len(page) < PAGE_SIZE:
            break
        end_block = min(int(t["blockNumber"]) for t in page) - 1
        time.sleep(THROTTLE_S)
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    args = ap.parse_args()
    api_key = get_key()
    if not api_key:
        print("No ETHERSCAN_API_KEY", file=sys.stderr); sys.exit(1)

    oldest_ts = int(time.time()) - args.days * 86400
    print(f"Fetching Tether Treasury USDT flow, last {args.days} days ...", file=sys.stderr)
    df = fetch_treasury_usdt(api_key, oldest_ts)
    if df.empty:
        print("No data", file=sys.stderr); sys.exit(1)
    df = df[df["ts"] >= oldest_ts].copy()

    # Classify. Exclude the USDT contract itself (those are mint/burn, separate).
    is_mint = df["from"] == "0x0000000000000000000000000000000000000000"
    is_burn = df["to"] == "0x0000000000000000000000000000000000000000"
    contract_side = (df["from"] == USDT_CONTRACT) | (df["to"] == USDT_CONTRACT)
    market = df[~contract_side & ~is_mint & ~is_burn].copy()
    market["out_to_market"] = np.where(market["from"] == TETHER_TREASURY, market["value"], 0.0)
    market["in_from_market"] = np.where(market["to"] == TETHER_TREASURY, market["value"], 0.0)
    market["day"] = (market["ts"] // 86400) * 86400

    daily = market.groupby("day").agg(
        issuance=("out_to_market", "sum"),
        redemption=("in_from_market", "sum"),
    ).reset_index()
    daily["net_to_market"] = daily["issuance"] - daily["redemption"]
    daily["ts"] = pd.to_datetime(daily["day"], unit="s", utc=True)
    daily = daily.set_index("ts").sort_index()

    print(f"\n  daily series: {len(daily)} days  {daily.index.min().date()} → {daily.index.max().date()}",
          file=sys.stderr)
    print(f"  total net to market: ${daily['net_to_market'].sum()/1e9:+.2f}B", file=sys.stderr)

    # ── Cross with ETH forward returns ──────────────────────────────
    t = pq.read_table(KLINES_PATH).to_pandas()[["ts_utc_ms", "close"]]
    t["ts"] = pd.to_datetime(t["ts_utc_ms"], unit="ms", utc=True)
    close_d = t.set_index("ts")["close"].resample("1D").last()

    print("\n" + "=" * 70)
    print("DOES STABLECOIN ISSUANCE LEAD ETH? (Spearman IC, daily)")
    print("=" * 70)
    # Align: net_to_market on day D vs ETH forward return D→D+h
    merged = pd.DataFrame({"net": daily["net_to_market"]}).copy()
    merged["close"] = close_d.reindex(merged.index, method="ffill")
    for h in (1, 3, 7, 14):
        fwd = close_d.shift(-h) / close_d - 1.0
        merged[f"fwd{h}d"] = fwd.reindex(merged.index)
    print(f"\n  {'horizon':>8} {'n':>5} {'IC(net→fwd)':>13} {'p':>8}")
    for h in (1, 3, 7, 14):
        sub = merged[["net", f"fwd{h}d"]].dropna()
        if len(sub) < 20:
            print(f"  {h:>6}d  {len(sub):>5}  (too few)"); continue
        ic, p = spearmanr(sub["net"], sub[f"fwd{h}d"])
        flag = " *" if p < 0.05 else ""
        print(f"  {h:>6}d  {len(sub):>5} {ic:>+13.4f} {p:>8.3f}{flag}")

    # Also test the 7-day rolling sum of net issuance (smoother dry-powder signal)
    merged["net_7d"] = merged["net"].rolling(7).sum()
    print(f"\n  Using 7-day rolling issuance (smoother):")
    print(f"  {'horizon':>8} {'n':>5} {'IC':>13} {'p':>8}")
    for h in (1, 3, 7, 14):
        sub = merged[["net_7d", f"fwd{h}d"]].dropna()
        if len(sub) < 20: continue
        ic, p = spearmanr(sub["net_7d"], sub[f"fwd{h}d"])
        flag = " *" if p < 0.05 else ""
        print(f"  {h:>6}d  {len(sub):>5} {ic:>+13.4f} {p:>8.3f}{flag}")

    print("\n  → IC > 0 & p<0.05 = issuance leads ETH up (dry powder → fuel). Worth a panel.")
    print("    IC ≈ 0 = no lead/lag edge; the macro story doesn't show in price timing.")

    # Persist the daily series for reuse
    out = os.path.join(REPO_ROOT, "data", "backfill", "stablecoin_issuance.parquet")
    save = daily.reset_index()[["day", "issuance", "redemption", "net_to_market"]].copy()
    save["ts_utc_ms"] = save["day"] * 1000
    pq.write_table(pq.Table.from_pandas(save) if False else __import__("pyarrow").Table.from_pandas(save), out, compression="zstd")
    print(f"\n  saved daily series → {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
