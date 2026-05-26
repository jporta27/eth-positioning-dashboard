---
tags: [ops, runbook, parquet]
---

# Backfill scripts

`scripts/backfill.py` — bulk historical downloads to `data/backfill/<source>.parquet`.

## What it downloads

| Source | History | API limit |
|---|---|---|
| `binance_klines_1h` | up to 5y (capped at perp listing Nov 2019) | unlimited |
| `binance_funding` | up to 5y (same cap) | unlimited |
| `binance_oi_hist` | last 30d only | hard limit on Binance side |
| `defillama_stables` | full history (~2017+) | unlimited |
| `macro` (Yahoo + FRED) | up to 5y | unlimited |
| `farside_etf` | full history (~2024+) | curl fallback needed |

## Usage

```bash
# Single source
python scripts/backfill.py --sources binance_klines_1h --years 5

# Multiple
python scripts/backfill.py --sources binance_klines_1h,binance_funding,macro --years 5

# Everything (default years=2)
python scripts/backfill.py --sources all
```

Output: `data/backfill/<source>.parquet`. Each parquet shares `ts_utc_ms` + `schema_version` + source-specific columns.

## Why we keep it

Per [[ADR-006 Removed backtest framework]]: the backtest framework was deleted but **the parquet data is kept**. It supports any future analysis (regime studies, manual queries, exporting to other tools). Costs nothing to retain.

## Live runtime persistence

Separate from backfill — when `PERSIST_ENABLED=true` in the env, `backend/main.py` writes a snapshot every fetch tick to `data/YYYY-MM-DD/snapshot.parquet`. This is OFF on Vercel (no disk), ON when running long-lived (Railway/local).

## See also

- [[Local dev setup]]
- [[ADR-006 Removed backtest framework]]
