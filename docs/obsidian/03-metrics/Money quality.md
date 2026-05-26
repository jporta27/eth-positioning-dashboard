---
tags: [metric, money-quality]
---

# Money quality

Classifies price movements by quality of underlying flow: **new money entering** vs **short covering / rotation**.

## Method

For each timeframe (1h, 4h, 12h, 24h, 3d, 7d, 14d):

```python
ratio = |ΔPrice%| / |ΔOI%|
```

## Buckets

| Ratio | Label | Interpretation |
|---|---|---|
| < 1 | Acumulación real | Mucha plata nueva por poco precio |
| 1–2 | Balanceado | Healthy move |
| 2–5 | Covering dominante | Rally/drop sin combustible |
| > 5 | Squeeze puro | Pure forced moves |

## Cut-anchored MQ

Variant that anchors the comparison window at the moment a slow stochastic %K entered the oversold/overbought zone. Lets us answer "since the floor signal fired, how is the OI behaving?" — high-quality long capitulation = `Long capitulation upgrade-high`.

Implemented in `compute_cut_anchored_mq`.

## Limitation

Binance `/futures/data/openInterestHist` only retains **30 days** of OI history. So multi-day MQ windows (3d, 7d, 14d) require reconstructing from per-bar taker delta or running our own persister. Currently the long windows rely on whatever fits in the 30d API window — limited.

## See also

- [[Binance]] (OI source)
