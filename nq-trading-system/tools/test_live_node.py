#!/usr/bin/env python3
"""Prueba el Code node del workflow simulando el runtime de n8n.

Verifica dos cosas que no se pueden dar por sentadas:

  A. Que sobre las MISMAS velas, el Code node emite la misma señal que el
     backtester en Python.
  B. Que el manejo de errores del requisito 6 realmente funciona: si la API
     falla, devuelve un body de error, o manda datos incompletos, el nodo
     tiene que salir con ok:false y SIN señal — nunca una alerta falsa, nunca
     una excepción que corte el workflow.

Uso:
    python tools/test_live_node.py
"""

from __future__ import annotations

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

# Simula el entorno de n8n: $input, $getWorkflowStaticData, y el `return`
# de nivel superior del Code node (n8n envuelve el código en una función).
HARNESS = r"""
const fs = require('fs');

const scenarios = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
let codeSource = fs.readFileSync(process.argv[2], 'utf8');

// n8n permite `return` en el nivel superior del Code node porque lo envuelve
// en una función. Para correrlo con Node hacemos lo mismo.
const makeRunner = (src) =>
  new Function('$input', '$getWorkflowStaticData', `${src}`);

const results = [];
// staticData compartido entre escenarios: así se puede probar la dedup.
const staticStore = {};

for (const sc of scenarios) {
  let src = codeSource;
  if (sc.disableMarketHours) {
    src = src.replace('enabled: true,', 'enabled: false,');
  }
  const $input = { all: () => sc.items };
  const $getWorkflowStaticData = () => staticStore;

  try {
    const out = makeRunner(src)($input, $getWorkflowStaticData);
    results.push({ name: sc.name, threw: false, output: out[0].json });
  } catch (err) {
    results.push({ name: sc.name, threw: true, error: err.message });
  }
}

process.stdout.write(JSON.stringify(results, null, 2));
"""


def to_twelvedata(df: pd.DataFrame, symbol: str, interval: str) -> dict:
    """Formatea un DataFrame como la respuesta de Twelve Data (orden desc)."""
    values = [
        {
            "datetime": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "open": f"{r['open']:.5f}",
            "high": f"{r['high']:.5f}",
            "low": f"{r['low']:.5f}",
            "close": f"{r['close']:.5f}",
            "volume": str(int(r["volume"])),
        }
        for ts, r in df.iterrows()
    ][::-1]  # desc, como la API real

    return {
        "meta": {
            "symbol": symbol, "interval": interval,
            "currency": "USD", "exchange_timezone": "America/New_York",
            "exchange": "NASDAQ", "type": "Common Stock",
        },
        "values": values,
        "status": "ok",
    }


def python_signal_on(df: pd.DataFrame, cfg) -> dict:
    """Señal que produce el Python sobre la última vela de df."""
    ind = add_indicators(df, cfg)
    strategy = get_strategy(
        cfg.require("strategy.name"), cfg.section("strategy").get("params", {})
    )

    def mk(ts, row) -> Bar:
        return Bar(
            timestamp=ts,
            open=float(row["open"]), high=float(row["high"]),
            low=float(row["low"]), close=float(row["close"]),
            volume=float(row["volume"]),
            indicators={
                k: (None if pd.isna(v) else float(v))
                for k, v in row.items()
                if k not in ("open", "high", "low", "close", "volume")
            },
        )

    bar = mk(ind.index[-1], ind.iloc[-1])
    prev = mk(ind.index[-2], ind.iloc[-2])
    sig = strategy.evaluate(
        Context(bar=bar, prev=prev, position=None, params=strategy.params)
    )
    return {"type": sig.type, "reason": sig.reason, "rsi": bar.get("rsi")}


def main() -> int:
    cfg = load_config()
    workflow_path = ROOT / "n8n" / "workflow_twelvedata_telegram.json"
    if not workflow_path.exists():
        print("Primero generá el workflow: python tools/build_n8n_workflow.py")
        return 1

    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    code_node = next(
        n for n in workflow["nodes"] if n["type"] == "n8n-nodes-base.code"
    )
    code = code_node["parameters"]["jsCode"]

    print("=" * 68)
    print("  TEST DEL CODE NODE DE n8n (simulando su runtime)")
    print("=" * 68)

    df = fetch_ohlcv(cfg, strict_limits=False)
    ind = add_indicators(df, cfg)

    # Buscamos una ventana que TERMINE en una señal real, para probar el
    # camino feliz con datos de verdad y no con un caso inventado.
    strategy = get_strategy(
        cfg.require("strategy.name"), cfg.section("strategy").get("params", {})
    )
    window = 120
    signal_end = None
    for i in range(len(ind) - 1, window, -1):
        sub = ind.iloc[i - window + 1: i + 1]
        try:
            if python_signal_on(df.loc[sub.index], cfg)["type"] in ("LONG", "SHORT"):
                signal_end = i
                break
        except Exception:
            continue

    if signal_end is None:
        print("  No encontré una ventana que termine en señal; uso la última.")
        signal_end = len(ind) - 1

    win_df = df.iloc[signal_end - window + 1: signal_end + 1]
    expected = python_signal_on(win_df, cfg)

    symbol = "QQQ"
    interval = "5min"
    good_response = to_twelvedata(win_df, symbol, interval)

    scenarios = [
        {
            "name": "1. Camino feliz — velas válidas, se espera señal",
            "disableMarketHours": True,
            "items": [{"json": good_response}],
        },
        {
            "name": "2. Dedup — misma vela otra vez, no debe re-alertar",
            "disableMarketHours": True,
            "items": [{"json": good_response}],
        },
        {
            "name": "3. La API devuelve 200 con body de error (key inválida)",
            "disableMarketHours": True,
            "items": [{"json": {
                "code": 401, "message": "Invalid API key", "status": "error",
            }}],
        },
        {
            "name": "4. El nodo HTTP falló (onError pasa un item con `error`)",
            "disableMarketHours": True,
            "items": [{"json": {"error": {"message": "ETIMEDOUT", "code": 500}}}],
        },
        {
            "name": "5. Respuesta sin velas (values vacío)",
            "disableMarketHours": True,
            "items": [{"json": {"status": "ok", "values": []}}],
        },
        {
            "name": "6. Velas insuficientes para los indicadores",
            "disableMarketHours": True,
            "items": [{"json": to_twelvedata(win_df.tail(10), symbol, interval)}],
        },
        {
            "name": "7. Sin items del nodo anterior",
            "disableMarketHours": True,
            "items": [],
        },
        {
            "name": "8. Compuerta de horario de mercado activa",
            "disableMarketHours": False,
            "items": [{"json": good_response}],
        },
    ]

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "harness.js").write_text(HARNESS, encoding="utf-8")
        (tmp_path / "code.js").write_text(code, encoding="utf-8")
        (tmp_path / "scenarios.json").write_text(
            json.dumps(scenarios), encoding="utf-8"
        )
        proc = subprocess.run(
            [
                "node", str(tmp_path / "harness.js"),
                str(tmp_path / "code.js"), str(tmp_path / "scenarios.json"),
            ],
            capture_output=True, text=True,
        )

    if proc.returncode != 0:
        print(f"  El harness de Node falló:\n{proc.stderr}")
        return 1

    results = json.loads(proc.stdout)

    print(f"\n  Ventana de prueba : {len(win_df)} velas, "
          f"última = {win_df.index[-1]:%Y-%m-%d %H:%M}")
    print(f"  Python espera     : {expected['type']} "
          f"(RSI {expected['rsi']:.2f})")
    print("-" * 68)

    failures = 0
    for res in results:
        name = res["name"]

        if res["threw"]:
            print(f"  [FALLA] {name}\n          lanzó excepción: {res['error']}")
            failures += 1
            continue

        out = res["output"]
        num = name.split(".")[0]

        if num == "1":
            ok = out["signal"] == expected["type"] and out["hasSignal"] is True
            detail = f"signal={out['signal']}, hasSignal={out['hasSignal']}"
        elif num == "2":
            ok = out["hasSignal"] is False and "skipReason" in out
            detail = f"hasSignal={out['hasSignal']}, skip={out.get('skipReason')}"
        elif num in {"3", "4", "5", "6", "7"}:
            ok = out["ok"] is False and out["hasSignal"] is False
            detail = f"ok={out['ok']}, error={str(out.get('error'))[:60]}"
        elif num == "8":
            # El test corre a cualquier hora: si el mercado está cerrado la
            # compuerta corta; si está abierto, deja pasar. Ambas son válidas;
            # lo que NO puede pasar es una excepción.
            ok = out["hasSignal"] in (True, False)
            detail = (
                f"skip={out.get('skipReason')}"
                if out.get("skipReason") else f"mercado abierto, signal={out['signal']}"
            )
        else:
            ok, detail = False, "escenario desconocido"

        status = " OK " if ok else "FALLA"
        print(f"  [{status}] {name}")
        print(f"          {detail}")
        if not ok:
            failures += 1

    print("\n" + "=" * 68)
    if failures == 0:
        print("  ✓ El Code node se comporta como debe en los 8 escenarios")
        print("    (señal correcta, dedup, y ningún error se vuelve alerta falsa)")
        print("=" * 68 + "\n")
        return 0
    print(f"  ✗ {failures} escenarios fallaron")
    print("=" * 68 + "\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
