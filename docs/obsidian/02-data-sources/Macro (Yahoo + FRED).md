---
tags: [data-source, macro]
---

# Macro (Yahoo + FRED)

Cross-asset context: DXY, SPX, VIX, US10Y, BTC, risk-free rate.

## Endpoints

| Source | URL pattern | What |
|---|---|---|
| Yahoo Finance v8 chart | `query2.finance.yahoo.com/v8/finance/chart/<symbol>` | Daily closes, range param ("5y", "max") |
| FRED CSV | `fred.stlouisfed.org/graph/fredgraph.csv?id=DTB3` | T-bill yield fallback |

Symbols used: `DX-Y.NYB` (DXY), `^GSPC` (SPX), `^VIX` (VIX), `^TNX` (US10Y), `BTC-USD` (BTC), `^IRX` (13-week T-bill = risk-free).

## Risk-free rate

Yahoo `^IRX` is primary, FRED `DTB3` is fallback. Used by `compute_options_skew` for Black-Scholes delta interpolation.

Sanity-check band: rate ∈ (0.001, 0.20). Outside that range = provider data corruption.

## Cache TTL

5 min (Yahoo intraday), 1 day (FRED daily).

## See also

- [[Deribit]] (options pricing consumer)
