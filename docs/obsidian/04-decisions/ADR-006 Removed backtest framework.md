---
tags: [adr, scope]
status: accepted
date: 2026-05
---

# ADR-006 — Removed backtest framework

**Status**: Accepted
**Context**: We built a backtest framework (`backtest/` module, `reports/` outputs, conditioning logic) over multiple weeks. Then realized it wasn't being consumed by the frontend or any decision-making loop.

## The decision

**Delete** `backtest/`, `reports/*.json`, `reports/_archive_pre_purge/`, and the related scripts (`run_all_presets.py`, `run_conditioned.py`, `build_*_summary.py`).

Keep:
- `data/backfill/*.parquet` — historical Parquet data is still useful for any future analysis
- `scripts/backfill.py` — the downloader
- `scripts/smoke_tests.py` — tests the live backend

## Why we removed it

After a comprehensive audit (4 parallel agents across the codebase) found 4 HIGH-severity issues in the backtest module:

- **R1**: Deflated Sharpe formula bug (excess vs raw kurtosis confused) — invalidated all 70 reports
- **R2**: ETF events fired at midnight UTC but Farside publishes ~21 UTC same day → look-ahead bias of ~1 day
- **R3**: Macro regimes used Yahoo close from same session → causality violation
- **R4**: Stables events fired at midnight UTC of day D but the value was end-of-day D → look-ahead

Each one alone would have required re-running everything. Combined, the maintenance burden didn't justify keeping code nobody consumed.

The user explicitly said the framework wasn't being used in the frontend and was OK to delete.

## What we lost

- 10 preset signal reports
- 60 regime-conditioned reports
- 2 narrative summary markdowns
- Statistical scaffolding (event study, IC, bootstrap CI, Deflated Sharpe)

All reversible: it's still in git history at `ed913b2`.

## What we kept

- Lessons: bias-detection rules (look-ahead, causality) inform any future analytics
- The audit findings (documented elsewhere in this vault)
- Parquet data (so we can rebuild from scratch if needed)

## When to revisit

If/when the dashboard adds a "research" tab that needs historical signal validation. At that point we'd:
- Rebuild the framework with the bugs fixed from the start
- Add it to the dashboard's consumption path
- Document it in this vault

## See also

- [[Backfill scripts]]
- [[ADR-005 Code review standard]]
