---
tags: [architecture, critical]
---

# Two-headed backend

The most important shape rule in this codebase. **There are two backend entry points serving the same logic, and they must be kept in sync.**

## The two heads

| File | Process model | Used in |
|---|---|---|
| `backend/main.py` | Long-running uvicorn process | Local dev, Docker, [[Deploy targets\|Railway]] |
| `api/index.py` | Vercel serverless Lambda, `maxDuration: 60s` | [[Deploy targets\|Vercel]] (primary prod) |

They differ in process lifecycle, **not in data logic**.

## Why two files

Vercel's serverless model can't run background tasks. `backend/main.py` has a `slow_data_refresher` async task that polls expensive sources (ETF flows, stablecoin supply, macro data, risk-free rate) every 60s and serves cached values from a 10s hot path. That's the right shape for a long-running process.

On Vercel, every request is a cold-able Lambda. We can't keep a `slow_data_refresher` alive. So `api/index.py`:

- Fetches everything in a fan-out per request (cached in process memory between warm invocations)
- Uses **fire-and-forget** Dune triggers (POST execute, don't await — next request picks up the result)
- Pares any logic that can't fit a single Lambda invocation budget

See [[ADR-001 Two backend mirror]] for the full rationale.

## The sync rule

When changing data logic (any fetcher, any `process_*` function, any response shape), **you must update both files**. After editing:

1. `grep -n <function_name> backend/main.py api/index.py` — both files should appear
2. Diff the functions mentally — they should be semantically equivalent
3. Comments and docstrings should also match (see [[ADR-005 Code review standard]] rule 5)
4. Run [[Smoke tests]] against the local backend to catch shape regressions

## Known divergences as of this writing

- `backend/main.py` has [[Backfill scripts]] integration (Parquet persistence)
- `api/index.py` skips the persistence layer (no disk writes from Lambda)
- Cache TTLs differ slightly (10s vs 15s in some places — documented in code comments)

These are intentional, scoped, and live in clearly-marked sections.

## What goes in which

- **Hot path** (≤10s data): served by `/api/data`, includes everything the [[Frontend layout]] consumes per refresh
- **Slow path** (1h+ cache): ETF, stables, macro, options expiries — refreshed by background task in main.py, refreshed on-demand in api/index.py

## See also

- [[ADR-001 Two backend mirror]]
- [[ADR-002 Dune partial bucket exclusion]]
- [[Frontend layout]]
- [[Smoke tests]]
