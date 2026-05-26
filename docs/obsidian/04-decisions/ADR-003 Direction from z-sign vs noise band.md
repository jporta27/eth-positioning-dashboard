---
tags: [adr, metrics, cex-netflows]
status: accepted
date: 2026-05
---

# ADR-003 — Direction from z-sign when |z|≥1, noise band when below

**Status**: Accepted
**Context**: The CEX netflow panel was showing `bias=NEUTRAL` for a +28.9k ETH inflow at z=+1.14σ, p=92% — which is clearly BEARISH (top decile of recent supply pressure).

## The bug we had

Original direction logic:
```python
noise_band = max(abs(mean_24h), 500)
if net_24h_eth < -noise_band:
    direction = "BULLISH"
elif net_24h_eth > noise_band:
    direction = "BEARISH"
else:
    direction = "NEUTRAL"
```

When the **regime mean** drifts large (e.g. mean_24h = −29k during an outflow regime), `noise_band` becomes 29k. A net +28.9k inflow then sits **inside** the band → NEUTRAL.

But statistically, +28.9k vs mean=−29k is +1.14σ deviation. That's elevated by any honest measure.

The noise band was supposed to suppress dead-flat-zero regimes (where ±300 ETH flips don't mean anything). Instead it suppressed clearly-elevated readings when regime mean was extreme.

## The fix

**Trust the z-sign when |z|≥1, fall back to noise band only when |z|<1.**

```python
if abs_z >= 1.0:
    direction = "BEARISH" if z_score > 0 else "BULLISH"
else:
    noise_band = max(abs(mean_24h), 500)
    if net_24h_eth < -noise_band:
        direction = "BULLISH"
    elif net_24h_eth > noise_band:
        direction = "BEARISH"
    else:
        direction = "NEUTRAL"
```

## Why this works

- `|z|≥1`: the flow is statistically significant relative to recent regime. Sign of z gives direction unambiguously. More inflow than typical → BEARISH (sell pressure). Less than typical (or net outflow) → BULLISH.
- `|z|<1`: not statistically meaningful. Fall back to absolute net vs noise band so tiny near-zero flips don't toggle direction.

## Known limitation

`|z|≥1` threshold is still arbitrary. A more sophisticated approach: also check `|z| ≥ 0.5` AND `|net_24h_usd| > $30M` as a separate "MEDIUM but material" branch. Currently MEDIUM-magnitude readings (`0.3 ≤ |z| < 1`) fall to the absolute band, which can still under-signal in regime-drift conditions.

Marked as known issue in [[ADR-005 Code review standard]] — not yet fixed.

## See also

- [[Z-score (CEX netflows)]]
- [[Dune (CEX netflows)]]
- [[ADR-002 Dune partial bucket exclusion]]
