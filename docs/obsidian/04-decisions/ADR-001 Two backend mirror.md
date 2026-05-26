---
tags: [adr, architecture]
status: accepted
date: 2026-Q2
---

# ADR-001 — Two backend mirror

**Status**: Accepted
**Context**: Need to ship the dashboard to Vercel (free tier, serverless) while keeping a long-running backend for local dev, persistence, and Railway backup.

## The decision

Maintain **two backend files** with semantically equivalent code:

- `backend/main.py` — uvicorn process, long-running
- `api/index.py` — Vercel serverless Lambda

## Why not just one

### Option A: Only `backend/main.py`, deploy to Railway
Railway works but: more expensive, single region, no edge CDN integration, can't take advantage of Vercel's free tier for the static + API combo.

### Option B: Only `api/index.py`, deploy to Vercel
Lambda model breaks:
- No background task → can't pre-warm slow caches (ETF, stables, macro have 1h+ TTL and 5–30s fetch latency)
- 60s `maxDuration` → some flows (full Dune polling, 5y backfill) just can't fit
- No disk persistence → can't write parquet snapshots

### Option C: Mirror (chosen)
Accept the duplication cost in exchange for:
- Local dev keeps the rich process model
- Railway is a deploy-target backup if Vercel breaks
- Persistence + backfill keep working in the long-running shape
- Vercel still gets the same data contract through its serverless mirror

## Consequences

### Positive
- Both deploy targets work, neither is constrained by the other
- Backfill scripts (parquet writes) don't bloat the Vercel bundle
- Local dev iteration is fast (no Lambda emulator needed)

### Negative
- **Every change to data logic must be made twice.** This is the recurring cost.
- Drift risk: easy to forget to mirror. We've already accumulated some [[Two-headed backend|known drift]].
- Code review must verify mirror sync — encoded as rule 1 in [[ADR-005 Code review standard]].

## Mitigations adopted

1. CLAUDE.md documents the rule loudly at the top
2. Code review checklist enforces sync explicitly
3. Functional names match across files (greppable)
4. Existing drift is documented, not hidden

## See also

- [[Two-headed backend]]
- [[ADR-005 Code review standard]]
- [[Deploy targets]]
