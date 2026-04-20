# ETH Signal Backtest Framework

Event-study framework to answer "does this signal have edge?" with statistical
rigor: IC (Spearman), forward-return mean + bootstrap CI, hit rate vs. baseline,
annualized Sharpe, max drawdown, deflated Sharpe (López de Prado).

## Quick start

```bash
# Show what data is available
python -m backtest.cli --audit

# List available signal presets
python -m backtest.cli --list

# Run a signal
python -m backtest.cli --signal funding_hot_inflow \
  --horizons 1h,4h,1d,3d,7d \
  --output reports/funding_hot_inflow.json

# Run self-tests (synthetic signal with known edge; null signal)
python -m backtest.tests.test_eventstudy
```

## Terminology

- **IC (Information Coefficient)**: Spearman rank correlation between the
  signal value and the forward return. Rank-based is robust to outliers.
  An IC of 0.05 is economically meaningful in liquid markets; 0.1+ is strong.
- **Bootstrap CI**: 95% confidence interval for the mean forward return,
  computed by resampling (5000 iterations by default). Reported as
  `ci95_bootstrap`. If it excludes 0, the effect is statistically significant
  at α=0.05.
- **Bootstrap p-value (IC)**: permutation-test p-value for the null hypothesis
  that the signal is independent of future returns. Shuffle the returns 5000
  times and count how often the shuffled |IC| exceeds the observed |IC|.
- **Hit rate baseline**: fraction of *all* hourly-rolling N-horizon windows in
  the klines history that closed up. A signal's hit rate must exceed baseline
  by a margin too large to be explained by "ETH drifted up during the period"
  alone.
- **Deflated Sharpe**: adjusts the observed Sharpe ratio for (a) non-normal
  returns (skew + excess kurtosis) and (b) multiple testing — pass
  `--n-trials` = number of strategies you've searched. Reported as a
  probability that the true Sharpe is > 0. DSR < 0.95 means the result is
  not defensible as alpha.

## Why bootstrap, not t-test?

- t-test assumes i.i.d. normal returns. Crypto returns are heavy-tailed and
  may be serially correlated, which makes the t-statistic over-confident.
- Bootstrap makes no distributional assumption; the CI is valid whatever
  the shape of the return distribution, as long as the observations are
  approximately exchangeable.
- Permutation test for IC: the null is "signal and returns are independent".
  That's exactly what we want to reject, and it's non-parametric by
  construction.

## Purged cross-validation

`eventstudy.purged_split_indices()` drops training events whose forward
horizon overlaps the test window (leakage control à la López de Prado).
Not yet wired into the CLI — currently we run a single-sample event study.
For regime conditioning (Fase B) or composite signal search (future work),
purged CV should replace the single-shot evaluation.

## Limits of the framework today

- **No intraday signals < 1h**. We use `binance_klines_1h` so horizons below
  1 hour are not meaningful. If we backfill 5m klines we can lower that.
- **Microstructure features not usable historically** — the `microstructure.parquet`
  layer has only been persisted since 2026-04-18 (15 rows). Wait for more
  persister time or use the orderbook HTTP snapshot history separately.
- **Signals requiring OI history**: Binance `/futures/data/openInterestHist`
  only retains ~30 days, so `moneyQuality_*_label`, `cut_anchored_mq`, and
  anything keyed off OI z-score can't be backtested deeply. Needs offline
  reconstruction from per-bar taker delta (feasible but not yet built).
- **Signals derived from Dune or live-only sources**: `cex_netflow_zscore`,
  L/S top-vs-retail divergence, RR25 canonical. No historical source with
  full coverage — blocked until we reconstruct from first principles.

## Data coverage (audit via `--audit`)

| Source                 | ts_min       | ts_max       | rows   | Usable for |
|------------------------|--------------|--------------|--------|-----------|
| binance_klines_1h      | 2024-04-20   | 2026-04-20   | 17,520 | Forward returns, price-based signals |
| binance_funding        | 2024-04-20   | 2026-04-20   | 2,190  | Funding-rate signals |
| defillama_stables      | 2017-11-28   | 2026-04-17   | 8,906  | Stables signals (but only 2024+ is intersectable with returns) |
| macro (Yahoo + FRED)   | 2021-04-18   | 2026-04-18   | 8,108  | Cross-asset regime labels (VIX, DXY, US10Y, SPX, BTC) |
| farside_etf            | MISSING      | —            | 0      | Blocked: backfill script doesn't yet use the curl subprocess fallback the runtime has |
| snapshots              | 2026-04-18   | (ongoing)    | small  | Wait for persister to accumulate |

## Results summary — Fase A runs (2024-04-20 → 2026-04-20 intersect)

Reports in `reports/*.json`. Quick glance:

| Signal                   | n_valid | 4h mean   | 4h CI95               | IC (p)        | Verdict |
|--------------------------|---------|-----------|-----------------------|---------------|---------|
| `funding_hot_inflow`     | 77      | +0.04 bp  | [-24, +33] bp         | −0.06 (0.63)  | no edge |
| `funding_hot_outflow`    | 138     | −0.01 bp  | [-30, +25] bp         | −0.04 (0.61)  | no edge |
| `funding_hot_abs`        | 213     | +0.01 bp  | [-20, +21] bp         | ≈0    (0.99)  | no edge |
| `stables_expanding`      | 13*     | +0.59%    | **[+0.12%, +1.04%]**  | −0.21 (0.47)  | CI > 0 — weakly suggestive, n small |
| `stables_contracting`    | 22*     | +0.26%    | [-0.31%, +0.81%]      | −0.16 (0.48)  | no edge at 4h, but 1d-7d means trend negative |

*Only events where the 30d-delta window AND the forward klines horizon both
fall inside 2024-04 — 2026-04 count as valid. stables_expanding has 68 events
total but 55 fall in the 2017-2023 pre-klines era.

Observations:
1. Funding z-score at |z|≥1.5 has **no statistically detectable edge** at any
   horizon. Both the inflow side ("longs saturated → revert down") and outflow
   side ("shorts saturated → squeeze up") were tested; neither beats noise.
   This is a useful null result — the naive "funding extreme = mean revert"
   setup doesn't pay by itself on 2024-2026 ETHUSDT perp.
2. **stables_expanding**'s 4h CI95 crosses above zero (border of significance),
   suggesting ETH rallies modestly in the hours after a stables-supply
   surge — consistent with "dry powder hits exchange → marginal buying".
   But n=13 is low, and the IC p-value (0.47) doesn't support the ordering.
   Treat as a hypothesis to revisit with more data.
3. **stables_contracting** trends bearish at 1d-7d (mean-returns
   -0.54%/-1.34%/-2.07%) but CI95s include zero. With more data (2017-2024
   klines backfill), this would likely reach significance given the
   directional consistency.

## Roadmap

**Fase B (not yet started)**: regime conditioning. Add `regime.py` with
labels (vix_quartile, rr25_sign, etf_7d_sign, etc.) and run every signal
sliced by regime combination. Expected uplift: signal that's flat in
aggregate may be bi-modal across regimes (e.g. funding extreme only pays
during high-VIX periods).

**Fase C (blocked by data)**: moneyQuality labels, CEX netflow z-score,
L/S top-vs-retail divergence, RR25 canonical. All require offline
reconstruction from klines + per-bar taker delta + Deribit trade history.
Approx 2-3 days of work; proceed when the simple signals are exhausted.

**Fase D (optional)**: composite signal search (LightGBM over engineered
features). Skipped in Fase A because (a) no single signal shows clear edge
yet and (b) composites without base-signal understanding lead to overfitting.
