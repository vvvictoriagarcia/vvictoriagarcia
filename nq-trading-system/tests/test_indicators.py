"""Tests de los indicadores y del motor de backtesting.

Correr con:
    pip install pytest
    python -m pytest tests/ -v

Estos tests NO tocan la red: usan series sintéticas construidas a mano para
que los valores esperados sean verificables a ojo.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.backtester import Backtester, build_equity_curve, compute_metrics  # noqa: E402
from src.config import Config  # noqa: E402
from src.indicators import (  # noqa: E402
    add_indicators, atr, ema, rma, rsi, sma, support_resistance, warmup_bars,
)
from src.strategy import Bar, Context, get_strategy  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_config(**overrides) -> Config:
    data = {
        "data": {
            "ticker": "TEST", "interval": "5m", "lookback_days": 10,
            "regular_hours_only": False, "close_at_session_end": True,
            "use_cache": False,
        },
        "indicators": {
            "ema_fast": 3, "ema_slow": 5, "rsi_period": 5,
            "volume_ma_period": 5, "sr_lookback": 3,
        },
        # Bandas de RSI fuera del rango [0, 100] a propósito: así el filtro
        # nunca bloquea nada y estos tests miden solo la mecánica del motor
        # (fills, TP/SL, costos). Ojo: los límites son EXCLUSIVOS, y en una
        # tendencia pura el RSI vale exactamente 0 o 100, así que un tope de
        # 100.0 sí filtraría.
        "strategy": {"name": "ema_cross_rsi", "params": {
            "rsi_long_min": -1.0, "rsi_long_max": 101.0,
            "rsi_short_min": -1.0, "rsi_short_max": 101.0,
            "allow_long": True, "allow_short": True,
        }},
        "risk": {
            "mode": "points", "take_profit": 10.0, "stop_loss": 5.0,
            "atr_period": 5, "exit_on_opposite_signal": True,
        },
        "execution": {
            "fill": "next_open", "commission_points": 0.0,
            "slippage_points": 0.0, "point_value_usd": 20.0,
            "contracts": 1, "initial_capital_usd": 10000.0,
        },
        "output": {"dir": "outputs"},
    }
    for dotted, value in overrides.items():
        node = data
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return Config(data)


def make_ohlcv(closes: list[float], *, freq: str = "5min") -> pd.DataFrame:
    """DataFrame OHLCV sintético a partir de una lista de cierres."""
    idx = pd.date_range("2026-01-05 09:30", periods=len(closes), freq=freq,
                        tz="America/New_York")
    close = np.array(closes, dtype=float)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.full(len(close), 1000.0),
        },
        index=idx,
    )


# ---------------------------------------------------------------------------
# EMA / SMA / RMA
# ---------------------------------------------------------------------------

def test_ema_recursion_and_seed():
    """EMA arranca en el primer valor (no en la SMA) y enmascara n-1 barras."""
    s = pd.Series([10.0, 11, 12, 11, 13])
    out = ema(s, 3)

    assert out[:2].isna().all(), "las primeras n-1 barras deben ser NaN"

    alpha = 2 / (3 + 1)
    expected = 10.0
    for v in [11.0, 12.0]:
        expected = alpha * v + (1 - alpha) * expected
    assert out.iloc[2] == pytest.approx(expected)


def test_ema_of_constant_series_is_the_constant():
    out = ema(pd.Series([42.0] * 10), 4)
    assert out.dropna().eq(42.0).all()


def test_sma_matches_manual_mean():
    s = pd.Series([1.0, 2, 3, 4, 5])
    out = sma(s, 3)
    assert out[:2].isna().all()
    assert out.iloc[2] == pytest.approx(2.0)
    assert out.iloc[4] == pytest.approx(4.0)


def test_rma_uses_wilder_alpha_not_ema_alpha():
    """RMA usa alpha=1/n; una EMA de igual período usa 2/(n+1). No son lo mismo."""
    s = pd.Series([10.0, 20, 30, 40, 50, 60])
    assert rma(s, 3).iloc[-1] != pytest.approx(ema(s, 3).iloc[-1])


# ---------------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------------

def test_rsi_pure_uptrend_is_100():
    s = pd.Series(np.arange(1.0, 21.0))
    assert rsi(s, 14).dropna().eq(100.0).all()


def test_rsi_pure_downtrend_is_zero():
    s = pd.Series(np.arange(20.0, 0.0, -1.0))
    assert rsi(s, 14).dropna().eq(0.0).all()


def test_rsi_flat_series_is_50_by_convention():
    """Sin ganancias ni pérdidas el RS es 0/0; por convención RSI = 50."""
    assert rsi(pd.Series([100.0] * 20), 14).dropna().eq(50.0).all()


def test_rsi_stays_within_bounds():
    rng = np.random.default_rng(42)
    s = pd.Series(100 + np.cumsum(rng.standard_normal(300)))
    values = rsi(s, 14).dropna()
    assert values.between(0, 100).all()


def test_rsi_first_valid_index_is_period():
    """delta[0] es NaN, así que el primer RSI válido cae en el índice n."""
    rng = np.random.default_rng(0)
    s = pd.Series(100 + np.cumsum(rng.standard_normal(30)))
    assert rsi(s, 5).first_valid_index() == 5


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------

def test_atr_of_constant_range_equals_that_range():
    df = make_ohlcv([100.0] * 20)   # high-low = 2 en cada vela
    assert atr(df, 5).dropna().iloc[-1] == pytest.approx(2.0)


def test_atr_is_never_negative():
    rng = np.random.default_rng(7)
    df = make_ohlcv(list(100 + np.cumsum(rng.standard_normal(100))))
    assert (atr(df, 14).dropna() >= 0).all()


# ---------------------------------------------------------------------------
# Soportes / resistencias — el punto clave es que NO miren al futuro
# ---------------------------------------------------------------------------

def test_support_resistance_does_not_look_ahead():
    """El nivel en la vela i solo puede venir de pivots ya confirmados en i.

    Un pivot en la vela p recién se confirma en p+lookback, así que el nivel
    disponible en i tiene que provenir de una vela <= i - lookback.
    """
    rng = np.random.default_rng(3)
    df = make_ohlcv(list(100 + np.cumsum(rng.standard_normal(200))))
    lookback = 5
    sr = support_resistance(df, lookback)

    for i in range(len(df)):
        level = sr["resistance"].iloc[i]
        if pd.isna(level):
            continue
        # El nivel tiene que existir entre los highs de velas <= i - lookback.
        visible = df["high"].iloc[: max(0, i - lookback + 1)]
        assert (visible == level).any(), (
            f"la resistencia en la vela {i} no existe entre las velas visibles"
        )


# ---------------------------------------------------------------------------
# add_indicators / warmup
# ---------------------------------------------------------------------------

def test_add_indicators_creates_expected_columns():
    cfg = make_config()
    df = add_indicators(make_ohlcv(list(np.arange(100.0, 160.0))), cfg)
    for col in ("ema_fast", "ema_slow", "ema_spread", "rsi", "atr",
                "volume_ma", "volume_ratio", "resistance", "support"):
        assert col in df.columns


def test_warmup_covers_the_longest_period():
    cfg = make_config()
    # sr_lookback=3 -> ventana 2*3+1 = 7; el máximo período es 7, +5 de colchón
    assert warmup_bars(cfg) >= 2 * 3 + 1


# ---------------------------------------------------------------------------
# Estrategia
# ---------------------------------------------------------------------------

def _bar(**ind) -> Bar:
    return Bar(timestamp=pd.Timestamp("2026-01-05 10:00"), open=100, high=101,
               low=99, close=100, volume=1000, indicators=ind)


def test_strategy_fires_long_on_cross_up():
    strat = get_strategy("ema_cross_rsi", {"rsi_long_min": 50, "rsi_long_max": 70})
    prev = _bar(ema_fast=99.0, ema_slow=100.0)     # spread negativo
    bar = _bar(ema_fast=101.0, ema_slow=100.0, rsi=60.0)  # cruza hacia arriba
    assert strat.evaluate(Context(bar=bar, prev=prev, position=None,
                                  params=strat.params)).type == "LONG"


def test_strategy_respects_rsi_ceiling():
    """Cruce alcista pero RSI en sobrecompra: el filtro lo tiene que bloquear."""
    strat = get_strategy("ema_cross_rsi", {"rsi_long_min": 50, "rsi_long_max": 70})
    prev = _bar(ema_fast=99.0, ema_slow=100.0)
    bar = _bar(ema_fast=101.0, ema_slow=100.0, rsi=85.0)
    assert strat.evaluate(Context(bar=bar, prev=prev, position=None,
                                  params=strat.params)).type == "NONE"


def test_strategy_ignores_trend_without_a_fresh_cross():
    """ema_fast > ema_slow ya venía de antes: no es un cruce, no es señal."""
    strat = get_strategy("ema_cross_rsi", {"rsi_long_min": 0, "rsi_long_max": 100})
    prev = _bar(ema_fast=105.0, ema_slow=100.0)
    bar = _bar(ema_fast=106.0, ema_slow=100.0, rsi=60.0)
    assert strat.evaluate(Context(bar=bar, prev=prev, position=None,
                                  params=strat.params)).type == "NONE"


def test_strategy_returns_none_during_warmup():
    strat = get_strategy("ema_cross_rsi", {})
    bar = _bar(ema_fast=None, ema_slow=None, rsi=None)
    assert strat.evaluate(Context(bar=bar, prev=None, position=None,
                                  params=strat.params)).type == "NONE"


# ---------------------------------------------------------------------------
# Backtester
# ---------------------------------------------------------------------------

def test_backtester_take_profit_exit_and_pnl():
    """Sube en línea recta: entra long y sale por TP con el P&L exacto."""
    cfg = make_config()
    closes = [100.0] * 20 + list(np.arange(100.0, 140.0, 1.0))
    df = add_indicators(make_ohlcv(closes), cfg)

    engine = Backtester(cfg, get_strategy("ema_cross_rsi", cfg.get("strategy.params")))
    trades = engine.run(df)

    assert len(trades) >= 1
    first = trades.iloc[0]
    assert first["direction"] == "long"
    assert first["exit_reason"] == "TP"
    # Sin costos configurados, el neto es exactamente el take profit.
    assert first["points"] == pytest.approx(10.0)
    assert first["pnl_usd"] == pytest.approx(200.0)   # 10 pts * $20


def test_costs_are_deducted_from_every_trade():
    cfg = make_config(**{
        "execution.commission_points": 0.25,
        "execution.slippage_points": 0.5,
    })
    closes = [100.0] * 20 + list(np.arange(100.0, 140.0, 1.0))
    df = add_indicators(make_ohlcv(closes), cfg)

    engine = Backtester(cfg, get_strategy("ema_cross_rsi", cfg.get("strategy.params")))
    trades = engine.run(df)

    first = trades.iloc[0]
    assert first["costs_points"] == pytest.approx(1.5)  # (0.25+0.5) ida y vuelta
    assert first["points"] == pytest.approx(first["points_gross"] - 1.5)


def test_entry_fills_at_next_bar_open_not_signal_close():
    """Con fill=next_open la entrada NO puede ser el cierre de la vela señal."""
    cfg = make_config()
    closes = [100.0] * 20 + list(np.arange(100.0, 140.0, 1.0))
    df = add_indicators(make_ohlcv(closes), cfg)

    engine = Backtester(cfg, get_strategy("ema_cross_rsi", cfg.get("strategy.params")))
    trades = engine.run(df)

    entry_time = trades.iloc[0]["entry_time"]
    entry_price = trades.iloc[0]["entry_price"]
    # El precio de entrada tiene que ser el OPEN de la vela de entrada.
    assert entry_price == pytest.approx(float(df.loc[entry_time, "open"]))


def test_ambiguous_tp_and_sl_in_same_bar_resolves_as_stop():
    """Si la vela toca TP y SL, se asume el peor caso (SL) y se contabiliza."""
    cfg = make_config(**{"risk.take_profit": 2.0, "risk.stop_loss": 2.0})
    closes = [100.0] * 20 + list(np.arange(100.0, 130.0, 1.0))
    df = add_indicators(make_ohlcv(closes), cfg)
    # Ensanchamos las velas para que cada una cubra TP y SL a la vez.
    df["high"] = df["close"] + 10.0
    df["low"] = df["close"] - 10.0

    engine = Backtester(cfg, get_strategy("ema_cross_rsi", cfg.get("strategy.params")))
    trades = engine.run(df)

    assert engine.ambiguous_bars > 0
    assert (trades["exit_reason"] == "SL").any()


def test_mae_never_exceeds_the_stop_distance():
    """MAE mayor que el stop sería imposible: ya estaríamos afuera."""
    cfg = make_config(**{"risk.stop_loss": 5.0})
    rng = np.random.default_rng(11)
    closes = list(100 + np.cumsum(rng.standard_normal(400)))
    df = add_indicators(make_ohlcv(closes), cfg)

    engine = Backtester(cfg, get_strategy("ema_cross_rsi", cfg.get("strategy.params")))
    trades = engine.run(df)

    stopped = trades[trades["exit_reason"] == "SL"]
    if not stopped.empty:
        assert (stopped["mae_points"].abs() <= 5.0 + 1e-9).all()


def test_no_position_is_left_open_at_the_end():
    cfg = make_config()
    rng = np.random.default_rng(5)
    closes = list(100 + np.cumsum(rng.standard_normal(300)))
    df = add_indicators(make_ohlcv(closes), cfg)

    engine = Backtester(cfg, get_strategy("ema_cross_rsi", cfg.get("strategy.params")))
    trades = engine.run(df)

    assert not trades.empty
    assert trades["exit_time"].notna().all()
    # Todas las salidas tienen que ser posteriores o iguales a su entrada.
    assert (trades["exit_time"] >= trades["entry_time"]).all()


# ---------------------------------------------------------------------------
# Métricas
# ---------------------------------------------------------------------------

def test_metrics_win_rate_and_profit_factor():
    trades = pd.DataFrame({
        "trade_id": [1, 2, 3, 4],
        "direction": ["long"] * 4,
        "entry_time": pd.to_datetime(["2026-01-05 10:00", "2026-01-05 11:00",
                                      "2026-01-05 12:00", "2026-01-05 13:00"]),
        "exit_time": pd.to_datetime(["2026-01-05 10:30", "2026-01-05 11:30",
                                     "2026-01-05 12:30", "2026-01-05 13:30"]),
        "points": [10.0, -5.0, 10.0, -5.0],
        "points_gross": [10.0, -5.0, 10.0, -5.0],
        "costs_points": [0.0] * 4,
        "pnl_usd": [200.0, -100.0, 200.0, -100.0],
        "exit_reason": ["TP", "SL", "TP", "SL"],
        "bars_held": [3, 2, 3, 2],
        "mae_points": [-1.0, -5.0, -1.0, -5.0],
        "mfe_points": [10.0, 1.0, 10.0, 1.0],
    })
    equity = build_equity_curve(trades, 10000.0)
    m = compute_metrics(trades, equity, initial_capital=10000.0)

    assert m["trades_total"] == 4
    assert m["win_rate_pct"] == pytest.approx(50.0)
    assert m["profit_factor"] == pytest.approx(2.0)   # 400 ganado / 200 perdido
    assert m["total_points"] == pytest.approx(10.0)


def test_metrics_handle_zero_trades_without_crashing():
    empty = pd.DataFrame(columns=["points", "pnl_usd", "exit_reason"])
    m = compute_metrics(empty, pd.DataFrame(), initial_capital=10000.0)
    assert m["trades_total"] == 0
    assert "note" in m


def test_drawdown_is_negative_or_zero():
    trades = pd.DataFrame({
        "trade_id": [1, 2, 3],
        "direction": ["long"] * 3,
        "entry_time": pd.to_datetime(["2026-01-05 10:00", "2026-01-05 11:00",
                                      "2026-01-05 12:00"]),
        "exit_time": pd.to_datetime(["2026-01-05 10:30", "2026-01-05 11:30",
                                     "2026-01-05 12:30"]),
        "points": [10.0, -20.0, 5.0],
        "pnl_usd": [200.0, -400.0, 100.0],
    })
    equity = build_equity_curve(trades, 10000.0)
    assert (equity["drawdown_usd"] <= 0).all()
    assert equity["drawdown_usd"].min() == pytest.approx(-400.0)
