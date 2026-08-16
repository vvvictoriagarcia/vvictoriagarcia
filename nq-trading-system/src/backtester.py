"""Motor de backtesting: recorre el histórico vela por vela.

Decisiones de diseño (importantes para que los números sean creíbles)
---------------------------------------------------------------------

1. SIN LOOKAHEAD. La estrategia solo ve la vela actual ya cerrada y la
   anterior. Con execution.fill = "next_open" (default) la entrada se ejecuta
   en la apertura de la vela SIGUIENTE: en vivo, cuando la vela cierra y la
   señal aparece, lo más temprano que podés estar adentro es la apertura de
   la próxima. "signal_close" existe para comparar, pero es optimista.

2. TP/SL SE EVALÚAN INTRA-VELA con High/Low, no con el Close. Si el precio
   tocó el stop en el medio de la vela, el trade salió ahí aunque haya
   cerrado a favor.

3. SI TP Y SL CAEN EN LA MISMA VELA, GANA EL STOP. Con datos OHLC no se sabe
   cuál se tocó primero. Asumir el stop es la convención conservadora; la
   alternativa infla el win rate con trades que en vivo eran perdedores.
   El contador `ambiguous_bars` en las métricas te dice cuántas veces pasó:
   si es alto respecto del total de trades, tus TP/SL son chicos para la
   volatilidad del timeframe y el backtest es poco confiable.

4. UNA POSICIÓN A LA VEZ, tamaño fijo. Sin pirámide ni sizing dinámico.

5. LOS COSTOS SE RESTAN SIEMPRE. commission_points + slippage_points salen
   del resultado de cada trade, ida y vuelta. Un backtest sin costos en 5m
   no significa nada.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from datetime import time as dtime
from typing import Any

import numpy as np
import pandas as pd

from .config import Config
from .indicators import warmup_bars
from .strategy import Bar, Context, Signal, Strategy

# Columnas del DataFrame que son precio/volumen; el resto son indicadores.
_OHLCV = ("open", "high", "low", "close", "volume")


@dataclass
class Trade:
    """Un trade cerrado. Cada campo de acá termina en trades.csv."""

    trade_id: int
    direction: str                 # 'long' | 'short'
    entry_time: pd.Timestamp
    entry_price: float
    exit_time: pd.Timestamp
    exit_price: float
    exit_reason: str               # TP | SL | SIGNAL | SESSION_END | EOD_DATA
    points_gross: float            # movimiento del precio a favor de la posición
    costs_points: float            # comisión + slippage, ida y vuelta
    points: float                  # neto = gross - costs  <- el que importa
    pct: float                     # neto sobre el precio de entrada
    pnl_usd: float                 # neto * point_value * contratos
    bars_held: int
    mae_points: float              # Maximum Adverse Excursion: peor punto en contra
    mfe_points: float              # Maximum Favorable Excursion: mejor punto a favor
    entry_reason: str
    signal_context: dict[str, Any]

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        ctx = row.pop("signal_context", {}) or {}
        # Los indicadores que gatillaron la señal se aplanan como ind_*
        for key, value in ctx.items():
            row[f"ind_{key}"] = value
        return row


@dataclass
class OpenPosition:
    direction: str
    entry_time: pd.Timestamp
    entry_price: float
    take_profit: float
    stop_loss: float
    entry_reason: str
    signal_context: dict[str, Any]
    bars_held: int = 0
    mae_points: float = 0.0
    mfe_points: float = 0.0


class Backtester:
    def __init__(self, cfg: Config, strategy: Strategy):
        self.cfg = cfg
        self.strategy = strategy

        risk = cfg.section("risk")
        self.risk_mode: str = risk.get("mode", "points")
        self.tp_value: float = float(risk.get("take_profit", 40.0))
        self.sl_value: float = float(risk.get("stop_loss", 20.0))
        self.exit_on_opposite: bool = bool(risk.get("exit_on_opposite_signal", True))

        ex = cfg.section("execution")
        self.fill: str = ex.get("fill", "next_open")
        self.commission: float = float(ex.get("commission_points", 0.0))
        self.slippage: float = float(ex.get("slippage_points", 0.0))
        self.point_value: float = float(ex.get("point_value_usd", 20.0))
        self.contracts: int = int(ex.get("contracts", 1))
        self.initial_capital: float = float(ex.get("initial_capital_usd", 25000.0))

        self.close_at_session_end: bool = bool(
            cfg.get("data.close_at_session_end", True)
        )
        self.warmup = warmup_bars(cfg)

        self.trades: list[Trade] = []
        self.ambiguous_bars = 0
        self._pending: Signal | None = None   # señal esperando fill en next_open
        self._trade_seq = 0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _round_trip_costs(self) -> float:
        """Costos totales del trade en puntos (entrada + salida)."""
        return (self.commission + self.slippage) * 2.0

    def _targets(self, direction: str, price: float, atr_value: float | None
                 ) -> tuple[float, float]:
        """Calcula (take_profit, stop_loss) como niveles de precio absolutos."""
        if self.risk_mode == "points":
            tp_pts, sl_pts = self.tp_value, self.sl_value
        elif self.risk_mode == "percent":
            tp_pts = price * self.tp_value / 100.0
            sl_pts = price * self.sl_value / 100.0
        elif self.risk_mode == "atr":
            if atr_value is None or math.isnan(atr_value) or atr_value <= 0:
                # ATR todavía en warm-up: caemos a puntos para no abrir un
                # trade sin stop definido.
                tp_pts, sl_pts = self.tp_value, self.sl_value
            else:
                tp_pts = self.tp_value * atr_value
                sl_pts = self.sl_value * atr_value
        else:
            raise ValueError(f"risk.mode desconocido: {self.risk_mode!r}")

        if direction == "long":
            return price + tp_pts, price - sl_pts
        return price - tp_pts, price + sl_pts

    @staticmethod
    def _make_bar(ts: pd.Timestamp, row: pd.Series) -> Bar:
        indicators = {
            k: (None if pd.isna(v) else float(v))
            for k, v in row.items()
            if k not in _OHLCV
        }
        return Bar(
            timestamp=ts,
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
            indicators=indicators,
        )

    def _open_position(
        self, signal: Signal, ts: pd.Timestamp, price: float, atr_value: float | None
    ) -> OpenPosition:
        direction = signal.direction
        assert direction is not None
        tp, sl = self._targets(direction, price, atr_value)
        return OpenPosition(
            direction=direction,
            entry_time=ts,
            entry_price=price,
            take_profit=tp,
            stop_loss=sl,
            entry_reason=signal.reason,
            signal_context=dict(signal.context),
        )

    def _close_position(
        self, pos: OpenPosition, ts: pd.Timestamp, price: float, reason: str
    ) -> Trade:
        self._trade_seq += 1

        if pos.direction == "long":
            gross = price - pos.entry_price
        else:
            gross = pos.entry_price - price

        # MAE/MFE se acumulan con el High/Low completo de cada vela, incluida
        # la de salida. Pero en la vela de salida el precio siguió moviéndose
        # DESPUÉS de que ya estábamos afuera, y esa parte no la sufrimos.
        # Si salimos por stop, la peor excursión real es exactamente la
        # distancia al stop; si salimos por target, la mejor es la distancia
        # al target. Sin este recorte, MAE puede dar mayor que el stop, que es
        # imposible y hace desconfiar (con razón) de toda la tabla.
        if reason == "SL":
            sl_distance = -abs(pos.entry_price - pos.stop_loss)
            pos.mae_points = max(pos.mae_points, sl_distance)
        elif reason == "TP":
            tp_distance = abs(pos.take_profit - pos.entry_price)
            pos.mfe_points = min(pos.mfe_points, tp_distance)

        costs = self._round_trip_costs()
        net = gross - costs
        pct = (net / pos.entry_price) * 100.0 if pos.entry_price else 0.0

        return Trade(
            trade_id=self._trade_seq,
            direction=pos.direction,
            entry_time=pos.entry_time,
            entry_price=round(pos.entry_price, 2),
            exit_time=ts,
            exit_price=round(price, 2),
            exit_reason=reason,
            points_gross=round(gross, 2),
            costs_points=round(costs, 2),
            points=round(net, 2),
            pct=round(pct, 4),
            pnl_usd=round(net * self.point_value * self.contracts, 2),
            bars_held=pos.bars_held,
            mae_points=round(pos.mae_points, 2),
            mfe_points=round(pos.mfe_points, 2),
            entry_reason=pos.entry_reason,
            signal_context=pos.signal_context,
        )

    @staticmethod
    def _is_last_bar_of_session(
        ts: pd.Timestamp, next_ts: pd.Timestamp | None
    ) -> bool:
        return next_ts is None or next_ts.date() != ts.date()

    # ------------------------------------------------------------------
    # Loop principal
    # ------------------------------------------------------------------

    def run(self, df: pd.DataFrame) -> pd.DataFrame:
        """Recorre el histórico y devuelve la tabla de trades."""
        if df.empty:
            raise ValueError("El DataFrame de entrada está vacío")
        if len(df) <= self.warmup:
            raise ValueError(
                f"Necesito más de {self.warmup} velas para el warm-up de los "
                f"indicadores y tengo {len(df)}. Subí data.lookback_days o bajá "
                f"los períodos en indicators."
            )

        timestamps = df.index
        rows = [df.iloc[i] for i in range(len(df))]

        position: OpenPosition | None = None
        prev_bar: Bar | None = None
        self.trades.clear()
        self.ambiguous_bars = 0
        self._pending = None
        self._trade_seq = 0

        for i in range(len(df)):
            ts = timestamps[i]
            row = rows[i]
            bar = self._make_bar(ts, row)
            next_ts = timestamps[i + 1] if i + 1 < len(df) else None
            atr_value = bar.get("atr")

            # ----------------------------------------------------------
            # (1) Ejecutar la señal pendiente de la vela anterior.
            #     Va PRIMERO: en vivo la apertura ocurre antes que cualquier
            #     movimiento intra-vela.
            # ----------------------------------------------------------
            if self._pending is not None and position is None:
                position = self._open_position(
                    self._pending, ts, float(row["open"]), atr_value
                )
                self._pending = None
            elif self._pending is not None:
                # Ya había posición cuando llegó el fill (p.ej. la señal
                # contraria cerró y quiso reabrir el mismo bar): descartamos.
                self._pending = None

            # ----------------------------------------------------------
            # (2) Gestionar la posición abierta: TP / SL intra-vela.
            # ----------------------------------------------------------
            if position is not None:
                position.bars_held += 1

                high, low = float(row["high"]), float(row["low"])
                if position.direction == "long":
                    position.mfe_points = max(
                        position.mfe_points, high - position.entry_price
                    )
                    position.mae_points = min(
                        position.mae_points, low - position.entry_price
                    )
                    hit_tp = high >= position.take_profit
                    hit_sl = low <= position.stop_loss
                else:
                    position.mfe_points = max(
                        position.mfe_points, position.entry_price - low
                    )
                    position.mae_points = min(
                        position.mae_points, position.entry_price - high
                    )
                    hit_tp = low <= position.take_profit
                    hit_sl = high >= position.stop_loss

                # No evaluamos TP/SL en la misma vela de entrada si la entrada
                # fue en el open de esta vela: sí lo hacemos, porque el precio
                # efectivamente recorrió high/low DESPUÉS de la apertura.
                if hit_tp and hit_sl:
                    # Ambos en la misma vela: no sabemos el orden -> stop.
                    self.ambiguous_bars += 1
                    self.trades.append(
                        self._close_position(position, ts, position.stop_loss, "SL")
                    )
                    position = None
                elif hit_sl:
                    self.trades.append(
                        self._close_position(position, ts, position.stop_loss, "SL")
                    )
                    position = None
                elif hit_tp:
                    self.trades.append(
                        self._close_position(position, ts, position.take_profit, "TP")
                    )
                    position = None

            # ----------------------------------------------------------
            # (3) Consultar a la estrategia (después del warm-up).
            # ----------------------------------------------------------
            signal: Signal | None = None
            if i >= self.warmup:
                ctx = Context(
                    bar=bar,
                    prev=prev_bar,
                    position=position.direction if position else None,
                    bars_in_position=position.bars_held if position else 0,
                    entry_price=position.entry_price if position else None,
                    params=self.strategy.params,
                )
                signal = self.strategy.evaluate(ctx)

            # ----------------------------------------------------------
            # (4) Salida por señal (EXIT explícito o señal contraria).
            # ----------------------------------------------------------
            if position is not None and signal is not None:
                opposite = (
                    signal.is_entry
                    and signal.direction != position.direction
                    and self.exit_on_opposite
                )
                if signal.type == "EXIT" or opposite:
                    self.trades.append(
                        self._close_position(
                            position, ts, float(row["close"]), "SIGNAL"
                        )
                    )
                    position = None

            # ----------------------------------------------------------
            # (5) Cierre forzado al final de la sesión / de los datos.
            # ----------------------------------------------------------
            if position is not None:
                last_of_session = self._is_last_bar_of_session(ts, next_ts)
                if next_ts is None:
                    self.trades.append(
                        self._close_position(
                            position, ts, float(row["close"]), "EOD_DATA"
                        )
                    )
                    position = None
                elif self.close_at_session_end and last_of_session:
                    self.trades.append(
                        self._close_position(
                            position, ts, float(row["close"]), "SESSION_END"
                        )
                    )
                    position = None

            # ----------------------------------------------------------
            # (6) Entrada nueva: agendar el fill.
            # ----------------------------------------------------------
            if position is None and signal is not None and signal.is_entry:
                # No abrimos en la última vela de la sesión: entraríamos para
                # que el cierre forzado nos saque inmediatamente.
                blocked_by_session = (
                    self.close_at_session_end
                    and self._is_last_bar_of_session(ts, next_ts)
                )
                if not blocked_by_session:
                    if self.fill == "signal_close":
                        position = self._open_position(
                            signal, ts, float(row["close"]), atr_value
                        )
                    else:  # next_open
                        self._pending = signal

            prev_bar = bar

        return trades_to_frame(self.trades)


# --------------------------------------------------------------------------
# Trades -> DataFrame
# --------------------------------------------------------------------------

TRADE_COLUMNS = [
    "trade_id", "direction",
    "entry_time", "entry_price",
    "exit_time", "exit_price",
    "exit_reason",
    "points_gross", "costs_points", "points", "pct", "pnl_usd",
    "bars_held", "mae_points", "mfe_points",
    "entry_reason",
]


def trades_to_frame(trades: list[Trade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame(columns=TRADE_COLUMNS)

    df = pd.DataFrame([t.to_row() for t in trades])
    ind_cols = sorted(c for c in df.columns if c.startswith("ind_"))
    ordered = [c for c in TRADE_COLUMNS if c in df.columns] + ind_cols
    df = df[ordered]

    # Timestamps al minuto — es lo que se pidió: fecha y hora exactas.
    for col in ("entry_time", "exit_time"):
        df[col] = pd.to_datetime(df[col])
    return df


# --------------------------------------------------------------------------
# Curva de equity y métricas
# --------------------------------------------------------------------------

def build_equity_curve(
    trades: pd.DataFrame, initial_capital: float
) -> pd.DataFrame:
    """Equity marcada a la salida de cada trade (no mark-to-market intra-trade)."""
    if trades.empty:
        return pd.DataFrame(
            columns=["exit_time", "pnl_usd", "equity_usd", "peak_usd",
                     "drawdown_usd", "drawdown_pct", "cum_points"]
        )

    eq = trades[["exit_time", "pnl_usd", "points"]].copy().sort_values("exit_time")
    eq["equity_usd"] = initial_capital + eq["pnl_usd"].cumsum()
    eq["peak_usd"] = eq["equity_usd"].cummax()
    eq["drawdown_usd"] = eq["equity_usd"] - eq["peak_usd"]
    eq["drawdown_pct"] = (eq["drawdown_usd"] / eq["peak_usd"]) * 100.0
    eq["cum_points"] = eq["points"].cumsum()
    return eq[
        ["exit_time", "pnl_usd", "equity_usd", "peak_usd",
         "drawdown_usd", "drawdown_pct", "cum_points"]
    ]


def compute_metrics(
    trades: pd.DataFrame,
    equity: pd.DataFrame,
    *,
    initial_capital: float,
    ambiguous_bars: int = 0,
    bars_total: int = 0,
) -> dict[str, Any]:
    """Métricas agregadas del backtest."""
    if trades.empty:
        return {
            "trades_total": 0,
            "note": (
                "La estrategia no generó ningún trade. Probá relajar los filtros "
                "(rangos de RSI más amplios), ampliar el rango de fechas, o bajar "
                "el timeframe."
            ),
            "bars_evaluated": bars_total,
        }

    pnl = trades["pnl_usd"]
    pts = trades["points"]
    wins = trades[pts > 0]
    losses = trades[pts < 0]
    scratches = trades[pts == 0]

    gross_profit = float(wins["pnl_usd"].sum())
    gross_loss = float(abs(losses["pnl_usd"].sum()))

    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = float("inf") if gross_profit > 0 else 0.0

    max_dd_usd = float(equity["drawdown_usd"].min()) if not equity.empty else 0.0
    max_dd_pct = float(equity["drawdown_pct"].min()) if not equity.empty else 0.0

    final_equity = (
        float(equity["equity_usd"].iloc[-1]) if not equity.empty else initial_capital
    )
    total_return_pct = ((final_equity / initial_capital) - 1.0) * 100.0

    # Racha máxima de pérdidas consecutivas.
    max_losing_streak = streak = 0
    for value in pts:
        if value < 0:
            streak += 1
            max_losing_streak = max(max_losing_streak, streak)
        else:
            streak = 0

    exit_counts = trades["exit_reason"].value_counts().to_dict()
    direction_counts = trades["direction"].value_counts().to_dict()

    expectancy_pts = float(pts.mean())

    metrics: dict[str, Any] = {
        # --- volumen de actividad ---
        "trades_total": int(len(trades)),
        "trades_long": int(direction_counts.get("long", 0)),
        "trades_short": int(direction_counts.get("short", 0)),
        "bars_evaluated": int(bars_total),

        # --- acierto ---
        "wins": int(len(wins)),
        "losses": int(len(losses)),
        "scratches": int(len(scratches)),
        "win_rate_pct": round(len(wins) / len(trades) * 100.0, 2),

        # --- rentabilidad ---
        "total_points": round(float(pts.sum()), 2),
        "total_points_gross": round(float(trades["points_gross"].sum()), 2),
        "total_pnl_usd": round(float(pnl.sum()), 2),
        "total_return_pct": round(total_return_pct, 2),
        "profit_factor": (
            round(profit_factor, 3) if math.isfinite(profit_factor) else None
        ),
        "gross_profit_usd": round(gross_profit, 2),
        "gross_loss_usd": round(gross_loss, 2),
        "expectancy_points": round(expectancy_pts, 3),
        "expectancy_usd": round(float(pnl.mean()), 2),

        # --- distribución ---
        "avg_win_points": round(float(wins["points"].mean()), 2) if len(wins) else 0.0,
        "avg_loss_points": (
            round(float(losses["points"].mean()), 2) if len(losses) else 0.0
        ),
        "best_trade_points": round(float(pts.max()), 2),
        "worst_trade_points": round(float(pts.min()), 2),
        "avg_bars_held": round(float(trades["bars_held"].mean()), 1),

        # --- riesgo ---
        "max_drawdown_usd": round(max_dd_usd, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "max_losing_streak": int(max_losing_streak),
        "avg_mae_points": round(float(trades["mae_points"].mean()), 2),
        "avg_mfe_points": round(float(trades["mfe_points"].mean()), 2),

        # --- costos ---
        "total_costs_points": round(float(trades["costs_points"].sum()), 2),
        "total_costs_usd": round(
            float(trades["costs_points"].sum()) * 0.0, 2
        ),  # se completa abajo

        # --- calidad del backtest ---
        "exit_reasons": {str(k): int(v) for k, v in exit_counts.items()},
        "ambiguous_tp_sl_bars": int(ambiguous_bars),

        # --- período ---
        "first_entry": str(trades["entry_time"].min()),
        "last_exit": str(trades["exit_time"].max()),
    }

    # Ratio tipo Sharpe sobre la serie de resultados por trade (no anualizado:
    # con pocos trades intradiarios el anualizado es puro ruido).
    if len(trades) > 1 and float(pnl.std(ddof=1)) > 0:
        metrics["trade_sharpe"] = round(
            float(pnl.mean()) / float(pnl.std(ddof=1)), 3
        )
    else:
        metrics["trade_sharpe"] = None

    # Advertencias: mejor que la herramienta te avise a que te des cuenta tarde.
    warnings_list: list[str] = []
    if len(trades) < 30:
        warnings_list.append(
            f"Solo {len(trades)} trades: la muestra es demasiado chica para "
            f"sacar conclusiones estadísticas. Buscá 100+ antes de confiar."
        )
    if ambiguous_bars > 0:
        pct_amb = ambiguous_bars / len(trades) * 100.0
        warnings_list.append(
            f"{ambiguous_bars} velas ({pct_amb:.0f}% de los trades) tocaron TP y "
            f"SL en la misma vela; se resolvieron como SL (conservador). Si el "
            f"porcentaje es alto, tus targets son chicos para la volatilidad del "
            f"timeframe."
        )
    gross_points = float(trades["points_gross"].sum())
    if metrics["total_costs_points"] and abs(gross_points) > 1e-9:
        cost_ratio = metrics["total_costs_points"] / abs(gross_points)
        if cost_ratio > 0.3:
            warnings_list.append(
                f"Los costos ({metrics['total_costs_points']:.1f} pts) se comen "
                f"el {cost_ratio:.0%} del resultado BRUTO ({gross_points:+.1f} pts). "
                f"La estrategia opera demasiado para el edge que tiene."
            )
    if warnings_list:
        metrics["warnings"] = warnings_list

    return metrics


def finalize_costs(metrics: dict[str, Any], point_value: float, contracts: int) -> None:
    """Convierte los costos de puntos a USD una vez conocido el multiplicador."""
    if "total_costs_points" in metrics:
        metrics["total_costs_usd"] = round(
            metrics["total_costs_points"] * point_value * contracts, 2
        )
