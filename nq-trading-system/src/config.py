"""Carga y validación de config.yaml + .env.

La config se expone como un dict anidado envuelto en `Config`, que permite
acceso por path con puntos: cfg.get("risk.take_profit").

Precedencia (de menor a mayor):
    valores por defecto  <  config.yaml  <  variables de entorno (.env)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv es opcional en runtime
    def load_dotenv(*_args, **_kwargs):  # type: ignore
        return False


ROOT = Path(__file__).resolve().parent.parent

# Overrides por variable de entorno: ENV_VAR -> (path.en.config, tipo)
_ENV_OVERRIDES: dict[str, tuple[str, type]] = {
    "BACKTEST_TICKER": ("data.ticker", str),
    "BACKTEST_INTERVAL": ("data.interval", str),
    "BACKTEST_LOOKBACK_DAYS": ("data.lookback_days", int),
    "BACKTEST_START": ("data.start", str),
    "BACKTEST_END": ("data.end", str),
}


class Config:
    """Wrapper de solo lectura sobre el dict de configuración."""

    def __init__(self, data: dict[str, Any], path: Path | None = None):
        self._data = data
        self.path = path

    def get(self, dotted: str, default: Any = None) -> Any:
        """cfg.get("risk.take_profit") -> 40.0"""
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node if node is not None else default

    def require(self, dotted: str) -> Any:
        """Igual que get(), pero explota si falta la clave."""
        sentinel = object()
        value = self.get(dotted, sentinel)
        if value is sentinel:
            raise KeyError(
                f"Falta la clave '{dotted}' en {self.path or 'la configuración'}"
            )
        return value

    def section(self, name: str) -> dict[str, Any]:
        return dict(self.get(name, {}) or {})

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self._data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def as_dict(self) -> dict[str, Any]:
        return self._data

    def __repr__(self) -> str:
        return f"Config(path={self.path}, keys={list(self._data)})"


def _apply_env_overrides(data: dict[str, Any]) -> None:
    cfg = Config(data)
    for env_var, (dotted, caster) in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_var)
        if raw is None or raw == "":
            continue
        try:
            cfg.set(dotted, caster(raw))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"No se pudo interpretar {env_var}={raw!r} como {caster.__name__}"
            ) from exc


def load_config(path: str | Path | None = None) -> Config:
    """Carga config.yaml, aplica .env y valida lo esencial."""
    cfg_path = Path(path) if path else ROOT / "config.yaml"
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"No encontré {cfg_path}. Copiá config.yaml del repo o pasá --config."
        )

    load_dotenv(ROOT / ".env")

    with cfg_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    if not isinstance(data, dict):
        raise ValueError(f"{cfg_path} no contiene un mapping YAML en la raíz")

    _apply_env_overrides(data)
    cfg = Config(data, cfg_path)
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    """Chequeos baratos que evitan corridas que fallan 30 segundos después."""
    errors: list[str] = []

    if not cfg.get("data.ticker"):
        errors.append("data.ticker no puede estar vacío")

    interval = cfg.get("data.interval")
    if not interval:
        errors.append("data.interval no puede estar vacío")

    if not cfg.get("data.lookback_days") and not (
        cfg.get("data.start") and cfg.get("data.end")
    ):
        errors.append(
            "definí data.lookback_days, o bien data.start y data.end juntos"
        )

    ema_fast = cfg.get("indicators.ema_fast", 0)
    ema_slow = cfg.get("indicators.ema_slow", 0)
    if ema_fast >= ema_slow:
        errors.append(
            f"indicators.ema_fast ({ema_fast}) debe ser menor que "
            f"indicators.ema_slow ({ema_slow})"
        )

    risk_mode = cfg.get("risk.mode", "points")
    if risk_mode not in {"points", "percent", "atr"}:
        errors.append(f"risk.mode debe ser points|percent|atr, no {risk_mode!r}")

    for key in ("risk.take_profit", "risk.stop_loss"):
        value = cfg.get(key)
        if value is None or value <= 0:
            errors.append(f"{key} debe ser un número positivo (es {value!r})")

    fill = cfg.get("execution.fill", "next_open")
    if fill not in {"next_open", "signal_close"}:
        errors.append(
            f"execution.fill debe ser next_open|signal_close, no {fill!r}"
        )

    if not cfg.get("strategy.name"):
        errors.append("strategy.name no puede estar vacío")

    if errors:
        raise ValueError(
            "Errores de configuración:\n" + "\n".join(f"  - {e}" for e in errors)
        )


def output_dir(cfg: Config) -> Path:
    d = ROOT / cfg.get("output.dir", "outputs")
    d.mkdir(parents=True, exist_ok=True)
    return d
