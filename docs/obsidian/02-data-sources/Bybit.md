---
tags: [data-source, bybit]
---

# Bybit

Used for: ETHUSDT funding, OI, L/S account ratio, options chain (greek + OI).

Endpoints:
- `/v5/market/tickers` (funding + OI + IV)
- `/v5/market/account-ratio` (L/S, account-weighted)

Multi-period L/S for [[Whale vs Retail divergence]]: 5min, 15min, 1h, 4h, 1d.

Bybit `account-ratio` returns `buyRatio` and `sellRatio` (both 0-1 decimals). We compute:
```python
ratio = buyRatio / sellRatio
long_pct = buyRatio
short_pct = sellRatio
```

No API key.

## See also

- [[Binance]]
- [[OKX]]
- [[Whale vs Retail divergence]]
