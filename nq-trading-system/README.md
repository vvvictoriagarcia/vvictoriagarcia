# NQ Trading System

Backtester intradiario + bot de alertas para futuros de índices (NQ, ES) e
índices en general.

Dos partes que comparten la misma lógica de estrategia:

1. **Backtester en Python** — descarga histórico, aplica las reglas vela por
   vela, y devuelve una tabla de trades con timestamps al minuto, resultado en
   puntos y en %, métricas agregadas y gráfico.
2. **Workflow de n8n** — corre cada 5 minutos en horario de mercado y avisa por
   Telegram cuando se cumple la condición de entrada.

> **Solo análisis y alertas.** No se conecta a ningún broker, no ejecuta
> órdenes, no mueve plata. Manda mensajes y escribe CSVs.

---

## Empezar

```bash
cd nq-trading-system
pip install -r requirements.txt
cp .env.example .env      # solo hace falta para las alertas (Parte 2)

python -m src.main
```

Sin configurar nada corre NQ=F en 5 minutos, últimos 55 días, con la
estrategia placeholder.

### Salida de una corrida real

Sobre datos reales de NQ=F, 5 minutos, 2026-06-22 → 2026-08-14 (2.973 velas de
sesión regular):

```
  ACTIVIDAD
  Trades totales........................ 102
    long / short........................ 51 / 51
  Duración media........................ 2.0 velas

  ACIERTO
  Win rate.............................. 36.27 %
    ganadores / perdedores.............. 37 / 65
  Racha perdedora máxima................ 16 trades

  RENTABILIDAD
  Resultado total....................... -22.50 pts
  Resultado total....................... -450.00 USD
  Retorno total......................... -1.80 %
  Profit factor......................... 0.984
  Expectativa por trade................. -0.22 pts / -4.41 USD

  RIESGO
  Drawdown máximo....................... -7,305.00 USD  (-23.13 %)

  COSTOS
  Costos totales........................ 153.00 pts / 3,060.00 USD

  MOTIVOS DE SALIDA
    SL..................................   64  (63 %)
    TP..................................   35  (34 %)
    SESSION_END.........................    3  (3 %)

  [!] Los costos (153.0 pts) se comen el 117% del resultado BRUTO (+130.5 pts).
```

Los tres primeros trades del CSV:

| # | dir | entrada | precio | salida | precio | pts | % | motivo |
|---|---|---|---|---|---|---|---|---|
| 1 | long | 2026-06-23 12:45 | 29.902,50 | 2026-06-23 12:45 | 29.882,50 | −21,50 | −0,072 | SL |
| 2 | short | 2026-06-23 12:55 | 29.854,75 | 2026-06-23 12:55 | 29.874,75 | −21,50 | −0,072 | SL |
| 4 | short | 2026-06-23 13:40 | 29.850,00 | 2026-06-23 13:50 | 29.810,00 | +38,50 | +0,129 | TP |

Corrida completa en [`outputs/example_run/`](outputs/example_run/) (trades.csv,
equity.csv, metrics.json, chart.png).

**Ese resultado es negativo, y está bien que lo sea.** La estrategia cargada es
un placeholder para validar la cañería, no una estrategia para operar. Lo
interesante del número es *cómo* pierde: en bruto gana +130 puntos, y los
costos (153 puntos en 102 trades) se lo comen entero. Es el modo típico de
morir de un cruce de medias en 5 minutos — opera demasiado para el edge que
tiene.

---

## Estructura

```
nq-trading-system/
├── config.yaml              # TODO lo parametrizable
├── .env.example             # claves (Telegram, API de datos)
├── requirements.txt
│
├── src/
│   ├── config.py            # carga y valida config.yaml + .env
│   ├── data_fetcher.py      # yfinance + manejo de sus límites
│   ├── indicators.py        # EMA, RSI, ATR, soportes/resistencias, volumen
│   ├── strategy.py          # ← LA LÓGICA. Es el archivo que vas a editar
│   ├── backtester.py        # motor vela por vela + métricas
│   ├── plotting.py          # gráficos (matplotlib / plotly)
│   ├── signal_service.py    # API HTTP opcional para n8n (ver n8n/README.md)
│   └── main.py              # punto de entrada
│
├── n8n/
│   ├── workflow_twelvedata_telegram.json   # listo para importar
│   ├── strategy.js          # port exacto de strategy.py, verificado
│   ├── live_wrapper.js      # glue de n8n
│   └── README.md            # guía de instalación de la Parte 2
│
├── tools/
│   ├── check_parity.py      # ¿Python y JS dan lo mismo?
│   ├── build_n8n_workflow.py# genera el JSON del workflow
│   └── test_live_node.py    # testea el Code node de n8n
│
├── tests/                   # pytest (27 tests, sin red)
├── docs/DATA_PROVIDERS.md   # qué proveedor usar y por qué
└── outputs/                 # resultados (gitignored, salvo example_run/)
```

El diseño responde a una sola regla: **la estrategia se cambia sin tocar nada
más**.

---

## Cómo reemplazar la lógica de la estrategia

Todo pasa en `src/strategy.py`. El resto del sistema no sabe qué reglas usás.

### El contrato

Escribís una clase con un método `evaluate(ctx)` que recibe el estado del
mercado en una vela y devuelve una `Signal`:

```python
@register("mi_estrategia")
class MiEstrategia(Strategy):
    def evaluate(self, ctx: Context) -> Signal:
        ...
        return Signal("LONG", reason="...", context={...})
```

**Lo que recibís en `ctx`:**

| Campo | Qué es |
|---|---|
| `ctx.bar` | La vela actual, ya cerrada, con todos los indicadores |
| `ctx.prev` | La vela anterior (o `None` si es la primera) |
| `ctx.position` | `'long'`, `'short'` o `None` |
| `ctx.bars_in_position` | Cuántas velas llevás dentro |
| `ctx.entry_price` | Precio de entrada de la posición abierta |
| `ctx.params` | Tus parámetros desde `config.yaml` |

Los indicadores se leen con `.get()`, que devuelve `None` si todavía está en
warm-up:

```python
rsi = ctx.bar.get("rsi")           # None si aún no hay valor
if ctx.bar.ready("ema_fast", "rsi"):   # chequea varios de una
    ...
```

Indicadores disponibles: `ema_fast`, `ema_slow`, `ema_spread`, `rsi`, `atr`,
`volume_ma`, `volume_ratio`, `resistance`, `support`, `dist_to_resistance`,
`dist_to_support`.

**Lo que devolvés:** `Signal("LONG" | "SHORT" | "EXIT" | "NONE", reason, context)`.

El `reason` va al CSV y al mensaje de Telegram. El `context` son los valores
que gatillaron la señal — aparecen como columnas `ind_*` en el CSV y en la
alerta.

### Ejemplo completo

Digamos que querés: **comprar cuando el precio rompe la resistencia con
volumen alto, y vender cuando pierde el soporte.**

**1. Agregá la clase en `src/strategy.py`:**

```python
@register("breakout_sr")
class BreakoutSR(Strategy):
    """Ruptura de soporte/resistencia con confirmación de volumen."""

    required_indicators = ("resistance", "support", "volume_ma")

    def evaluate(self, ctx: Context) -> Signal:
        bar, prev = ctx.bar, ctx.prev
        if prev is None or not bar.ready("resistance", "support", "volume_ma"):
            return NO_SIGNAL

        p = self.params
        vol_factor = float(p.get("volume_factor", 1.5))

        # Confirmación de volumen: sin esto, cualquier mecha rompe niveles.
        if bar.volume < bar.get("volume_ma") * vol_factor:
            return NO_SIGNAL

        resistance = bar.get("resistance")
        support = bar.get("support")

        ctx_vals = {
            "resistance": round(resistance, 2),
            "support": round(support, 2),
            "volume_ratio": round(bar.volume / bar.get("volume_ma"), 2),
        }

        # Ruptura al alza: el cierre anterior estaba debajo, este arriba.
        if prev.close <= resistance < bar.close and p.get("allow_long", True):
            return Signal("LONG", f"Ruptura de resistencia {resistance:.2f}", ctx_vals)

        if prev.close >= support > bar.close and p.get("allow_short", True):
            return Signal("SHORT", f"Pérdida de soporte {support:.2f}", ctx_vals)

        return NO_SIGNAL
```

**2. Apuntá `config.yaml` a la nueva estrategia:**

```yaml
strategy:
  name: "breakout_sr"
  params:
    volume_factor: 1.5
    allow_long: true
    allow_short: true
```

**3. Corré:**

```bash
python -m src.main
```

No tocaste `data_fetcher.py`, `backtester.py`, `indicators.py` ni `main.py`.

### Las tres reglas del contrato

1. **No mires al futuro.** Solo tenés `ctx.bar` y `ctx.prev`. El backtester
   nunca te pasa velas posteriores, así que el lookahead es imposible salvo
   que lo introduzcas vos usando un indicador centrado — ver la advertencia en
   `indicators.pivots()`.
2. **`evaluate` tiene que ser pura.** Mismos inputs, mismo output, sin estado
   entre llamadas. De eso depende que el backtest y las alertas en vivo den lo
   mismo.
3. **No manejes TP/SL acá.** Los maneja el backtester con la sección `risk` de
   `config.yaml`, porque necesita ver los High/Low intra-vela. Acá solo decidís
   entrar o salir por señal.

### Si necesitás un indicador nuevo

Agregalo en `src/indicators.py` y sumale una línea a `add_indicators()`.
Queda disponible como `ctx.bar.get("mi_indicador")`.

Si es un indicador con período, sumalo a `warmup_bars()` para que el
backtester no opere antes de que esté listo.

### Después de cambiar la estrategia

```bash
python -m pytest tests/ -q       # nada roto
python -m src.main               # backtest nuevo
```

Y si vas a usar las alertas en vivo, reflejá el cambio en `n8n/strategy.js` y:

```bash
python tools/check_parity.py       # ¿Python y JS siguen dando lo mismo?
python tools/build_n8n_workflow.py # regenerar el workflow
```

Ver [`n8n/README.md`](n8n/README.md) para el detalle, incluida la opción de
mantener una sola implementación.

---

## Configuración

Todo en [`config.yaml`](config.yaml). Lo que más se toca:

```yaml
data:
  ticker: "NQ=F"          # NQ=F, ES=F, ^NDX, QQQ...
  interval: "5m"          # 1m, 5m, 15m, 30m, 1h, 1d
  lookback_days: 55
  regular_hours_only: true    # recorta a 09:30-16:00 ET

indicators:
  ema_fast: 9
  ema_slow: 21
  rsi_period: 14

strategy:
  name: "ema_cross_rsi"   # cambiando esto cambiás toda la lógica
  params: {...}

risk:
  mode: "points"          # points | percent | atr
  take_profit: 40.0
  stop_loss: 20.0

execution:
  fill: "next_open"       # next_open (realista) | signal_close (optimista)
  commission_points: 0.25
  slippage_points: 0.50
  point_value_usd: 20.0   # NQ=$20/pt · MNQ=$2/pt
```

Todo se puede pisar por CLI:

```bash
python -m src.main --ticker ES=F --interval 15m --lookback 30
python -m src.main --strategy rsi_reversion --no-cache
python -m src.main --list-strategies
```

---

## Límites de yfinance

Yahoo no guarda histórico intradiario profundo, y **falla en silencio**:
devuelve menos velas de las pedidas sin avisar. `data_fetcher.py` valida el
rango antes de descargar y aborta con una explicación en vez de dejarte
backtestear sobre datos incompletos.

| Intervalo | Historia máxima |
|---|---|
| `1m` | 7 días |
| `2m` – `90m` | 60 días |
| `1h` | 730 días |
| `1d` y mayores | sin límite práctico |

Si te pasás:

```
  ┌─ RANGO FUERA DE LOS LÍMITES DE YFINANCE ─────────────────────────
  │ Pediste  : 5m desde 2026-01-15 (213 días atrás)
  │ Yahoo da : 5m solo hasta 60 días atrás (desde 2026-06-17)
  │
  │ Opciones:
  │  1. Bajá el rango:      data.lookback_days: 60
  │  2. Subí el timeframe:  data.interval: "15m" (hasta 60 días)
  │  3. Cambiá de fuente para historia intradiaria profunda:
  │       • Databento, Massive (ex-Polygon), FirstRate Data, IBKR
  └──────────────────────────────────────────────────────────────────
```

Con `--allow-range-overflow` avisa pero corre igual.

**Sobre `NQ=F`:** es el continuo del front month, con saltos de precio en cada
rollover trimestral (mar/jun/sep/dic). Para backtests de más de un trimestre
conviene una serie ajustada por rollover.

---

## Decisiones del backtester

Los números de un backtest son tan creíbles como sus supuestos. Estos son los
de acá:

1. **Sin lookahead.** Con `fill: next_open` (default) la entrada se ejecuta en
   la apertura de la vela *siguiente* a la señal. En vivo, cuando la vela
   cierra y aparece la señal, lo más temprano que podés estar adentro es la
   apertura de la próxima. `signal_close` existe para comparar, pero es
   optimista.
2. **TP/SL se evalúan intra-vela** con High/Low, no con el Close. Si el precio
   tocó el stop en el medio de la vela, saliste ahí aunque haya cerrado a favor.
3. **Si TP y SL caen en la misma vela, gana el stop.** Con datos OHLC no se
   sabe cuál se tocó primero; asumir el stop es la convención conservadora. El
   contador `ambiguous_tp_sl_bars` te dice cuántas veces pasó — si es alto
   respecto del total de trades, tus targets son chicos para la volatilidad del
   timeframe y el backtest es poco confiable.
4. **Los costos se restan siempre.** Comisión + slippage, ida y vuelta. Un
   backtest de 5 minutos sin costos no significa nada — este mismo placeholder
   es rentable en bruto y perdedor en neto.
5. **Una posición por vez**, tamaño fijo, sin pirámide ni sizing dinámico.
6. **Nada queda abierto overnight** (`close_at_session_end: true`).

Las métricas incluyen advertencias automáticas cuando la muestra es chica, los
costos dominan, o hay muchas velas ambiguas.

---

## Tests

```bash
python -m pytest tests/ -q        # 27 tests, sin red
```

Cubren las fórmulas de los indicadores (EMA/RSI/ATR contra valores calculados
a mano), que los soportes/resistencias **no miren al futuro**, la mecánica del
motor (fills, TP/SL, costos, MAE acotado por el stop) y las métricas.

Además, dos verificaciones que necesitan red:

```bash
python tools/check_parity.py      # Python vs JavaScript sobre datos reales
python tools/test_live_node.py    # el Code node de n8n en 8 escenarios
```

---

## Parte 2 — Alertas en vivo

Ver [`n8n/README.md`](n8n/README.md).

**Resumen del proveedor de datos** (detalle completo en
[`docs/DATA_PROVIDERS.md`](docs/DATA_PROVIDERS.md)):

Ninguno de los tres free tiers que preguntaste cubre NQ en tiempo real. Los
datos de CME tienen licencia paga y eso se traslada.

| | Twelve Data | Massive (ex-Polygon) | Alpha Vantage |
|---|---|---|---|
| Futuros CME | ❌ ningún plan | ✅ sí | ❌ no |
| Free tier | 800/día · 8/min | 5/min | **25/día** |
| Free en tiempo real | ✅ acciones y ETFs EE.UU. | ❌ EOD/diferido | ❌ diferido |
| **Veredicto** | **usar para alertas** | **usar para histórico** | descartado |

- **Para alertar: Twelve Data free + `QQQ`.** Es el único free tier con
  precios en tiempo real de un instrumento que sigue al Nasdaq-100. El cron de
  5 minutos usa ~78 de los 800 créditos diarios. El costo de usar QQQ en vez
  de NQ: escala de precios ~1/41 (hay que reescalar TP/SL), sin sesión
  overnight, y gaps de apertura.
- **Para histórico: Massive Futures Basic (gratis)** — 2 años de velas de 1
  minuto de NQ real, contra los 60 días de yfinance. Es diferido, pero para
  leer el pasado no importa.
- **Alpha Vantage queda afuera** por aritmética: 25 requests/día contra los
  ~78 que necesita el cron.
- **Si querés NQ real y en vivo hay que pagar:** Massive Futures Advanced USD
  199/mes, Databento por uso, o —lo más barato si ya operás— la API de datos
  de tu propio broker (IBKR, Tradovate, Rithmic).

---

## Advertencia

La estrategia que viene cargada (`ema_cross_rsi`) es un **placeholder**. Un
cruce de EMAs desnudo en 5 minutos es de las cosas más estudiadas y arbitradas
que existen: en mercado lateral genera whipsaw constante y, con costos reales,
tiende a perder — como muestra la corrida de arriba.

Existe para que puedas verificar que el pipeline funciona de punta a punta
antes de meter tu lógica. No la operes.

Un backtest positivo tampoco alcanza: sobre 55 días y ~100 trades no hay
significancia estadística. Antes de arriesgar plata, buscá 300+ trades sobre
varios regímenes de mercado, testeá fuera de muestra, y verificá que el
resultado no dependa de un puñado de trades.

Este sistema es una herramienta de análisis. No ejecuta órdenes ni se conecta
a ningún broker, y no constituye asesoramiento financiero.
