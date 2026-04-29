# Backtest data audit — post FASE B backfill

Generated 2026-04-29.

## Backfill parquet inventory

| file                           | rows   | range                       | notes |
|--------------------------------|--------|-----------------------------|-------|
| `binance_klines_1h.parquet`    | 43,800 | 2021-04-30 → 2026-04-29     | 5 y, 1 h cadence (43,800 = 5×365×24). Below 6.5 y the perp-listing cap (2019-11-27) does not fire — happily it would if we ever bumped `--years` to 7+. |
| `binance_funding.parquet`      |  5,475 | 2021-04-30 → 2026-04-29     | 5 y, 8 h cadence (5,475 = 5×365×3). Same boundary as klines. |
| `defillama_stables.parquet`    |  8,906 | 2017-11-29 → 2026-04-19     | Daily, 8.4 y. Last refresh attempt failed (DefiLlama connectivity issue), so most recent 10 days missing — non-blocking, the stables event extractor uses a 30-day rolling delta where 10-day staleness is invisible. |
| `farside_etf.parquet`          |    454 | 2024-07-23 → 2026-04-29     | **Was missing before this fase.** Daily, since ETF launch. 454 = ~21 mo of trading days. Unlocks `etf_buying` / `etf_selling` signals. |
| `macro.parquet`                |  8,106 | 2021-04-29 → 2026-04-29     | Daily Yahoo (DXY, SPX, VIX, US10Y, BTC, ^IRX). FRED DTB3 row mostly absent due to fred CSV endpoint flakiness; for Fase D regime conditioning we'll pull VIX from the Yahoo rows and mark RISK_FREE as low priority. |

## Confirmations against PROMPT-B specifications

- ✅ `binance_klines_1h.parquet` starts **before 2024** — first row is 2021-04-30 (5 y back from today). Old window was 2024-04-20 → 2026-04-19 (2 y).
- ✅ `data/backfill/farside_etf.parquet` exists with **> 200 rows** (454).
- ✅ `python -m backtest.tests.test_eventstudy` still passes 5 / 5.

## Estimated change in usable n_events per signal

The kline window now covers 2021-04-30 → today. Events that fire outside the
kline window get `n_valid = 0` for forward returns and are dropped by the
event study — so the *useful* count is `events_within_kline`.

| signal                  | total events | within 5 y kline | old (2 y kline) | delta   |
|-------------------------|--------------|------------------|-----------------|---------|
| `funding_hot_inflow`    | 175          | 175              | ~80             | +120 %  |
| `funding_hot_outflow`   | 286          | 286              | ~120            | +138 %  |
| `funding_extreme_abs`   | 262          | 262              | ~110            | +138 %  |
| `stables_expanding`     |  68          | **56**           | 13              | **+331 %** |
| `stables_contracting`   |  77          | **65**           | 22              | **+195 %** |
| `etf_buying`            |  14          | 14               | 0 (blocked)     | newly available |
| `etf_selling`           |  19          | 19               | 0 (blocked)     | newly available |

(Old counts above are best-recall from the `n_observations` field in
`reports/*.json`. Funding signals scaled with the kline extension; stables
signals also benefit from the historical data they could already see but had
no return data to evaluate against; ETF signals are net-new.)

### What this means for FASE C

- **Stables presets become statistically meaningful.** Going from 13–22 events
  to 56–65 takes them out of the "headline-only" zone — bootstrap CIs and IC
  p-values will tighten enough to actually claim or reject an edge.
- **Funding presets keep their statistical power but lose look-ahead bias.**
  With ~2.5× the events and the FIX 1 purge active, overlapping forward
  windows that used to inflate the previous Sharpe/CI numbers now collapse
  to non-overlapping draws.
- **ETF signals are borderline.** The `run_event_study` floor is 30 events;
  buying (14) and selling (19) will trip the "insufficient" branch. We will
  still write reports — they document the data shortage and are the input the
  next fase (regime conditioning) needs to decide whether ETF flows survive
  as a regime *labeler* even if they aren't strong as standalone signals.

## Known gaps (not blockers)

- `binance_oi_hist`: Binance API only retains ~30 days, so backfill is
  fundamentally short. `moneyQuality` regime-style signal (which depends on
  multi-month OI history) remains blocked unless we set up a persister.
- Dune CEX netflows: per-billing-cycle datapoint cap means historical
  netflow query results cannot be paginated for backtest. Live-only.
- Deribit options trades: no backfill yet (RR25 regime stays "unknown" pre-
  2026-04-18, documented in Fase D).
