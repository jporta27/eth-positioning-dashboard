---
tags: [metric, score, market-state, scoring]
status: production
created: 2026-05-28
last_review: 2026-05-28
---

# Market State Score

The headline scoring system on top of the dashboard — the one that shows **ALCISTA / PRECAUCIÓN / BAJISTA / NEUTRAL** with a numerical score in `[-10, +10]`.

Implemented in `frontend/src/Dashboard.jsx` → `computeSignals(data, period, stochTf)` → returns `signals.marketState`.

Related: [[Regime classifier (HMM K=4)]] (a structural slower signal), [[Z-score (CEX netflows)]] (one of the inputs), [[Market State Score - Quant Review]] (validation plan + improvement priorities)

---

## 1. What the score IS (and is NOT)

**IS**:
- A snapshot signal that aggregates **12 independent indicators** with **time-horizon-aware weights**
- A directional read in `[-10, +10]` where positive = bullish bias, negative = bearish bias
- A confidence indicator via the **magnitude** of the score (|score| ≥ 3 = high conviction)
- Designed to **change FAST** as indicators shift — re-computed every render (sub-second)

**IS NOT**:
- A regime classifier — that's the slower HMM K=4 (see [[Regime classifier (HMM K=4)]]) which moves on days/weeks
- A return predictor — there's no historical backtest validating the +3 → bullish call
- An execution signal on its own — it's a **bias indicator** to combine with your trade thesis
- Stationary across regimes — what +5 meant in 2023 may not match what +5 means today

The two signals are **complementary**:
- HMM regime tells you "what kind of market we're in" (CRASH / STRESS / CHOP / UP)
- Market State Score tells you "what's the directional bias RIGHT NOW within that regime"

---

## 2. The 12 factors

Each factor produces:
- A **text chip** describing what's happening (visible in the "ESTADO DEL MERCADO" panel)
- A **±integer contribution to score** scaled by its weight `w` for the current period

The chips list every active factor with its multiplier (e.g. `Funding +0.0185% — Longs cargados (×2)` means this factor contributed −2 to the score).

| # | Factor | Source | Direction logic |
|---|---|---|---|
| 1 | Funding rate | Binance perp | High positive → bearish (longs paying, vulnerable to flush); High negative → bullish |
| 2 | OI change 48h | Binance perp | OI↑ + funding↑ → bearish (longs piling on); OI↑ + funding↓ → bullish (shorts piling on); paired with price for "new longs / new shorts / liquidating" reading |
| 3 | Taker buy/sell | Binance perp + spot | Perp + Spot both bull → strong bullish; both bear → strong bearish; mixed → caution (specs ≠ real) |
| 4 | L/S divergence | Binance top vs retail | Retail long + smart money neutral → bearish (retail overextended); vice versa |
| 5 | Realized vol | Binance klines | **Amplifier** — low vol amplifies whatever bias exists (compression before move); does not give direction |
| 6 | IV vs RV spread | Deribit + Binance | **Amplifier** — IV premium > 15 means options market expects move; amplifies existing direction |
| 7 | ETH/BTC | Binance ETHBTC | ETH outperforming → bullish for ETH; underperforming → bearish |
| 8 | Gamma flip | Deribit options | Above flip → calls dominate, dealers buy spot (combustible alcista); below → puts dominate, dealers sell spot |
| 9 | Volume profile | Binance klines | Above VA → sobreextensión alcista → mean reversion bias bearish; below VA → bias bullish; at POC → neutral congestion |
| 10 | Stochastics | Binance klines (TF user-selected) | Slow stoch OB → bearish; OS → bullish; Fast stoch cross in OS/OB zone → timing confirmation |
| 11 | Money Quality | OI + price multi-window | `ratio = \|ΔPrice%\| / \|ΔOI%\|` — labels: real accumulation (r<1) / covering / squeeze / etc. Direction × quality |
| 12 | Setup detector | Stoch + cut-anchored MQ | Quality grade A++ / A+ / A / BLOCKED; state TRIGGERED / ARMED / LATE — high-conviction final overlay |

---

## 3. Per-period weights (the W matrix)

What matters for a 5-minute scalp is NOT what matters for a 15-day macro view. Each factor has its own weight per period:

```
period   funding  oi  oiPrice  taker  ls  vol  ivRv  ethBtc  gamma  vp  stoch  mq  setup
─────────────────────────────────────────────────────────────────────────────────────────
5m         0      0      0       2     1    0     0      0       0    1     1    1     3
15m        0      0      0       2     1    0     0      0       0    1     1    1     3
1h         1      1      1       1     1    0     0      0       1    1     1    2     3
4h         1      1      1       1     1    1     0      1       1    1     2    2     4
12h        2      1      1       1     1    1     1      1       1    1     2    2     3
1d         2      2      1       1     1    1     1      1       2    1     2    1     2
15d        2      2      1       0     1    1     2      2       2    1     2    0     0
```

**Reading the weights:**

- **Scalp (5m/15m)**: Only the fast-moving signals matter — taker flow, stochastics, setup detector. Slow signals like funding/OI/IV are explicitly weighted to 0 (they barely change within minutes).
- **Intraday (1h/4h)**: Balanced — funding/OI become relevant, taker still strong, setup detector heavy (×3-4).
- **Swing (12h/1d)**: Funding and OI become primary (×2). Setup detector reduced because daily TFs don't have the same precision triggers.
- **Macro (15d)**: Funding/OI/IV/ETHBTC dominate. Taker flow becomes irrelevant (×0). Setup detector turned off (×0) — at this TF the entry-trigger signals are noise.

Setting a weight to **0** means: the factor is still **displayed** as a chip in the panel (so you know what's happening), but it doesn't contribute to the score. This keeps the panel informative without polluting the bias for a TF where that signal doesn't apply.

---

## 4. How the score is built (step by step)

For a given period (say `1h`), the algorithm walks through each factor in order:

### Step A: Direct contributions

Each factor checks its conditions and adds/subtracts:

```python
# Pseudocode for factor 1 (funding)
if funding > +0.0003:        score -= w.funding;          chip("Longs cargados (×w)")
elif funding > +0.0001:      score -= ceil(w.funding/2);  chip("Sesgo long")
elif funding < -0.0003:      score += w.funding;          chip("Shorts cargados (×w)")
elif funding < -0.0001:      score += ceil(w.funding/2);  chip("Sesgo short")
```

Same pattern for taker, ETH/BTC, gamma, stoch, etc — each factor is **independent** and produces a signed integer.

### Step B: Amplifiers (vol + IV)

After direct contributions, two factors act as **amplifiers** rather than independent contributors:

```python
# Vol compression amplifies an existing bias
if w.vol > 0 and volRatio < 0.7 and |score| >= 1:
    amp = sign(score) * w.vol
    score += amp
    chip("Vol comprimida amplifica score ±amp")

# IV premium amplifies an existing bias
if w.ivRv > 0 and ivRvSpread > 15 and |score| >= 1:
    amp = sign(score) * w.ivRv
    score += amp
    chip("IV premium amplifica score ±amp")
```

The amplifier only kicks in if there's already a bias (`|score| >= 1`) — vol compression on its own isn't bullish or bearish, but vol compression + existing bullish bias = stronger bullish bias.

### Step C: Money Quality (multiplicative weight)

MQ doesn't use the raw `w.mq` — it scales by the **quality** of the move:

```python
qMult = {high: 1.0, medium: 0.5, low: 0.25}[mqInfo.quality]
points = max(1, round(w.mq * qMult))
if mqInfo.direction == 'bullish': score += points
elif mqInfo.direction == 'bearish': score -= points
```

High-quality money flow (lots of price move per unit of OI = real accumulation) contributes more than low-quality (price moves on covering, not new money).

### Step D: Setup detector (the high-conviction final overlay)

This is the heaviest single factor when triggered (weight up to ×4 in 4h). Uses two multipliers:

```python
qMult = {'A++': 1.5, 'A+': 1.25, 'A': 1.0, 'BLOCKED': 0}[quality]
stateMult = {'TRIGGERED': 1.0, 'ARMED': 0.6, 'LATE': 0.2}[state]
points = round(w.setup * qMult * stateMult)
```

A `A++ TRIGGERED` setup in 4h: `4 × 1.5 × 1.0 = 6 points` (massive shift).
A `A ARMED` setup in 1h: `3 × 1.0 × 0.6 = 1.8 → 2 points` (modest).
A `BLOCKED` setup: `0 points` (chip is shown explaining why blocked, no score impact).

### Step E: Clamp

```python
score = max(-10, min(10, score))
```

Without clamp, the score could spike to ±20 in extreme situations. The clamp keeps the scale interpretable.

---

## 5. Score → state level mapping

After all 12 factors have run:

| Score | Level | Color | Label | Meaning |
|---|---|---|---|---|
| `≥ +3` | `bullish` | green | ALCISTA | High conviction long bias |
| `+2` | `caution` | amber | PRECAUCIÓN | Moderate long bias, confirm before entering |
| `+1` | `caution` | amber | PRECAUCIÓN | Weak long bias, reduce size |
| `0` | `neutral` | gray | NEUTRAL | No bias detected |
| `−1` | `caution` | amber | PRECAUCIÓN | Weak short bias, reduce size |
| `−2` | `caution` | amber | PRECAUCIÓN | Moderate short bias, confirm before entering |
| `≤ −3` | `bearish` | red | BAJISTA | High conviction short bias |

**Special case — deleveraging override**: if `oiChange < −8%` AND `|score| ≤ 1`, the state becomes `neutral` with conclusion "mercado limpiándose — esperar nuevo ciclo". This catches the dead zone after a big liquidation flush when no clear bias has formed yet.

### Conclusion text contextualizes the bias with VOL

The text "combustible para suba" / "combustible para caída" is then qualified by vol percentile:

- `volPct ≤ 30` (favorable) → `condiciones ideales para [side]`
- `volPct ≥ 75` (desfavorable) → `pero vol alta = movimiento puede estar agotándose`

So a `bullish` score at low vol reads "Combustible para SUBA + vol baja = condiciones ideales para long" while the same score at high vol reads "Combustible para SUBA pero vol alta = movimiento puede estar agotándose".

---

## 6. Sub-notes (gamma overlay + POC)

After the conclusion, the panel may add **sub-notes** based on options structure:

```
if (below gamma flip + bearish score)  → "puts dominan, dealers venden spot → cascada bajista"
if (below gamma flip + bullish score)  → "subida contra puts → resistencia por hedging"
if (above gamma flip + bullish score)  → "calls dominan, dealers compran spot → impulso alcista"
if (above gamma flip + bearish score)  → "caída contra calls → soporte por hedging"
if (precio cerca del POC + |score| ≤ 2) → "esperar ruptura"
if (max pain a < 2% del precio)         → "fuerza gravitacional hacia vencimiento"
```

These are **explanatory**, they don't change the score itself — they nuance the reading.

---

## 7. Regime warnings (contradictions)

A separate output `regimeWarnings[]` lists **internal contradictions** between indicators that suggest a regime change is brewing:

| Trigger | Warning |
|---|---|
| Funding loaded long + takers buying | Combustible para corrección, posible techo |
| Funding loaded short + takers selling | Combustible para rebote, posible piso |
| OI dropping fast + vol high | Liquidación en cascada en curso |
| Vol compressed + OI rising | Spring cargado, movimiento explosivo inminente |

These appear separately from the main bias chip and exist to highlight **when the score is about to flip** because the underlying indicators are diverging from one another.

---

## 8. Known limitations

### 8.1 Thresholds are heuristic, not calibrated

The `+3 → bullish` boundary, the `0.0003` funding threshold, the `1.15` taker ratio threshold — all of these are educated guesses. None have been backtested for predictive accuracy on actual returns.

**What this means in practice**: the score is **descriptive** (here's what the indicators say) but its **predictive** value hasn't been measured. A user shouldn't treat `+5` as "X% probability of upmove" — there's no such mapping derived from data.

### 8.2 No regime conditioning

The same threshold table is used in `STRESS` regimes and in `CHOP` regimes. But intuitively, +0.0003 funding in a STRESS environment may mean something different than in a CHOP environment.

**Future improvement**: cross with the HMM K=4 regime to adjust thresholds. E.g. in `STRESS`, scale funding/OI thresholds wider to avoid noise.

### 8.3 Period weights are subjective

The weight matrix `W` is hand-tuned based on the author's intuition. Different traders would weight differently. There's no objective optimization (e.g. "what weights maximize Sharpe?") behind it.

### 8.4 Factor independence assumption

The score sums factor contributions **assuming independence**. But funding + taker are correlated (both reflect positioning extreme). Adding both with full weight may double-count the same signal.

**Empirical mitigation**: the amplifier mechanics (vol, IV) explicitly only fire when there's an existing bias, which partially handles correlation. But the direct contributions don't account for it.

### 8.5 No backtest of the final label → returns mapping

We don't have data showing "when the score was ALCISTA, returns over the next N bars were X%". The score is intuitive, not validated.

---

## 9. Where to look in the code

- Function: `frontend/src/Dashboard.jsx:3901` → `computeSignals(data, period, stochTf)`
- Weight matrix: same file, around line 4006 (`const W = {...}`)
- Final state mapping: same file, around line 4279 (`if score >= 3...`)
- Render: same file, around line 4698 (`{signals.marketState && (() => {...})}`)
- The 12 factor blocks are commented in order `// ── 1. Funding ──`, `// ── 2. OI ──`, etc

---

## 10. References

- [[Regime classifier (HMM K=4)]] — the complementary slower structural signal
- [[Hedge ratio + label]] — similar pattern (multi-input → categorical output)
- [[Z-score (CEX netflows)]] — one of the implicit inputs (via flow direction/magnitude)
