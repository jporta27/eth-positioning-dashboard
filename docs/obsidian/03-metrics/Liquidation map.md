---
tags: [metric, liquidations, heuristic]
---

# Liquidation map

Heuristic estimate of where leveraged long/short positions would liquidate. Two layers:

## Layer 1 — Heuristic (existing)

`estimate_liquidation_map(oi_value, spot_price, funding_rate)`:

1. Hardcoded leverage tier distribution (2x–100x with assumed % of OI per tier)
2. Long bias derived from funding rate (`long_bias = clamp(0.5 + funding * 5000, 0.3, 0.7)`)
3. Per-tier liq price: `entry × (1 ± 0.8/leverage)` (80% MMR assumption)
4. Result: clusters of {leverage, longLiqPrice, shortLiqPrice, longOiUsd, shortOiUsd}

**Honest about its limitations.** The distribution is GUESS, not measured. The long_bias formula saturates at 0.7 with any positive funding. The MMR is single-value across exchanges (real varies 0.4%–5%).

Good for: identifying rough zones, comparing long vs short side relative magnitudes.
Bad for: predicting exact liq cascade $ amounts.

## Layer 2 — Real (from [[Hyperliquid]] Whales)

For each tracked whale with a `liq_px`, we bucket the liq price into $25 bins and produce `liqClusters`:

```python
{ priceLevel, side, sizeUsd, count }
```

These are **real** liquidation prices for known whale wallets, not heuristics. Shown as chips in the Hyperliquid Whales panel.

## How to validate

Compare visually against Coinglass.com/LiquidationData or coinank.com. If our heuristic clusters fall within ±20% of Coinglass density zones → directionally useful. If ±50% off → the distribution heuristics are wrong for the current regime.

In recent comparisons, our 20x cluster (±4%) matched Coinglass density best. The 100x cluster (±0.8%) matched what Coinglass calls "long max pain". The 10x cluster (±8%) was generally too far from where real density sat.

## Improvements pending

- Replace `long_bias` heuristic with real Binance `topLongShortAccountRatio` (data we already fetch elsewhere)
- Use per-exchange MMR tables instead of `0.8 / lev`
- Calibrate tier distribution from historical Binance `allForceOrders` (last ~1000 liquidations)

None of these have been implemented.

## See also

- [[Binance]]
- [[Hyperliquid]]
