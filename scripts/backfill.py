"""Bulk historical backfill for the ETH positioning dashboard.

Downloads as much history as each public free source allows and writes to
data/backfill/<source>.parquet with a unified schema (ts_utc_ms + schema_version
+ source-specific columns). The backtest reads these files alongside the
live-streamed data/YYYY-MM-DD/snapshot.parquet layout.

Usage:
    python scripts/backfill.py --sources all --years 2
    python scripts/backfill.py --sources binance_klines_1h,macro --years 3
    python scripts/backfill.py --sources farside_etf

Available sources: see ALL_SOURCES below.

Known limitations (as of 2026):
    • Binance /futures/data/openInterestHist limit = ~30 days, not years.
    • Stooq (formerly our macro source) now requires an API key; we use
      Yahoo Finance v8 chart + FRED fredgraph CSV instead.
    • Farside Investors has no public CSV — HTML scrape only (needs browser UA).
    • SoSoValue public endpoint requires an API key; included as best-effort.
    • CME settlement scrape deferred to fase 2 (stub only).
    • Deribit options trade history is huge; skipped by default (stub only).
"""

import os
import sys
import argparse
import asyncio
import time
import csv
import io
import re
from datetime import datetime, timezone
from urllib.parse import quote as urlquote

import httpx

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:
    print("pyarrow is required: pip install pyarrow", file=sys.stderr)
    sys.exit(1)

# Share the runtime's Farside parser (QW2) — single source of truth
_backend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
sys.path.insert(0, _backend_dir)
from farside_parse import (  # noqa: E402
    parse_farside_html,
    parse_farside_date_to_epoch,
)

# ── Config ───────────────────────────────────────────────────────────
DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "backfill")

BINANCE_FAPI           = "https://fapi.binance.com"
DEFILLAMA_STABLES_BASE = "https://stablecoins.llama.fi"
YAHOO_CHART_BASE       = "https://query2.finance.yahoo.com/v8/finance/chart/"
FRED_CSV_URL           = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FARSIDE_URL            = "https://farside.co.uk/eth/"

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

MACRO_SYMBOLS = {
    "DXY":       "DX-Y.NYB",
    "SPX":       "^GSPC",
    "VIX":       "^VIX",
    "US10Y":     "^TNX",
    "BTC":       "BTC-USD",
    "RISK_FREE": "^IRX",    # CBOE 13-week T-bill (proxies FRED DTB3; same infra as macro)
}

SCHEMA_VERSION = 1

# Earliest available history on Binance perp ETHUSDT.
# Listing date for the perpetual contract: 2019-11-27 UTC.
# Both klines and funding-rate history start at the same boundary; requesting
# earlier just returns empty pages, but we still want to cap silently so
# `--years 5` from 2026-04 doesn't keep paginating against a no-data window.
BINANCE_ETHUSDT_PERP_LISTING_MS = int(
    datetime(2019, 11, 27, tzinfo=timezone.utc).timestamp() * 1000
)


def _start_ms_capped(years: int, source_label: str) -> int:
    """Compute the start_ms for a `years` request, capping at the Binance perp
    listing date when the requested window pre-dates it. Logs at INFO level so
    the silent cap is auditable (visible in stdout)."""
    end_ms = int(time.time() * 1000)
    requested = end_ms - years * 365 * 86400 * 1000
    if requested < BINANCE_ETHUSDT_PERP_LISTING_MS:
        capped_dt = datetime.fromtimestamp(BINANCE_ETHUSDT_PERP_LISTING_MS / 1000, tz=timezone.utc)
        print(f"  [{source_label}] INFO requested {years}y exceeds Binance perp ETHUSDT history; "
              f"capping start at {capped_dt:%Y-%m-%d}")
        return BINANCE_ETHUSDT_PERP_LISTING_MS
    return requested


# ── Parquet writer ───────────────────────────────────────────────────
def write_parquet(path: str, rows: list) -> None:
    if not rows:
        print(f"  (no rows to write to {path})")
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    all_keys = set()
    for r in rows:
        all_keys.update(r.keys())
    cols = sorted(all_keys)
    table = pa.Table.from_pydict({c: [r.get(c) for r in rows] for c in cols})
    pq.write_table(table, path, compression="zstd")
    print(f"  -> {path}  ({len(rows):,} rows, {len(cols)} cols)")


# ── Binance klines 1h (primary price + volume history) ───────────────
async def backfill_binance_klines_1h(client: httpx.AsyncClient, years: int) -> list:
    print(f"[binance_klines_1h] {years}y of 1h ETHUSDT futures klines ...")
    end_ms   = int(time.time() * 1000)
    start_ms = _start_ms_capped(years, "binance_klines_1h")
    rows = []
    cursor = start_ms
    while cursor < end_ms:
        r = await client.get(
            f"{BINANCE_FAPI}/fapi/v1/klines",
            params={"symbol": "ETHUSDT", "interval": "1h",
                    "startTime": cursor, "endTime": end_ms, "limit": 1500},
            timeout=30.0,
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        for k in batch:
            rows.append({
                "ts_utc_ms":       int(k[0]),
                "schema_version":  SCHEMA_VERSION,
                "source":          "binance_perp",
                "symbol":          "ETHUSDT",
                "open":            float(k[1]),
                "high":            float(k[2]),
                "low":             float(k[3]),
                "close":           float(k[4]),
                "volume":          float(k[5]),
                "close_time_ms":   int(k[6]),
                "quote_volume":    float(k[7]),
                "trades":          int(k[8]),
                "taker_buy_base":  float(k[9]),
                "taker_buy_quote": float(k[10]),
            })
        new_cursor = int(batch[-1][0]) + 3600000
        if new_cursor <= cursor:
            break
        cursor = new_cursor
        await asyncio.sleep(0.1)  # rate-limit friendly
    return rows


# ── Binance funding history (8h cadence, full history available) ─────
async def backfill_binance_funding(client: httpx.AsyncClient, years: int) -> list:
    print(f"[binance_funding] {years}y of funding-rate history ...")
    end_ms   = int(time.time() * 1000)
    start_ms = _start_ms_capped(years, "binance_funding")
    rows = []
    cursor = start_ms
    while cursor < end_ms:
        r = await client.get(
            f"{BINANCE_FAPI}/fapi/v1/fundingRate",
            params={"symbol": "ETHUSDT", "startTime": cursor, "endTime": end_ms, "limit": 1000},
            timeout=30.0,
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        for item in batch:
            rows.append({
                "ts_utc_ms":       int(item.get("fundingTime")),
                "schema_version":  SCHEMA_VERSION,
                "source":          "binance_funding",
                "symbol":          item.get("symbol"),
                "funding_rate":    float(item.get("fundingRate")),
            })
        new_cursor = int(batch[-1].get("fundingTime")) + 1
        if new_cursor <= cursor:
            break
        cursor = new_cursor
        await asyncio.sleep(0.1)
    return rows


# ── Binance OI history (Binance API keeps only ~30 days!) ────────────
async def backfill_binance_oi_hist(client: httpx.AsyncClient, days: int = 30) -> list:
    print(f"[binance_oi_hist] {days}d of OI history (Binance limit) ...")
    r = await client.get(
        f"{BINANCE_FAPI}/futures/data/openInterestHist",
        params={"symbol": "ETHUSDT", "period": "1h", "limit": 500},
        timeout=30.0,
    )
    r.raise_for_status()
    rows = []
    for item in r.json():
        rows.append({
            "ts_utc_ms":        int(item.get("timestamp")),
            "schema_version":   SCHEMA_VERSION,
            "source":           "binance_oi_hist",
            "symbol":           item.get("symbol"),
            "sum_oi":           float(item.get("sumOpenInterest")),
            "sum_oi_value":     float(item.get("sumOpenInterestValue")),
        })
    return rows


# ── Farside ETF flows (full history since July 2024) ─────────────────
# Uses the shared parser from backend/farside_parse.py (QW2 — single source
# of truth with the live runtime).
async def backfill_farside_etf(client: httpx.AsyncClient) -> list:
    print("[farside_etf] scraping full ETF flow history ...")
    r = await client.get(FARSIDE_URL, timeout=30.0, headers={
        "User-Agent":      BROWSER_UA,
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9",
        "Accept-Language": "en-US,en;q=0.9",
    })
    r.raise_for_status()
    parsed = parse_farside_html(r.text)
    if not parsed or not parsed.get("daily"):
        print("  shared parser returned no rows from Farside HTML")
        return []
    issuers = parsed.get("issuers", [])
    rows = []
    for day in parsed["daily"]:
        ts_sec = parse_farside_date_to_epoch(day["date"])
        if ts_sec == 0:
            continue
        row = {
            "ts_utc_ms":      ts_sec * 1000,
            "schema_version": SCHEMA_VERSION,
            "source":         "farside",
            "date":           day["date"],
        }
        for iss in issuers:
            row[f"flow_{iss}"] = day.get("byIssuer", {}).get(iss)
        row["flow_total"] = day.get("total")
        rows.append(row)
    return rows


# ── DefiLlama stablecoin supply history ──────────────────────────────
async def backfill_defillama_stables(client: httpx.AsyncClient) -> list:
    print("[defillama_stables] fetching aggregate + per-stable supply history ...")
    rows = []

    r = await client.get(f"{DEFILLAMA_STABLES_BASE}/stablecoincharts/all", timeout=60.0)
    r.raise_for_status()
    chart = r.json()
    if isinstance(chart, list):
        for pt in chart:
            try:
                ts = int(pt.get("date"))
                tc = pt.get("totalCirculating", {})
                usd = tc.get("peggedUSD") if isinstance(tc, dict) else tc
                usd_val = float(usd) if usd is not None else None
                if usd_val:
                    rows.append({
                        "ts_utc_ms":      ts * 1000,
                        "schema_version": SCHEMA_VERSION,
                        "source":         "defillama_stables_agg",
                        "total_usd":      usd_val,
                    })
            except (TypeError, ValueError):
                continue

    rl = await client.get(f"{DEFILLAMA_STABLES_BASE}/stablecoins",
                          params={"includePrices": "false"}, timeout=30.0)
    if rl.status_code == 200:
        peggedAssets = rl.json().get("peggedAssets", []) if isinstance(rl.json(), dict) else []
        id_map = {p.get("symbol"): p.get("id") for p in peggedAssets if p.get("symbol") in ("USDT", "USDC")}
        for sym, sid in id_map.items():
            if sid is None:
                continue
            try:
                rs = await client.get(
                    f"{DEFILLAMA_STABLES_BASE}/stablecoincharts/all",
                    params={"stablecoin": str(sid)},
                    timeout=30.0,
                )
                if rs.status_code == 200:
                    for pt in rs.json() or []:
                        try:
                            ts = int(pt.get("date"))
                            tc = pt.get("totalCirculating", {})
                            usd = tc.get("peggedUSD") if isinstance(tc, dict) else tc
                            usd_val = float(usd) if usd is not None else None
                            if usd_val:
                                rows.append({
                                    "ts_utc_ms":      ts * 1000,
                                    "schema_version": SCHEMA_VERSION,
                                    "source":         f"defillama_stables_{sym.lower()}",
                                    "total_usd":      usd_val,
                                })
                        except (TypeError, ValueError):
                            continue
            except Exception as e:
                print(f"  per-stable fetch failed for {sym}: {e}")
    return rows


# ── Macro (Yahoo v8 chart + FRED CSV) ────────────────────────────────
async def backfill_macro(client: httpx.AsyncClient, range_: str = "5y") -> list:
    print(f"[macro] Yahoo {range_} + FRED DTB3 (max available) ...")
    rows = []
    for label, symbol in MACRO_SYMBOLS.items():
        try:
            r = await client.get(
                f"{YAHOO_CHART_BASE}{urlquote(symbol, safe='')}",
                params={"interval": "1d", "range": range_},
                timeout=30.0,
                headers={"User-Agent": BROWSER_UA, "Accept": "application/json, */*"},
            )
            r.raise_for_status()
            result_list = r.json().get("chart", {}).get("result", [])
            if not result_list:
                continue
            res = result_list[0]
            timestamps = res.get("timestamp") or []
            quote = (res.get("indicators", {}).get("quote") or [{}])[0]
            closes = quote.get("close") or []
            for i, ts in enumerate(timestamps):
                if ts is None or i >= len(closes) or closes[i] is None:
                    continue
                rows.append({
                    "ts_utc_ms":      int(ts) * 1000,
                    "schema_version": SCHEMA_VERSION,
                    "source":         "yahoo",
                    "symbol":         symbol,
                    "label":          label,
                    "close":          float(closes[i]),
                })
            await asyncio.sleep(0.2)
        except Exception as e:
            print(f"  Yahoo fetch failed for {symbol}: {e}")

    # FRED DTB3 (3-month T-bill yield)
    try:
        r = await client.get(FRED_CSV_URL, params={"id": "DTB3"}, timeout=30.0,
                             headers={"User-Agent": BROWSER_UA})
        if r.status_code == 200:
            reader = csv.reader(io.StringIO(r.text))
            next(reader, None)
            for row in reader:
                if len(row) < 2:
                    continue
                date_str, val = row[0].strip(), row[1].strip()
                if val in ("", ".", "-"):
                    continue
                try:
                    v = float(val)
                    ts_ms = int(datetime.strptime(date_str, "%Y-%m-%d")
                                .replace(tzinfo=timezone.utc).timestamp() * 1000)
                    rows.append({
                        "ts_utc_ms":      ts_ms,
                        "schema_version": SCHEMA_VERSION,
                        "source":         "fred",
                        "symbol":         "DTB3",
                        "label":          "RISK_FREE",
                        "close":          v,
                    })
                except (ValueError, TypeError):
                    continue
    except Exception as e:
        print(f"  FRED fetch failed: {e}")

    return rows


# ── Stubs: Deribit options trades + CME ──────────────────────────────
async def backfill_deribit_options(client: httpx.AsyncClient, *_) -> list:
    print("[deribit_options] TODO — trade history is deep (~5M trades/yr).")
    print("                  Implement with /public/get_last_trades_by_currency_and_time")
    print("                  when the storage budget is known. Skipped.")
    return []


async def backfill_cme(client: httpx.AsyncClient, *_) -> list:
    print("[cme] TODO fase 2 — scrape CME ETH settlement page for institutional-TradFi basis.")
    return []


# ── Dispatcher ───────────────────────────────────────────────────────
ALL_SOURCES = {
    "binance_klines_1h":  lambda c, y: backfill_binance_klines_1h(c, y),
    "binance_funding":    lambda c, y: backfill_binance_funding(c, y),
    "binance_oi_hist":    lambda c, y: backfill_binance_oi_hist(c, 30),
    "farside_etf":        lambda c, y: backfill_farside_etf(c),
    "defillama_stables":  lambda c, y: backfill_defillama_stables(c),
    "macro":              lambda c, y: backfill_macro(c, "5y" if y <= 5 else "max"),
    "deribit_options":    lambda c, y: backfill_deribit_options(c),
    "cme":                lambda c, y: backfill_cme(c),
}


async def main() -> None:
    parser = argparse.ArgumentParser(description="Bulk historical backfill")
    parser.add_argument(
        "--sources", default="all",
        help=f"Comma-separated or 'all'. Available: {', '.join(ALL_SOURCES)}",
    )
    parser.add_argument("--years", type=int, default=2,
                        help="Years of history to request (applied where source allows).")
    parser.add_argument("--out", default=DEFAULT_OUT,
                        help="Output directory for parquet files.")
    args = parser.parse_args()

    selected = list(ALL_SOURCES.keys()) if args.sources == "all" else [s.strip() for s in args.sources.split(",")]

    print(f"Backfill targets: {selected}")
    print(f"Years requested:  {args.years}")
    print(f"Output:           {args.out}")
    os.makedirs(args.out, exist_ok=True)

    async with httpx.AsyncClient() as client:
        for name in selected:
            fn = ALL_SOURCES.get(name)
            if not fn:
                print(f"[{name}] unknown source, skipping")
                continue
            t0 = time.time()
            try:
                rows = await fn(client, args.years)
                write_parquet(os.path.join(args.out, f"{name}.parquet"), rows)
            except Exception as e:
                print(f"[{name}] FAILED: {e}")
                import traceback
                traceback.print_exc()
            dt = time.time() - t0
            print(f"[{name}] done in {dt:.1f}s")
    print("All done.")


if __name__ == "__main__":
    asyncio.run(main())
