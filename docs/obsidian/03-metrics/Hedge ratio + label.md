---
tags: [metric, whales, hedge]
---

# Hedge ratio + label

Per-whale classifier in the [[Hyperliquid]] Whales panel. Tells you whether a perp position is a directional bet or a hedge.

## Inputs

For each whale's ETH perp position:

```python
ueth_spot     = float  # HL spot UETH balance
mainnet_eth   = float  # Ethereum L1 balance via [[Etherscan (mainnet ETH)]]
perp_size_eth = float  # abs(szi) from clearinghouseState
side          = "LONG" | "SHORT"
total_spot_eth = ueth_spot + mainnet_eth
```

## hedge_ratio (SHORT only)

```python
hedge_ratio = min(total_spot_eth / perp_size_eth, 1.0)
```

Bounded at 1.0 because >100% spot coverage is "over-hedged" — the *extra* spot is its own position, not part of the hedge.

For LONG positions `hedge_ratio` is `None` (spot doesn't offset spot — it stacks).

## hedge_label

Constants (from [[ADR-005 Code review standard|CL-A]]):

```python
HEDGE_FULL_THRESHOLD = 0.8        # SHORT ≥80% covered → FULLY_HEDGED
HEDGE_PARTIAL_THRESHOLD = 0.3     # SHORT 30–80% covered → PARTIAL_HEDGE
DOUBLE_BULL_SPOT_FRACTION = 0.3   # LONG ≥30% covered → DOUBLE_BULL
```

| side | condition | label | interpretation |
|---|---|---|---|
| SHORT | ratio ≥ 0.8 | `FULLY_HEDGED` | Neutral — short is hedging spot exposure |
| SHORT | 0.3 ≤ ratio < 0.8 | `PARTIAL_HEDGE` | Mixed — some hedge, some bet |
| SHORT | ratio < 0.3 | `DIRECTIONAL_BET` | Real bearish — short with no spot to back it |
| LONG  | spot ≥ 0.3 × size | `DOUBLE_BULL` | Concentration — perp long + spot long |
| LONG  | spot < 0.3 × size | `DIRECTIONAL_BET` | Real bullish — bet without doubling down on spot |

## Tested via

`backend/tests/test_hedge_label.py` — 11 unit tests covering both sides + boundary inclusivity at each threshold + edge cases (non-ETH coins skipped, `totalSpotEth = ueth + mainnet` exactly).

Run with:
```
python -m backend.tests.test_hedge_label
```

## Known limitation

The whale list defaults to **trading wallets**, not **custody wallets**. So most positions show `mainnet_eth ≈ 0` and get classified as `DIRECTIONAL_BET` even if the trader actually has 10k ETH custody elsewhere. See [[ADR-004 Hedge uses HL UETH plus L1 mainnet]] for the workaround (trace deposit flows, add custody wallet to env).

## See also

- [[Hyperliquid]]
- [[Etherscan (mainnet ETH)]]
- [[ADR-004 Hedge uses HL UETH plus L1 mainnet]]
