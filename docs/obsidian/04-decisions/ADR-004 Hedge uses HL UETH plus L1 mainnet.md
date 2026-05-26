---
tags: [adr, whales, hedge]
status: accepted
date: 2026-05
---

# ADR-004 — Hedge ratio uses HL UETH + L1 mainnet

**Status**: Accepted
**Context**: The [[Hyperliquid]] Whales panel needed to distinguish between a SHORT perp that's a directional bet vs one that's hedged by spot ETH held elsewhere.

## The problem

Same trade has opposite interpretation:

```
Whale 0x50b SHORT 47.6k ETH perp ($100M, 23x)
  ├─ holds 50k ETH spot → it's a HEDGE, neutral-ish
  └─ holds 0 ETH spot → it's a DIRECTIONAL BET, materially bearish
```

V1 of the panel only checked HL UETH (wrapped ETH on Hyperliquid chain). That misses 99% of real spot custody because most whales hold ETH on **Ethereum L1 mainnet**, not on HL.

Without mainnet data, every whale appeared as `DIRECTIONAL_BET`. The label was useless.

## The decision

```python
total_spot_eth = ueth_spot_HL + mainnet_eth_L1
hedge_ratio = min(total_spot_eth / perp_size_eth, 1.0)  # SHORT only
```

Sources:
- `ueth_spot_HL`: from `spotClearinghouseState` (POST /info, no key)
- `mainnet_eth_L1`: from Etherscan `balancemulti` V2 (`chainid=1`)

Both treated as **real ETH custody** — they back the perp position equivalently.

## Threshold labels

Thresholds extracted to named constants in [[ADR-005 Code review standard|CL-A]] follow-up:

```python
HEDGE_FULL_THRESHOLD = 0.8        # SHORT ≥80% covered → FULLY_HEDGED (neutral)
HEDGE_PARTIAL_THRESHOLD = 0.3     # SHORT 30–80% covered → PARTIAL_HEDGE
DOUBLE_BULL_SPOT_FRACTION = 0.3   # LONG ≥30% covered → DOUBLE_BULL (concentration)
```

Label is `DIRECTIONAL_BET` otherwise.

## What we don't track (yet)

- **ERC-20 tokens** (USDC, USDT, WBTC) — could matter for stablecoin-quoted bets. Not added because it's not part of "ETH exposure".
- **DeFi positions** (staked ETH in Lido, Aave collateral, etc.) — those are also real ETH exposure but require per-protocol queries. Deferred until we have a use case.
- **Other chains** — L2s (Arbitrum, Optimism, Base) could hold whale ETH. Etherscan V2 supports them via `chainid`, but our query only checks mainnet.

## Practical gotcha

The default whale list in `HYPERLIQUID_DEFAULT_WHALES` is mostly **trading wallets** (where the perp positions live), not **custody wallets** (where the spot ETH actually sits). So `mainnet_eth` shows dust (~0.0002 ETH).

For real hedge detection to work, you need to:
1. Open a whale wallet on Etherscan
2. Trace inbound transfers to find the source wallet that bridged ETH to it
3. Add the source (custody) wallet to `HYPERLIQUID_WHALE_ADDRESSES` env

## See also

- [[Hyperliquid]]
- [[Etherscan (mainnet ETH)]]
- [[Hedge ratio + label]]
