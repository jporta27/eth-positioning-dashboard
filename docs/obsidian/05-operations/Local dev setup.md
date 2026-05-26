---
tags: [ops, runbook, dev]
---

# Local dev setup

## One-time

```bash
# Python deps
cd backend && pip install -r requirements.txt

# Frontend deps
cd ../frontend && npm install
```

Python 3.12 recommended (matches the Vercel runtime). Earlier versions may work.

## Env vars

Create `backend/.env` with at least `DUNE_API_KEY` and `DUNE_QUERY_ID`. See [[Environment variables]] for the full list.

## Run

Two terminals:

```bash
# Backend (port 8000)
cd backend && python -m uvicorn main:app --port 8000

# Frontend (port 5174, proxies /api/* to localhost:8000)
cd frontend && npm run dev
```

Then open http://localhost:5174.

## Auto-reload

Backend: NOT enabled by default (uvicorn without `--reload`). Add `--reload` if you want live restart on save:

```bash
python -m uvicorn main:app --port 8000 --reload
```

Frontend: Vite has HMR by default.

## Smoke check after each backend change

```bash
python scripts/smoke_tests.py
```

See [[Smoke tests]].

## Run the hedge_label unit tests

```bash
python -m backend.tests.test_hedge_label
```

## See also

- [[Smoke tests]]
- [[Environment variables]]
- [[Backfill scripts]]
