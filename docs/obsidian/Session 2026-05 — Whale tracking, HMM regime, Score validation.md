---
tags: [session-log, context, score, validation, event-study]
created: 2026-05-29
status: active
---

# Sesión Mayo 2026 — Whale tracking, HMM regime, Score validation

Log de contexto de una sesión larga. Tres arcos: (1) features nuevos en el dashboard, (2) clasificador de régimen HMM, (3) el journey de validación del Market State Score que terminó reescribiendo cómo lo testeamos. El arco 3 es el que importa para el trabajo en curso.

Enlaces: [[Market State Score]], [[Market State Score - Quant Review]], [[Regime classifier (HMM K=4)]], [[ADR-006 Removed backtest framework]]

---

## Arco 1 — Features construidos (contexto, ya mergeado)

Trabajo cerrado durante la sesión, todo en `master`:

### CEX netflows (Dune) — fixes de fondo
- **Causa raíz del "sin datos"**: no era código ni quota — era el endpoint sin paginar. `GET /query/{id}/results` sin `?limit=` devuelve 1452 rows × 7 cols ≈ 10k datapoints en un request, supera el cap **por request** del plan free → HTTP 402. Fix: paginar con `limit=200`, fan-out paralelo del resto vía `execution_id`.
- **Fallback de API key**: `DUNE_API_KEY` → `DUNE_API_KEY_FALLBACK`, backoff por (key, method) en 402. Caveat descubierto: la query 6984181 está en el **motor V1 (Spark) deprecado** — el POST `/execute` da 400 "Deprecated query engine" con cualquier key. La data se sigue sirviendo del último snapshot cacheado de Dune (no se puede re-ejecutar hasta migrar la query a V2 / Dune SQL).
- **Bucket parcial**: la hora más reciente que devuelve Dune está en indexing activo (~30-90 min lag). `process_dune_netflows` detecta vía `(exec_ended_at − bucket_end) < 90min` y la excluye de los agregados (usa `[-2]` como ancla "ahora"), pero la mantiene en `hourly_series` para el chart, exponiéndola como `partialBucketTs`. Esto mató la varianza "los números saltan cada refresh".
- **Cache TTL 5min → 30min**: mata varianza multi-Lambda en Vercel + ahorra credits.
- **Direction logic**: cuando `|z| >= 1` se confía en el signo del z (z>0 = inflow regime = BEARISH); con `|z| < 1` se cae a net vs noise band. Sin ese split, el drift de la media del régimen hacía que el noise band se tragara lecturas claramente elevadas. → [[ADR-003 Direction from z-sign vs noise band]]

### Whale vs Retail panel
- L/S multi-exchange: Binance global (retail) + OKX + Bybit agregados como "retail", vs Binance top traders (`topLongShortPositionRatio`, top 20% por tamaño de posición = proxy whale).
- Deltas 1h/4h/24h de retail vs whale, divergencia con z-score histórico, sparklines, confluencia (whale dir + netflow dir + funding → reading).
- **USD exposure** (opción C): long/short/net USD por cohorte usando Pareto 75/25 (top 20% cuentas ≈ 75% del OI), con disclaimer visible de que es estimación.

### Hyperliquid Whales panel
- Posiciones on-chain reales de wallets curadas (`clearinghouseState` + `spotClearinghouseState`), fan-out paralelo, cache 5min.
- Hedge label: combina perp con spot HL (UETH) + **mainnet ETH via Etherscan** (V2 endpoint, `balancemulti`). `total_spot = ueth_HL + mainnet_L1`. Thresholds: `FULLY_HEDGED ≥0.8`, `PARTIAL ≥0.3`, sino `DIRECTIONAL_BET`. LONG perp + spot ≥0.3× = `DOUBLE_BULL`. → [[Hedge ratio + label]], [[ADR-004 Hedge uses HL UETH plus L1 mainnet]]
- **Descubrimiento**: las wallets curadas por default son **trading wallets de HL**, no custody. Por eso muestran ~0 ETH mainnet (dust de gas) y casi todo sale `DIRECTIONAL_BET`. Para hedges reales, agregar custody addresses a `HYPERLIQUID_WHALE_ADDRESSES`.

### UX
- Sidebar nav con categorías, quick stats bar arriba, paneles collapsibles. 4 layouts por horizonte (scalp/intraday/swing/macro) reordenan los mismos paneles.

### Vercel gotchas documentados (en CLAUDE.md)
- `includeFiles` NO funcionó para bundlear `data/regime/latest.json`. Solución: copiar el snapshot a `api/regime_snapshot.json` (sibling de la función → Vercel lo bundlea solo).
- `import json` faltaba en `api/index.py` — drift de imports entre los dos backends. El snapshot estaba bundleado pero `json.load` crasheaba silencioso → `regime: None`. Lección en CLAUDE.md: al agregar lógica a `api/index.py` que use un stdlib no importado, grepear el top primero.

---

## Arco 2 — Clasificador de régimen HMM K=4

Doc completo en [[Regime classifier (HMM K=4)]]. Resumen del journey:

- **Hipótesis nula descartada**: Markov observable sobre terciles de retorno 4h NO tiene estructura (diagonales 0.3-0.4, Δ +0.09pp vs persistencia). Los estados definidos solo por percentil de retorno no capturan régimen.
- **HMM con 8 features SÍ**: diagonales 0.96-0.98, dwell 4-10 días, estados interpretables (CRASH/STRESS/CHOP/UP).
- **K=4 elegido** sobre K=5 aunque BIC prefería K=5 — el 5to estado de K=5 es "centered noise" (max|mean| 0.156), artefacto de EM, no régimen operativo.
- **Robustez (6 tests)**: seed stability PASS, transition drift PASS, pero state-identity drift FAIL (vol signatures driftean 0.79σ entre años) → mitigado con **rolling re-fit semanal**.
- **Métrica clave (test 6)**: a 1 bar Markov = persistencia (trivial), pero a 24-48 bars (4-8 días) Markov bate persistencia +3 a +6pp. El valor predictivo está en horizontes de días, no next-bar.
- **Deploy**: snapshot file-based (`data/regime/latest.json` + mirror `api/regime_snapshot.json`), re-fit con `python scripts/run_regime_classifier.py --refresh-data --refit`. **El `--refresh-data` es obligatorio** — sin él los parquets quedan congelados y el modelo clasifica con data vieja (bug real observado: panel decía UP mientras el precio caía 2.59%, porque el modelo estaba entrenado con data de 29 días atrás).

---

## Arco 3 — Validación del Market State Score (EL TRABAJO EN CURSO)

### Qué es el score (recap)
12 factores fusionados con pesos por período → `[-10, +10]` → ALCISTA/PRECAUCIÓN/NEUTRAL/BAJISTA. Detalle completo en [[Market State Score]]. El quant review ([[Market State Score - Quant Review]]) marcó 3 debilidades estructurales: sin función objetivo, parámetros son priors no posteriors, suma de contribuciones asume independencia.

### Lo que implementamos (P1+P2+P3 del quant review)
- **P2 (logging)**: endpoint POST `/api/log/state-snapshot` → `data/state_log/YYYY-MM-DD.jsonl`. Frontend loguea throttled. Local-only (Vercel es efímero, no-op gracioso).
- **P1 (modulator)**: factores 3 (taker) y 4 (L/S) escalados por intensidad: `clip(|log(ratio)|/typical, 0.5, 1.5)`, centrado en 1.0 → neutral en promedio. Se loguean `scorePreModulator` y `scorePostModulator` para A/B.
- **P3 (validación)**: `scripts/validate_score_magnitude.py` — IC, sign-accuracy-condicional-a-|score|, IC decay, lift vs baseline, A/B del modulator.

### El backtest histórico parcial (lo que salió mal)
`scripts/backtest_score_historical.py` reconstruye el score de 5 años de parquets. Solo **6 de 12 factores** son reconstruibles (funding, taker, vol, ETH/BTC, VP-proxy, stoch); los otros 6 (OI, L/S, options, gamma, MQ, setup) necesitan persistencia que nunca tuvimos.

Resultado con n=1441 (60 días):
- **IC consistentemente NEGATIVO** (-0.10 a 12h, estadísticamente significativo)
- **Sign accuracy DECRECE con |score|** (|s|=1 → 52%, |s|≥5 → 35%)
- **Lift -2 a -4pp** vs baseline
- Modulator P1: Δ +0.02 (ayuda levemente)

### ⚠️ El error metodológico (lo más importante de la sesión)

El usuario marcó: **"no sabemos contra qué y cómo estamos testeando"**. Tenía razón. Aplicamos la mecánica del quant review (IC, sign accuracy) pero salteamos el paso 0: definir el TARGET antes de medir. Cuatro agujeros:

1. **Target equivocado**: medimos "¿el sign del score predice el sign del retorno a N horas?" cuando el score nunca afirmó eso. El propio MD dice "IS NOT a return predictor".
2. **Objeto equivocado**: testeamos 6/12 factores. No es el score. Y los 6 que faltan son los más direccionales.
3. **Horizonte sin relación**: un score "4h" no es "pronóstico a 4h", es "bias estructural para alguien operando swing 4h".
4. **Benchmark falso**: "persistencia" en trending es imbatible y no informa; "IC vs cero" asume linealidad que el score no pretende.

### 💡 El insight que reescribe el marco

Cuando se le preguntó **qué hace realmente con el score**, el usuario respondió:

> **"Entramos con los estocásticos + la confluencia del resto de datos. Siempre como mean[-reversion], por eso filtro con el resto de cosas."**

Esto cambia todo:
- El score **NO es un generador direccional** que seguís en cada barra.
- **El estocástico da el trigger** (mean-reversion: OS→long esperando rebote, OB→short).
- **El resto de factores es un filtro de confluencia**: el mean-reversion ciego pierde en trending (comprar el OS te liquida), entonces solo tomás el trigger cuando el contexto dice que el rebote es probable.

Por eso el backtest dio anti-predictivo: medíamos el score en las 1441 barras, pero vos solo operás en ~5-10% (cuando hay trigger). El otro 90% es ruido contrarian (VP en mean-rev, stoch OB/OS sin cruce). **Promediamos la señal real con 90% de ruido.**

### El marco correcto: EVENT STUDY (no panel-regression)

**Evento** = barra con cruce de fast stoch en zona extrema del slow:
- Long trigger: cruce al alza, fast %K < 30, slow %K < 40
- Short trigger: cruce a la baja, fast %K > 70, slow %K > 60

**Dirección** = mean-reversion siempre (OS→long, OB→short).

**Forward return** = en la dirección del trade, a varios horizontes.

Tres mediciones encadenadas:
1. **Baseline**: ¿el trigger solo tiene edge? (retorno medio + hit rate de TODOS los triggers en la dirección del trade)
2. **Attribution**: para cada factor de confluencia, partir triggers en "confirma" vs "contradice" la dirección. Si confirma >> contradice, ese factor es buen filtro. → te dice cuál filtro aporta y cuál es decoración.
3. **Confluencia**: ¿triggers donde N factores acompañan rinden más que donde pocos acompañan? Valida la idea misma del filtro.

Esto sí mide lo que el usuario hace, contra un benchmark con sentido (trigger crudo), con target alineado al horizonte (mean-reversion a pocos días). Y el trigger es 100% reconstruible del histórico — no necesita esperar logs ni los factores con persistencia faltante.

### Definiciones tomadas para el primer corrido (configurables, documentadas)

El usuario dijo "luego testea" sin fijar las 3 definiciones finas, así que se tomaron defaults razonables:
1. **Horizonte del trade**: medir múltiples (4h, 12h, 24h, 48h) y reportar todos — no elegir uno a priori.
2. **TF del trigger**: correr 1h y 4h por separado (los más usados para mean-rev intraday/swing).
3. **Dedup**: gap mínimo entre eventos = el horizonte medido (no re-contar el mismo rebote). Configurable.

### Estado del modulator P1
Flag MIXTO: la live data (n=72) decía Δ negativo, el backtest (n=1441) dice Δ +0.02 positivo. NO rollback todavía — esperar más data. Es chico en cualquier caso.

---

## Decisiones de diseño no triviales de la sesión

1. **K=4 sobre K=5 en el HMM** pese a BIC — parsimonia + interpretabilidad sobre fit puro.
2. **Snapshot file-based para regime** (no recompute per-request) — el fit toma 30-60s, no entra en Lambda 60s.
3. **Mirror del snapshot a `api/`** en vez de `includeFiles` — el segundo no funcionó en Vercel.
4. **No rollback del modulator P1** pese a flag negativo en live — n insuficiente, backtest contradice.
5. **Event study sobre triggers, no panel-regression sobre todas las barras** — porque el score es filtro de confluencia, no generador. ESTE es el cambio conceptual central.

## Próximo paso inmediato
Implementar `scripts/event_study_stoch_triggers.py` con el marco de arriba. Medir baseline → attribution → confluencia. Reportar honestamente si el filtro de confluencia mejora los triggers de mean-reversion.
