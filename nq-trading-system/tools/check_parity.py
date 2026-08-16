#!/usr/bin/env python3
"""Verifica que la estrategia en Python y la de JavaScript den LO MISMO.

Por qué existe
--------------
El backtester corre en Python y las alertas en vivo corren en un Code node de
JavaScript dentro de n8n. Son dos implementaciones de las mismas reglas, y dos
implementaciones divergen sin avisar: un `>=` donde iba `>`, un RSI suavizado
con EMA en vez de RMA, un redondeo distinto. Cuando eso pasa, el backtest deja
de describir lo que hace el bot y las métricas se vuelven ficción.

Este script descarga velas reales, corre AMBAS implementaciones sobre las
mismas barras, y compara:

  1. los indicadores, vela por vela (tolerancia 1e-9)
  2. las señales, vela por vela (tienen que coincidir exactamente)

Sale con código 1 si divergen. Corrélo después de cada cambio en la lógica.

Uso:
    python tools/check_parity.py
    python tools/check_parity.py --ticker QQQ --interval 15m --lookback 20
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from src.config import load_config  # noqa: E402
from src.data_fetcher import fetch_ohlcv  # noqa: E402
from src.indicators import add_indicators  # noqa: E402
from src.strategy import Bar, Context, get_strategy  # noqa: E402

TOLERANCE = 1e-9
INDICATORS = ["ema_fast", "ema_slow", "rsi", "atr", "volume_ma"]

# Script Node que corre la implementación JS y escupe JSON por stdout.
NODE_RUNNER = r"""
const fs = require('fs');
const path = require('path');
const { addIndicators, evaluate } = require(process.argv[2]);

const payload = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const { candles, indicatorConfig, params } = payload;

const withInd = addIndicators(candles, indicatorConfig);

const results = withInd.map((bar, i) => {
  const prev = i > 0 ? withInd[i - 1] : null;
  const sig = evaluate(bar, prev, params);
  return {
    timestamp: bar.timestamp,
    ema_fast: bar.ema_fast,
    ema_slow: bar.ema_slow,
    rsi: bar.rsi,
    atr: bar.atr,
    volume_ma: bar.volume_ma,
    signal: sig.type,
    reason: sig.reason,
  };
});

process.stdout.write(JSON.stringify(results));
"""


def run_python_side(df: pd.DataFrame, cfg) -> list[dict]:
    """Corre indicadores + estrategia en Python, vela por vela."""
    strategy = get_strategy(
        cfg.require("strategy.name"), cfg.section("strategy").get("params", {})
    )

    out: list[dict] = []
    prev_bar: Bar | None = None

    for ts, row in df.iterrows():
        indicators = {
            k: (None if pd.isna(v) else float(v))
            for k, v in row.items()
            if k not in ("open", "high", "low", "close", "volume")
        }
        bar = Bar(
            timestamp=ts,
            open=float(row["open"]), high=float(row["high"]),
            low=float(row["low"]), close=float(row["close"]),
            volume=float(row["volume"]),
            indicators=indicators,
        )
        # position=None a propósito: comparamos la lógica de ENTRADA pura, que
        # es la que el bot en vivo evalúa (el bot no lleva posición).
        signal = strategy.evaluate(
            Context(bar=bar, prev=prev_bar, position=None, params=strategy.params)
        )
        out.append(
            {
                "timestamp": ts.isoformat(),
                **{k: bar.get(k) for k in INDICATORS},
                "signal": signal.type,
                "reason": signal.reason,
            }
        )
        prev_bar = bar

    return out


def run_js_side(df: pd.DataFrame, cfg) -> list[dict]:
    """Corre la implementación JS con Node y devuelve sus resultados."""
    strategy_js = ROOT / "n8n" / "strategy.js"
    if not strategy_js.exists():
        raise FileNotFoundError(f"No encuentro {strategy_js}")

    candles = [
        {
            "timestamp": ts.isoformat(),
            "open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"]),
            "volume": float(r["volume"]),
        }
        for ts, r in df.iterrows()
    ]

    payload = {
        "candles": candles,
        "indicatorConfig": {
            "emaFast": int(cfg.get("indicators.ema_fast", 9)),
            "emaSlow": int(cfg.get("indicators.ema_slow", 21)),
            "rsiPeriod": int(cfg.get("indicators.rsi_period", 14)),
            "volumeMaPeriod": int(cfg.get("indicators.volume_ma_period", 20)),
            "atrPeriod": int(cfg.get("risk.atr_period", 14)),
        },
        "params": cfg.section("strategy").get("params", {}),
    }

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        runner = tmp_path / "runner.js"
        runner.write_text(NODE_RUNNER, encoding="utf-8")
        data_file = tmp_path / "payload.json"
        data_file.write_text(json.dumps(payload), encoding="utf-8")

        proc = subprocess.run(
            ["node", str(runner), str(strategy_js), str(data_file)],
            capture_output=True, text=True,
        )

    if proc.returncode != 0:
        raise RuntimeError(f"Node falló:\n{proc.stderr}")

    return json.loads(proc.stdout)


def compare(py: list[dict], js: list[dict]) -> int:
    """Compara ambos lados. Devuelve la cantidad de divergencias."""
    if len(py) != len(js):
        print(f"  [FALLA] Distinta cantidad de velas: Python {len(py)}, JS {len(js)}")
        return 1

    indicator_mismatches: list[str] = []
    signal_mismatches: list[str] = []
    max_diff = 0.0
    worst_indicator = ""

    for i, (p, j) in enumerate(zip(py, js)):
        for key in INDICATORS:
            pv, jv = p.get(key), j.get(key)
            if pv is None and jv is None:
                continue
            if (pv is None) != (jv is None):
                indicator_mismatches.append(
                    f"    vela {i} ({p['timestamp']}) {key}: "
                    f"Python={pv} vs JS={jv} (uno es None)"
                )
                continue
            diff = abs(float(pv) - float(jv))
            if diff > max_diff:
                max_diff, worst_indicator = diff, key
            if diff > TOLERANCE:
                indicator_mismatches.append(
                    f"    vela {i} ({p['timestamp']}) {key}: "
                    f"Python={pv:.10f} vs JS={jv:.10f} (Δ={diff:.2e})"
                )

        if p["signal"] != j["signal"]:
            signal_mismatches.append(
                f"    vela {i} ({p['timestamp']}): "
                f"Python={p['signal']} vs JS={j['signal']}"
            )

    py_signals = [p for p in py if p["signal"] != "NONE"]
    js_signals = [j for j in js if j["signal"] != "NONE"]

    print(f"\n  Velas comparadas ............ {len(py)}")
    print(f"  Señales en Python ........... {len(py_signals)}")
    print(f"  Señales en JavaScript ....... {len(js_signals)}")
    print(
        f"  Máxima diferencia numérica .. {max_diff:.2e}"
        + (f"  (en {worst_indicator})" if worst_indicator else "")
    )

    if indicator_mismatches:
        print(f"\n  [FALLA] {len(indicator_mismatches)} indicadores divergen "
              f"(tolerancia {TOLERANCE:.0e}):")
        for line in indicator_mismatches[:15]:
            print(line)
        if len(indicator_mismatches) > 15:
            print(f"    ... y {len(indicator_mismatches) - 15} más")

    if signal_mismatches:
        print(f"\n  [FALLA] {len(signal_mismatches)} señales divergen:")
        for line in signal_mismatches[:15]:
            print(line)
        if len(signal_mismatches) > 15:
            print(f"    ... y {len(signal_mismatches) - 15} más")

    return len(indicator_mismatches) + len(signal_mismatches)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Verifica la paridad Python <-> JavaScript de la estrategia"
    )
    ap.add_argument("--config", default=None)
    ap.add_argument("--ticker", default=None)
    ap.add_argument("--interval", default=None)
    ap.add_argument("--lookback", type=int, default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.ticker:
        cfg.set("data.ticker", args.ticker)
    if args.interval:
        cfg.set("data.interval", args.interval)
    if args.lookback:
        cfg.set("data.lookback_days", args.lookback)

    print("=" * 68)
    print("  PARIDAD  Python (backtester)  <->  JavaScript (n8n)")
    print("=" * 68)
    print(f"  Ticker     : {cfg.get('data.ticker')} @ {cfg.get('data.interval')}")
    print(f"  Estrategia : {cfg.get('strategy.name')}")
    print("-" * 68)

    df = fetch_ohlcv(cfg, strict_limits=False)
    df = add_indicators(df, cfg)

    print("  Corriendo implementación Python...")
    py = run_python_side(df, cfg)

    print("  Corriendo implementación JavaScript (node)...")
    js = run_js_side(df, cfg)

    failures = compare(py, js)

    print("\n" + "=" * 68)
    if failures == 0:
        print("  ✓ PARIDAD OK — backtest y alertas en vivo usan la misma lógica")
        print("=" * 68 + "\n")
        return 0

    print(f"  ✗ PARIDAD ROTA — {failures} divergencias")
    print("    El backtest NO describe lo que hará el bot en vivo.")
    print("    Alineá n8n/strategy.js con src/strategy.py antes de seguir.")
    print("=" * 68 + "\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
