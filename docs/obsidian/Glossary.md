---
tags: [glossary, reference]
---

# Glossary

Cross-cutting terms used throughout the dashboard. Linked from anywhere they appear.

## A–D

- **ADR** — Architecture Decision Record. Lightweight markdown capturing the **why** of a non-obvious choice. See [[ADR-001 Two backend mirror|the index]].
- **API V1 (Etherscan)** — Deprecated; returns NOTOK status=0. Use V2 with `chainid=1`. See [[Etherscan (mainnet ETH)]].
- **balancemulti** — Etherscan endpoint that batches up to 20 wallet balances per call. See [[Etherscan (mainnet ETH)]].
- **BF25** — 25-delta butterfly. Measures curvature of the IV smile. See [[Options skew (RR25, BF25)]].
- **Bias** — Final user-facing label combining [[Z-score (CEX netflows)|magnitude + direction]]: `BULLISH`, `BEARISH`, `NEUTRAL`, `BULLISH_MILD`, `BEARISH_MILD`.
- **Clearinghouse state** — Hyperliquid's per-address position snapshot. Two flavors: perps (`clearinghouseState`) and spot (`spotClearinghouseState`). See [[Hyperliquid]].
- **CL** — Change List. The unit of code review per [[ADR-005 Code review standard]].

## E–H

- **`DIRECTIONAL_BET`** — One of the `hedge_label` enum values: SHORT perp with <30% spot coverage, or LONG perp with <30% spot. See [[Hedge ratio + label]].
- **`DOUBLE_BULL`** — `hedge_label` for LONG perp with ≥30% spot. Concentration play, not a hedge.
- **`FULLY_HEDGED`** — SHORT perp with ≥80% spot coverage. Effectively market-neutral.
- **Gamma flip** — Strike where dealer net gamma flips sign. Above = stabilizing flows, below = amplifying. See [[Options skew (RR25, BF25)]].

## I–O

- **JA3 / TLS fingerprint** — How [[Farside ETF]] blocks httpx requests; bypassed with `curl` subprocess.
- **`maxDuration`** — Vercel Lambda hard ceiling. 60s for this project. See [[Deploy targets]].
- **Max pain** — Strike where the most $ of options expire worthless. Magnetic for spot near expiry.
- **MOC** — Map of Content. Index note that links a cluster of related notes. [[Home]] is the top-level MOC.
- **MMR** — Maintenance Margin Ratio. Used in liquidation price calc. We use 0.8 as a single fudge factor across exchanges (real varies 0.4–5%).
- **OI** — Open Interest. Total $ of derivative positions outstanding on a venue.

## P–R

- **`PARTIAL_HEDGE`** — `hedge_label` for SHORT perp with 30-80% spot coverage. Mixed posture.
- **`partialBucketTs`** — Timestamp of the in-progress Dune bucket excluded from aggregates. See [[ADR-002 Dune partial bucket exclusion]].
- **Pareto 75/25** — Heuristic that top 20% of accounts hold ~75% of OI. Used in [[Whale vs Retail divergence]] $ exposure calc.
- **Perp** — Perpetual futures contract. No expiry, funding rate paid every 8h.
- **RR25** — 25-delta risk reversal. `IV(call_25) − IV(put_25)`. Skew sign indicator. See [[Options skew (RR25, BF25)]].

## S–Z

- **Spark line / Spark chart** — Tiny inline trend chart used in many dashboard panels.
- **Spot HL** — Hyperliquid spot DEX (HIP-1 / HIP-2 tokens). UETH = wrapped ETH on HL. Distinct from L1 mainnet ETH.
- **stdev_24h** — Rolling-window stdev of 24h netflow sums. See [[Z-score (CEX netflows)]].
- **Trino / Dune SQL** — V2 query engine on Dune. Replaced V1/Spark. V1 queries return `400 Deprecated query engine` when triggered.
- **uPnL** — Unrealized PnL. Mark-to-market profit on an open position.
- **UETH** — Wrapped ETH on Hyperliquid spot chain. Distinct from ETH on Ethereum L1. See [[Hyperliquid]].
- **Whales (this dashboard)** — Two distinct definitions:
  - In [[Binance]] L/S context: top 20% of accounts by position size (`topLongShortPositionRatio`)
  - In [[Hyperliquid]] context: addresses on the curated `HYPERLIQUID_WHALE_ADDRESSES` list
- **Z-score (CEX netflows)** — `(net_24h − mean_24h) / stdev_24h`. See [[Z-score (CEX netflows)]].
