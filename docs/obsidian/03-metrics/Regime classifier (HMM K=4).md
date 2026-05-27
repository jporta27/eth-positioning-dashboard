---
tags: [metric, regime, hmm, classifier]
status: production
created: 2026-05-27
last_review: 2026-05-27
---

# Regime classifier — HMM K=4

Latent-state market regime classifier for ETH at 4h cadence. Reads OHLC + funding + macro, outputs current regime label, posterior probabilities, and multi-step transition forecast.

Lives at:
- Module: `backtest/regime_classifier.py`
- CLI: `scripts/run_regime_classifier.py`
- Snapshot: `data/regime/latest.json` (committed to git, ~2KB)
- Panel: `RegimePanel` in `frontend/src/Dashboard.jsx`

Related notes: [[ADR-002 Dune partial bucket exclusion]], [[ADR-003 Direction from z-sign vs noise band]], [[Z-score (CEX netflows)]]

---

## 1. Problem statement

The dashboard reports many individual signals (funding, netflows, OI, taker imbalance, etc). What's missing: a **higher-level structural state** that aggregates these into a small set of operational regimes. The user wanted:

1. A robust classifier — "what regime is the market in NOW?"
2. Robust transition probabilities — "what's the probability of changing regime in the next K bars?"

Explicitly NOT the goal: next-bar return prediction. The framing is **regime filter for downstream strategy decisions**, not a directional predictor.

## 2. Modeling choices

### 2.1 Gaussian HMM, K=4 hidden states

**Why HMM, not observable Markov on percentile-labeled states**

A naive baseline that splits 4h returns into 3 quantile buckets (DOWN/CHOP/UP) and computes the transition matrix gives:
- Diagonal probabilities ~0.32–0.42 (states flip almost every bar)
- Dwell time ≈ 1.5 bars (6h)
- Markov accuracy vs persistence: Δ = +0.09pp (effectively zero)

The HMM with 8 emission features instead gives:
- Diagonal probabilities ~0.96–0.98
- Dwell time ≈ 22–28 bars (88–112h, i.e. 4–5 days)
- States that are interpretable and operationally distinct

The difference is that observable Markov is dominated by the random component of 4h returns. The HMM aggregates 8 features into a latent state that captures the slower-moving regime.

**Why K=4 specifically**

| K | BIC (lower better) | Interpretation |
|---|---|---|
| 2 | 155,588 | Conflates everything; loses CHOP/STRESS distinction |
| 3 | 145,007 | DOWN/CHOP/UP — but CHOP and UP collapse to one calm state |
| **4** | **136,677** | CRASH/STRESS/CHOP/UP — clean separation on vol × direction axes |
| 5 | 131,250 | BIC favors K=5 but the extra state is a "centered noise" cluster (max \|mean\| = 0.156 across all 8 features) |

K=4 captures real structure; K=5 adds an artifact. Choice documented in [[ADR-004 K=4 vs K=5]] (forthcoming).

### 2.2 The 4 canonical states

Defined by their feature signatures (z-space, train-set distribution):

| State | log_ret | rv_24 | parkinson_24 | taker_imb | skew_20 | funding_z | eth_btc_ret | vix_z | Interpretation |
|---|---|---|---|---|---|---|---|---|---|
| **CRASH** | −0.02 | +0.39 | +0.35 | −0.04 | −0.09 | −0.05 | +0.02 | −0.00 | Bear drift con vol moderada |
| **STRESS** | +0.02 | **+1.76** | **+1.79** | +0.13 | −0.02 | **−0.17** | −0.09 | **+0.42** | High-vol expansion — cascade / squeeze / melt-up. Funding negative = shorts paying longs = short crowded |
| **CHOP** | +0.00 | **−1.10** | −1.04 | +0.03 | +0.14 | +0.09 | −0.04 | +0.11 | Calm sideways consolidation, very low vol |
| **UP** | +0.01 | −0.43 | −0.45 | −0.03 | +0.02 | +0.06 | +0.04 | −0.25 | Calm bull grind — low vol, slight positive skew, VIX quiet |

Label assignment is deterministic post-fit (no human choice):
1. Sort raw HMM states by `realized_vol_24` descending → top 2 are high-vol, bottom 2 calm
2. Within each pair, sort by `log_return_4h` → upper is bull-flavored
3. High-vol pair: bull-flavored = STRESS, bear-flavored = CRASH
4. Low-vol pair: bull-flavored = UP, bear-flavored = CHOP

This guarantees that the same label corresponds to the same conceptual regime across every refit, even though `hmmlearn` assigns arbitrary integer indices each time.

### 2.3 The 8 emission features

All causally computable at the time t (no lookahead). Defined in `build_features()`:

| # | Feature | Source | Purpose |
|---|---|---|---|
| 1 | `log_return_4h` | klines 4h | Direction + magnitude of price |
| 2 | `realized_vol_24` | klines 4h, std of log returns | Close-to-close vol in last ~4 days |
| 3 | `parkinson_vol_24` | klines 4h, range estimator | Range-based vol — less noisy than #2 |
| 4 | `taker_buy_imbalance` | klines 4h | (taker_buy − sell)/volume — who's agressing the book |
| 5 | `return_skew_20` | klines 4h | Rolling skewness — separates ordered down from crash |
| 6 | `funding_zscore` | binance_funding (ffilled) | Z-score of perp funding rate — measures positioning extreme |
| 7 | `eth_btc_relret_20` | klines + macro BTC | ETH 20-bar return − BTC 20-bar return — does ETH lead/lag BTC |
| 8 | `vix_level_z` | macro VIX (daily ffilled) | Z-score of VIX level — global risk-on/risk-off proxy |

### 2.4 Diagonal vs full covariance

`covariance_type="diag"` → K(2d) + K(K−1) + (K−1) = 79 free params for K=4, d=8.
`covariance_type="full"` → K·d·(d+1)/2 + Kd + ... ≈ 159 params.

With 7,790 4h training observations, full covariance is ~50 obs/param (marginal); diagonal is ~100 obs/param (comfortable). The off-diagonal correlations the full model would learn are mostly noise at 4h cadence — the gain in BIC is small and the overfitting risk meaningful. Diagonal is the parsimonious choice.

### 2.5 Random restarts

EM has multiple local maxima. `_fit_hmm_with_restarts` runs `N_RESTARTS = 20` and picks the model with the highest converged log-likelihood. Empirically, ~95% of restarts land within 5% of best logL — the basin of attraction is well-defined. The remaining 5% are clearly stuck local minima.

## 3. Robustness — what we tested

Six tests in `scripts/regime_robustness_tests.py`:

| # | Test | Result | Action taken |
|---|---|---|---|
| 1 | **Random seed stability** | ✓ PASS — 29/30 fits within ±5% best logL, max state-mean std 0.18σ | none |
| 2 | **State identity across rolling windows (14 windows, 18m each)** | ✗ FAIL — CRASH/STRESS parkinson_vol drifts 0.77–0.79σ | **Mitigated via rolling re-fit** (weekly cadence) |
| 3 | **K sensitivity (BIC sweep K=2…5)** | ✗ Initially BIC favored K=5, but the K=5 "extra" state has all \|means\| < 0.16 → artifact | K=4 chosen on parsimony grounds |
| 4 | **OOS sanity — val period state assignments** | ✓ INSPECT-pass — STRESS periods in val coincided with known vol events (Feb-Mar 2025 cascade, Apr-2025 sell-off) | none |
| 5 | **Transition matrix drift across windows** | ✓ PASS — max diag std 0.014, max off-diag std 0.013 | none |
| 6 | **Multi-step horizon: Markov vs persistence** | INFO — at h=1 bar: Δ=0 (persistence = Markov); at h=24 bars (4d): Δ=+2.82pp; at h=48 bars (8d): Δ=+5.97pp | Confirms predictive value at 4–8 day horizons, not next-bar |

**The single FAIL** (Test 2) is the reason we use rolling re-fit in production. A stationary model trained once will degrade as ETH's vol regime evolves.

## 4. Production architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  CRON / MANUAL (weekly)                                           │
│  python scripts/run_regime_classifier.py --refit                  │
│    1. Load 5y of klines + funding + macro from parquet            │
│    2. Build 8 features (causal)                                   │
│    3. Standardize using train-window stats only                   │
│    4. Fit K=4 HMM with 20 restarts                                │
│    5. Assign canonical labels                                     │
│    6. Persist: data/regime/model.pkl + data/regime/latest.json    │
└─────────────────────────────────┬────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│  RUNTIME (per dashboard request)                                  │
│  backend/main.py + api/index.py — fetch_regime_snapshot()         │
│    Reads data/regime/latest.json from disk                        │
│    60s in-process cache (file rarely changes within a minute)     │
│  Returns the JSON as the `regime` field in /api/data              │
└─────────────────────────────────┬────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│  FRONTEND — Dashboard.jsx                                         │
│  RegimePanel renders:                                             │
│    - Current state + color + description                          │
│    - Confidence (max posterior) + entropy                         │
│    - 4 probability bars (full posterior)                          │
│    - Transition forecast at horizons 1/6/24/48 bars               │
│    - Full transition matrix                                       │
│    - Model age + STALE flag if > 14d                              │
└──────────────────────────────────────────────────────────────────┘
```

### Why file-based snapshot (not inline compute)

- The fit + 1y forward-backward pass takes ~30–60s. Can't run on a 60s-bounded Vercel Lambda for every request.
- The classification only changes when a new 4h bar closes — way slower than the cache TTL.
- Decoupling refit (slow, weekly) from serving (fast, per-request) is the standard ML inference pattern.
- The snapshot is small (~2KB JSON) and rides along in git so Vercel deploys it automatically.

### Refit cadence

`REFIT_EVERY_DAYS = 7` in `backtest/regime_classifier.py`. Reasoning:
- Test 2 drift develops on ~3-month timescale, so 7-day refit is way more frequent than needed.
- Weekly cadence is the natural human/cron schedule.
- Cost per refit: ~50s of CPU, ~5MB of memory peak. Negligible.

If the snapshot file is older than `MODEL_STALE_DAYS = 14` (2× the cadence), the frontend flags STALE. This catches forgotten cron jobs or broken refits.

## 5. Output schema

```json
{
  "timestamp": "2026-05-27T20:50:55Z",
  "lastBarTs": 1777420800000,
  "modelFitDate": "2026-05-27T20:50:52Z",
  "modelAgeDays": 0.0,
  "modelTrainEnd": "2026-04-29 00:00:00+00:00",
  "modelTrainNObs": 3283,
  "currentState": "UP",
  "confidence": 1.0,
  "entropy": 0.0001,
  "probabilities": {
    "CRASH": 0.0, "STRESS": 0.0, "CHOP": 0.0, "UP": 1.0
  },
  "expectedDwellBars": 27.02,
  "expectedDwellHours": 108.1,
  "transitionForecast": {
    "1bar":  { "CRASH": 0.001, "STRESS": 0.016, "CHOP": 0.020, "UP": 0.963 },
    "6bar":  { "CRASH": 0.012, "STRESS": 0.080, "CHOP": 0.102, "UP": 0.806 },
    "24bar": { "CRASH": 0.073, "STRESS": 0.201, "CHOP": 0.233, "UP": 0.493 },
    "48bar": { "CRASH": 0.142, "STRESS": 0.250, "CHOP": 0.264, "UP": 0.344 }
  },
  "transitionMatrix": {
    "CRASH":  { "CRASH": 0.969, "STRESS": 0.020, "CHOP": 0.012, "UP": 0.000 },
    "STRESS": { "CRASH": 0.013, "STRESS": 0.957, "CHOP": 0.017, "UP": 0.013 },
    "CHOP":   { "CRASH": 0.011, "STRESS": 0.014, "CHOP": 0.967, "UP": 0.008 },
    "UP":     { "CRASH": 0.000, "STRESS": 0.013, "CHOP": 0.024, "UP": 0.963 }
  }
}
```

## 6. Known limitations

- **Non-stationarity is mitigated, not solved.** Test 2 showed vol signatures drift across years. Rolling re-fit absorbs slow drift but cannot react to a regime change within a 7-day refit window. If something structural breaks (e.g. ETF approval, major protocol change), the model may misclassify for a week before refit catches up.

- **Markov order 1.** The transition probabilities are conditioned only on the current state, not on history. ETH may have higher-order dependencies (e.g. "CHOP for >2 weeks then UP" is more likely than "CHOP yesterday then UP"). We chose not to model these — order-2 HMMs have K² = 16 hidden joint states which more than doubles the param count.

- **No regime confidence interval.** The output gives a point posterior `confidence`, but not a CI on the posterior itself across refits. If the model is borderline between two states, the displayed confidence may be over-stated.

- **Crypto-native features only.** No on-chain (gas, active addresses), no order book depth, no options skew. The 8 features chosen are robust + available in 5y history. Adding more requires longer backfills (e.g. options skew has 6 months of clean data).

- **Trading wallets ≠ holders.** This is unrelated to the classifier, but worth flagging: the dashboard's whale-tracking module follows trading-account positions, not custody balances. The regime classifier sees market-wide aggregates, not whale-specific behavior.

## 7. Maintenance runbook

### When the snapshot is stale (frontend shows STALE flag)

```bash
cd C:/Users/Jorge/Downloads/setup-eth/eth-positioning-dashboard
python scripts/run_regime_classifier.py --refit
git add data/regime/latest.json
git commit -m "regime: weekly refit YYYY-MM-DD"
git push origin master
```

Vercel auto-deploys, snapshot is live in production within ~2 min.

### When the model fails to fit

Common causes:
- `data/backfill/binance_klines_1h.parquet` is stale → re-run `python scripts/backfill.py --sources binance_klines_1h --years 5`
- macro.parquet missing VIX recent data → re-run macro backfill
- hmmlearn version mismatch → `pip install hmmlearn==0.3.3`

### When the canonical labels look wrong

If after a refit the state with high vol gets labeled UP (which would mean the canonical sort is broken), check:
- `state_means.realized_vol_24` ordering in `assign_canonical_labels`
- This shouldn't happen — the sort is deterministic on objective features

### When to bump REFIT_EVERY_DAYS

- If Test 5 (transition drift) starts showing std > 0.05 on diagonals across the last 8 windows → tighten to 3 days
- If a refit produces a model with a state that doesn't match the canonical signatures (e.g. a STRESS state with negative VIX) → manual investigation needed

## 8. References

- `backtest/regime_classifier.py` — module
- `scripts/run_regime_classifier.py` — CLI
- `scripts/markov_hmm_backtest.py` — exploratory backtest (K=3 + K=4)
- `scripts/check_k5_states.py` — K=5 interpretability check
- `scripts/regime_robustness_tests.py` — 6-test suite
- `data/regime/latest.json` — current snapshot
- `data/regime/model.pkl` — fitted model artifact (local only, gitignored)
- Working doc: `ETH_Regimen_HMM_Documento_de_Trabajo.md` (one-up directory) — original research notes from Roan / @RohOnChain framework adaptation
