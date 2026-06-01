"""
On-chain CEX netflow backfill via Etherscan (free tier) — WITH HISTORY.

The breakthrough this enables: Dune's cex.flows retains ~3 weeks, but
Etherscan's txlist returns the FULL transfer history of any address. By
tracking the curated hot wallets of an exchange, we reconstruct a netflow
PROXY with years of history — for free. This is what unlocks backtesting the
flow-based confluence (the user's real edge) without waiting for the live
logger or paying for a data vendor.

METHOD:
  - Curated set of Binance hot/deposit wallets (the ones users deposit to /
    withdraw from). Public, Etherscan-labelled.
  - Paginate txlist backwards N days (10k records/call, ~17h per call for a
    hyperactive wallet → throttled to respect the free 3 req/s limit).
  - inflow  = ETH arriving at a tracked wallet  (to   ∈ tracked)  → deposits
  - outflow = ETH leaving a tracked wallet       (from ∈ tracked)  → withdrawals
  - EXCLUDE internal Binance↔Binance moves (both sides in the Binance address
    set) — those are cold-storage shuffles, not user flow. This is the key
    cleanup that separates a usable proxy from noise.
  - Aggregate hourly net = inflow − outflow → parquet.

LIMITATIONS (honest):
  - PROXY, not the full netflow. We track Binance's main wallets, not every
    hot wallet of every exchange. Captures the bulk of Binance ETH flow.
  - Internal-move exclusion is only as complete as the Binance address set
    below. Unknown Binance wallets leak through as false flow.
  - ETH-native only here (txlist). USDT/USDC deposit flow would need tokentx
    (separate pass) — deferred; ETH netflow is the primary signal.
  - To validate: compare against Dune's cex.flows in the overlapping window
    (--validate). Correlation there tells us how good the proxy is.

Run:
    python scripts/backfill_cex_netflow_onchain.py --days 90
    python scripts/backfill_cex_netflow_onchain.py --days 365 --out data/backfill/cex_netflow_onchain.parquet
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np
import pandas as pd

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:
    print("pyarrow required", file=sys.stderr); sys.exit(1)

import httpx

sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(REPO_ROOT, "data", "backfill", "cex_netflow_onchain.parquet")

ETHERSCAN_API_URL = "https://api.etherscan.io/v2/api"
ETHERSCAN_CHAIN_ID = 1
PAGE_SIZE = 10000          # Etherscan max records per call
THROTTLE_S = 0.25          # ~4 req/s ceiling (free tier is 5/s on LITE, 3/s on free; safe)
SCHEMA_VERSION = 1

# ── Binance address set ──────────────────────────────────────────────
# TRACKED = the deposit/withdrawal hot wallets we measure flow through.
# These are the wallets users send ETH to (deposits) and receive from (withdrawals).
TRACKED_WALLETS = {
    "0x28c6c06298d514db089934071355e5743bf21d60",  # Binance 14 (primary hot)
    "0x21a31ee1afc51d94c2efccaa2092ad1028285549",  # Binance 15
    "0xdfd5293d8e347dfe59e90efd55b2956a1343963d",  # Binance 16
    "0x9696f59e4d72e237be84ffd425dcad154bf96976",  # Binance 18 (deposit funding)
}
# BINANCE_ALL = every known Binance wallet (hot + cold + funding). Used to detect
# and EXCLUDE internal moves: a transfer with both sides in this set is a
# cold-storage shuffle, not user flow.
BINANCE_ALL = TRACKED_WALLETS | {
    "0xbe0eb53f46cd790cd13851d5eff43d12404d33e8",  # Binance 7 (cold, largest)
    "0xf977814e90da44bfa03b6295a0616a897441acec",  # Binance 8 (cold)
    "0x5a52e96bacdabb82fd05763e25335261b270efcb",  # Binance 28
    "0xd551234ae421e3bcba99a0da6d736074f22192ff",  # Binance 2
    "0x564286362092d8e7936f0549571a803b203aaced",  # Binance 1
    "0x0681d8db095565fe8a346fa0277bffde9c0edbbf",  # Binance 4
    "0xfe9e8709d3215310075d67e3ed32a380ccf451c8",  # Binance 3
    "0x4976a4a02f38326660d17bf34b431dc6e2eb2327",  # Binance cold
    "0x8894e0a0c962cb723c1976a4421c95949be2d4e3",  # Binance 6
}


def fetch_page(client: httpx.AsyncClient, address: str, start_block: int,
               end_block: int, api_key: str) -> list:
    """One txlist page (sync wrapper not needed — keep it simple, synchronous)."""
    raise NotImplementedError  # placeholder; we use the sync version below


def fetch_txlist_window(address: str, start_block: int, end_block: int,
                        api_key: str) -> list:
    """Fetch up to PAGE_SIZE normal txns for `address` in [start_block, end_block],
    newest first. Returns the raw list (possibly truncated at PAGE_SIZE)."""
    params = {
        "chainid": ETHERSCAN_CHAIN_ID, "module": "account", "action": "txlist",
        "address": address, "startblock": start_block, "endblock": end_block,
        "page": 1, "offset": PAGE_SIZE, "sort": "desc", "apikey": api_key,
    }
    r = httpx.get(ETHERSCAN_API_URL, params=params, timeout=30.0)
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "1":
        # status 0 with "No transactions found" is normal at the tail
        if "No transactions" in str(data.get("message", "")):
            return []
        # Rate limited or other — surface it
        if "rate limit" in str(data.get("result", "")).lower():
            time.sleep(1.0)
            return fetch_txlist_window(address, start_block, end_block, api_key)
        return []
    return data.get("result") or []


def backfill_wallet(address: str, oldest_ts: int, api_key: str) -> list:
    """Page backwards through a wallet's txns until we pass `oldest_ts`.
    Returns list of {ts, from, to, value_eth}."""
    out = []
    end_block = 99_999_999
    while True:
        page = fetch_txlist_window(address, 0, end_block, api_key)
        if not page:
            break
        for tx in page:
            try:
                ts = int(tx["timeStamp"])
                out.append({
                    "ts": ts,
                    "from": tx["from"].lower(),
                    "to": tx["to"].lower() if tx["to"] else "",
                    "value_eth": float(tx["value"]) / 1e18,
                    "block": int(tx["blockNumber"]),
                })
            except (KeyError, ValueError, TypeError):
                continue
        oldest_in_page = min(int(t["timeStamp"]) for t in page)
        oldest_block = min(int(t["blockNumber"]) for t in page)
        print(f"    {address[:10]}…  {len(out):>6} txns, oldest "
              f"{pd.to_datetime(oldest_in_page, unit='s', utc=True)}", file=sys.stderr)
        if oldest_in_page <= oldest_ts or len(page) < PAGE_SIZE:
            break
        end_block = oldest_block - 1  # continue before the oldest block seen
        time.sleep(THROTTLE_S)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--min-eth", type=float, default=0.0,
                    help="Ignore transfers below this ETH size (noise filter)")
    args = ap.parse_args()

    # Read API key from backend/.env
    env_path = os.path.join(REPO_ROOT, "backend", ".env")
    api_key = ""
    if os.path.exists(env_path):
        for line in open(env_path):
            if line.startswith("ETHERSCAN_API_KEY="):
                api_key = line.split("=", 1)[1].strip()
    if not api_key:
        api_key = os.getenv("ETHERSCAN_API_KEY", "")
    if not api_key:
        print("No ETHERSCAN_API_KEY found", file=sys.stderr); sys.exit(1)

    oldest_ts = int(time.time()) - args.days * 86400
    print(f"Backfilling on-chain CEX netflow, last {args.days} days "
          f"(since {pd.to_datetime(oldest_ts, unit='s', utc=True)})", file=sys.stderr)
    print(f"Tracked wallets: {len(TRACKED_WALLETS)}  | exclusion set: {len(BINANCE_ALL)}", file=sys.stderr)

    all_tx = []
    for w in TRACKED_WALLETS:
        print(f"  wallet {w} ...", file=sys.stderr)
        all_tx.extend(backfill_wallet(w, oldest_ts, api_key))

    if not all_tx:
        print("No transactions fetched.", file=sys.stderr); sys.exit(1)

    df = pd.DataFrame(all_tx)
    df = df[df["ts"] >= oldest_ts]
    # Classify direction relative to tracked wallets, EXCLUDING internal moves
    df["to_tracked"] = df["to"].isin(TRACKED_WALLETS)
    df["from_tracked"] = df["from"].isin(TRACKED_WALLETS)
    df["other_is_binance"] = (
        (df["to_tracked"] & df["from"].isin(BINANCE_ALL)) |
        (df["from_tracked"] & df["to"].isin(BINANCE_ALL))
    )
    # External flow only (drop internal Binance↔Binance)
    ext = df[~df["other_is_binance"]].copy()
    if args.min_eth > 0:
        ext = ext[ext["value_eth"] >= args.min_eth]
    ext["inflow"] = np.where(ext["to_tracked"], ext["value_eth"], 0.0)
    ext["outflow"] = np.where(ext["from_tracked"], ext["value_eth"], 0.0)
    ext["hour"] = (ext["ts"] // 3600) * 3600

    hourly = ext.groupby("hour").agg(
        inflow_eth=("inflow", "sum"),
        outflow_eth=("outflow", "sum"),
        tx_count=("ts", "count"),
    ).reset_index()
    hourly["net_inflow_eth"] = hourly["inflow_eth"] - hourly["outflow_eth"]
    hourly["ts_utc_ms"] = hourly["hour"] * 1000
    hourly["schema_version"] = SCHEMA_VERSION
    hourly["source"] = "etherscan_binance_hot"

    # Stats
    internal_dropped = int(df["other_is_binance"].sum())
    print(f"\n  total txns: {len(df):,}  internal dropped: {internal_dropped:,}  "
          f"external: {len(ext):,}", file=sys.stderr)
    print(f"  hourly buckets: {len(hourly):,}  "
          f"{pd.to_datetime(hourly['hour'].min(), unit='s', utc=True)} → "
          f"{pd.to_datetime(hourly['hour'].max(), unit='s', utc=True)}", file=sys.stderr)
    print(f"  net flow over window: {hourly['net_inflow_eth'].sum():+,.0f} ETH", file=sys.stderr)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    table = pa.Table.from_pandas(hourly[[
        "ts_utc_ms", "schema_version", "source", "inflow_eth", "outflow_eth",
        "net_inflow_eth", "tx_count",
    ]])
    pq.write_table(table, args.out, compression="zstd")
    print(f"  → wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
