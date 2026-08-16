"""
===============================================================================
  ESTRATEGIA — ESTE ES EL ÚNICO ARCHIVO QUE TENÉS QUE TOCAR
===============================================================================

Todo el resto del sistema (data_fetcher, indicators, backtester, main, y el
workflow de n8n) es infraestructura. La lógica de decisión vive acá, aislada
detrás de una interfaz mínima.

CÓMO REEMPLAZAR LA ESTRATEGIA
-----------------------------
1. Escribí una clase que herede de `Strategy` e implemente `evaluate()`.
2. Decorala con @register("mi_estrategia").
3. En config.yaml poné strategy.name: "mi_estrategia" y tus params.
4. Corré `python -m src.main`. Listo — no tocaste nada más.

Ver README.md § "Cómo reemplazar la lógica de la estrategia" para un ejemplo
completo paso a paso.

EL CONTRATO
-----------
`evaluate(ctx)` recibe el estado del mercado en UNA vela y devuelve un
`Signal`. Las reglas del contrato:

  * ctx.bar    -> la vela actual, ya CERRADA (open/high/low/close/volume +
                  todos los indicadores como atributos del dict).
  * ctx.prev   -> la vela anterior, o None si es la primera.
  * ctx.position -> 'long', 'short' o None. Permite lógica de salida propia.
  * ctx.params -> dict con strategy.params de config.yaml.

  * NO mires al futuro. Solo tenés ctx.bar y ctx.prev. El backtester nunca
    te pasa velas posteriores, así que el lookahead bias es imposible por
    construcción — salvo que lo introduzcas vos usando un indicador centrado
    (ver la advertencia en indicators.pivots).

  * `evaluate` tiene que ser PURA: mismos inputs -> mismo output, sin estado
    entre llamadas y sin efectos de lado. De eso depende que el backtest y
    las alertas en vivo den lo mismo.

  * TP y SL NO se manejan acá. Los maneja el backtester según la sección
    `risk` de config.yaml, porque necesita ver los High/Low intra-vela.
    Acá solo decidís ENTRAR o SALIR-POR-SEÑAL.

SINCRONIZACIÓN CON LAS ALERTAS EN VIVO
--------------------------------------
El workflow de n8n reimplementa esta misma lógica en JavaScript
(n8n/strategy.js). Si cambiás las reglas acá, tenés que reflejarlas allá.
`python tools/check_parity.py` compara ambas implementaciones vela por vela
sobre datos reales y falla si divergen — corrélo después de cada cambio.

Si preferís no mantener dos implementaciones, corré `src/signal_service.py`:
expone ESTA función por HTTP y n8n la consume, con lo cual hay una sola
fuente de verdad. Ver n8n/README.md § Opción B.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

SignalType = Literal["LONG", "SHORT", "EXIT", "NONE"]


# --------------------------------------------------------------------------
# Tipos del contrato
# --------------------------------------------------------------------------

@dataclass
class Bar:
    """Una vela ya cerrada, con sus indicadores.

    Los indicadores se acceden con .get(): bar.get("rsi"). Devuelve None si
    el indicador todavía está en warm-up (NaN), así que siempre chequeá None
    antes de comparar.
    """

    timestamp: Any
    open: float
    high: float
    low: float
    close: float
    volume: float
    indicators: dict[str, float] = field(default_factory=dict)

    def get(self, name: str) -> float | None:
        value = self.indicators.get(name)
        if value is None:
            return None
        if isinstance(value, float) and math.isnan(value):
            return None
        return value

    def ready(self, *names: str) -> bool:
        """True si todos los indicadores nombrados tienen valor."""
        return all(self.get(n) is not None for n in names)


@dataclass
class Context:
    """Todo lo que la estrategia puede ver en el momento de decidir."""

    bar: Bar
    prev: Bar | None
    position: str | None          # 'long' | 'short' | None
    bars_in_position: int = 0
    entry_price: float | None = None
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Signal:
    """Lo que la estrategia devuelve.

    type:
        "LONG"  -> abrir largo
        "SHORT" -> abrir corto
        "EXIT"  -> cerrar la posición actual por decisión de la estrategia
        "NONE"  -> no hacer nada
    reason:  texto corto que va al CSV y al mensaje de Telegram
    context: valores que dispararon la señal; se muestran en la alerta
    """

    type: SignalType = "NONE"
    reason: str = ""
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def is_entry(self) -> bool:
        return self.type in ("LONG", "SHORT")

    @property
    def direction(self) -> str | None:
        if self.type == "LONG":
            return "long"
        if self.type == "SHORT":
            return "short"
        return None


NO_SIGNAL = Signal()


# --------------------------------------------------------------------------
# Registro de estrategias
# --------------------------------------------------------------------------

STRATEGY_REGISTRY: dict[str, type["Strategy"]] = {}


def register(name: str) -> Callable[[type["Strategy"]], type["Strategy"]]:
    """Decorator para registrar una estrategia bajo un nombre."""

    def wrapper(cls: type["Strategy"]) -> type["Strategy"]:
        if name in STRATEGY_REGISTRY:
            raise ValueError(f"Ya existe una estrategia registrada como {name!r}")
        cls.name = name
        STRATEGY_REGISTRY[name] = cls
        return cls

    return wrapper


class Strategy:
    """Clase base. Heredá de acá e implementá evaluate()."""

    name: str = "base"
    #: Indicadores que esta estrategia necesita. Se usa para documentar y
    #: para que el chequeo de warm-up sepa qué esperar.
    required_indicators: tuple[str, ...] = ()

    def __init__(self, params: dict[str, Any] | None = None):
        self.params = dict(params or {})

    def evaluate(self, ctx: Context) -> Signal:
        raise NotImplementedError

    def describe(self) -> str:
        return f"{self.name}({self.params})"


def get_strategy(name: str, params: dict[str, Any] | None = None) -> Strategy:
    if name not in STRATEGY_REGISTRY:
        raise KeyError(
            f"Estrategia {name!r} no registrada. Disponibles: "
            f"{sorted(STRATEGY_REGISTRY)}.\n"
            f"Para agregar una: definí la clase en src/strategy.py con "
            f"@register(\"{name}\")."
        )
    return STRATEGY_REGISTRY[name](params)


# ==========================================================================
#  ▼▼▼  ESTRATEGIA PLACEHOLDER — REEMPLAZAR POR LA TUYA  ▼▼▼
# ==========================================================================

@register("ema_cross_rsi")
class EmaCrossRsi(Strategy):
    """Cruce EMA rápida/lenta con filtro de RSI (y volumen opcional).

    REGLAS
    ------
    Entrada LONG, todas las condiciones a la vez:
        1. Cruce alcista: ema_fast cruza por encima de ema_slow en esta vela.
           Se detecta comparando el signo del spread ahora vs. en la vela
           anterior — no basta con ema_fast > ema_slow, que sería cierto
           durante toda la tendencia y entraría tarde.
        2. RSI dentro de (rsi_long_min, rsi_long_max). El piso confirma
           momentum; el techo evita entrar en sobrecompra ya extendida.
        3. (opcional) volumen > volume_ma * volume_factor.

    Entrada SHORT: la imagen espejo (cruce bajista + RSI en su banda).

    Salida:
        La estrategia no emite EXIT propio. Sale por TP, por SL o por señal
        contraria — todo eso lo maneja el backtester con la sección `risk`.
        Si quisieras una salida propia (ej: cerrar si RSI vuelve a 50),
        agregala en el bloque marcado más abajo.

    HONESTIDAD SOBRE ESTA ESTRATEGIA
    --------------------------------
    Es un PLACEHOLDER para validar que el pipeline funciona end-to-end. Un
    cruce de EMAs desnudo en 5m es de las cosas más estudiadas y arbitradas
    que hay: en mercado lateral genera whipsaw constante y, con costos
    reales, tiende a perder plata. No la operes. Sirve para confirmar que
    los timestamps, el P&L, las métricas y las alertas salen bien; después
    metés tu lógica real acá.
    """

    required_indicators = ("ema_fast", "ema_slow", "rsi")

    def evaluate(self, ctx: Context) -> Signal:
        bar, prev = ctx.bar, ctx.prev

        # --- Guardas ------------------------------------------------------
        # Sin vela previa no hay cruce que detectar.
        if prev is None:
            return NO_SIGNAL

        # Warm-up: si algún indicador todavía es NaN, no operamos.
        if not bar.ready("ema_fast", "ema_slow", "rsi"):
            return NO_SIGNAL
        if not prev.ready("ema_fast", "ema_slow"):
            return NO_SIGNAL

        p = self.params
        rsi_value = bar.get("rsi")

        # --- Detección del cruce -----------------------------------------
        spread_now = bar.get("ema_fast") - bar.get("ema_slow")
        spread_prev = prev.get("ema_fast") - prev.get("ema_slow")

        cross_up = spread_prev <= 0.0 < spread_now
        cross_down = spread_prev >= 0.0 > spread_now

        if not (cross_up or cross_down):
            return NO_SIGNAL

        # --- Filtro de volumen (opcional) --------------------------------
        if p.get("use_volume_filter", False):
            vol_ma = bar.get("volume_ma")
            factor = float(p.get("volume_factor", 1.0))
            if vol_ma is None or bar.volume <= vol_ma * factor:
                return NO_SIGNAL

        # Contexto común que viaja al CSV y a la alerta de Telegram.
        ind_ctx = {
            "ema_fast": round(bar.get("ema_fast"), 2),
            "ema_slow": round(bar.get("ema_slow"), 2),
            "rsi": round(rsi_value, 2),
            "volume": float(bar.volume),
            "close": round(bar.close, 2),
        }

        # --- LONG ---------------------------------------------------------
        if cross_up and p.get("allow_long", True):
            lo = float(p.get("rsi_long_min", 50.0))
            hi = float(p.get("rsi_long_max", 70.0))
            if lo < rsi_value < hi:
                return Signal(
                    type="LONG",
                    reason=f"Cruce alcista EMA + RSI {rsi_value:.1f} en ({lo:g}, {hi:g})",
                    context=ind_ctx,
                )
            return NO_SIGNAL

        # --- SHORT --------------------------------------------------------
        if cross_down and p.get("allow_short", True):
            lo = float(p.get("rsi_short_min", 30.0))
            hi = float(p.get("rsi_short_max", 50.0))
            if lo < rsi_value < hi:
                return Signal(
                    type="SHORT",
                    reason=f"Cruce bajista EMA + RSI {rsi_value:.1f} en ({lo:g}, {hi:g})",
                    context=ind_ctx,
                )
            return NO_SIGNAL

        # --- (opcional) Salida propia de la estrategia ---------------------
        # Si quisieras cerrar por tu cuenta, sería acá. Ejemplo:
        #
        #   if ctx.position == "long" and rsi_value < 45:
        #       return Signal("EXIT", reason="RSI perdió 45")
        #
        # Recordá: TP y SL ya los maneja el backtester, no los repliques acá.

        return NO_SIGNAL


# ==========================================================================
#  Segunda estrategia de ejemplo — muestra que agregar una es trivial
# ==========================================================================

@register("rsi_reversion")
class RsiReversion(Strategy):
    """Reversión a la media con RSI, filtrada por la EMA lenta como tendencia.

    Existe solo como segundo ejemplo del patrón de registro: se cambia
    strategy.name en config.yaml y el sistema entero usa esta en vez de la
    otra, sin tocar una línea del backtester.

    Reglas:
        LONG  : RSI cruza `oversold` de abajo hacia arriba y close > ema_slow
        SHORT : RSI cruza `overbought` de arriba hacia abajo y close < ema_slow

    Params (en config.yaml -> strategy.params):
        oversold: 30, overbought: 70, use_trend_filter: true
    """

    required_indicators = ("rsi", "ema_slow")

    def evaluate(self, ctx: Context) -> Signal:
        bar, prev = ctx.bar, ctx.prev
        if prev is None or not bar.ready("rsi", "ema_slow") or not prev.ready("rsi"):
            return NO_SIGNAL

        p = self.params
        oversold = float(p.get("oversold", 30.0))
        overbought = float(p.get("overbought", 70.0))
        trend_filter = bool(p.get("use_trend_filter", True))

        rsi_now, rsi_prev = bar.get("rsi"), prev.get("rsi")
        ema_slow = bar.get("ema_slow")

        ind_ctx = {
            "rsi": round(rsi_now, 2),
            "rsi_prev": round(rsi_prev, 2),
            "ema_slow": round(ema_slow, 2),
            "close": round(bar.close, 2),
        }

        if rsi_prev <= oversold < rsi_now and p.get("allow_long", True):
            if not trend_filter or bar.close > ema_slow:
                return Signal("LONG", f"RSI recuperó {oversold:g}", ind_ctx)

        if rsi_prev >= overbought > rsi_now and p.get("allow_short", True):
            if not trend_filter or bar.close < ema_slow:
                return Signal("SHORT", f"RSI perdió {overbought:g}", ind_ctx)

        return NO_SIGNAL
