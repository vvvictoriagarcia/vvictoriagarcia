#!/usr/bin/env python3
"""Genera el workflow de n8n listo para importar.

Por qué se genera en vez de escribirse a mano
---------------------------------------------
El Code node del workflow necesita el código de la estrategia embebido como
un string dentro del JSON. Si ese string se mantiene a mano, se desincroniza
de n8n/strategy.js — que es justamente el archivo que tools/check_parity.py
verifica contra Python. Entonces estarías testeando un archivo y desplegando
otro.

Generándolo, la cadena queda cerrada:

    src/strategy.py  ──(check_parity.py)──>  n8n/strategy.js
                                                   │
                                        (build_n8n_workflow.py)
                                                   ▼
                                      workflow_*.json (Code node)

Uso:
    python tools/build_n8n_workflow.py
    python tools/build_n8n_workflow.py --symbol SPY --interval 15min
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
N8N_DIR = ROOT / "n8n"

# --- Posiciones en el canvas de n8n (x, y) --------------------------------
POS = {
    "trigger": [-200, 300],
    "http": [40, 300],
    "code": [280, 300],
    "if": [520, 300],
    "telegram": [780, 200],
    "noop": [780, 400],
}


def strip_module_export(js: str) -> str:
    """Saca el bloque module.exports: n8n no es CommonJS y tira error."""
    return re.sub(
        r"\n// -+\n// Export.*?\n\}\n\s*$",
        "\n",
        js,
        flags=re.DOTALL,
    ).rstrip() + "\n"


def build_code(symbol: str, interval: str, interval_minutes: int) -> str:
    """Concatena strategy.js (verificado) + live_wrapper.js (glue de n8n)."""
    strategy_js = (N8N_DIR / "strategy.js").read_text(encoding="utf-8")
    wrapper_js = (N8N_DIR / "live_wrapper.js").read_text(encoding="utf-8")

    strategy_js = strip_module_export(strategy_js)

    # Aplicar overrides de símbolo/intervalo sobre el CONFIG del wrapper.
    wrapper_js = re.sub(
        r"symbol: '[^']*',", f"symbol: '{symbol}',", wrapper_js, count=1
    )
    wrapper_js = re.sub(
        r"interval: '[^']*',", f"interval: '{interval}',", wrapper_js, count=1
    )
    wrapper_js = re.sub(
        r"intervalMinutes: \d+,",
        f"intervalMinutes: {interval_minutes},",
        wrapper_js,
        count=1,
    )

    header = (
        "/* ═══════════════════════════════════════════════════════════════════\n"
        " *  GENERADO POR tools/build_n8n_workflow.py — NO EDITAR ACÁ\n"
        " *\n"
        " *  Editá n8n/strategy.js (lógica) o n8n/live_wrapper.js (config y\n"
        " *  glue de n8n) y volvé a correr el generador. Si editás este nodo\n"
        " *  directamente en la UI de n8n, el próximo build te lo pisa y además\n"
        " *  perdés la garantía de paridad con el backtester.\n"
        " *\n"
        " *  Verificá la paridad con:  python tools/check_parity.py\n"
        " * ═══════════════════════════════════════════════════════════════════ */\n\n"
    )
    return header + strategy_js + "\n\n" + wrapper_js


def build_workflow(
    symbol: str, interval: str, interval_minutes: int, bars: int
) -> dict:
    code = build_code(symbol, interval, interval_minutes)

    telegram_message = (
        "={{ $json.signal === 'LONG' ? '🟢' : '🔴' }} *SEÑAL "
        "{{ $json.signal }}*\n\n"
        "*Ticker:* `{{ $json.symbol }}`\n"
        "*Timeframe:* {{ $json.interval }}\n"
        "*Vela:* {{ $json.timestamp }}\n"
        "*Precio:* {{ $json.price }}\n\n"
        "*Motivo:* {{ $json.reason }}\n\n"
        "*Indicadores*\n"
        "• EMA rápida: {{ $json.indicators.ema_fast }}\n"
        "• EMA lenta: {{ $json.indicators.ema_slow }}\n"
        "• RSI: {{ $json.indicators.rsi }}\n"
        "• ATR: {{ $json.indicators.atr }}\n"
        "• Volumen: {{ $json.indicators.volume }} "
        "(media {{ $json.indicators.volume_ma }})\n\n"
        "_Alerta de análisis. No es una orden ni ejecuta operaciones._"
    )

    nodes = [
        {
            "parameters": {
                "rule": {
                    "interval": [
                        {
                            "field": "cronExpression",
                            # Filtro grueso en UTC: lun-vie, 13-20h UTC cubre
                            # 09:30-16:00 ET tanto en EDT como en EST. El
                            # chequeo fino de horario (con DST) lo hace el
                            # Code node en America/New_York.
                            "expression": "*/5 13-20 * * 1-5",
                        }
                    ]
                }
            },
            "id": "trigger-cron",
            "name": "Cron 5 min (horario mercado)",
            "type": "n8n-nodes-base.scheduleTrigger",
            "typeVersion": 1.2,
            "position": POS["trigger"],
            "notes": (
                "Cron en UTC. 13-20h UTC cubre la sesión regular de NY en "
                "horario de verano y de invierno; el Code node descarta las "
                "corridas fuera de 09:30-16:00 America/New_York."
            ),
        },
        {
            "parameters": {
                "url": "https://api.twelvedata.com/time_series",
                "authentication": "genericCredentialType",
                "genericAuthType": "httpQueryAuth",
                "sendQuery": True,
                "queryParameters": {
                    "parameters": [
                        {"name": "symbol", "value": symbol},
                        {"name": "interval", "value": interval},
                        {"name": "outputsize", "value": str(bars)},
                        {"name": "order", "value": "desc"},
                        {"name": "timezone", "value": "America/New_York"},
                    ]
                },
                "options": {
                    "timeout": 15000,
                    "response": {"response": {"neverError": True}},
                },
            },
            "id": "http-marketdata",
            "name": "Twelve Data · time_series",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": POS["http"],
            "credentials": {
                "httpQueryAuth": {
                    "id": "REEMPLAZAR",
                    "name": "PLACEHOLDER · Twelve Data API Key",
                }
            },
            # Estas tres líneas son el manejo de errores del requisito 6:
            # reintenta, y si igual falla NO corta el workflow — deja pasar un
            # item con `error` que el Code node convierte en ok:false.
            "retryOnFail": True,
            "maxTries": 3,
            "waitBetweenTries": 3000,
            "onError": "continueRegularOutput",
            "notes": (
                "La API key va en una credencial 'Query Auth' (nombre: apikey), "
                "NUNCA hardcodeada acá. neverError=true porque Twelve Data "
                "devuelve HTTP 200 con {status:'error'} en el body."
            ),
        },
        {
            "parameters": {"jsCode": code},
            "id": "code-strategy",
            "name": "Indicadores + Estrategia",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": POS["code"],
            "notes": (
                "GENERADO por tools/build_n8n_workflow.py a partir de "
                "n8n/strategy.js + n8n/live_wrapper.js. No editar acá."
            ),
        },
        {
            "parameters": {
                "conditions": {
                    "options": {
                        "caseSensitive": True,
                        "leftValue": "",
                        "typeValidation": "strict",
                        "version": 2,
                    },
                    "conditions": [
                        {
                            "id": "cond-ok",
                            "leftValue": "={{ $json.ok }}",
                            "rightValue": True,
                            "operator": {"type": "boolean", "operation": "true",
                                         "singleValue": True},
                        },
                        {
                            "id": "cond-signal",
                            "leftValue": "={{ $json.hasSignal }}",
                            "rightValue": True,
                            "operator": {"type": "boolean", "operation": "true",
                                         "singleValue": True},
                        },
                    ],
                    "combinator": "and",
                },
                "options": {},
            },
            "id": "if-signal",
            "name": "¿Señal válida?",
            "type": "n8n-nodes-base.if",
            "typeVersion": 2.2,
            "position": POS["if"],
            "notes": (
                "Doble compuerta: ok=true (no hubo error de API ni excepción) "
                "Y hasSignal=true. Es lo que garantiza que un fallo de la API "
                "nunca se convierta en una alerta falsa."
            ),
        },
        {
            "parameters": {
                "chatId": "={{ $env.TELEGRAM_CHAT_ID }}",
                "text": telegram_message,
                "additionalFields": {"parse_mode": "Markdown"},
            },
            "id": "telegram-alert",
            "name": "Telegram · Enviar alerta",
            "type": "n8n-nodes-base.telegram",
            "typeVersion": 1.2,
            "position": POS["telegram"],
            "credentials": {
                "telegramApi": {
                    "id": "REEMPLAZAR",
                    "name": "PLACEHOLDER · Telegram Bot",
                }
            },
            "onError": "continueRegularOutput",
            "notes": (
                "chatId sale de la variable de entorno TELEGRAM_CHAT_ID. "
                "El token del bot va en la credencial 'Telegram API'. "
                "Ninguno de los dos se guarda en este JSON."
            ),
        },
        {
            "parameters": {},
            "id": "noop-nosignal",
            "name": "Sin señal / error registrado",
            "type": "n8n-nodes-base.noOp",
            "typeVersion": 1,
            "position": POS["noop"],
            "notes": (
                "Rama silenciosa. El motivo queda en el output del Code node "
                "(campos error / skipReason), visible en Executions. Si querés "
                "que los errores te avisen, colgá acá un segundo nodo de "
                "Telegram filtrando por $json.error."
            ),
        },
    ]

    connections = {
        "Cron 5 min (horario mercado)": {
            "main": [[{"node": "Twelve Data · time_series", "type": "main", "index": 0}]]
        },
        "Twelve Data · time_series": {
            "main": [[{"node": "Indicadores + Estrategia", "type": "main", "index": 0}]]
        },
        "Indicadores + Estrategia": {
            "main": [[{"node": "¿Señal válida?", "type": "main", "index": 0}]]
        },
        "¿Señal válida?": {
            "main": [
                [{"node": "Telegram · Enviar alerta", "type": "main", "index": 0}],
                [{"node": "Sin señal / error registrado", "type": "main", "index": 0}],
            ]
        },
    }

    return {
        "name": f"Alertas {symbol} · {interval} · EMA cross + RSI",
        "nodes": nodes,
        "connections": connections,
        "active": False,
        "settings": {
            "executionOrder": "v1",
            "saveManualExecutions": True,
            "saveExecutionProgress": True,
        },
        "pinData": {},
        "tags": [],
        "meta": {
            "instanceId": "PLACEHOLDER",
            "generatedBy": "tools/build_n8n_workflow.py",
            "strategySource": "n8n/strategy.js (verificado con tools/check_parity.py)",
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Genera el workflow de n8n")
    ap.add_argument("--symbol", default="QQQ",
                    help="Símbolo para la API en vivo (default: QQQ)")
    ap.add_argument("--interval", default="5min",
                    help="Intervalo en formato Twelve Data (default: 5min)")
    ap.add_argument("--interval-minutes", type=int, default=5)
    ap.add_argument("--bars", type=int, default=120,
                    help="outputsize a pedir: warm-up + colchón (default: 120)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    workflow = build_workflow(
        args.symbol, args.interval, args.interval_minutes, args.bars
    )

    out = Path(args.out) if args.out else N8N_DIR / "workflow_twelvedata_telegram.json"
    out.write_text(json.dumps(workflow, indent=2, ensure_ascii=False), encoding="utf-8")

    code_len = len(workflow["nodes"][2]["parameters"]["jsCode"])
    print(f"✓ Workflow generado: {out}")
    print(f"  símbolo    : {args.symbol} @ {args.interval}")
    print(f"  velas/req  : {args.bars}")
    print(f"  nodos      : {len(workflow['nodes'])}")
    print(f"  code node  : {code_len:,} caracteres "
          f"(strategy.js + live_wrapper.js)")
    print("\n  Importalo en n8n: Workflows -> ⋯ -> Import from File")
    print("  Después completá las 2 credenciales marcadas como PLACEHOLDER.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
