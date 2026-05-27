# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

### Dev (local)
```bash
# Backend (FastAPI, uvicorn) — needs backend/.env with DUNE_API_KEY etc.
cd backend && python -m uvicorn main:app --port 8000

# Frontend (Vite, React)
cd frontend && npm run dev    # port 5174

# Frontend prod build
cd frontend && npm run build
```

### Smoke tests (every backend change)
```bash
# Against local
python scripts/smoke_tests.py

# Against prod
python scripts/smoke_tests.py --host https://eth-positioning-dashboard.vercel.app
```
Exit 0 = all checks pass. Run before every push that touches `backend/main.py` or `api/index.py`.

### Data backfill (parquet history for any future analysis)
```bash
python scripts/backfill.py --sources binance_klines_1h,binance_funding --years 5
python scripts/backfill.py --sources farside_etf       # uses curl subprocess fallback for TLS fingerprint
python scripts/backfill.py --sources all --years 5
```
Output → `data/backfill/<source>.parquet`. Live runtime persistence (when `PERSIST_ENABLED=true`) → `data/YYYY-MM-DD/snapshot.parquet`.

## Architecture

### Two-headed backend (CRITICAL)

There are **two backend entry points serving the same logic** and they must be kept in sync:

- `backend/main.py` (4364 lines) — long-running uvicorn process. Used in dev, Docker, Railway. Has a background `slow_data_refresher` task that polls expensive sources every 60s; the 10s hot path serves cached values. **Polls Dune** (waits for fresh execution).
- `api/index.py` (2409 lines) — Vercel serverless mirror with `maxDuration: 60s`. Cannot run background tasks, so it does **fire-and-forget** Dune triggers (next request picks up the result) and pares any logic that doesn't fit a single Lambda invocation.

When changing data logic (signal computation, processing functions like `process_dune_netflows`, `process_options_skew`, etc.) you **must update both files**. `api/index.py` typically lags or simplifies; check both with `grep -n` after editing.

Known drift today (per audit): `api/index.py` does NOT yet emit `etfFlows`, `stablesSupply`, `macro`, `riskFreeRate`, `deribitBasis`, `perpBasis`, `optionsSkew`, `optionsExpiries`. Those panels render in dev but stay empty in prod.

### Frontend
Single-file React: `frontend/src/Dashboard.jsx` (4335 lines). Vite build, no test framework, no lint script. Frontend reads from `/api/data` (the hot-path snapshot) and renders all panels from that one payload.

### Data persistence (`data/`)

- `data/backfill/<source>.parquet` — historical bulk loads (2-5y) from `scripts/backfill.py`
- `data/YYYY-MM-DD/snapshot.parquet` — live runtime persistence (only when `PERSIST_ENABLED=true`)
- All parquets share `ts_utc_ms` + `schema_version` + source-specific columns

The parquets are intentionally retained for any future analysis even though the previous backtest framework was removed.

### Deploy targets

- **Vercel** (primary): `vercel.json` rewrites `/api/*` → `api/index.py`, everything else → `frontend/dist/index.html`. Region `sin1`. **`maxDuration: 60s`** — fan-out fetches must fit in this budget. **Any data file the serverless function needs to read at runtime (e.g. `data/regime/latest.json`) must be listed in `functions["api/*.py"].includeFiles`** — Vercel's Python builder only bundles the function file + requirements by default, NOT the rest of the repo.
- **Railway** (backup): `Dockerfile` builds frontend + serves with uvicorn from `backend.main:app`. Healthcheck at `/api/health`.

## Project-specific gotchas

### Dune (CEX netflows, query 6984181)
- Free plan = 2,500 credits/month; billing cycle runs **11th of each month → 11th of next** (not calendar month)
- Per-request datapoint cap: `total_rows × cols > ~3500 datapoints` returns HTTP 402. `fetch_dune_cex_netflows` **paginates** with `limit=200` (1400 datapoints/req) and parallel-fetches the rest via `execution_id`
- Two API keys are tried in order via `_dune_request`: `DUNE_API_KEY` then `DUNE_API_KEY_FALLBACK`. On 402 the key is backed-off (per-method) for 6h and the next is tried. The fallback key reads any public query but can only TRIGGER queries owned by its own account (or public V2 queries) — V1/Spark-engine queries return 400 "Deprecated query engine"
- The most recent hour bucket is in active Dune indexing (~30-90 min lag) — `process_dune_netflows` detects via `(exec_ended_at - bucket_end) < 90min` and excludes it from aggregates (uses `[-2]` as the "now" anchor). The partial bucket stays in `hourly_series` for the chart and is surfaced as `partialBucketTs` in the response
- Cache TTL: 30 min — kills multi-Lambda variance on Vercel + saves credits

### Farside ETF (HTML scrape)
Farside has a JA3/TLS fingerprint check that **rejects httpx with 403** but accepts curl. Backend `_curl_get()` is the fallback. `scripts/backfill.py` mirrors this fallback. Shared parser: `backend/farside_parse.py`.

### CEX netflow `direction` logic
When `|z| >= 1.0`, trust the z-sign (`z > 0` → BEARISH inflow regime). When `|z| < 1`, fall back to absolute net vs `noise_band = max(|mean_24h|, 500)`. Without this split, a regime mean drift makes `noise_band` swallow even clearly elevated readings — see comment in `process_dune_netflows`.

### Secrets
- `backend/.env` is **gitignored** (was accidentally tracked historically — the legacy `DUNE_API_KEY` value is in git history of commit `9b8b119`)
- Vercel needs the same env vars set in Project Settings → Environment Variables: `DUNE_API_KEY`, `DUNE_API_KEY_FALLBACK`, `DUNE_QUERY_ID`, `ETHERSCAN_API_KEY`, `HYPERLIQUID_WHALE_ADDRESSES` (optional)
- Never `git add -A` near `backend/.env` — use specific paths

### Etherscan (mainnet ETH balance for HL whales)
- Free tier: 5 req/s, 100k req/day — plenty for ≤20 whales every 5 min
- **Must use V2 API**: `https://api.etherscan.io/v2/api?chainid=1&...` — V1 (`/api?...`) returns "deprecated V1 endpoint" status=0
- `balancemulti` action returns up to 20 balances in one call
- Used by `fetch_etherscan_eth_balances` to enrich the HL whales bundle with `mainnetEth` — feeds `hedge_ratio` calc together with HL UETH so a SHORT perp + spot ETH on L1 is correctly labelled `FULLY_HEDGED` instead of `DIRECTIONAL_BET`

### Known bugs (from audit, not yet fixed)
- `process_dune_netflows` builds the 24h rolling distribution from `hourly_series` (which still includes the partial bucket) → z-score / percentile / magnitude are computed against a contaminated denominator. The "exclude partial" path only drops aggregates, not the comparator.
- Z-score can fire with `n=2` samples (post-cold-start), producing fake EXTREME magnitude until the rolling window fills.
- `lookup_oi_pair` returns `oi_delta=0` (treated as "OI plano") when `steps_back=0` — happens with very recent anchors against coarse OI granularity. Should return `None`.

## Code review standard (based on google/eng-practices)

When reviewing or finishing a CL in this repo, apply this checklist. Invoke via `/review-cl` to get an automated pass over the current `git diff`.

### Blocking (must fix before merge)
1. **Two-backend sync.** Any change to data logic (fetch / process / response shape) must be mirrored between `backend/main.py` and `api/index.py`. After editing, grep both files and confirm parity. Drift documented in "Known drift today" must shrink, not grow.
2. **Smoke tests pass.** `python scripts/smoke_tests.py` exits 0 when the CL touches either backend.
3. **No magic numbers.** Hardcoded thresholds (z-scores, hedge ratios, cache TTLs, lookback windows, etc.) live as named constants at module top, not inline.
4. **Comments explain WHY.** Comments restating what the code does are noise. Comments explaining a non-obvious decision, a known gotcha, or the rationale for a threshold are mandatory. Model: the existing comments in `process_dune_netflows` and the CEX netflow `direction` logic.
5. **Mirror file comments.** If a function exists in both `backend/main.py` and `api/index.py`, the explanatory comments must be in both, not just one.

### Important (fix in this CL or as immediate follow-up)
6. **CL size.** A CL doing more than one logical thing should be split. Natural splits in this repo: (a) new data source fetch + propagation, (b) processing logic + constants + test, (c) frontend rendering. Target: <200 net lines per CL.
7. **API response field naming.** Response fields are `camelCase`. Backend internals are `snake_case`. Don't mix at the boundary.
8. **New derived metrics need coverage.** Any new computed field (basis, skew, hedge ratio, regime label) needs either a `smoke_tests.py` assertion or a unit test demonstrating expected behavior on known inputs.

### Nits (non-blocking)
- PEP8. No semicolon-joined statements.
- Frontend: `useMemo` for derived arrays/objects inside panels. Class-level constants (color maps, label maps) live outside the component function.
- Colors via theme constants, not hex inline.
- Commit subject ≤ 70 chars. Body explains the *why* when the subject can't.

### Reviewer's mantra
> Approve once the CL definitely improves overall code health, even if imperfect.
> Push back on design or scope, not on style preferences covered by these rules.
