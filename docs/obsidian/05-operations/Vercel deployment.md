---
tags: [ops, runbook, vercel]
---

# Vercel deployment

Primary production deploy target. See [[Deploy targets]] for architecture context.

## Continuous deploy

Every push to `master` triggers a new Vercel deploy automatically. Build:

```
cd frontend && npm install && npm run build
```

Output: `frontend/dist/` (static), `api/index.py` (Lambda).

## Manual redeploy (when env vars change)

Env vars don't inject into existing deploys (see [[Environment variables]]). Procedure:

1. Vercel dashboard → your project → **Deployments**
2. Find the latest "Ready" deploy
3. Three-dot menu → **Redeploy**
4. **UNCHECK** "Use existing Build Cache" — you want a clean build so new env vars get picked up
5. Confirm

Wait ~2 min for build, then ~5–30 min for any data caches to expire.

## maxDuration ceiling

```json
"functions": { "api/*.py": { "maxDuration": 60 } }
```

60s is the hard ceiling for a single Lambda invocation. Anything that fetches must fit. Mitigations baked into `api/index.py`:

- Dune trigger is fire-and-forget (no waiting for fresh execution)
- DefiLlama reserves fetched in parallel per exchange
- ETF/stables/macro have aggressive caches so most requests skip them
- Heavy backfill scripts never run on Vercel — that's `backend/main.py` territory

## Health check

`/api/health` returns cache ages for each slow-path source:

```json
{ "etf_age": 234, "risk_free_age": 1230, "stables_age": 870 }
```

Used by `scripts/smoke_tests.py --wait` to wait for caches to warm.

## See also

- [[Deploy targets]]
- [[Environment variables]]
- [[Smoke tests]]
