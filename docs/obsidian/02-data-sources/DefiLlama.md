---
tags: [data-source, defillama]
---

# DefiLlama

Two distinct data products from llama.fi, both free, no API key:

## 1. CEX ETH reserves (`/protocol/<slug>`)

Per-exchange ETH balance held on-chain. Used in the Capital Map panel and as part of CEX netflows context.

Slugs we hit: `binance-cex`, `okx`, `bybit`, `bitfinex`, `crypto-com`, `kucoin`, `gate`, `kraken`, `coinbase-cex`, `huobi`.

Returns ETH amount + stablecoin reserves + 90d daily history.

Cache TTL: 1 hour.

## 2. Stablecoin supply (`/stablecoincharts/all`)

Total stablecoin USD circulating + per-stable (USDT, USDC) daily history. Used for stablecoin supply delta as a context signal.

Cache TTL: 30 min.

## See also

- [[Dune (CEX netflows)]]
