/* ===========================================================================
 *  strategy.js — PORT EXACTO de src/indicators.py + src/strategy.py
 * ===========================================================================
 *
 *  Este archivo es el contenido del Code node "Indicadores + Estrategia" del
 *  workflow de n8n. Se mantiene también como archivo suelto para poder
 *  testearlo con Node y verificar la paridad contra Python.
 *
 *  ┌──────────────────────────────────────────────────────────────────────┐
 *  │  SI CAMBIÁS LA ESTRATEGIA EN PYTHON, CAMBIALA TAMBIÉN ACÁ.           │
 *  │  Después corré:  python tools/check_parity.py                        │
 *  │  Compara ambas implementaciones vela por vela sobre datos reales de  │
 *  │  NQ=F y falla si divergen. Un backtest que no coincide con las       │
 *  │  alertas en vivo es peor que no tener backtest.                      │
 *  │                                                                       │
 *  │  ¿No querés mantener dos implementaciones? Usá src/signal_service.py │
 *  │  (Opción B en n8n/README.md): n8n llama por HTTP a la función Python │
 *  │  y hay una sola fuente de verdad.                                    │
 *  └──────────────────────────────────────────────────────────────────────┘
 *
 *  PARIDAD NUMÉRICA — los detalles que importan
 *  --------------------------------------------
 *  pandas tiene semánticas específicas que hay que replicar al pie de la
 *  letra o los números divergen en el tercer decimal y las señales cambian:
 *
 *   1. EMA (ewm adjust=False): la recursión arranca en el PRIMER valor de la
 *      serie (ema[0] = x[0]), NO en la SMA de las primeras n barras. Los
 *      primeros n-1 resultados se enmascaran como null.
 *   2. RMA (ewm alpha=1/n): igual, pero arranca en el primer valor NO NULO.
 *      Como delta[0] es null (viene de un diff), la recursión del RSI
 *      empieza en el índice 1 y el primer RSI válido cae en el índice n.
 *   3. RSI usa RMA (alpha = 1/n), no EMA (alpha = 2/(n+1)). Confundirlas da
 *      un RSI sistemáticamente distinto al de TradingView.
 */

// ---------------------------------------------------------------------------
// Indicadores
// ---------------------------------------------------------------------------

/**
 * EMA — equivale a pandas: series.ewm(span=period, adjust=False,
 * min_periods=period).mean()
 * @param {number[]} values
 * @param {number} period
 * @returns {(number|null)[]}
 */
function ema(values, period) {
  if (period < 1) throw new Error(`El período de EMA debe ser >= 1, recibí ${period}`);
  const alpha = 2 / (period + 1);
  const out = new Array(values.length).fill(null);
  let prev = null;

  for (let i = 0; i < values.length; i++) {
    const v = values[i];
    if (v === null || v === undefined || Number.isNaN(v)) continue;
    // Semilla en el primer valor válido; después, recursión.
    prev = prev === null ? v : alpha * v + (1 - alpha) * prev;
    // min_periods: recién a partir de la barra period-1 hay valor.
    if (i >= period - 1) out[i] = prev;
  }
  return out;
}

/**
 * SMA — equivale a pandas: series.rolling(period, min_periods=period).mean()
 */
function sma(values, period) {
  if (period < 1) throw new Error(`El período de SMA debe ser >= 1, recibí ${period}`);
  const out = new Array(values.length).fill(null);
  let sum = 0;
  for (let i = 0; i < values.length; i++) {
    sum += values[i];
    if (i >= period) sum -= values[i - period];
    if (i >= period - 1) out[i] = sum / period;
  }
  return out;
}

/**
 * RMA / suavizado de Wilder — pandas: ewm(alpha=1/period, adjust=False,
 * min_periods=period).mean()
 *
 * Los nulls se SALTEAN (no rompen la recursión) y no cuentan para
 * min_periods, exactamente como hace pandas.
 */
function rma(values, period) {
  const alpha = 1 / period;
  const out = new Array(values.length).fill(null);
  let prev = null;
  let seen = 0;

  for (let i = 0; i < values.length; i++) {
    const v = values[i];
    if (v === null || v === undefined || Number.isNaN(v)) continue;
    seen += 1;
    prev = prev === null ? v : alpha * v + (1 - alpha) * prev;
    if (seen >= period) out[i] = prev;
  }
  return out;
}

/**
 * RSI de Wilder. Port exacto de src/indicators.py::rsi
 */
function rsi(values, period = 14) {
  if (period < 2) throw new Error(`El período de RSI debe ser >= 2, recibí ${period}`);

  // delta = close.diff()  -> delta[0] es null
  const gain = new Array(values.length).fill(null);
  const loss = new Array(values.length).fill(null);
  for (let i = 1; i < values.length; i++) {
    const d = values[i] - values[i - 1];
    gain[i] = d > 0 ? d : 0;
    loss[i] = d < 0 ? -d : 0;
  }

  const avgGain = rma(gain, period);
  const avgLoss = rma(loss, period);

  const out = new Array(values.length).fill(null);
  for (let i = 0; i < values.length; i++) {
    const g = avgGain[i];
    const l = avgLoss[i];
    if (g === null || l === null) continue;

    if (l === 0 && g === 0) {
      out[i] = 50;          // mercado plano
    } else if (l === 0) {
      out[i] = 100;         // racha alcista pura
    } else {
      out[i] = 100 - 100 / (1 + g / l);
    }
  }
  return out;
}

/**
 * ATR de Wilder. Port exacto de src/indicators.py::atr
 */
function atr(highs, lows, closes, period = 14) {
  const tr = new Array(closes.length).fill(null);
  for (let i = 0; i < closes.length; i++) {
    if (i === 0) {
      tr[i] = highs[i] - lows[i];
      continue;
    }
    const pc = closes[i - 1];
    tr[i] = Math.max(highs[i] - lows[i], Math.abs(highs[i] - pc), Math.abs(lows[i] - pc));
  }
  return rma(tr, period);
}

/**
 * Calcula todos los indicadores sobre un array de velas.
 * Devuelve el mismo array con los indicadores agregados a cada vela.
 *
 * @param {Array<{timestamp:string, open:number, high:number, low:number,
 *                close:number, volume:number}>} candles  (ascendente: la
 *                última posición es la vela más reciente)
 * @param {object} cfg  { emaFast, emaSlow, rsiPeriod, volumeMaPeriod, atrPeriod }
 */
function addIndicators(candles, cfg) {
  const closes = candles.map((c) => c.close);
  const highs = candles.map((c) => c.high);
  const lows = candles.map((c) => c.low);
  const volumes = candles.map((c) => c.volume);

  const emaFast = ema(closes, cfg.emaFast);
  const emaSlow = ema(closes, cfg.emaSlow);
  const rsiArr = rsi(closes, cfg.rsiPeriod);
  const atrArr = atr(highs, lows, closes, cfg.atrPeriod);
  const volMa = sma(volumes, cfg.volumeMaPeriod);

  return candles.map((c, i) => ({
    ...c,
    ema_fast: emaFast[i],
    ema_slow: emaSlow[i],
    ema_spread: emaFast[i] !== null && emaSlow[i] !== null ? emaFast[i] - emaSlow[i] : null,
    rsi: rsiArr[i],
    atr: atrArr[i],
    volume_ma: volMa[i],
    volume_ratio: volMa[i] ? c.volume / volMa[i] : null,
  }));
}

// ---------------------------------------------------------------------------
// Estrategia — PORT DE src/strategy.py::EmaCrossRsi
// ---------------------------------------------------------------------------

const NO_SIGNAL = { type: 'NONE', reason: '', context: {} };

/**
 * Evalúa la estrategia sobre la ÚLTIMA vela cerrada.
 *
 * @param {object} bar   vela actual con indicadores
 * @param {object} prev  vela anterior con indicadores
 * @param {object} params  strategy.params de config.yaml
 * @returns {{type:'LONG'|'SHORT'|'NONE', reason:string, context:object}}
 */
function evaluate(bar, prev, params) {
  // --- Guardas (mismo orden que en Python) ---
  if (!prev) return NO_SIGNAL;
  if (bar.ema_fast === null || bar.ema_slow === null || bar.rsi === null) return NO_SIGNAL;
  if (prev.ema_fast === null || prev.ema_slow === null) return NO_SIGNAL;

  const rsiValue = bar.rsi;

  // --- Detección del cruce ---
  const spreadNow = bar.ema_fast - bar.ema_slow;
  const spreadPrev = prev.ema_fast - prev.ema_slow;

  const crossUp = spreadPrev <= 0 && spreadNow > 0;
  const crossDown = spreadPrev >= 0 && spreadNow < 0;

  if (!crossUp && !crossDown) return NO_SIGNAL;

  // --- Filtro de volumen (opcional) ---
  if (params.use_volume_filter) {
    const volMa = bar.volume_ma;
    const factor = params.volume_factor ?? 1.0;
    if (volMa === null || bar.volume <= volMa * factor) return NO_SIGNAL;
  }

  const round2 = (x) => Math.round(x * 100) / 100;
  const indCtx = {
    ema_fast: round2(bar.ema_fast),
    ema_slow: round2(bar.ema_slow),
    rsi: round2(rsiValue),
    volume: bar.volume,
    close: round2(bar.close),
  };

  // --- LONG ---
  if (crossUp && (params.allow_long ?? true)) {
    const lo = params.rsi_long_min ?? 50.0;
    const hi = params.rsi_long_max ?? 70.0;
    if (rsiValue > lo && rsiValue < hi) {
      return {
        type: 'LONG',
        reason: `Cruce alcista EMA + RSI ${rsiValue.toFixed(1)} en (${lo}, ${hi})`,
        context: indCtx,
      };
    }
    return NO_SIGNAL;
  }

  // --- SHORT ---
  if (crossDown && (params.allow_short ?? true)) {
    const lo = params.rsi_short_min ?? 30.0;
    const hi = params.rsi_short_max ?? 50.0;
    if (rsiValue > lo && rsiValue < hi) {
      return {
        type: 'SHORT',
        reason: `Cruce bajista EMA + RSI ${rsiValue.toFixed(1)} en (${lo}, ${hi})`,
        context: indCtx,
      };
    }
    return NO_SIGNAL;
  }

  return NO_SIGNAL;
}

// ---------------------------------------------------------------------------
// Export — solo para poder testear con Node (tools/check_parity.py).
// n8n ignora esto: en el Code node el archivo termina en el bloque de abajo.
// ---------------------------------------------------------------------------
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { ema, sma, rma, rsi, atr, addIndicators, evaluate };
}
