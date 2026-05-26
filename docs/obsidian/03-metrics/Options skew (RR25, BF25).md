---
tags: [metric, options]
---

# Options skew (RR25, BF25)

Derived from [[Deribit]] options chain. Tells you the **shape** of the implied vol smile.

## RR25 (25-delta risk reversal)

```
RR25 = IV(25-delta call) − IV(25-delta put)
```

Sign tells you the directional skew bias:
- RR25 > 0 → calls more expensive → market pricing upside risk
- RR25 < 0 → puts more expensive → market pricing downside risk

## BF25 (25-delta butterfly)

```
BF25 = 0.5 × (IV_call25 + IV_put25) − IV_ATM
```

Measures **curvature** of the smile — how much the wings cost above ATM.

## Known limitation

ATM IV is currently picked as `min(all_strikes, key=abs(strike - spot))["iv"]` — that's a single option (call OR put, whichever's nearest), not a straddle average. This bleeds call-put skew into BF25. Documented as a known issue, not yet fixed.

## Term structure

We also compute the IV-vs-DTE curve per expiry (0, 1, 2, 9, 16, 37, 72d). Contango (further dates higher IV) vs backwardation (front higher) tells you about realized vol expectations.

## Max pain + gamma flip

Same Deribit chain feeds:
- **Max pain**: strike at which the most $ of options expire worthless. Magnetic for spot near expiry.
- **Gamma flip**: strike where dealer net gamma flips from negative to positive. Above = stabilizing flows, below = amplifying.

## See also

- [[Deribit]]
- [[Macro (Yahoo + FRED)]] (risk-free rate for delta calc)
