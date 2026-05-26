---
tags: [metric, whales, retail]
---

# Whale vs Retail divergence

Multi-exchange L/S ratio aggregate vs Binance top-traders. Surfaces structural disagreement between small-account positioning and large-account positioning.

## Sources

| Side | Source | Type |
|---|---|---|
| Retail | Binance `globalLongShortAccountRatio` + OKX `long-short-account-ratio` + Bybit `account-ratio` | Account-weighted, averaged across 3 exchanges |
| Whales | Binance `topLongShortPositionRatio` | Position-weighted, top 20% by size, **Binance only** |

OKX and Bybit don't expose a clean whale-vs-retail split. So whale = Binance only.

## Outputs in `whaleVsRetail`

```python
exchanges: { period: { binance_retail | okx_retail | bybit_retail | binance_whale: {longPct, shortPct, ratio, history} } }
aggregate: { period: { longPct, ratio, exchangeCount } }   # retail avg of 3 exchanges
deltas:    { period: { retail | whale: {delta1h, delta4h, delta24h} } }
divergenceSeries1h: [{ts, value: retail_longPct - whale_longPct}, ...]
divergence: { current, zScore, percentile, samplesN }       # vs trailing 7d
confluence: { whaleDirection, netflowDirection, fundingLevel, reading, interpretation }
exposure:  { binanceOiUsd, whale:{...}, retail:{...} }      # Pareto 75/25 USD estimate
```

## Z-score of divergence

The gap `retail_longPct − whale_longPct` is tracked over the last 168h (1h period). z-score of current vs trailing 7d tells you whether the current gap is statistically extreme or typical noise.

Reliability gate: `samplesN ≥ 12` (we need a meaningful denominator before reporting z).

## USD exposure (Pareto 75/25 assumption)

For the "long $ vs short $" by cohort:

```python
WHALE_OI_SHARE = 0.75   # heuristic: top 20% accounts hold ~75% of OI
RETAIL_OI_SHARE = 0.25  # other 80% hold the remaining 25%
```

This is **NOT measured data** — Binance doesn't publish per-cohort OI split. The 75/25 is a documented Pareto heuristic. Surfaced in the frontend with a visible disclaimer.

Whale long $ = `binance_oi_usd × 0.75 × whale_longPct`

## Confluence label

Combines whale direction + CEX netflow direction + funding level into a single read:

| whale | netflow | funding | reading |
|---|---|---|---|
| LONG | BULLISH | NORMAL/LOW | CONFIRMED_BULL |
| SHORT | BEARISH | any | CONFIRMED_BEAR |
| LONG | BEARISH | HIGH | DIVERGENT_WHALE_WRONG_SIDE |
| SHORT | BULLISH | any | DIVERGENT_WHALE_WRONG_SIDE |
| LONG | * | HIGH | OVERHEATED_LONG |
| SHORT | * | NEGATIVE | OVERHEATED_SHORT |
| NEUTRAL | * | * | WHALES_UNDECIDED |
| else | * | * | MIXED |

## See also

- [[Binance]]
- [[OKX]]
- [[Bybit]]
- [[Z-score (CEX netflows)]] (similar z-score methodology applied here)
