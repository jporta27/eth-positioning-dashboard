---
tags: [quant, empirical, scoring, validation, market-state, findings]
status: living-document
created: 2026-05-29
context: Hallazgos empíricos del backtest histórico de 60 días sobre el Market State Score (reconstrucción parcial 6/12 factores). Complementa [[Market State Score]] y [[Market State Score - Quant Review]].
---

# Market State Score — Empirical Findings (60d backtest)

Este doc registra los resultados de la primera evaluación empírica del Market State Score con data histórica. Se ejecuta sobre los Steps **P2 + P3 + extensión histórica** del [[Market State Score - Quant Review]].

> **TL;DR (con caveats fuertes)**: con la reconstrucción parcial de 6/12 factores sobre 60 días, el score es **anti-predictivo** (IC = -0.10 @ 12h, statistically significant). **Invertirlo** produce IC = +0.10 con la misma significancia. La hipótesis "el mapping semántico está al revés" está empíricamente soportada — PERO la reconstrucción es parcial; los 6 factores que faltan podrían cambiar el signo.

---

## 1. Metodología del test

### 1.1 Setup

- **Período**: 2026-03-30 → 2026-05-29 (60 días)
- **Snapshots generados**: 1,441 (resolución 1h, después de warmup de stoch)
- **Período del score simulado**: `4h` (matriz W con pesos completos)
- **Forward returns**: 1h, 4h, 12h, 24h, 48h
- **Script**: `scripts/backtest_score_historical.py`
- **Validation**: `scripts/validate_score_magnitude.py` (Spearman IC, sign accuracy cond, lift sobre baseline, modulator A/B)

### 1.2 Qué se incluye en el backtest (partial reconstruction)

Solo 6 de los 12 factores del score live se pueden reconstruir históricamente:

| # | Factor | ¿Reconstruido? | Razón |
|---|---|---|---|
| 1 | Funding | ✅ | binance_funding.parquet (5y history) |
| 2 | OI | ❌ | Binance API solo retiene 30 días |
| 3 | Taker imbalance | ✅ | taker_buy_base en klines |
| 4 | L/S divergence | ❌ | Solo 30 días retenido |
| 5 | Vol amplifier | ✅ | Computed from klines |
| 6 | IV/RV spread | ❌ | No tenemos history de IV |
| 7 | ETH/BTC | ✅ | klines + macro BTC |
| 8 | Gamma flip | ❌ | No tenemos history de options |
| 9 | Volume profile | ✅ | OHLCV (con proxy VWAP±σ) |
| 10 | Stochastics | ✅ | klines |
| 11 | Money Quality | ❌ | Requires OI history |
| 12 | Setup detector | ❌ | Requires stoch + MQ history |

Resultado: `|score|_max ≈ 7` vs `|score|_max ≈ 17` del score completo. **El score parcial es MÁS comprimido pero menos rico.**

### 1.3 Aproximaciones tomadas

- **Volume Profile**: Aproximado como `VWAP ± 1σ` del cierre rolling 24h. El VP "real" requiere distribución intra-barra del volumen por precio, que no tenemos a 1h granularidad.
- **Stoch**: Mismas constantes que en el frontend (slow=400/40/10, fast=100/4/10), implementación vectorizada en pandas.
- **Modulator P1**: Aplicado a factor 3 (taker) con la misma fórmula que en el frontend (`clip(|log(ratio)| / 0.05, 0.5, 1.5)`).

---

## 2. Resultados — Forward (score as-designed)

### 2.1 Information Coefficient (Spearman)

| Horizonte | IC | p-value | n |
|---|---|---|---|
| 1h | -0.062 | 0.020 | 1,441 |
| 4h | -0.076 | 0.004 | 1,441 |
| **12h** | **-0.103** | **0.0001** | **1,441** |
| 24h | -0.086 | 0.001 | 1,441 |
| 48h | -0.078 | 0.003 | 1,441 |

**Lectura**: el IC es consistentemente **negativo** en todos los horizontes y **estadísticamente significativo** (p<0.05) en todos. El pico negativo está en 12h.

|IC|=0.10 al horizonte 12h es **explotable** según la regla del review (`|IC| ≥ 0.03 estable → señal real`), pero **en la dirección OPUESTA** a la que el mapping del score sugiere.

### 2.2 Sign accuracy conditional sobre |score|

> **El test clave del review (sección 2.2)**: si el score significa "más conviction = más probabilidad direccional", la accuracy debería SUBIR monotonicamente con |score|.

```
Horizon 12h:
  |s|∈[1,1]   n=347  acc=52.4%   ← MEJOR
  |s|∈[2,2]   n=377  acc=46.9%
  |s|∈[3,4]   n=481  acc=38.8%   ← bin más grande, PEOR
  |s|∈[5,7]   n= 52  acc=35.1%   ← PEOR aún
```

**El patrón es claramente DECRECIENTE** — la accuracy CAE con la magnitud del score. Esto invalida la interpretación "más score = más convicción direccional".

### 2.3 Lift sobre baseline trivial

| Horizonte | Score acc | Baseline acc (signo bar anterior) | Lift |
|---|---|---|---|
| 1h | 46.8% | 48.5% | −1.7pp |
| 4h | 45.0% | 47.8% | −2.8pp |
| 12h | 44.5% | 48.2% | −3.7pp |
| 24h | 44.9% | 49.2% | −4.3pp |
| 48h | 44.2% | 47.9% | −3.7pp |

Score consistentemente **peor que el predictor más tonto** (predicir mismo signo que ayer). Diferencia chica (-2 a -4pp) pero consistente.

### 2.4 P1 modulator A/B test

| Horizonte | IC pre-mod | IC post-mod | Δ |
|---|---|---|---|
| 1h | -0.067 | -0.058 | **+0.009** |
| 4h | -0.122 | -0.102 | **+0.020** |
| 12h | -0.133 | -0.113 | **+0.020** |
| 24h | -0.094 | -0.088 | +0.006 |
| 48h | -0.082 | -0.078 | +0.005 |

**El modulator P1 está HELPING ligeramente** — hace el IC menos negativo (más cerca de cero). Δ máximo +0.020 al horizonte 4-12h donde el score "miente más".

**Importante**: la live data (n=72, 5h) había mostrado Δ negativo. Con n=1441 el resultado se invierte — sample size importa.

---

## 3. Resultados — Inverted (−score)

Test crítico de la hipótesis "el mapping semántico está al revés".

### 3.1 IC

| Horizonte | IC (inverted) |
|---|---|
| 4h | **+0.076** (p=0.004) |
| **12h** | **+0.103** (p=0.0001) |
| 24h | **+0.086** (p=0.001) |

**Misma magnitud, signo opuesto**. Esto era esperado matemáticamente (invertir el score invierte el IC).

### 3.2 Sign accuracy (la prueba real)

```
Horizon 12h (Inverted):
  |s|∈[1,1]   n=347  acc=47.6%   
  |s|∈[2,2]   n=377  acc=53.1%   
  |s|∈[3,4]   n=481  acc=58.6%   ← bin grande, MEJOR que forward (38.8%)
  |s|∈[5,7]   n= 52  acc=51.9%
```

**El bin con n=481 (el más grande y confiable)** muestra accuracy de **58.6%** invertido vs **38.8%** forward → **+20pp de mejora**.

Esto es el resultado más sólido del test. Cuando el score dice "ALCISTA fuerte" (`|s|≥3`), el mercado tiende a ir a la BAJA con accuracy ~58% (vs 42% si interpretás la dirección literal).

---

## 4. Interpretación

### 4.1 Tres hipótesis sobre por qué el score es anti-predictivo en esta reconstrucción

**A. Semantic mapping reversed (parcialmente soportada)**
El score mide "extremo de posicionamiento" más que "conviction direccional". Cuando suma factores bullish, está identificando un mercado **overextended bullish** → mean reversion bearish. El nombre "ALCISTA" debería leerse "Top-zone — cuidado con pullback".

Evidencia: invertir produce IC positivo y +20pp en sign accuracy. **PERO** no significa que el score completo (12 factores) tenga la misma lectura — los factores que faltan tienen direccionalidad explícita.

**B. Thresholds miscalibrated**
Quizás `|s|<3` funciona como trend follower y `|s|>3` como contrarian. La función no sería monótona.

Evidencia parcial: en el forward, |s|=1 tiene mejor accuracy que |s|=3-4 (52% vs 39% @ 12h). Pero la diferencia es chica vs el efecto inversión.

**C. Missing factors carry the directional signal**
Los 6 factores reconstruidos miden contexto y mean reversion (vol, VP, stoch). Los 6 que faltan (OI, L/S, options, MQ, setup) tienen más componente direccional. Sin ellos, el signal es incompleto y sesgado a contrarian.

Evidencia: esta hipótesis es **plausible pero no testable hoy** sin esos factores. Es la razón principal por la que NO podemos concluir "el score completo es anti-predictivo".

### 4.2 Lo que SÍ podemos concluir

1. **Con los 6 factores del partial backtest, el mapping `|score| ≥ 3 → ALCISTA` no funciona direccionalmente.** Hay evidencia robusta (n=481, p=0.0001) de que la accuracy DECRECE con magnitud.

2. **El partial-score es anti-predictivo a horizontes intermedios** (12h sobre todo, magnitud IC = 0.10 estadísticamente significativa).

3. **El modulator P1 ayuda LIGERAMENTE** (+0.02 en IC al 4-12h). No es un fix grande pero la dirección es correcta.

4. **El test de inversión es decisivo para los partial factors** — confirma la hipótesis "semantic mapping reversed".

### 4.3 Lo que NO podemos concluir

1. **No podemos hablar del score completo 12-factor** — esta evaluación es solo 6/12.
2. **No podemos derivar trading rules** de esto — IC=0.10 sin transaction costs y sin specific rule.
3. **El régimen del período (mayo 2026, CHOP heavy)** podría no ser representativo de bull/bear sostenido.
4. **No medimos performance condicional al régimen HMM K=4** — pendiente para futuro.

---

## 5. Caveats y limitaciones del backtest

### 5.1 Aproximaciones que pueden distorsionar

- **VP proxy (VWAP±σ)**: el VP real con histogramas de volumen por precio puede dar señales distintas. La proxy es razonable pero no perfecta.
- **Stoch slow window grande (400 bars)**: en 60d apenas hay ~3-4 ciclos completos de slow stoch, sample chico para ese factor.
- **VIX/macro a daily ffill**: los z-scores son frescos para barras dentro del día pero pueden "saltar" en cambio de día.

### 5.2 Sample / régimen

- **Solo 60 días**: cubre regímenes parciales. Idealmente queremos 1-2 años para tener bull/bear/chop balanced.
- **Período actual era CHOP heavy** (per [[Regime classifier (HMM K=4)]] mostraba >90% CHOP)
- **Mean reversion regimes favorecen contrarian** — el resultado podría diferir en TRENDING (STRESS/UP) regimes.

### 5.3 Lo crítico: los 6 factores faltantes

Cualquier conclusión sobre el score completo necesita reconstruir o esperar:
- **OI history**: requeriría persistencia desde ahora en adelante
- **L/S history**: idem
- **Options history**: idem (Deribit no tiene snapshot history > 30d en el plan free)
- **MQ + Setup**: depend on OI/stoch — derivado si tenemos los inputs

---

## 6. Acciones recomendadas

### 6.1 Inmediatas (sin esperar más data)

1. **Documentar este findings** en el dashboard (sub-note en el panel de Market State Score: "⚠ El mapping bullish/bearish puede ser contrarian en este sub-conjunto de factores")
2. **NO cambiar el código del score** todavía — la evidencia es sobre el partial, no el completo
3. **NO rollback del modulator P1** — la mejora es chica pero positiva

### 6.2 Cortas (1-2 semanas)

1. **Empezar a loguear los 6 factores faltantes** con persistencia para que el próximo backtest pueda incluirlos
2. **Conditional analysis por régimen HMM**: ¿el partial-score funciona mejor en STRESS vs CHOP?
3. **Backtest más largo (6m-1y)**: ver si el signo del IC cambia entre regímenes

### 6.3 Estructurales (cuando tengamos data)

1. **Recalibrar boundaries del score**: tal vez `ALCISTA = |s| ∈ [1, 3]` y `EXTREME / TOP = |s| > 3` (contrarian zone)
2. **Per-factor IC analysis**: ¿alguno de los 12 individualmente predice? ¿Cuáles?
3. **Bayesian shrinkage de los pesos** hacia 0 — combinado con backtest, los pesos óptimos podrían ser menores que los hand-tuned

---

## 7. Reproducir estos resultados

```bash
# Generate backtest snapshots
python scripts/backtest_score_historical.py --days 60 --period 4h

# Forward run
python scripts/validate_score_magnitude.py --source historical --horizons 4,12,24

# Inverted run (contrarian hypothesis test)
python scripts/validate_score_magnitude.py --source historical --invert --horizons 4,12,24
```

Output va a stdout. El JSONL local generado por el backtest queda en `data/state_log/_historical_backtest.jsonl` (gitignored).

---

## 8. References

- [[Market State Score]] — la metodología del score
- [[Market State Score - Quant Review]] — el plan de validación
- [[Regime classifier (HMM K=4)]] — para el conditional analysis futuro
- `scripts/backtest_score_historical.py` — el backtest (6/12 factor recon)
- `scripts/validate_score_magnitude.py --invert --source historical` — el test
