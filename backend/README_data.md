# Data Sources Reference

This file documents every data source the dashboard consumes, its refresh
policy, the endpoints exposed, and the parquet schema used by the persistence
layer (Fase 2) and backfill script (Fase 3).

## Live sources already integrated (pre-Fase 1)

| Source                  | Endpoint(s)                                              | TTL      | Notes |
|-------------------------|----------------------------------------------------------|----------|-------|
| Binance perp            | fapi.binance.com/fapi/v1/*                               | 10s      | primary price, funding, OI, L/S, taker |
| Binance spot            | api.binance.com/api/v3/*                                 | 10s      | spot ticker, klines, ETHBTC |
| OKX perp                | www.okx.com/api/v5/public/*                              | 10s      | funding, OI, L/S |
| Bybit perp              | api.bybit.com/v5/market/*                                | 10s      | tickers, funding, OI |
| Hyperliquid             | api.hyperliquid.xyz/info (POST)                          | 10s      | meta + assetCtxs |
| Deribit options         | /public/get_book_summary_by_currency?kind=option         | 10s      | IV, OI per strike |
| Dune netflows           | Query 6984181                                            | 30min    | triggers fresh execution if >2h old |
| DefiLlama reserves      | api.llama.fi/protocol/{slug}                             | 1h       | ETH + stablecoin reserves per CEX |
| DefiLlama DeFi map      | api.llama.fi/protocol/{slug}                             | 1h       | ETH in Lido, Aave, Maker, EigenLayer... |
| ETH/BTC rotation        | api.binance.com spot klines ETHBTC                       | 10s      | BTC→ETH taker-buy rotation signal |

## Fase 1 — new sources (this iteration)

| Source                   | Endpoint                                                            | Fallback chain                                    | TTL    | Cache var              |
|--------------------------|---------------------------------------------------------------------|---------------------------------------------------|--------|------------------------|
| ETF flows (Farside)      | https://farside.co.uk/eth/ (HTML scrape)                            | farside_csv → farside_html (httpx) → farside_html (curl subproc) → SoSoValue → stale | 6h     | `etf_cache`            |
| Stablecoin supply        | https://stablecoins.llama.fi/stablecoincharts/all                   | aggregate + per-stable chart by id                | 30min  | `stables_cache`        |
| Deribit futures basis    | /public/get_book_summary_by_currency?kind=future                    | —                                                 | 30s    | `deribit_basis_cache`  |
| Spot-perp basis          | *derived from cached venue prices*                                  | —                                                 | 10s    | main cache             |
| Macro (Yahoo v8 chart)   | query2.finance.yahoo.com/v8/finance/chart/{symbol}                  | —                                                 | 5min   | `macro_cache`          |
| Risk-free rate           | Yahoo `^IRX` (CBOE 13-week T-bill) → FRED DTB3 CSV fallback         | yahoo → fred → stale                              | 1d     | `risk_free_cache`      |
| Options skew (RR25/BF25) | *derived from Deribit book_summary (options) + risk-free rate*      | —                                                 | 10s    | main cache             |
| Options expiry calendar  | *derived from Deribit book_summary (options)*                       | —                                                 | 10s    | main cache             |

### Endpoints exposed

```
GET /api/etf/flows           — daily per-issuer + aggregate + 5d/20d rolling
GET /api/stables/supply      — aggregate + USDT/USDC with 1d/7d/30d deltas
GET /api/basis/deribit       — dated futures basis per expiry + annualized
GET /api/basis/cme           — STUB (fase 2)
GET /api/basis/perp          — spot-perp by venue + perp-quarterly
GET /api/options/skew        — RR25, BF25 per expiry + canonical tenors
GET /api/options/expiries    — next 8 expiries with notional USD + pin risk
GET /api/macro               — DXY, SPX, VIX, US10Y, BTC with 1d/7d/30d + realized vol 20d
GET /api/riskfree            — current BS risk-free rate (FRED DTB3)
GET /api/health              — cache ages per source (null ⇒ warming)
```

All endpoints return `{"status": "warming"}` if their cache is empty.

### Design notes

- **Non-blocking warmup**: ETF, stables, macro, risk-free are refreshed by a
  background task (`slow_data_refresher`) that ticks every 60s but respects
  per-source TTL. They stay out of the main 10s `fetch_all_data` hot path so
  a Farside HTML scrape can't block the UI.
- **Risk-free rate**: loaded from Yahoo `^IRX` (CBOE 13-week T-bill) daily,
  with FRED `DTB3` CSV as fallback. Used by
  the in-house Black-Scholes delta interpolation in `compute_options_skew`
  (`RR25 = IV(call Δ=0.25) − IV(put Δ=−0.25)`,
  `BF25 = ½(IV_call25 + IV_put25) − IV_ATM`). Strikes are filtered to ±30%
  of spot before delta computation. Falls back to 0.04 if cache cold.
- **Farside fallback**: farside.co.uk blocks Python httpx with 403 due to
  Cloudflare TLS fingerprinting. Backend retries once via subprocess `curl`,
  which uses the OS TLS stack and mimics a real browser. If curl is absent
  from PATH, falls through to SoSoValue → stale cache.
- **SoSoValue**: Public endpoint coded as tertiary, but DNS is intermittent.
  Kept defensively for when the public key becomes free again.
- **Yahoo `^TNX`**: Quoted as yield×10 (e.g. 4.51 means 4.51% yield). We
  surface the raw value — consumer divides if normalizing.

### CME TODO (fase 2)

CME front-month basis is **not** implemented yet. Deribit futures already
cover the "dated futures vs spot" math, but CME reflects TradFi institutional
positioning during US regulated hours — a distinct audience the crypto-native
Deribit book doesn't capture. Daily cadence with 10-min settlement delay is
fine. Implement as an HTML scrape of the CME settlement page when ready.

### api/index.py (Vercel) TODO

The serverless mirror at `api/index.py` was intentionally NOT updated in
Fase 1 — see the `TODO fase 2` comment at the top of the new-sources block in
`backend/main.py`. Replicate after main validates in production.

---

## Fase 2 — Parquet persistence

### Layout

```
{PERSIST_PATH}/YYYY-MM-DD/snapshot.parquet         # one row per minute
{PERSIST_PATH}/YYYY-MM-DD/microstructure.parquet   # one row per 5s
```

Enabled via env var `PERSIST_ENABLED=true` (default `false`). Path defaults to
`{repo}/data/`. Requires `pyarrow` (added to `requirements.txt`).

### Snapshot schema (hybrid)

- `ts_utc_ms` — BIGINT, primary key, epoch ms UTC. No naive `datetime.now()`
  anywhere in the pipeline.
- `schema_version` — INT (currently `1`). Bump when the set of flat columns
  changes; backtest code keys decisions off this.
- **Plain scalar columns** (one per metric): ratios, z-scores, flags, prices,
  OIs, per-timeframe metrics (`mq_1h_ratio`, `stochastics_4h_k`, etc.). These
  are what backtest `WHERE` clauses filter on.
- **JSON string columns**: arrays of variable length — histories, per-strike
  greeks, depth clusters, expiry calendars. Hydrate on demand.

Rule of thumb: _if a backtest would filter on it in a `WHERE` clause, it goes
plain. If it's context to hydrate per row, it goes JSON._

### Microstructure schema

| Column              | Type        | Notes                                   |
|---------------------|-------------|-----------------------------------------|
| `ts_utc_ms`         | BIGINT      | epoch ms UTC                            |
| `schema_version`    | INT         |                                         |
| `midPrice`          | DOUBLE      | from Binance fapi depth                 |
| `spread`            | DOUBLE      | best bid/ask USD delta                  |
| `bidAskImbalance`   | DOUBLE      | derived                                 |
| `totalBidQty`       | DOUBLE      | top-N level aggregate                   |
| `totalAskQty`       | DOUBLE      |                                         |
| `bids_json`         | STRING      | top 30 levels JSON                      |
| `asks_json`         | STRING      | top 30 levels JSON                      |
| `bidWalls_json`     | STRING      | detected walls                          |
| `askWalls_json`     | STRING      |                                         |

### Append semantics

- Atomic writes: serialise to `path.tmp`, then `os.replace(tmp, path)`.
- Schema evolution: new columns pad existing rows with NULL via
  `pa.concat_tables(promote_options='default')`.
- Non-blocking: both persister tasks wrap the whole body in try/except;
  errors log as WARNING, the task sleeps a short backoff, then continues.
  A failed disk does not kill the API.

### Cadence

- Snapshot: tick every 15s, but only write once per minute-floor (dedup by
  `floor(now / 60)`). So ~1440 rows/day.
- Microstructure: tick every 5s. ~17,280 rows/day.

---

## Fase 3 — Bulk backfill (scripts/backfill.py)

```
python scripts/backfill.py --sources all --years 2
python scripts/backfill.py --sources binance_klines_1h,macro --years 3
python scripts/backfill.py --sources farside_etf
```

Output: `data/backfill/<source>.parquet`, unified row schema
`{ts_utc_ms, schema_version, source, symbol, ...metrics}`.

| Source              | Scope                                                | Limitations |
|---------------------|------------------------------------------------------|-------------|
| `binance_klines_1h` | 1h perp klines (OHLCV + taker buys), 2y default     | ~17.5k rows per 2y |
| `binance_funding`   | full funding rate history                            | 8h cadence |
| `binance_oi_hist`   | OI history                                           | Binance keeps only ~30 days (API limit — hard cap) |
| `farside_etf`       | full ETF flow history since Jul 2024                | HTML scrape via browser UA |
| `defillama_stables` | aggregate + USDT/USDC supply series                  | Full history |
| `macro`             | 5y Yahoo + FRED DTB3 full history                    | DXY, SPX, VIX, US10Y, BTC |
| `deribit_options`   | **STUB**                                             | Trade history is ~5M/yr — implement per storage budget |
| `cme`               | **STUB**                                             | Fase 2 |

---

## Known caveats

- **Stooq** went paywalled in 2026. Replaced with Yahoo v8 chart (no-key
  public JSON). If Yahoo adds auth, fallback is the `yfinance` library.
- **Yahoo `^TNX`** (US10Y) is yield×10. Divide by 10 for the % yield.
- **Farside** works from residential IPs but may 403 from some data centers
  (Cloudflare fingerprint). The `curl` subprocess fallback handles this.
- **Dune** netflows query (`6984181`) has a 2h stale threshold — past that,
  the backend triggers a fresh execution but falls back to stale if it
  times out (90s poll budget).
