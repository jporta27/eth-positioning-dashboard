---
tags: [metric, cex-netflows]
---

# Z-score (CEX netflows)

Statistical "is this 24h netflow elevated vs recent regime?" measure for the [[Dune (CEX netflows)|CEX netflows panel]].

## Formula

```
mean_24h  = average of rolling 24h netflow sums over the last ~144 1h windows
stdev_24h = stdev of same distribution
z_score   = (net_24h_eth − mean_24h) / max(stdev_24h, MIN_STDEV_ETH)
```

Where `MIN_STDEV_ETH = 500` ETH (noise floor to prevent z blowup in calm regimes).

## Reliability gates

Computed only when **n ≥ 24** rolling windows are available (`MIN_DIST_SAMPLES`). Below that, z is set to 0 and a `z_score_reliable=false` flag is surfaced. This avoids post-cold-start fake EXTREME readings when there's only 2–3 samples and stdev is meaningless.

## Magnitude label

```python
if abs_z >= 2.0: magnitude = "EXTREME"
elif abs_z >= 1.0: magnitude = "ELEVATED"
elif abs_z >= 0.3: magnitude = "NORMAL"
else: magnitude = "NOISE"
```

## Direction label

See [[ADR-003 Direction from z-sign vs noise band]] for the bifurcation logic. Summary:

- `|z| ≥ 1`: trust z-sign (positive z → BEARISH inflow regime; negative → BULLISH)
- `|z| < 1`: fall back to `noise_band = max(|mean_24h|, 500)` absolute comparison

## Bias label (final user-facing)

| direction | magnitude | bias |
|---|---|---|
| NEUTRAL | any | NEUTRAL |
| BEARISH/BULLISH | EXTREME or ELEVATED | BEARISH / BULLISH |
| BEARISH/BULLISH | NORMAL | BEARISH_MILD / BULLISH_MILD |

## Partial bucket consideration

The rolling distribution is built from `hourly_series` which **includes** the partial in-progress bucket. But the rolling sums are constructed such that **only the last sum** (the one ending at the partial bucket) includes partial data. We exclude that last sum via `hist_distribution[:-1]`. See [[ADR-002 Dune partial bucket exclusion]] for the full reasoning.

## See also

- [[Dune (CEX netflows)]]
- [[ADR-002 Dune partial bucket exclusion]]
- [[ADR-003 Direction from z-sign vs noise band]]
