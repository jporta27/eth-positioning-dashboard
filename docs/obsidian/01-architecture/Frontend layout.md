---
tags: [architecture, frontend]
---

# Frontend layout

Single-file React app at `frontend/src/Dashboard.jsx` (~4500 lines as of this writing). Vite build, no test framework, no lint script.

## Data flow

```
GET /api/data ─→ everything in one big JSON payload ─→ Dashboard.jsx renders ~15 panels
```

The frontend doesn't paginate or stream. One snapshot in, all panels rendered from it. Refreshes are full-page polls.

## Layout modes

Four modes (`scalp`, `intraday`, `swing`, `macro`). Each reorders the same panel set:

```js
scalp:    ['state','taker_grid','whale_vs_retail','hyperliquid_whales',...]
intraday: ['state','setup','mq','funding_grid','netflows','whale_vs_retail',...]
swing:    ['state','netflows','whale_vs_retail','hyperliquid_whales','mq',...]
macro:    ['state','capitalmap','netflows','whale_vs_retail','hyperliquid_whales','iv_grid',...]
```

Adding a new panel means: (1) define a section key in the page render, (2) add it to all 4 mode arrays.

## Panels currently rendered

- State + setup summary
- Funding grid (per-exchange spreads, history)
- Money quality panel (price vs OI ratio)
- CEX netflows (Dune-driven, big primary panel)
- [[Whale vs Retail divergence|Whale vs Retail]] panel
- [[Hyperliquid]] Whales panel (on-chain positions)
- Options skew + expiries
- [[Liquidation map]]
- ETH/BTC rotation
- IV term structure
- Capital map (DeFi distribution)
- Volume profile

## Frontend re-render hazards to avoid

Per [[ADR-005 Code review standard]]: when editing components in this file, watch for:

- Inline object/array literals inside the JSX body (re-created every render)
- IIFEs computing derived state (should be `useMemo` or extracted)
- Large prop trees passed by reference (children re-render unnecessarily)

Most existing panels don't follow these, but new code should.

## See also

- [[Two-headed backend]]
- [[ADR-005 Code review standard]]
