# Proveedores de datos — cuál usar para las alertas en vivo

*Verificado en agosto 2026. Los planes cambian; confirmá antes de pagar.*

## Resumen en una línea

**Ningún free tier te da NQ en tiempo real.** La mejor combinación gratuita es
**Twelve Data + QQQ** para alertar en vivo, y **Massive (ex-Polygon) Futures
Basic** para bajar histórico de NQ más profundo que el que da yfinance.

---

## El problema de fondo

Los datos de futuros de CME **no son gratis para nadie**. CME cobra licencia
por redistribuirlos, así que cualquier API que te dé NQ en tiempo real está
pagando esa licencia y te la traslada. Los free tiers que existen se financian
con datos de acciones y ETFs de EE.UU., donde el costo de licencia es mucho
menor.

Por eso la pregunta no es "¿cuál free tier tiene NQ?" sino "¿qué uso como
sustituto de NQ, y cuánto me cuesta esa diferencia?".

---

## Los tres que pediste comparar

### 1. Twelve Data — ✅ RECOMENDADO para las alertas en vivo

| | |
|---|---|
| **Futuros (NQ)** | ❌ **No cubre futuros en ningún plan** |
| **Free tier** | 800 créditos/día, 8 por minuto |
| **Qué incluye gratis** | Acciones y ETFs de EE.UU. **en tiempo real**, forex, cripto |
| **Planes pagos** | Grow USD 29/mes · Pro USD 99/mes · Ultra USD 329/mes |

**Por qué gana igual sin tener futuros:** es el único de los tres cuyo plan
gratuito da **precios en tiempo real** (no diferidos) de un instrumento que
sigue al Nasdaq-100 con precisión: el ETF **QQQ**.

La cuenta de cuota cierra con margen cómodo:

```
Sesión regular 09:30–16:00 ET = 6.5 h = 78 velas de 5 min
1 request por corrida del cron  →  78 requests/día
Cuota free: 800/día  →  usás ~10 %
```

Te sobra cuota para agregar un segundo símbolo (SPY, IWM) o bajar a velas de
1 minuto en una ventana acotada.

**Endpoint que usa el workflow:**

```
GET https://api.twelvedata.com/time_series
    ?symbol=QQQ
    &interval=5min
    &outputsize=120
    &order=desc
    &timezone=America/New_York
    &apikey=<va en la credencial de n8n, no en la URL>
```

---

### 2. Massive (ex-Polygon.io) — ✅ para histórico de futuros

> Polygon.io se renombró a **massive.com** en 2026. Los endpoints y la
> documentación viven ahí ahora; `polygon.io/pricing` redirige.

| | |
|---|---|
| **Futuros (NQ)** | ✅ **Sí** — CME, CBOT, NYMEX, COMEX completo |
| **Free tier ("Futures Basic")** | USD 0 · 5 requests/min · 2 años de histórico |
| **El problema** | El tier gratuito es **EOD / diferido**, no tiempo real |
| **Planes pagos** | Starter USD 29/mes · Developer USD 79/mes · Advanced USD 199/mes (este último es el que trae tiempo real) |

**Es el único free tier que realmente tiene NQ.** Pero al ser diferido, una
alerta de un cruce en la vela de las 10:35 te llegaría cerca de las 10:50 —
inservible para intradía en 5 minutos.

**Dónde sí conviene:** como fuente **histórica**. Su free tier te da 2 años de
velas de 1 minuto de NQ, contra los 60 días (y 7 días para 1m) de yfinance.
Para backtestear en serio, eso es la diferencia entre 100 y 2.000 trades de
muestra.

---

### 3. Alpha Vantage — ❌ descartado

| | |
|---|---|
| **Futuros (NQ)** | ❌ No cubre futuros |
| **Free tier** | **25 requests por día** |
| **Planes pagos** | Desde USD 49,99/mes (75 req/min, diferido 15 min); tiempo real desde USD 99,99/mes |

Queda afuera por aritmética: un cron de 5 minutos en horario de mercado
necesita ~78 requests diarios y el free tier da 25. Te quedás sin cuota antes
del mediodía. El límite gratuito supo ser 500/día y después 100/día; hoy son
25.

---

## Tabla comparativa

| | Twelve Data | Massive (ex-Polygon) | Alpha Vantage |
|---|---|---|---|
| NQ / futuros CME | ❌ ningún plan | ✅ sí | ❌ no |
| Free: requests | 800/día · 8/min | 5/min | **25/día** |
| Free: tiempo real | ✅ acciones/ETF EE.UU. | ❌ EOD/diferido | ❌ diferido |
| Alcanza para cron 5 min | ✅ (usa 10 % de cuota) | ✅ por rate, ❌ por delay | ❌ |
| Histórico intradía profundo | limitado | ✅ 2 años (free) | limitado |
| Precio del tiempo real | USD 29/mes+ (sin futuros) | USD 199/mes (futuros) | USD 99,99/mes (sin futuros) |
| **Veredicto** | **Alertas en vivo** | **Histórico de futuros** | Descartado |

---

## La recomendación, y qué se pierde

### Para las alertas en vivo: Twelve Data free + QQQ

**Qué se pierde al usar QQQ en vez de NQ:**

1. **Escala de precios distinta.** QQQ cotiza cerca de 1/41 del Nasdaq-100.
   Un movimiento de 40 puntos de NQ ≈ 1 dólar de QQQ. **Los TP/SL del
   backtest no se trasladan tal cual**: si backtesteás en NQ y alertás en
   QQQ, reescalá o backtesteá directamente sobre QQQ.
2. **Sin sesión overnight.** QQQ opera 09:30–16:00 ET; NQ opera casi 23 h.
   Si tu estrategia toca la apertura europea o la reacción a datos
   pre-market, QQQ no la ve.
3. **Gaps de apertura.** Como QQQ no cotiza de noche, abre con un gap que en
   NQ es movimiento continuo. Los indicadores se comportan distinto en la
   primera media hora.
4. **Dividendos.** QQQ paga dividendo trimestral y el precio ajusta; NQ no.
   Irrelevante en intradía, relevante si mirás series largas.

**Qué se mantiene:** la correlación intradiaria QQQ↔NDX es prácticamente 1.
Para una estrategia de cruce de medias sobre la sesión regular, las señales
salen casi en los mismos momentos. Es un buen proxy — pero es un proxy, y hay
que decirlo.

**Alternativa dentro del mismo plan:** si preferís el índice en vez del ETF,
probá `NDX`. Los índices suelen contar como un market aparte y podrían no
estar en el free tier — verificá con tu key antes de depender de eso. QQQ es
la opción segura porque los ETFs de EE.UU. sí están incluidos.

### Para backtestear más profundo: Massive Futures Basic (gratis)

Te da NQ real con 2 años de velas de 1 minuto. El delay no importa cuando
estás leyendo el pasado.

### Si querés NQ real y en tiempo real, hay que pagar

| Opción | Costo | Comentario |
|---|---|---|
| **Massive Futures Advanced** | USD 199/mes | CME completo en tiempo real, API limpia |
| **Databento** | pago por uso (GB) | Tick a tick desde 2010; lo mejor para research serio, precio variable |
| **Tu broker** | incluido | Si ya tenés cuenta en IBKR / Tradovate / Rithmic, la API de datos suele venir con la cuenta o costar ~USD 10–15/mes en fees de exchange. **La opción más barata si ya operás.** |
| **FirstRate Data** | pago único | Datasets históricos de NQ; no sirve para vivo |

> **Nota sobre "non-professional".** Los precios de retail de arriba asumen
> estatus no profesional. Si operás para un fondo o una empresa, CME cobra
> tarifas profesionales bastante más altas.

---

## Camino sugerido

1. **Ahora:** yfinance (NQ=F, 60 días) para backtestear + Twelve Data free
   (QQQ) para alertar. Costo: **USD 0**. Sirve para validar que la estrategia
   tiene algo antes de gastar.
2. **Si la estrategia muestra edge:** sumá Massive Futures Basic (gratis) para
   backtestear sobre 2 años de NQ real y ver si el edge sobrevive a más
   regímenes de mercado.
3. **Recién si el edge aguanta:** pagá tiempo real. Empezá por la API de tu
   broker, que es lo más barato si ya tenés cuenta.

No pagues datos en tiempo real para una estrategia que todavía no
backtesteaste bien. El orden importa.

---

## Fuentes

- [Twelve Data — Pricing](https://twelvedata.com/pricing)
- [Twelve Data — Documentación de la API](https://twelvedata.com/docs)
- [Massive — Futures Data API](https://massive.com/futures)
- [Massive — Pricing](https://massive.com/pricing)
- [Alpha Vantage — límites de la API (Macroption)](https://www.macroption.com/alpha-vantage-api-limits/)
- [Comparación de APIs de futuros: Polygon vs Databento (edgeful)](https://www.edgeful.com/blog/posts/futures-data-api-polygon-databento-edgeful-comparison)
- [Databento — datos históricos de NQ](https://databento.com/catalog/cme/GLBX.MDP3/futures/NQ)
