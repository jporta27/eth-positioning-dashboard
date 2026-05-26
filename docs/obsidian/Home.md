---
tags: [moc, index]
---

# 🏠 Home — ETH Positioning Dashboard

Map of content for this vault. Click any link to dive in.

## 🏗 Architecture

- [[Two-headed backend]] — `backend/main.py` + `api/index.py` mirror
- [[Frontend layout]] — single-file React in `Dashboard.jsx`
- [[Deploy targets]] — Vercel (primary) vs Railway (backup)

## 🔌 Data sources

| Source | What we use it for | Note |
|---|---|---|
| Binance perp/spot | Funding, OI, L/S ratio, klines, taker | [[Binance]] |
| Dune Analytics | CEX netflows (query 6984181) | [[Dune (CEX netflows)]] |
| Hyperliquid | Whale positions (perp + spot) | [[Hyperliquid]] |
| Etherscan | Mainnet L1 ETH balance per whale | [[Etherscan (mainnet ETH)]] |
| Farside | ETF flows (HTML scrape via curl) | [[Farside ETF]] |
| DefiLlama | CEX reserves + stablecoin supply | [[DefiLlama]] |
| OKX, Bybit | Multi-exchange L/S aggregate | [[OKX]] · [[Bybit]] |
| Deribit | Options chain (IV, OI, skew) | [[Deribit]] |
| Yahoo + FRED | Macro (VIX, DXY, US10Y, BTC, T-bill) | [[Macro (Yahoo + FRED)]] |

## 📐 Metrics & calculations

- [[Z-score (CEX netflows)]] — direction + magnitude of supply pressure
- [[Hedge ratio + label]] — classify HL perp positions by spot coverage
- [[Liquidation map]] — heuristic distribution by leverage tier
- [[Whale vs Retail divergence]] — multi-exchange L/S aggregate
- [[Money quality]] — price-vs-OI ratio classifier
- [[Options skew (RR25, BF25)]] — vol surface curvature
- [[ETH-BTC rotation]] — dominance flip signal

## 🧠 Decisions (ADRs)

- [[ADR-001 Two backend mirror]]
- [[ADR-002 Dune partial bucket exclusion]]
- [[ADR-003 Direction from z-sign vs noise band]]
- [[ADR-004 Hedge uses HL UETH plus L1 mainnet]]
- [[ADR-005 Code review standard]]
- [[ADR-006 Removed backtest framework]]

## ⚙ Operations (runbook)

- [[Environment variables]] — local + Vercel
- [[Vercel deployment]] — region, maxDuration, redeploy gotcha
- [[Smoke tests]]
- [[Dune API quota]]
- [[Backfill scripts]]
- [[Local dev setup]]

## 📖 Reference

- [[Glossary]] — terms used across the dashboard

---

## Reading order if you're new

1. [[Two-headed backend]] (understand the architecture)
2. [[Frontend layout]]
3. [[ADR-001 Two backend mirror]] (why this shape exists)
4. Skim each [[Glossary]] entry for any acronym you don't recognize
5. Pick the data source you care about and read its note
