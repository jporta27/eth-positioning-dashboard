---
tags: [quant, review, scoring, validation, market-state]
status: review
created: 2026-05-28
context: Complemento quant del Market State Score. Asume lectura previa de [[Market State Score]]
---

# Market State Score — Revisión Quant y Plan de Validación

Este documento NO redescribe el score (ver [[Market State Score]]). Asume que ya
conocés las 12 capas, la matriz W de pesos por período, el mapeo score→estado y las
limitaciones documentadas (secciones 8.1–8.5). Acá hay dos cosas: **cómo lo evaluaría
como quant** y **qué haría, en orden de prioridad, para volverlo defendible**.

---

## 1. El diagnóstico honesto antes de tocar nada

El score, tal como está, es un **sistema experto descriptivo bien diseñado**, no un
modelo cuantitativo validado. Esto no es un insulto: es una descripción precisa de su
estado de madurez. Hace bien una cosa difícil —agregar 12 señales heterogéneas en una
lectura coherente y legible— pero hereda tres debilidades estructurales que cualquier
quant marcaría antes de confiar capital en él:

1. **No hay función objetivo.** El score predice "bias", pero nunca se definió contra
   qué se mide el acierto. ¿Retorno a N barras? ¿Sign accuracy? ¿Sharpe de una regla
   que lo use? Sin esto, "funciona" es una afirmación no falsable.

2. **Los parámetros son priors, no posteriors.** Umbrales (`+3→bullish`, `0.0003`
   funding, `1.15` taker) y pesos (matriz W) son intuición codificada. Razonables como
   punto de partida, pero ninguno tocó datos. Hoy son hipótesis, no resultados.

3. **Suma de contribuciones ≠ medida ponderada.** El score suma enteros con signo
   asumiendo independencia entre factores (limitación 8.4). Factores correlacionados
   (funding+taker, ambos miden extremo de posicionamiento) pueden contar dos veces la
   misma información. Y ningún factor comparte una vara común —el volumen efectivo es
   amplificador, no denominador— que es la grieta principal identificada.

La buena noticia: las tres son arreglables sin reescribir el sistema, y en un orden que
da valor incremental sin romper lo que ya anda.

---

## 2. Cómo lo evaluaría (la métrica primero, siempre)

**Regla quant número uno: no se optimiza lo que no se mide.** Antes de tocar un umbral
o un peso, hay que poder responder "¿esto mejoró o empeoró?". Eso exige fijar la métrica
ANTES de mirar resultados, para no caer en elegir la métrica que hace ver bien al modelo.

### 2.1 Definir el target

El score es por período. La evaluación también. Para cada período `p` de la matriz W,
el target natural es el **retorno forward** a un horizonte proporcional al período:

```
target(t, p) = sign( price(t + h_p) − price(t) )      # para sign accuracy
o
target(t, p) = (price(t + h_p) − price(t)) / price(t)  # para correlación / IC
```

donde `h_p` es el horizonte de tenencia típico de ese período (ej: para `1h`, h≈1–4h;
para `4h`, h≈4–24h; para `1d`, h≈1–7d). El horizonte debe alinearse con cómo USÁS la
señal, no elegirse para que el número quede lindo.

### 2.2 Las métricas que importan

En orden de utilidad para este caso:

- **Information Coefficient (IC):** correlación de Spearman entre `score(t)` y
  `target(t)`. Es la métrica madre. Un IC de 0.03–0.05 ya es explotable en cripto si es
  estable; 0.10+ sostenido sería excelente y sospechoso (revisar leakage).
- **Sign accuracy condicionada a |score|:** ¿el acierto direccional sube con la
  magnitud? Si `|score|≥3` no acierta más que `|score|=1`, entonces la magnitud no
  informa convicción y el mapeo de estados es decorativo. **Este es el test más
  importante para tu sistema**, porque toda la semántica (ALCISTA vs PRECAUCIÓN)
  depende de que la magnitud signifique algo.
- **Decay del IC por horizonte:** calcular IC a h = 1, 2, 4, 8, 16 barras. Te dice
  cuánto dura la señal. Si decae a cero en 2 barras, es señal de microestructura; si
  persiste 16, es estructural. Esto valida (o no) tu intuición de qué TF usar para qué.
- **Lift sobre baseline:** comparar contra el predictor tonto. En cripto el baseline
  brutal es "momentum del último retorno" o "siempre el signo de la tendencia". Si el
  score de 12 factores no le gana a `sign(retorno_anterior)`, hay un problema serio.

### 2.3 Cómo NO autoengañarse

- **Walk-forward, nunca in-sample.** Calibrar en ventana `[t-N, t]`, evaluar en
  `(t, t+M]`, rodar. Cualquier número in-sample es ficción.
- **Cuidado con el survivorship del régimen.** Cripto 2023–2025 tuvo regímenes muy
  distintos. Un score calibrado solo en bull no vale en crash. Evaluar por régimen HMM
  (ya tenés el clasificador K=4) por separado.
- **Transaction costs.** Cualquier regla derivada del score tiene que sobrevivir
  fees+slippage. Un IC positivo que muere después de costos no es alfa, es ruido caro.
- **Multiple testing.** Vas a probar muchos umbrales. Si probás 50, alguno va a verse
  bien por azar. Corregir (Bonferroni es brutal pero honesto; o reservar un test set
  intocable hasta el final).

---

## 3. Qué haría, en orden de prioridad

Ordenado por **(valor esperado / riesgo de romper algo que anda)**. Lo de arriba es
seguro e incremental; lo de abajo es más potente pero más invasivo.

### Prioridad 1 — Cerrar la grieta del volumen efectivo (modulación, no nuevo factor)

Es la mejora con mejor relación valor/riesgo porque ataca el defecto conceptual central
y ya tenés el dato (`Delta/Vol` por período, computado en el panel de flujo taker).

**Qué:** convertir el volumen efectivo en **modulador** de los factores de flujo
(taker #3, L/S #4, Money Quality #11), no en amplificador aparte. La contribución
direccional se escala por cuán respaldada está por volumen efectivo.

**Cómo hacerlo seguro (neutro en promedio):** centrar el modulador en 1.0 para un
`Delta/Vol` típico del período, de modo que solo se desvíe en los extremos.

```python
# dv = Delta/Vol del período (ej +8.96% perp)
# dv_typical = mediana móvil de |Delta/Vol| en ventana de calibración del período
m = clip(abs(dv) / dv_typical, 0.5, 1.5)   # modulador centrado ~1.0
contrib_taker = base_taker_contrib * m      # base = lo que ya calculás hoy
```

Así un movimiento respaldado (dv alto) pesa más, uno vacío pesa menos, y el caso típico
contribuye igual que hoy → **no descalibra los umbrales de estado**. La grieta se cierra
donde hacía daño (los extremos) sin tocar el resto del sistema.

**Bonus:** esto le pone un techo natural al double-counting de la 8.4. Funding y taker
solo pueden sumar pleno cuando el volumen efectivo respalda, y ese respaldo es único por
flujo, así que no se duplica.

**Cuidado:** mantener separados el modulador-por-Delta/Vol (¿la agresión es real?) y la
contextualización-por-vol de la sección 5 (¿el contexto la sostiene?). Son dos
multiplicadores conceptualmente distintos; mezclarlos en el código es un bug esperando
pasar.

### Prioridad 2 — Instrumentar para medir (logging del score y sus componentes)

No se puede validar lo que no se registró. Antes de calibrar nada hay que **loguear,
en cada cómputo**: timestamp, período, score final, contribución de cada uno de los 12
factores por separado, y el precio. Sin el desglose por factor no se puede hacer
attribution (qué factor aporta IC y cuál es ruido). Esto es trabajo de plomería, no
glamoroso, pero es el prerequisito de TODO lo demás. Un mes de log limpio vale más que
cualquier intuición.

### Prioridad 3 — Validar la magnitud (el test que decide si el mapeo de estados vive)

Con datos logueados, correr el test de sign-accuracy-condicionada-a-|score| (sección
2.2). Resultado y consecuencia:
- Si el acierto sube monótonamente con |score| → el mapeo de estados está validado,
  dormís tranquilo.
- Si es plano → la magnitud no informa convicción. Hay que **recalibrar los boundaries**
  (quizá ALCISTA debería ser ≥+5, no ≥+3) o aceptar que el score solo da signo, no
  convicción. Mejor saberlo que asumirlo.

### Prioridad 4 — Atacar la independencia con datos (no a ojo)

Con el log por factor, calcular la **matriz de correlación entre contribuciones de
factores**. Donde haya correlación alta (la sospecha es funding↔taker, y vol↔ivRv),
hay dos caminos:
- **Pragmático:** bajar el peso conjunto de pares correlacionados en la matriz W
  (descuento manual informado por la correlación medida).
- **Correcto:** un paso de PCA o de whitening sobre el bloque de factores correlacionados
  antes de sumar, para que cada componente independiente cuente una vez. Más limpio, pero
  rompe la legibilidad del chip ("este factor aportó −2") que es una virtud del sistema
  actual. Trade-off real: interpretabilidad vs corrección estadística.

### Prioridad 5 — Regime-conditioning de umbrales (limitación 8.2)

Una vez que hay métrica y log, cruzar con el HMM K=4: calibrar umbrales **por régimen**.
La hipótesis (8.2) es que +0.0003 de funding significa cosas distintas en STRESS vs CHOP.
Ahora es testeable: calcular el IC del factor funding por régimen y ver si el umbral
óptimo difiere. Si difiere, tabla de umbrales por régimen. Si no, la simplicidad actual
gana y se documenta que se testeó.

### Prioridad 6 — Optimización de pesos (lo último, y con escepticismo)

La matriz W hand-tuned (8.3) es la tentación obvia de "optimizar", y por eso la pongo
ÚLTIMA. Optimizar pesos sobre datos de cripto es la forma más rápida de overfittear:
pocas observaciones independientes, regímenes cambiantes, y 7 períodos × 13 factores =
muchísimos grados de libertad. Si se hace:
- Maximizar IC out-of-sample, no Sharpe (Sharpe invita a overfittear la cola).
- Regularización fuerte (los pesos óptimos deberían parecerse a los intuitivos; si el
  optimizador quiere poner el peso de funding en 0 a 5m, probablemente confirma la
  intuición, no la contradice).
- Anclar a los pesos manuales como prior (optimización bayesiana / shrinkage hacia W).
- Sospechar de cualquier mejora grande. Una mejora de IC de 0.02→0.08 por reoptimizar
  pesos es casi seguro overfitting.

---

## 4. Lo que NO haría

- **No reemplazaría el sistema experto por un modelo ML de caja negra.** El valor del
  score es que es legible y auditable: cada chip explica por qué. Un gradient boosting
  con IC 0.06 que nadie puede interpretar es peor para operar discrecionalmente que un
  score interpretable con IC 0.05. La interpretabilidad es alfa cuando el humano está en
  el loop.
- **No perseguiría IC alto a costa de robustez.** En cripto, un IC de 0.04 estable a
  través de regímenes vale más que 0.12 que solo existe en bull market.
- **No tocaría los umbrales antes de tener log y métrica.** Cambiar `+3` a `+5` sin
  datos es cambiar un prior por otro prior. Inútil.
- **No confiaría en backtests de menos de ~2 regímenes completos.** Un solo régimen no
  prueba nada sobre generalización.

---

## 5. El orden mínimo viable (si solo hubiera tiempo para 3 cosas)

1. **Loguear** score + 12 contribuciones + precio (Prioridad 2). Sin esto nada más es
   posible.
2. **Cerrar la grieta de volumen** con el modulador neutro-en-promedio (Prioridad 1).
   Mejora conceptual con riesgo casi nulo, no necesita esperar al log.
3. **Test de magnitud** apenas haya ~1 mes de log (Prioridad 3). Es el que te dice si la
   semántica de estados (ALCISTA/PRECAUCIÓN/etc) está validada o es decorativa.

Todo lo demás (correlación, regime-conditioning, optimización de pesos) viene después y
solo tiene sentido sobre la base de los tres anteriores.

---

## 6. La frase para recordar

El score hoy es una **hipótesis bien estructurada sobre cómo se combina la información
del mercado**. El trabajo quant no es reemplazarlo —está bien pensado— sino convertir
cada pieza de intuición en una afirmación testeable, medirla, y dejar que los datos
confirmen o ajusten los priors. La grieta del volumen efectivo es la única corrección
*conceptual* pendiente; el resto es disciplina de validación.
