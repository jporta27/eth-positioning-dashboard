---
tags: [metric, eth-btc, rotation]
---

# ETH-BTC rotation

Signal that surfaces dominance shifts: ETH outperforming BTC = potential risk-on alt rotation.

## Inputs

- ETHBTC spot ratio (from Binance `api.binance.com/api/v3/klines`, symbol `ETHBTC`)
- ETH/BTC taker buy-sell ratio per hour (we synthesize from Binance perp taker volumes)

## Outputs

```
currentRatio:      ratio of ETH taker-buy : BTC taker-buy in last hour
avg24h, avg7d:     historical baselines
priceChange7dPct:  ETHBTC spot change last 7d
currentPrice:      ETHBTC spot
volume24hEth:      total ETH spot volume in last 24h
signal:            BALANCED | ROTATING_TO_ETH | ROTATING_FROM_ETH
hourly:            time series of ratio + volume
```

## When the signal fires

`ROTATING_TO_ETH` when currentRatio > avg7d AND priceChange7dPct > 0.
`ROTATING_FROM_ETH` when currentRatio < avg7d AND priceChange7dPct < 0.

`BALANCED` otherwise.

## See also

- [[Binance]]
