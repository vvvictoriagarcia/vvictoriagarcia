"""Indicadores técnicos en pandas puro.

¿Por qué no pandas-ta / ta?
--------------------------
Porque el workflow de n8n tiene que calcular EXACTAMENTE los mismos valores
dentro de un Code node de JavaScript. Con una implementación propia podemos
portarla 1:1 a JS (n8n/strategy.js) y verificar la paridad numéricamente
(tools/check_parity.py). Con una librería de terceros eso es imposible de
garantizar, y un backtest que no coincide con las alertas en vivo es peor
que no tener backtest.

Las fórmulas siguen la convención de la librería `ta`:
  - EMA  : suavizado exponencial con adjust=False (recursivo, seed = 1er valor)
  - RSI  : Wilder, con suavizado RMA (alpha = 1/n), no SMA
  - ATR  : True Range suavizado con RMA

Todas las funciones son causales: el valor en la vela i usa solo datos de
velas <= i. La única excepción está marcada explícitamente (`pivots`, que se
usa para dibujar S/R y NO debe usarse para generar señales sin shift).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import Config


# --------------------------------------------------------------------------
# Medias
# --------------------------------------------------------------------------

def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average.

    adjust=False da la forma recursiva estándar de trading:
        EMA_t = alpha * price_t + (1 - alpha) * EMA_{t-1},  alpha = 2/(n+1)
    Es la que usan TradingView, `ta` y pandas-ta, y la que portamos a JS.

    Detalle de semilla, importante para la paridad con JS: pandas arranca la
    recursión en el PRIMER valor de la serie (EMA_0 = x_0), no en la SMA de
    las primeras n barras, y después enmascara como NaN los primeros n-1
    resultados. n8n/strategy.js replica exactamente esto.
    """
    if period < 1:
        raise ValueError(f"El período de EMA debe ser >= 1, recibí {period}")
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple Moving Average."""
    if period < 1:
        raise ValueError(f"El período de SMA debe ser >= 1, recibí {period}")
    return series.rolling(window=period, min_periods=period).mean()


def rma(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing (RMA / SMMA): alpha = 1/n.

    Es el suavizado que Wilder define para RSI y ATR. Ojo: NO es lo mismo que
    una EMA de período n (esa usa alpha = 2/(n+1)). Confundirlas da un RSI
    sistemáticamente distinto al de TradingView.
    """
    return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


# --------------------------------------------------------------------------
# Osciladores
# --------------------------------------------------------------------------

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index (Wilder).

        delta = close.diff()
        gain  = max(delta, 0),  loss = max(-delta, 0)
        RS    = RMA(gain, n) / RMA(loss, n)
        RSI   = 100 - 100 / (1 + RS)

    Cuando RMA(loss) == 0 el RS diverge; por convención RSI = 100.
    """
    if period < 2:
        raise ValueError(f"El período de RSI debe ser >= 2, recibí {period}")

    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    avg_gain = rma(gain, period)
    avg_loss = rma(loss, period)

    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))

    # avg_loss == 0 con avg_gain > 0  -> RSI 100 (racha alcista pura)
    # avg_loss == 0 y avg_gain == 0   -> mercado plano, RSI 50 por convención
    flat = (avg_loss == 0.0) & (avg_gain == 0.0)
    only_gains = (avg_loss == 0.0) & (avg_gain > 0.0)
    out = out.mask(only_gains, 100.0)
    out = out.mask(flat, 50.0)
    return out


# --------------------------------------------------------------------------
# Volatilidad
# --------------------------------------------------------------------------

def true_range(df: pd.DataFrame) -> pd.Series:
    """True Range = max(H-L, |H-C_prev|, |L-C_prev|)."""
    prev_close = df["close"].shift(1)
    ranges = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range (Wilder)."""
    return rma(true_range(df), period)


# --------------------------------------------------------------------------
# Estructura de precio: soportes y resistencias
# --------------------------------------------------------------------------

def pivots(df: pd.DataFrame, lookback: int = 10) -> tuple[pd.Series, pd.Series]:
    """Detecta pivots altos y bajos (fractales).

    Un pivot alto en la vela i cumple: high[i] es el máximo de la ventana
    [i-lookback, i+lookback].

    ¡ATENCIÓN — MIRA AL FUTURO!
    Un pivot solo se confirma `lookback` velas DESPUÉS de ocurrir. Estas
    series sirven para DIBUJAR niveles en el gráfico, no para generar señales.
    Si querés usarlas en una estrategia, usá `support_resistance()`, que ya
    aplica el shift necesario.

    Devuelve (pivot_high, pivot_low): series con el precio del pivot donde
    lo hay, y NaN en el resto.
    """
    if lookback < 1:
        raise ValueError(f"sr_lookback debe ser >= 1, recibí {lookback}")

    window = 2 * lookback + 1
    high_roll = df["high"].rolling(window=window, center=True, min_periods=window).max()
    low_roll = df["low"].rolling(window=window, center=True, min_periods=window).min()

    is_ph = df["high"] >= high_roll
    is_pl = df["low"] <= low_roll

    return df["high"].where(is_ph), df["low"].where(is_pl)


def support_resistance(df: pd.DataFrame, lookback: int = 10) -> pd.DataFrame:
    """Niveles de soporte/resistencia vigentes, sin mirar al futuro.

    Toma los pivots y los desplaza `lookback` velas hacia adelante — el
    tiempo que tarda el pivot en confirmarse — y después hace forward-fill.
    Así, en la vela i, `resistance` es el último pivot alto YA CONFIRMADO en
    ese momento, que es lo que habrías podido ver operando en vivo.

    Columnas: resistance, support, dist_to_resistance, dist_to_support
    (las distancias, en puntos: positivas si el precio está por debajo de la
    resistencia / por encima del soporte).
    """
    ph, pl = pivots(df, lookback)

    resistance = ph.shift(lookback).ffill()
    support = pl.shift(lookback).ffill()

    return pd.DataFrame(
        {
            "resistance": resistance,
            "support": support,
            "dist_to_resistance": resistance - df["close"],
            "dist_to_support": df["close"] - support,
        },
        index=df.index,
    )


# --------------------------------------------------------------------------
# Orquestador
# --------------------------------------------------------------------------

def add_indicators(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Agrega al DataFrame todos los indicadores configurados.

    Columnas que agrega:
        ema_fast, ema_slow, ema_spread
        rsi
        atr
        volume_ma, volume_ratio
        resistance, support, dist_to_resistance, dist_to_support

    Los nombres son genéricos a propósito (`ema_fast`, no `ema_9`), para que
    cambiar los períodos en config.yaml no obligue a tocar strategy.py.
    """
    if df.empty:
        return df

    out = df.copy()

    ema_fast_p = int(cfg.get("indicators.ema_fast", 9))
    ema_slow_p = int(cfg.get("indicators.ema_slow", 21))
    rsi_p = int(cfg.get("indicators.rsi_period", 14))
    vol_p = int(cfg.get("indicators.volume_ma_period", 20))
    sr_p = int(cfg.get("indicators.sr_lookback", 10))
    atr_p = int(cfg.get("risk.atr_period", 14))

    out["ema_fast"] = ema(out["close"], ema_fast_p)
    out["ema_slow"] = ema(out["close"], ema_slow_p)
    out["ema_spread"] = out["ema_fast"] - out["ema_slow"]

    out["rsi"] = rsi(out["close"], rsi_p)
    out["atr"] = atr(out, atr_p)

    out["volume_ma"] = sma(out["volume"], vol_p)
    out["volume_ratio"] = out["volume"] / out["volume_ma"].replace(0.0, np.nan)

    sr = support_resistance(out, sr_p)
    for col in sr.columns:
        out[col] = sr[col]

    return out


def warmup_bars(cfg: Config) -> int:
    """Cuántas velas iniciales hay que descartar antes de operar.

    Es el máximo de los períodos configurados más un colchón. Antes de eso
    los indicadores están en NaN o todavía dominados por su seed, y las
    señales que salgan de ahí son ruido.

    El workflow de n8n usa este mismo número para dimensionar su ventana
    rolling: si le pasás menos velas que esto, los indicadores no coinciden
    con el backtest.
    """
    periods = [
        int(cfg.get("indicators.ema_fast", 9)),
        int(cfg.get("indicators.ema_slow", 21)),
        int(cfg.get("indicators.rsi_period", 14)),
        int(cfg.get("indicators.volume_ma_period", 20)),
        int(cfg.get("risk.atr_period", 14)),
        2 * int(cfg.get("indicators.sr_lookback", 10)) + 1,
    ]
    return max(periods) + 5
