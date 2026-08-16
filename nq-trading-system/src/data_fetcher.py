"""Descarga de OHLCV histórico vía yfinance, con manejo explícito de límites.

yfinance no documenta bien sus límites y falla en silencio (devuelve un
DataFrame vacío o recortado) cuando pedís más historia de la que Yahoo
guarda para ese timeframe. Este módulo valida el rango ANTES de descargar
y explica qué hacer si no entra.

Límites reales de Yahoo Finance por intervalo (a agosto 2026):

    intervalo   historia máxima
    ---------   ---------------
    1m          7 días
    2m          60 días
    5m          60 días
    15m         60 días
    30m         60 días
    60m / 1h    730 días
    90m         60 días
    1d y mayor  sin límite práctico

Además: el rango de UNA request de 1m no puede superar los 7 días, así que
para 1m hay que trocear. Eso lo hace `_download_chunked`.
"""

from __future__ import annotations

import hashlib
import os
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from .config import Config, ROOT

# Historia máxima que Yahoo devuelve, por intervalo (en días).
INTERVAL_MAX_DAYS: dict[str, int] = {
    "1m": 7,
    "2m": 60,
    "5m": 60,
    "15m": 60,
    "30m": 60,
    "60m": 730,
    "1h": 730,
    "90m": 60,
    "1d": 36500,
    "5d": 36500,
    "1wk": 36500,
    "1mo": 36500,
    "3mo": 36500,
}

# Tamaño máximo de una sola request, por intervalo (en días).
INTERVAL_MAX_REQUEST_DAYS: dict[str, int] = {
    "1m": 7,
    "2m": 60,
    "5m": 60,
    "15m": 60,
    "30m": 60,
    "60m": 730,
    "1h": 730,
    "90m": 60,
}

# Timeframe inmediatamente superior, para sugerir una alternativa cuando el
# rango pedido no entra.
NEXT_INTERVAL_UP: dict[str, str] = {
    "1m": "5m",
    "2m": "5m",
    "5m": "15m",
    "15m": "30m",
    "30m": "1h",
    "60m": "1d",
    "90m": "1d",
    "1h": "1d",
}

MARKET_TZ = "America/New_York"


class DataRangeError(ValueError):
    """El rango pedido excede lo que Yahoo guarda para ese intervalo."""


class DataFetchError(RuntimeError):
    """La descarga falló o devolvió datos inutilizables."""


@dataclass
class FetchRequest:
    ticker: str
    interval: str
    start: datetime
    end: datetime

    @property
    def span_days(self) -> float:
        return (self.end - self.start).total_seconds() / 86400.0


# --------------------------------------------------------------------------
# Resolución del rango pedido
# --------------------------------------------------------------------------

def resolve_range(cfg: Config) -> FetchRequest:
    """Traduce la sección `data` de la config a un FetchRequest concreto."""
    ticker = cfg.require("data.ticker")
    interval = cfg.require("data.interval")

    start_raw = cfg.get("data.start")
    end_raw = cfg.get("data.end")
    lookback = cfg.get("data.lookback_days")

    if start_raw and end_raw:
        start = pd.Timestamp(start_raw).to_pydatetime().replace(tzinfo=timezone.utc)
        end = pd.Timestamp(end_raw).to_pydatetime().replace(tzinfo=timezone.utc)
        if end <= start:
            raise DataRangeError(
                f"data.end ({end_raw}) tiene que ser posterior a data.start ({start_raw})"
            )
    elif lookback:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=int(lookback))
    else:
        raise DataRangeError(
            "Definí data.lookback_days, o data.start y data.end juntos, en config.yaml"
        )

    return FetchRequest(ticker=ticker, interval=interval, start=start, end=end)


def check_limits(req: FetchRequest, *, strict: bool = True) -> str | None:
    """Valida el rango contra los límites de Yahoo.

    Devuelve None si está todo bien, o un string con la advertencia.
    Con strict=True (default) lanza DataRangeError en vez de advertir.
    """
    interval = req.interval
    max_days = INTERVAL_MAX_DAYS.get(interval)

    if max_days is None:
        return (
            f"Intervalo {interval!r} desconocido. Los soportados son: "
            f"{', '.join(sorted(INTERVAL_MAX_DAYS))}"
        )

    # La antigüedad del bar más viejo pedido es lo que importa, no el span:
    # Yahoo mide desde HOY hacia atrás.
    now = datetime.now(timezone.utc)
    age_days = (now - req.start).total_seconds() / 86400.0

    if age_days <= max_days + 0.5:  # medio día de tolerancia
        return None

    suggestion = NEXT_INTERVAL_UP.get(interval, "1d")
    msg = (
        f"\n"
        f"  ┌─ RANGO FUERA DE LOS LÍMITES DE YFINANCE ─────────────────────────\n"
        f"  │ Pediste  : {interval} desde {req.start.date()} "
        f"({age_days:.0f} días atrás)\n"
        f"  │ Yahoo da : {interval} solo hasta {max_days} días atrás "
        f"(desde {(now - timedelta(days=max_days)).date()})\n"
        f"  │\n"
        f"  │ Opciones:\n"
        f"  │  1. Bajá el rango:      data.lookback_days: {max_days}\n"
        f"  │  2. Subí el timeframe:  data.interval: \"{suggestion}\" "
        f"(hasta {INTERVAL_MAX_DAYS.get(suggestion, '?')} días)\n"
        f"  │  3. Cambiá de fuente para historia intradiaria profunda:\n"
        f"  │       • Massive (ex-Polygon) — free tier: 2 años de NQ en 1m\n"
        f"  │       • Databento           — CME real, tick/1m desde 2010, pago por uso\n"
        f"  │       • FirstRate Data      — pago único por dataset histórico de NQ\n"
        f"  │       • IBKR / Rithmic      — si ya tenés cuenta, historia por API\n"
        f"  │     (comparación completa en docs/DATA_PROVIDERS.md)\n"
        f"  │\n"
        f"  │ Sobre el ticker: NQ=F es el continuo del front month, con saltos\n"
        f"  │ en cada rollover trimestral (mar/jun/sep/dic). Para backtests\n"
        f"  │ largos conviene una serie ajustada por rollover.\n"
        f"  └──────────────────────────────────────────────────────────────────\n"
    )

    if strict:
        raise DataRangeError(msg)
    return msg


# --------------------------------------------------------------------------
# Descarga
# --------------------------------------------------------------------------

def _make_session():
    """Sesión de yfinance, con escape hatch para proxies que re-terminan TLS.

    yfinance usa curl_cffi con impersonation de Chrome. Detrás de un proxy
    MITM (redes corporativas, sandboxes CI) ese handshake puede ser rechazado.
    Poniendo YF_IMPERSONATE=chrome110 en .env se fuerza un perfil TLS que sí
    pasa. En una máquina normal esto no hace falta: dejá la var vacía.
    """
    impersonate = os.environ.get("YF_IMPERSONATE", "").strip()
    if not impersonate:
        return None
    try:
        from curl_cffi import requests as curl_requests
    except ImportError:
        warnings.warn(
            "YF_IMPERSONATE está seteado pero curl_cffi no está instalado; "
            "sigo con la sesión por defecto de yfinance."
        )
        return None
    return curl_requests.Session(impersonate=impersonate)


def _download_once(
    ticker: str, interval: str, start: datetime, end: datetime, session
) -> pd.DataFrame:
    import yfinance as yf

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df = yf.download(
            tickers=ticker,
            interval=interval,
            start=start,
            end=end,
            progress=False,
            auto_adjust=False,
            actions=False,
            threads=False,
            session=session,
        )
    return df


def _download_chunked(req: FetchRequest, session) -> pd.DataFrame:
    """Descarga en tramos si el intervalo limita el tamaño de cada request."""
    max_req = INTERVAL_MAX_REQUEST_DAYS.get(req.interval)
    if max_req is None or req.span_days <= max_req:
        return _download_once(req.ticker, req.interval, req.start, req.end, session)

    frames: list[pd.DataFrame] = []
    cursor = req.start
    step = timedelta(days=max_req)
    while cursor < req.end:
        chunk_end = min(cursor + step, req.end)
        part = _download_once(req.ticker, req.interval, cursor, chunk_end, session)
        if not part.empty:
            frames.append(part)
        cursor = chunk_end
        if cursor < req.end:
            time.sleep(0.4)  # cortesía con el rate limit de Yahoo

    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames)
    return combined[~combined.index.duplicated(keep="last")].sort_index()


def _normalize(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Aplana el MultiIndex de columnas de yfinance y estandariza nombres."""
    if df.empty:
        return df

    # yfinance devuelve columnas MultiIndex (Price, Ticker) cuando descargás
    # incluso un solo ticker. Nos quedamos con el nivel de precio.
    if isinstance(df.columns, pd.MultiIndex):
        levels = df.columns.get_level_values(-1)
        if ticker in set(levels):
            df = df.xs(ticker, axis=1, level=-1)
        else:
            df.columns = df.columns.get_level_values(0)

    df = df.rename(columns={c: str(c).strip().lower().replace(" ", "_") for c in df.columns})

    keep = ["open", "high", "low", "close", "volume"]
    missing = [c for c in keep if c not in df.columns]
    if missing:
        raise DataFetchError(
            f"Faltan columnas {missing} en la respuesta de yfinance. "
            f"Llegaron: {list(df.columns)}"
        )
    df = df[keep].copy()

    # Índice a tz-aware en hora de mercado.
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(MARKET_TZ)
    df.index.name = "timestamp"

    df = df[~df.index.duplicated(keep="last")].sort_index()

    # Velas sin precio no sirven. Volumen 0 sí es válido (sesión overnight).
    df = df.dropna(subset=["open", "high", "low", "close"])
    df["volume"] = df["volume"].fillna(0)

    for col in keep:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"])

    return df


def filter_session(cfg: Config, df: pd.DataFrame) -> pd.DataFrame:
    """Recorta a horario regular de mercado si la config lo pide."""
    if df.empty or not cfg.get("data.regular_hours_only", False):
        return df
    start = str(cfg.get("data.session_start", "09:30"))
    end = str(cfg.get("data.session_end", "16:00"))
    before = len(df)
    out = df.between_time(start, end, inclusive="left")
    print(
        f"  [session] filtro RTH {start}-{end} {MARKET_TZ}: "
        f"{before} -> {len(out)} velas"
    )
    return out


# --------------------------------------------------------------------------
# Caché
# --------------------------------------------------------------------------

def _cache_path(req: FetchRequest) -> Path:
    key = f"{req.ticker}|{req.interval}|{req.start:%Y%m%d}|{req.end:%Y%m%d}"
    digest = hashlib.sha1(key.encode()).hexdigest()[:12]
    safe_ticker = req.ticker.replace("=", "_").replace("^", "idx_").replace("/", "_")
    cache_dir = ROOT / "outputs" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{safe_ticker}_{req.interval}_{digest}.parquet"


def _read_cache(path: Path, ttl_minutes: int) -> pd.DataFrame | None:
    if not path.exists():
        return None
    age_min = (time.time() - path.stat().st_mtime) / 60.0
    if age_min > ttl_minutes:
        return None
    try:
        return pd.read_parquet(path)
    except Exception:  # parquet corrupto o falta pyarrow -> re-descargar
        return None


# --------------------------------------------------------------------------
# API pública
# --------------------------------------------------------------------------

def fetch_ohlcv(cfg: Config, *, strict_limits: bool = True) -> pd.DataFrame:
    """Descarga OHLCV según config y devuelve un DataFrame normalizado.

    Columnas: open, high, low, close, volume
    Índice   : DatetimeIndex tz-aware en America/New_York, ordenado ascendente
    """
    req = resolve_range(cfg)

    print(
        f"  [data] {req.ticker} @ {req.interval} | "
        f"{req.start:%Y-%m-%d} -> {req.end:%Y-%m-%d} ({req.span_days:.0f} días)"
    )

    warning = check_limits(req, strict=strict_limits)
    if warning:
        print(warning)

    use_cache = bool(cfg.get("data.use_cache", True))
    ttl = int(cfg.get("data.cache_ttl_minutes", 30))
    cache_file = _cache_path(req)

    if use_cache:
        cached = _read_cache(cache_file, ttl)
        if cached is not None and not cached.empty:
            print(f"  [data] usando caché: {cache_file.name} ({len(cached)} velas)")
            return filter_session(cfg, cached)

    session = _make_session()
    try:
        raw = _download_chunked(req, session)
    except Exception as exc:
        raise DataFetchError(
            f"yfinance falló al descargar {req.ticker} @ {req.interval}: {exc}\n"
            f"  - Verificá conexión y que el ticker exista en Yahoo Finance.\n"
            f"  - Si estás detrás de un proxy que re-termina TLS, probá "
            f"YF_IMPERSONATE=chrome110 en tu .env."
        ) from exc

    if raw is None or raw.empty:
        raise DataFetchError(
            f"yfinance devolvió 0 velas para {req.ticker} @ {req.interval} "
            f"entre {req.start:%Y-%m-%d} y {req.end:%Y-%m-%d}.\n"
            f"  Causas habituales:\n"
            f"   - El rango excede el límite del intervalo "
            f"({INTERVAL_MAX_DAYS.get(req.interval, '?')} días para {req.interval}).\n"
            f"   - Ticker mal escrito (probá NQ=F, ES=F, ^NDX, QQQ).\n"
            f"   - Rango que cae entero en fin de semana o feriado.\n"
            f"   - Rate limit de Yahoo (429): esperá un minuto y reintentá."
        )

    df = _normalize(raw, req.ticker)
    if df.empty:
        raise DataFetchError(
            f"Los datos de {req.ticker} quedaron vacíos después de normalizar."
        )

    got_days = (df.index[-1] - df.index[0]).total_seconds() / 86400.0
    print(
        f"  [data] {len(df)} velas | {df.index[0]:%Y-%m-%d %H:%M} -> "
        f"{df.index[-1]:%Y-%m-%d %H:%M} ({got_days:.0f} días de cobertura real)"
    )

    # Yahoo recorta en silencio: avisamos si devolvió mucho menos de lo pedido.
    if got_days < req.span_days * 0.6 and req.span_days > 3:
        print(
            f"  [data] AVISO: pediste {req.span_days:.0f} días y Yahoo devolvió "
            f"{got_days:.0f}. Suele pasar por el límite del intervalo o por "
            f"feriados/fines de semana dentro del rango."
        )

    if use_cache:
        try:
            df.to_parquet(cache_file)
        except Exception as exc:  # pyarrow ausente -> seguir sin caché
            print(f"  [data] no pude cachear ({exc}); sigo sin caché.")

    return filter_session(cfg, df)
