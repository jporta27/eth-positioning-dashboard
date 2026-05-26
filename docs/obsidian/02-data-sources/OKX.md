---
tags: [data-source, okx]
---

# OKX

Used for: ETH-USDT-SWAP funding, OI, L/S account ratio.

Endpoints:
- `/api/v5/public/funding-rate` (funding)
- `/api/v5/public/open-interest` (OI)
- `/api/v5/rubik/stat/contracts/long-short-account-ratio` (L/S, account-weighted)

Multi-period L/S fetched for [[Whale vs Retail divergence]]: 5m, 15m, 1H, 4H, 1D.

OKX returns L/S as a single ratio number (not separate long/short pct). We compute:
```python
long_pct = ratio / (1.0 + ratio)
short_pct = 1.0 - long_pct
```

No API key, no rate limit issues at our cadence.

## See also

- [[Binance]]
- [[Bybit]]
- [[Whale vs Retail divergence]]
