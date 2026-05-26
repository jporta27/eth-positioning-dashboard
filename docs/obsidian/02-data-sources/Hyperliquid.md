---
tags: [data-source, hyperliquid, whales]
---

# Hyperliquid

Used for the **HL Whales panel** — per-address perp + spot snapshot of a curated list of whale wallets.

## Endpoints

All POST to `https://api.hyperliquid.xyz/info` with a JSON body:

| `type` | What it returns | What we use |
|---|---|---|
| `clearinghouseState` | Perps state (positions, margin, leverage) | Size, side, entry, liq price, leverage, margin type, uPnL |
| `spotClearinghouseState` | HL chain spot balances per token | UETH (wrapped ETH on HL), USDC, HYPE |
| `metaAndAssetCtxs` | Asset metadata + funding + OI | Already used for the perp panel |

No API key required, no rate limit observed at our cadence.

## Per-address bundle shape

```python
{
  addr_lower: {
    "perp": <clearinghouseState dict>,
    "spot": <spotClearinghouseState dict>,
    "mainnetEth": <float, from Etherscan>,  # see [[Etherscan (mainnet ETH)]]
  }
}
```

## Curated whale list

The default seed list (5 addresses, in `HYPERLIQUID_DEFAULT_WHALES`) comes from public Onchain Lens posts.

The user can extend it via env:
```
HYPERLIQUID_WHALE_ADDRESSES=0xabc,0xdef,0x123,...
```

**Important**: there is no public Hyperliquid "leaderboard" endpoint. We can't auto-discover whales. The list is hardcoded. To find more, scrape sources like:
- Onchain Lens / Lookonchain posts
- HypurrScan UI (Cloudflare-protected so we can't API-scrape)
- Manual: follow on-chain deposit traces to find big depositors

## Why both perp AND spot

Without spot context, a "47k ETH SHORT perp" looks like a directional bet. But the same trader may hold 50k ETH on L1 mainnet as a hedge. We track both to label correctly.

See [[Hedge ratio + label]] for the classification logic.

## Cache TTL

5 minutes. Whale positions move slow enough that more frequent polling burns API rounds without new info.

## See also

- [[Etherscan (mainnet ETH)]]
- [[Hedge ratio + label]]
- [[ADR-004 Hedge uses HL UETH plus L1 mainnet]]
