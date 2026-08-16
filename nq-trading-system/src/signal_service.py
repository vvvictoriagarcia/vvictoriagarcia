"""Servicio HTTP que expone la estrategia Python — Opción B para n8n.

El problema que resuelve
------------------------
El workflow de n8n reimplementa la estrategia en JavaScript. Eso funciona
(tools/check_parity.py lo verifica), pero te obliga a escribir cada cambio
dos veces. Si un día editás solo el Python, el bot en vivo sigue operando la
lógica vieja y no te enterás hasta que las alertas dejan de tener sentido.

Este servicio elimina el problema: expone src/strategy.py por HTTP y n8n lo
consume. Una sola implementación, cero posibilidad de divergencia.

El precio: necesitás un proceso Python corriendo y accesible desde n8n. Si
n8n está en la nube (n8n.cloud) y esto en tu notebook, no se ven — habría que
exponerlo con un túnel o desplegarlo en algún lado. Por eso la Opción A (JS
embebido) sigue siendo el default: no necesita infraestructura.

Levantarlo
----------
    pip install fastapi uvicorn
    uvicorn src.signal_service:app --host 0.0.0.0 --port 8000

Probarlo
--------
    curl -X POST http://localhost:8000/signal \
      -H "Content-Type: application/json" \
      -H "X-Auth-Token: $SIGNAL_SERVICE_TOKEN" \
      -d '{"candles":[{"timestamp":"2026-08-14 09:30:00","open":1,"high":2,
                       "low":0.5,"close":1.5,"volume":100}]}'

Seguridad
---------
Si lo exponés a internet, definí SIGNAL_SERVICE_TOKEN en .env: el servicio
exige ese valor en el header X-Auth-Token. Sin la variable definida, el
endpoint queda abierto y arranca con una advertencia. Este servicio solo
LEE datos y devuelve señales; no toca ninguna cuenta ni ejecuta órdenes.
"""

from __future__ import annotations

import os
from typing import Any

import pandas as pd

try:
    from fastapi import FastAPI, Header, HTTPException
    from pydantic import BaseModel, Field
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "El signal service necesita fastapi y uvicorn:\n"
        "    pip install fastapi uvicorn"
    ) from exc

from .config import load_config
from .indicators import add_indicators, warmup_bars
from .strategy import Bar, Context, get_strategy

app = FastAPI(
    title="Signal Service",
    description=(
        "Expone la MISMA función de estrategia que usa el backtester, para "
        "que las alertas en vivo no puedan desincronizarse. Solo análisis: "
        "no ejecuta órdenes ni se conecta a ningún broker."
    ),
    version="1.0.0",
)

_cfg = load_config()
_strategy = get_strategy(
    _cfg.require("strategy.name"), _cfg.section("strategy").get("params", {})
)
_warmup = warmup_bars(_cfg)
_token = os.environ.get("SIGNAL_SERVICE_TOKEN", "").strip()

if not _token:
    print(
        "[signal_service] AVISO: SIGNAL_SERVICE_TOKEN no está definido; el "
        "endpoint queda sin autenticación. No lo expongas a internet así."
    )


class Candle(BaseModel):
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class SignalRequest(BaseModel):
    candles: list[Candle] = Field(
        ...,
        description=(
            "Velas en orden ASCENDENTE (la última es la más reciente y debe "
            "estar CERRADA). Mandá al menos `warmup_bars` velas."
        ),
    )
    symbol: str = "NQ=F"


class SignalResponse(BaseModel):
    ok: bool
    hasSignal: bool
    signal: str
    reason: str = ""
    symbol: str = ""
    timestamp: str | None = None
    price: float | None = None
    indicators: dict[str, Any] = {}
    barsUsed: int = 0
    error: str | None = None


def _check_auth(header_value: str | None) -> None:
    if _token and header_value != _token:
        raise HTTPException(status_code=401, detail="X-Auth-Token inválido o ausente")


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "strategy": _strategy.name,
        "params": _strategy.params,
        "min_bars_required": _warmup,
        "authenticated": bool(_token),
    }


@app.post("/signal", response_model=SignalResponse)
def signal(
    req: SignalRequest,
    x_auth_token: str | None = Header(default=None, alias="X-Auth-Token"),
) -> SignalResponse:
    """Evalúa la estrategia sobre la última vela recibida.

    Nunca lanza 500 por datos flojos: devuelve ok=false con el motivo, para
    que el workflow de n8n pueda cortar limpio en vez de romperse.
    """
    _check_auth(x_auth_token)

    if len(req.candles) < 2:
        return SignalResponse(
            ok=False, hasSignal=False, signal="NONE", symbol=req.symbol,
            error="Necesito al menos 2 velas",
        )

    if len(req.candles) < _warmup:
        return SignalResponse(
            ok=False, hasSignal=False, signal="NONE", symbol=req.symbol,
            barsUsed=len(req.candles),
            error=(
                f"Velas insuficientes: llegaron {len(req.candles)}, "
                f"necesito al menos {_warmup} para el warm-up de indicadores"
            ),
        )

    try:
        df = pd.DataFrame([c.model_dump() for c in req.candles])
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.set_index("timestamp").sort_index()

        df = add_indicators(df, _cfg)

        def mk(idx: int) -> Bar:
            ts, row = df.index[idx], df.iloc[idx]
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

        bar, prev = mk(-1), mk(-2)
        # position=None: el bot en vivo no lleva posición, solo detecta entradas.
        result = _strategy.evaluate(
            Context(bar=bar, prev=prev, position=None, params=_strategy.params)
        )

        return SignalResponse(
            ok=True,
            hasSignal=result.is_entry,
            signal=result.type,
            reason=result.reason,
            symbol=req.symbol,
            timestamp=str(bar.timestamp),
            price=round(bar.close, 2),
            indicators={
                k: (round(v, 2) if isinstance(v, float) else v)
                for k, v in {
                    "ema_fast": bar.get("ema_fast"),
                    "ema_slow": bar.get("ema_slow"),
                    "rsi": bar.get("rsi"),
                    "atr": bar.get("atr"),
                    "volume": bar.volume,
                    "volume_ma": bar.get("volume_ma"),
                }.items()
            },
            barsUsed=len(df),
        )
    except Exception as exc:
        return SignalResponse(
            ok=False, hasSignal=False, signal="NONE", symbol=req.symbol,
            error=f"Error evaluando la estrategia: {exc}",
        )
