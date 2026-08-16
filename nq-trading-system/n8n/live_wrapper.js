/* ===========================================================================
 *  live_wrapper.js — glue específico de n8n
 * ===========================================================================
 *
 *  Se concatena DESPUÉS de n8n/strategy.js para formar el Code node
 *  "Indicadores + Estrategia". No dupliques lógica de estrategia acá: acá
 *  va solo lo que tiene que ver con vivir dentro de n8n (parsear la
 *  respuesta de la API, ventana rolling, deduplicación, horario de mercado).
 *
 *  Regenerá el workflow con:  python tools/build_n8n_workflow.py
 *
 *  CONTRATO DE SALIDA — siempre devuelve exactamente 1 item con:
 *    { ok, hasSignal, signal, reason, symbol, timestamp, price,
 *      indicators, error, skipReason }
 *  Nunca lanza excepción: si algo falla, devuelve ok:false y el nodo IF
 *  posterior corta antes de Telegram. Un workflow que explota deja de
 *  avisarte, y una alerta falsa es peor que ninguna alerta.
 */

// ---------------------------------------------------------------------------
// Configuración — EDITAR ACÁ (tiene que coincidir con config.yaml)
// ---------------------------------------------------------------------------
const CONFIG = {
  symbol: 'QQQ',              // ETF Nasdaq-100. Ver DATA_PROVIDERS.md sobre NQ.
  interval: '5min',           // formato de Twelve Data
  intervalMinutes: 5,

  indicators: {
    emaFast: 9,
    emaSlow: 21,
    rsiPeriod: 14,
    volumeMaPeriod: 20,
    atrPeriod: 14,
  },

  // Mismos valores que strategy.params en config.yaml.
  params: {
    rsi_long_min: 50.0,
    rsi_long_max: 70.0,
    rsi_short_min: 30.0,
    rsi_short_max: 50.0,
    allow_long: true,
    allow_short: true,
    use_volume_filter: false,
    volume_factor: 1.0,
  },

  // Solo alertar en horario regular de mercado (America/New_York).
  marketHours: {
    enabled: true,
    open: '09:30',
    close: '16:00',
  },

  // Evitar alertar dos veces por la misma vela. Un cron de 5 min y velas de
  // 5 min se desalinean seguido, así que sin esto vas a recibir duplicados.
  deduplicate: true,
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Cuántas velas necesitamos para que los indicadores estén "calientes".
 *  Espeja src/indicators.py::warmup_bars y le suma margen: pedimos de más
 *  para que la EMA converja, no solo para que deje de ser null. */
function requiredBars(cfg) {
  const periods = [
    cfg.indicators.emaFast,
    cfg.indicators.emaSlow,
    cfg.indicators.rsiPeriod,
    cfg.indicators.volumeMaPeriod,
    cfg.indicators.atrPeriod,
  ];
  return Math.max(...periods) + 5 + 80; // warm-up + colchón de convergencia
}

/** Hora actual en America/New_York como {hhmm, weekday}. */
function nowInMarketTz() {
  const now = new Date();
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York',
    hour: '2-digit', minute: '2-digit', hour12: false,
    weekday: 'short',
  }).formatToParts(now);

  const get = (t) => parts.find((p) => p.type === t)?.value;
  return {
    hhmm: `${get('hour')}:${get('minute')}`,
    weekday: get('weekday'),
    iso: now.toISOString(),
  };
}

function isMarketOpen(cfg) {
  if (!cfg.marketHours.enabled) return true;
  const { hhmm, weekday } = nowInMarketTz();
  if (['Sat', 'Sun'].includes(weekday)) return false;
  return hhmm >= cfg.marketHours.open && hhmm < cfg.marketHours.close;
}

/**
 * Normaliza la respuesta de Twelve Data a velas ascendentes.
 *
 * Ojo con dos trampas de esta API:
 *   1. Devuelve HTTP 200 con {status:"error"} en el body cuando la key es
 *      inválida o se acabó la cuota. Si solo mirás el status code, procesás
 *      basura.
 *   2. `values` viene del más NUEVO al más VIEJO. Hay que invertirlo.
 */
function parseTwelveData(response) {
  if (!response || typeof response !== 'object') {
    return { error: 'Respuesta vacía o no-JSON de la API' };
  }
  if (response.status === 'error') {
    return { error: `API: ${response.message || 'error sin mensaje'}` };
  }
  if (!Array.isArray(response.values) || response.values.length === 0) {
    return { error: 'La API no devolvió velas (values vacío)' };
  }

  const candles = response.values
    .map((v) => ({
      timestamp: v.datetime,
      open: parseFloat(v.open),
      high: parseFloat(v.high),
      low: parseFloat(v.low),
      close: parseFloat(v.close),
      volume: v.volume !== undefined && v.volume !== null ? parseFloat(v.volume) : 0,
    }))
    .filter(
      (c) =>
        Number.isFinite(c.open) && Number.isFinite(c.high) &&
        Number.isFinite(c.low) && Number.isFinite(c.close)
    )
    .reverse(); // Twelve Data devuelve desc -> lo pasamos a ascendente

  if (candles.length === 0) return { error: 'Ninguna vela pasó la validación numérica' };
  return { candles };
}

/**
 * Descarta la última vela si TODAVÍA SE ESTÁ FORMANDO.
 *
 * Este es el bug clásico del alerting en vivo: la vela en curso cambia de
 * precio hasta que cierra, así que una señal calculada sobre ella "repinta"
 * — aparece y desaparece. El backtest solo ve velas cerradas; en vivo hay
 * que forzar lo mismo o los dos dejan de ser comparables.
 */
function dropFormingCandle(candles, intervalMinutes) {
  if (candles.length < 2) return candles;

  const last = candles[candles.length - 1];
  // Twelve Data devuelve "YYYY-MM-DD HH:mm:ss" en la timezone del exchange.
  // Comparamos contra el reloj de mercado, no contra UTC.
  const openMs = Date.parse(last.timestamp.replace(' ', 'T'));
  if (Number.isNaN(openMs)) return candles; // formato raro: mejor no tocar

  const nowMarket = new Date(
    new Date().toLocaleString('en-US', { timeZone: 'America/New_York' })
  ).getTime();

  const closesAt = openMs + intervalMinutes * 60 * 1000;
  if (nowMarket < closesAt) return candles.slice(0, -1);
  return candles;
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

function buildResult(extra) {
  return [
    {
      json: {
        ok: false,
        hasSignal: false,
        signal: 'NONE',
        symbol: CONFIG.symbol,
        interval: CONFIG.interval,
        checkedAt: new Date().toISOString(),
        ...extra,
      },
    },
  ];
}

// n8n static data: persiste entre ejecuciones del MISMO workflow.
// Lo usamos SOLO para deduplicar, no para acumular historial: el historial
// lo pedimos entero a la API en cada corrida (ventana rolling real), así el
// workflow es stateless respecto de los precios y no se corrompe si se
// pierde una ejecución.
const staticData = $getWorkflowStaticData('global');

try {
  // --- 1. ¿Estamos en horario de mercado? --------------------------------
  if (!isMarketOpen(CONFIG)) {
    return buildResult({
      ok: true,
      skipReason: 'Fuera del horario de mercado',
      marketTime: nowInMarketTz(),
    });
  }

  // --- 2. Traer y validar la respuesta de la API -------------------------
  const items = $input.all();
  if (!items.length) {
    return buildResult({ error: 'El nodo HTTP no devolvió ningún item' });
  }

  const response = items[0].json;

  // Si el HTTP Request falló, n8n (con onError: continueRegularOutput) pasa
  // un item con la propiedad `error`. No es motivo para romper el workflow.
  if (response.error) {
    return buildResult({
      error: `HTTP Request falló: ${JSON.stringify(response.error).slice(0, 300)}`,
    });
  }

  const parsed = parseTwelveData(response);
  if (parsed.error) return buildResult({ error: parsed.error });

  // --- 3. Ventana rolling ------------------------------------------------
  // Pedimos a la API exactamente las velas que necesitamos y nos quedamos con
  // las últimas N. Lo viejo se descarta solo, porque nunca lo guardamos.
  const needed = requiredBars(CONFIG);
  let candles = dropFormingCandle(parsed.candles, CONFIG.intervalMinutes);
  candles = candles.slice(-needed);

  if (candles.length < needed * 0.5) {
    return buildResult({
      error:
        `Velas insuficientes: llegaron ${candles.length}, necesito ~${needed}. ` +
        `Subí outputsize en el nodo HTTP Request.`,
      barsReceived: candles.length,
    });
  }

  // --- 4. Indicadores + estrategia (código verificado por check_parity) ---
  const withInd = addIndicators(candles, CONFIG.indicators);
  const bar = withInd[withInd.length - 1];
  const prev = withInd[withInd.length - 2] || null;

  const signal = evaluate(bar, prev, CONFIG.params);

  // --- 5. Deduplicación --------------------------------------------------
  if (CONFIG.deduplicate && signal.type !== 'NONE') {
    const key = `${CONFIG.symbol}|${bar.timestamp}`;
    if (staticData.lastAlertKey === key) {
      return buildResult({
        ok: true,
        skipReason: `Ya alerté sobre esta vela (${bar.timestamp})`,
        signal: signal.type,
        timestamp: bar.timestamp,
      });
    }
    staticData.lastAlertKey = key;
  }

  // --- 6. Salida ---------------------------------------------------------
  const round2 = (x) => (x === null || x === undefined ? null : Math.round(x * 100) / 100);

  return [
    {
      json: {
        ok: true,
        hasSignal: signal.type === 'LONG' || signal.type === 'SHORT',
        signal: signal.type,
        reason: signal.reason,
        symbol: CONFIG.symbol,
        interval: CONFIG.interval,
        timestamp: bar.timestamp,
        price: round2(bar.close),
        indicators: {
          ema_fast: round2(bar.ema_fast),
          ema_slow: round2(bar.ema_slow),
          rsi: round2(bar.rsi),
          atr: round2(bar.atr),
          volume: bar.volume,
          volume_ma: round2(bar.volume_ma),
        },
        barsUsed: candles.length,
        checkedAt: new Date().toISOString(),
      },
    },
  ];
} catch (err) {
  // Última red de contención: cualquier excepción inesperada sale como
  // ok:false y el IF corta. El workflow nunca queda en estado "error".
  return buildResult({
    error: `Excepción inesperada: ${err.message}`,
    stack: String(err.stack || '').slice(0, 500),
  });
}
