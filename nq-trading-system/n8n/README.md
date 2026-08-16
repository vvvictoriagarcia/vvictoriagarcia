# Parte 2 — Workflow de alertas en vivo (n8n)

Workflow que corre cada 5 minutos en horario de mercado, calcula los mismos
indicadores que el backtester, aplica las mismas reglas y te manda un mensaje
por Telegram cuando se cumple la condición de entrada.

> **Solo alertas.** El workflow no se conecta a ningún broker ni ejecuta
> órdenes. Manda un mensaje y termina.

---

## Archivos

| Archivo | Qué es |
|---|---|
| `workflow_twelvedata_telegram.json` | **El workflow listo para importar** (generado) |
| `strategy.js` | Port exacto de `src/strategy.py` + indicadores. Verificado contra Python |
| `live_wrapper.js` | Glue de n8n: config, ventana rolling, dedup, horario, manejo de errores |
| `../tools/build_n8n_workflow.py` | Genera el JSON a partir de los dos `.js` |
| `../tools/check_parity.py` | Verifica que el JS y el Python den lo mismo |
| `../tools/test_live_node.py` | Testea el Code node simulando el runtime de n8n |

---

## Instalación en 6 pasos

### 1. Conseguir una API key de datos

Registrate gratis en [twelvedata.com](https://twelvedata.com/) y copiá la key.
El plan gratuito alcanza de sobra: el workflow usa ~78 de los 800 créditos
diarios. Ver [`../docs/DATA_PROVIDERS.md`](../docs/DATA_PROVIDERS.md) para el
detalle de por qué Twelve Data y no otro.

### 2. Crear el bot de Telegram

1. Hablale a [@BotFather](https://t.me/BotFather) → `/newbot` → te da el
   **token**.
2. Mandale un mensaje cualquiera a tu bot nuevo (si no, no puede escribirte).
3. Abrí `https://api.telegram.org/bot<TOKEN>/getUpdates` y sacá el
   **chat id** de `result[0].message.chat.id`.
   - Para un grupo el id es negativo, tipo `-1001234567890`.

### 3. Importar el workflow

En n8n: **Workflows → ⋯ → Import from File** →
`workflow_twelvedata_telegram.json`.

### 4. Cargar las credenciales (están como PLACEHOLDER)

El JSON **no contiene ninguna clave**. Vas a ver dos credenciales marcadas
como `PLACEHOLDER` que hay que crear:

**a) `PLACEHOLDER · Twelve Data API Key`** — tipo **Query Auth**:

| Campo | Valor |
|---|---|
| Name | `apikey` |
| Value | tu key de Twelve Data |

**b) `PLACEHOLDER · Telegram Bot`** — tipo **Telegram API**:

| Campo | Valor |
|---|---|
| Access Token | el token que te dio BotFather |

Después abrí cada nodo y seleccioná la credencial que creaste.

### 5. Definir el chat id

El nodo de Telegram lee `{{ $env.TELEGRAM_CHAT_ID }}`. Definí esa variable de
entorno en tu n8n:

```bash
# docker run
docker run -e TELEGRAM_CHAT_ID=123456789 ... n8nio/n8n

# docker-compose.yml
environment:
  - TELEGRAM_CHAT_ID=123456789
```

> En **n8n.cloud** no se pueden definir variables de entorno arbitrarias en el
> plan base. Si es tu caso, reemplazá la expresión por el chat id literal en
> el campo *Chat ID* del nodo de Telegram. Es el único valor que quedaría
> escrito en el workflow, y no es un secreto en el mismo sentido que el token.

### 6. Probar y activar

1. **Execute Workflow** a mano y mirá el output del nodo
   *Indicadores + Estrategia*: tiene que decir `ok: true`.
   - Si estás fuera del horario de mercado vas a ver
     `skipReason: "Fuera del horario de mercado"`. Es correcto.
2. Para forzar una alerta de prueba, poné momentáneamente
   `marketHours.enabled: false` y bandas de RSI amplias (`0` y `100`) en el
   Code node.
3. Cuando funcione, **Activate**.

---

## Cómo está armado

```
┌──────────────────────────┐
│ Cron 5 min               │  */5 13-20 * * 1-5  (UTC)
│ (horario mercado)        │  filtro grueso; el fino lo hace el Code node
└───────────┬──────────────┘
            ▼
┌──────────────────────────┐
│ Twelve Data · time_series│  120 velas · retry x3 · onError: continuar
└───────────┬──────────────┘
            ▼
┌──────────────────────────┐
│ Indicadores + Estrategia │  ← strategy.js + live_wrapper.js
│ (Code node)              │    ventana rolling, dedup, EMA/RSI/ATR, señal
└───────────┬──────────────┘
            ▼
┌──────────────────────────┐
│ ¿Señal válida?           │  ok === true  Y  hasSignal === true
└─────┬──────────────┬─────┘
      │ true         │ false
      ▼              ▼
┌───────────┐  ┌──────────────────────┐
│ Telegram  │  │ Sin señal / error    │
└───────────┘  └──────────────────────┘
```

### Requisito 1 — Cron cada 5 minutos en horario de mercado

El cron es `*/5 13-20 * * 1-5` en **UTC**. Esa ventana cubre 09:30–16:00 de
Nueva York tanto en horario de verano como de invierno. El recorte exacto
(incluido el cambio de hora) lo hace el Code node con
`Intl.DateTimeFormat` sobre `America/New_York`, que maneja el DST solo.

Hacerlo en dos capas evita el bug clásico de un cron fijo en UTC que se
desfasa una hora dos veces al año.

### Requisito 2 — HTTP Request a una API de datos

Twelve Data `time_series`. La elección está justificada en
[`../docs/DATA_PROVIDERS.md`](../docs/DATA_PROVIDERS.md). El resumen: **ningún
free tier da NQ en tiempo real**; Twelve Data es el único que da tiempo real
gratis de un instrumento que sigue al Nasdaq-100 (QQQ).

### Requisito 3 — Ventana rolling sin acumular histórico

En cada corrida se le piden a la API exactamente las **120 velas** que hacen
falta y se descarta todo lo demás. No se guarda historial en ningún lado.

Es deliberado: la alternativa —ir acumulando velas en el static data de
n8n— parece más eficiente pero se corrompe sola. Si se pierde una ejecución
queda un hueco silencioso en la serie y los indicadores quedan mal para
siempre, sin ningún síntoma visible. Pedirle la ventana entera a la API en
cada corrida cuesta un request y se auto-repara.

Las 120 velas salen de `warmup + colchón`: el máximo de los períodos
configurados (21) + 5, más 80 velas para que la EMA converja de verdad. Una
EMA deja de ser `null` a las 21 velas pero recién a las ~80 coincide con la
que calculó el backtester sobre una serie larga.

El static data se usa **solo para deduplicar** (`lastAlertKey`), que es
información que sí se puede perder sin consecuencias.

### Requisito 4 — La MISMA lógica que el backtester

Este es el punto delicado, porque el backtester es Python y n8n corre
JavaScript. Hay dos formas de resolverlo y el repo trae las dos.

#### Opción A — JS embebido (la que viene configurada)

`n8n/strategy.js` es un port línea por línea de `src/strategy.py` y
`src/indicators.py`. Lo que hace que esto sea confiable y no una promesa:

1. **`tools/check_parity.py`** corre ambas implementaciones sobre las mismas
   velas reales y compara indicador por indicador y señal por señal.

   Resultado de la última corrida sobre NQ=F 5m:

   ```
   Velas comparadas ............ 2973
   Señales en Python ........... 105
   Señales en JavaScript ....... 105
   Máxima diferencia numérica .. 0.00e+00
   ✓ PARIDAD OK
   ```

   Cero diferencia, no "diferencia chica".

2. **`tools/build_n8n_workflow.py`** inyecta ese mismo archivo verificado
   dentro del Code node. No se copia y pega a mano, así que no se puede
   desincronizar entre lo que se testea y lo que se despliega.

```
src/strategy.py ──(check_parity)── n8n/strategy.js ──(build)── workflow.json
```

**Al cambiar la estrategia:**

```bash
# 1. editás src/strategy.py
# 2. reflejás el cambio en n8n/strategy.js
python tools/check_parity.py          # ¿siguen dando lo mismo?
python tools/build_n8n_workflow.py    # regenerar el JSON
python tools/test_live_node.py        # ¿el Code node se comporta bien?
# 3. reimportás el workflow en n8n
```

#### Opción B — Servicio HTTP (una sola implementación)

Si no querés mantener dos versiones, `src/signal_service.py` expone la función
Python real por HTTP:

```bash
pip install fastapi uvicorn
uvicorn src.signal_service:app --host 0.0.0.0 --port 8000
```

Y en n8n reemplazás el Code node por un **HTTP Request**:

```
POST http://tu-host:8000/signal
Header: X-Auth-Token: <SIGNAL_SERVICE_TOKEN>
Body:   { "candles": [ ...velas ascendentes... ], "symbol": "QQQ" }
```

La respuesta trae los mismos campos que el Code node (`ok`, `hasSignal`,
`signal`, `reason`, `indicators`), así que el resto del workflow no cambia.

**El costo:** necesitás un proceso Python al que n8n pueda llegar. Si n8n está
en la nube y esto en tu notebook, no se ven. Por eso la Opción A es el default.

**Cuál elegir:** si vas a tocar la estrategia seguido, la B te ahorra el doble
mantenimiento. Si querés que el workflow sea autónomo, la A.

### Requisito 5 — Mensaje de Telegram

```
🟢 SEÑAL LONG

Ticker: QQQ
Timeframe: 5min
Vela: 2026-08-14 14:05:00
Precio: 601.42

Motivo: Cruce alcista EMA + RSI 54.7 en (50, 70)

Indicadores
• EMA rápida: 601.18
• EMA lenta: 601.02
• RSI: 54.73
• ATR: 0.61
• Volumen: 481203 (media 442117)

Alerta de análisis. No es una orden ni ejecuta operaciones.
```

Incluye ticker, timestamp, precio, tipo de señal y los valores de los
indicadores que la gatillaron.

### Requisito 6 — Manejo de errores (nunca alertas falsas)

Tres capas:

1. **Nodo HTTP:** `retryOnFail` con 3 intentos y backoff de 3 s. Con
   `onError: continueRegularOutput` un fallo definitivo no corta el workflow.
   `neverError: true` es necesario porque **Twelve Data devuelve HTTP 200 con
   `{status:"error"}` en el body** cuando la key es inválida o se acabó la
   cuota — si solo mirás el status code, procesás basura como si fueran datos.
2. **Code node:** todo envuelto en `try/catch`, y devuelve `ok:false` con el
   motivo en vez de lanzar excepción. Valida la forma de la respuesta, que
   haya velas suficientes, y que los números sean finitos.
3. **Nodo IF:** exige `ok === true` **Y** `hasSignal === true`. Es la
   compuerta que garantiza que ningún error se convierta en alerta.

Además, dos cosas que no son "errores" pero producen alertas malas:

- **Vela en formación.** La vela en curso cambia de precio hasta que cierra,
  así que una señal calculada sobre ella aparece y desaparece ("repinta").
  `dropFormingCandle()` la descarta: en vivo se evalúa solo sobre velas
  cerradas, igual que en el backtest.
- **Alertas duplicadas.** Un cron de 5 minutos y velas de 5 minutos se
  desalinean seguido y la misma vela se evalúa dos veces. `lastAlertKey`
  deduplica por `símbolo|timestamp`.

**Verificación.** `tools/test_live_node.py` corre el Code node real contra 8
escenarios simulando el runtime de n8n:

```
[ OK ] 1. Camino feliz — velas válidas, se espera señal
[ OK ] 2. Dedup — misma vela otra vez, no debe re-alertar
[ OK ] 3. La API devuelve 200 con body de error (key inválida)
[ OK ] 4. El nodo HTTP falló (onError pasa un item con `error`)
[ OK ] 5. Respuesta sin velas (values vacío)
[ OK ] 6. Velas insuficientes para los indicadores
[ OK ] 7. Sin items del nodo anterior
[ OK ] 8. Compuerta de horario de mercado activa
```

En los 6 casos de error: `ok:false`, `hasSignal:false`, sin excepción.

---

## Configuración

Los parámetros viven en el objeto `CONFIG` arriba de `live_wrapper.js`:

```javascript
const CONFIG = {
  symbol: 'QQQ',
  interval: '5min',
  intervalMinutes: 5,
  indicators: { emaFast: 9, emaSlow: 21, rsiPeriod: 14, ... },
  params: { rsi_long_min: 50.0, rsi_long_max: 70.0, ... },
  marketHours: { enabled: true, open: '09:30', close: '16:00' },
  deduplicate: true,
};
```

**Tienen que coincidir con `config.yaml`.** Si backtesteás con EMA 9/21 y
alertás con 12/26, las alertas no tienen nada que ver con lo que validaste.

Para cambiar símbolo o timeframe sin editar a mano:

```bash
python tools/build_n8n_workflow.py --symbol SPY --interval 15min --interval-minutes 15
```

> Editá los `.js` y regenerá, **no el JSON ni el nodo en la UI de n8n**. Si
> editás el Code node en la UI, el próximo build te lo pisa y perdés la
> garantía de paridad.

---

## Problemas frecuentes

| Síntoma | Causa | Solución |
|---|---|---|
| `error: "API: Invalid API key"` | Credencial mal cargada | La credencial Query Auth tiene que llamarse `apikey` (minúscula) |
| `error: "API: You have run out of API credits"` | Cuota agotada | Free = 800/día. Revisá si tenés otros workflows pegándole a la misma key |
| `skipReason: "Fuera del horario de mercado"` | Corriste fuera de 09:30–16:00 ET | Es lo esperado. Para probar, poné `marketHours.enabled: false` |
| `error: "Velas insuficientes"` | `outputsize` bajo | Subí `--bars` y regenerá el workflow |
| Nunca llega ninguna alerta | Filtros de RSI muy estrictos | Ampliá `rsi_long_min/max`, o revisá en el backtest cuántas señales daba |
| Telegram: "chat not found" | No le escribiste al bot | Mandale un mensaje al bot primero; para grupos el id es negativo |
| Llegan alertas repetidas | Dedup desactivado | `deduplicate: true` |
| Las alertas no coinciden con el backtest | Se desincronizaron las implementaciones | `python tools/check_parity.py` |

---

## Antes de confiar en esto

La estrategia que viene cargada es un **placeholder** (cruce EMA 9/21 + RSI).
En el backtest sobre 55 días de NQ=F en 5 minutos dio **win rate 36 %,
profit factor 0.98 y −1.8 % de retorno**: bruto positivo (+130 puntos), pero
los costos (153 puntos) se lo comen entero.

No la operes. Sirve para confirmar que la cañería funciona de punta a punta.
Meté tu lógica real en `src/strategy.py`, reflejala en `strategy.js`, y recién
ahí mirá si las alertas valen algo.
