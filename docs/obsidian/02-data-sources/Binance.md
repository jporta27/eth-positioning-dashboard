---
tags: [data-source, binance]
---

# Binance

Primary source for spot price, perp price, funding, OI, L/S ratio, klines, and taker volume.

## Endpoints used

| Path | What we get |
|---|---|
| `/fapi/v1/premiumIndex` | Funding rate, next funding time |
| `/fapi/v1/openInterest` | OI in contracts |
| `/fapi/v1/klines` | 1h/5m klines (price + volume) |
| `/futures/data/globalLongShortAccountRatio` | Retail L/S (account-weighted, all accounts) |
| `/futures/data/topLongShortPositionRatio` | Whale L/S (top 20% by position size) |
| `/futures/data/takerBuySellVol` | Taker delta |
| `/futures/data/openInterestHist` | OI history (last 30d only) |
| `/api/v3/klines` (spot) | Spot klines for [[ETH-BTC rotation]] |
| `/api/v3/ticker/24hr` (spot) | Spot 24h ticker (price change %, volume) |

All public, no API key required. Rate limits observed at our cadence: none.

## L/S ratio — two different things

This bites people. Binance exposes **three** L/S endpoints:

1. `globalLongShortAccountRatio` — % of **accounts** long vs short (across all accounts). **Account-weighted**. Skews retail because most accounts are small.
2. `topLongShortAccountRatio` — same but only **top 20% by margin balance**. Still account-weighted.
3. `topLongShortPositionRatio` — top 20% by **position size**, **position-weighted**. 1 position of $100M counts more than 100 positions of $1M.

We use #1 for retail and #3 for whales. They're **not directly comparable** in $ terms — see [[Whale vs Retail divergence]] for how we reconcile.

## Funding rate ranges (for [[Whale vs Retail divergence]] confluence)

| Funding/8h | Label | APR proxy |
|---|---|---|
| ≥ +0.0003 | HIGH | ≥ +33% APR — longs paying caro |
| 0 → +0.00005 | LOW | < +5% APR — calm |
| < -0.0001 | NEGATIVE | shorts paying longs |

## Multi-period fetching

For the [[Whale vs Retail divergence]] panel we fetch L/S at 5m, 15m, 1h, 4h, 1d periods to allow timeframe selection in the UI.

## See also

- [[Whale vs Retail divergence]]
- [[OKX]]
- [[Bybit]]
