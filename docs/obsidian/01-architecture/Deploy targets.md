---
tags: [architecture, deployment]
---

# Deploy targets

Two deployment paths, both connected to the same git repo.

## Vercel (primary)

| Setting | Value | Why |
|---|---|---|
| Region | `sin1` | Closest to most Asian exchanges (Binance, OKX) |
| Function | `api/index.py` | All `/api/*` routes |
| Runtime | `@vercel/python@4.5.0` | Python 3.12 |
| `maxDuration` | 60s | **Hard ceiling for fan-out fetches**; informs cache TTL choices |
| Memory | 1024 MB | Enough for parquet+pyarrow |

Rewrites in `vercel.json`:

```json
{ "source": "/api/(.*)", "destination": "/api/index.py" },
{ "source": "/((?!api/).*)", "destination": "/frontend/dist/index.html" }
```

Build command builds the frontend Vite bundle, served from `frontend/dist/`.

**The 60s budget** is why `api/index.py` uses [[ADR-002 Dune partial bucket exclusion|fire-and-forget]] for Dune triggers and aggressive parallelism for the fan-out fetches.

## Railway (backup)

| Setting | Value |
|---|---|
| Build | `Dockerfile` (2-stage: frontend build → Python runtime) |
| CMD | `uvicorn backend.main:app --host 0.0.0.0 --port 8000` |
| Healthcheck | `/api/health`, 30s timeout, restart on failure |

Railway runs `backend/main.py` long-running. Used when we need:
- Persistence layer enabled (`PERSIST_ENABLED=true`)
- Background refresher running (60s slow-path)
- No 60s ceiling (e.g. running full Dune polling)

## Pick-which-target decision

| Need | Use |
|---|---|
| Public web dashboard | Vercel |
| Backfill data collection | Local + Railway |
| Heavy compute (5y backfill) | Local only |

## Env var matrix

See [[Environment variables]].

## See also

- [[Two-headed backend]]
- [[ADR-001 Two backend mirror]]
- [[Vercel deployment]] (runbook)
