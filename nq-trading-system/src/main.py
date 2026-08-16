"""Punto de entrada del backtester.

Uso:
    python -m src.main
    python -m src.main --ticker ES=F --interval 15m --lookback 30
    python -m src.main --strategy rsi_reversion
    python -m src.main --config mi_config.yaml --no-cache

Los flags de CLI pisan a config.yaml, que a su vez pisa a los defaults.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from .backtester import (
    Backtester,
    build_equity_curve,
    compute_metrics,
    finalize_costs,
)
from .config import Config, load_config, output_dir
from .data_fetcher import DataFetchError, DataRangeError, fetch_ohlcv
from .indicators import add_indicators, warmup_bars
from .strategy import STRATEGY_REGISTRY, get_strategy


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m src.main",
        description="Backtester intradiario para futuros de índices (NQ, ES, ...)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Ejemplos:\n"
            "  python -m src.main\n"
            "  python -m src.main --ticker QQQ --interval 15m --lookback 60\n"
            "  python -m src.main --strategy rsi_reversion --no-cache\n"
        ),
    )
    p.add_argument("--config", default=None, help="Ruta al config.yaml")
    p.add_argument("--ticker", default=None, help="Ticker de Yahoo (ej: NQ=F)")
    p.add_argument("--interval", default=None, help="Timeframe (1m, 5m, 15m, 1h...)")
    p.add_argument("--lookback", type=int, default=None, help="Días hacia atrás")
    p.add_argument("--start", default=None, help="Fecha inicio YYYY-MM-DD")
    p.add_argument("--end", default=None, help="Fecha fin YYYY-MM-DD")
    p.add_argument(
        "--strategy", default=None,
        help=f"Estrategia a usar. Registradas: {sorted(STRATEGY_REGISTRY)}",
    )
    p.add_argument("--no-cache", action="store_true", help="Ignorar la caché local")
    p.add_argument(
        "--allow-range-overflow", action="store_true",
        help="Advertir en vez de abortar si el rango excede el límite de yfinance",
    )
    p.add_argument(
        "--list-strategies", action="store_true",
        help="Listar las estrategias registradas y salir",
    )
    return p.parse_args(argv)


def apply_cli_overrides(cfg: Config, args: argparse.Namespace) -> None:
    if args.ticker:
        cfg.set("data.ticker", args.ticker)
    if args.interval:
        cfg.set("data.interval", args.interval)
    if args.lookback:
        cfg.set("data.lookback_days", args.lookback)
        cfg.set("data.start", None)
        cfg.set("data.end", None)
    if args.start:
        cfg.set("data.start", args.start)
    if args.end:
        cfg.set("data.end", args.end)
    if args.strategy:
        cfg.set("strategy.name", args.strategy)
    if args.no_cache:
        cfg.set("data.use_cache", False)


def print_metrics(metrics: dict) -> None:
    """Resumen legible en consola."""
    print("\n" + "=" * 68)
    print("  MÉTRICAS DEL BACKTEST")
    print("=" * 68)

    if metrics.get("trades_total", 0) == 0:
        print(f"  {metrics.get('note', 'Sin trades.')}")
        print("=" * 68)
        return

    def row(label: str, value, suffix: str = "") -> None:
        print(f"  {label:.<38} {value}{suffix}")

    print("\n  ACTIVIDAD")
    row("Trades totales", metrics["trades_total"])
    row("  long / short",
        f"{metrics['trades_long']} / {metrics['trades_short']}")
    row("Velas evaluadas", f"{metrics['bars_evaluated']:,}")
    row("Duración media", metrics["avg_bars_held"], " velas")

    print("\n  ACIERTO")
    row("Win rate", metrics["win_rate_pct"], " %")
    row("  ganadores / perdedores",
        f"{metrics['wins']} / {metrics['losses']}")
    row("Racha perdedora máxima", metrics["max_losing_streak"], " trades")

    print("\n  RENTABILIDAD")
    row("Resultado total", f"{metrics['total_points']:+,.2f}", " pts")
    row("Resultado total", f"{metrics['total_pnl_usd']:+,.2f}", " USD")
    row("Retorno total", f"{metrics['total_return_pct']:+.2f}", " %")
    pf = metrics.get("profit_factor")
    row("Profit factor", f"{pf:.3f}" if pf is not None else "∞ (sin pérdidas)")
    row("Expectativa por trade",
        f"{metrics['expectancy_points']:+.2f} pts / "
        f"{metrics['expectancy_usd']:+,.2f} USD")
    row("Ganancia media", f"{metrics['avg_win_points']:+.2f}", " pts")
    row("Pérdida media", f"{metrics['avg_loss_points']:+.2f}", " pts")

    print("\n  RIESGO")
    row("Drawdown máximo", f"{metrics['max_drawdown_usd']:,.2f}", " USD")
    row("Drawdown máximo", f"{metrics['max_drawdown_pct']:.2f}", " %")
    row("MAE media (peor excursión en contra)",
        f"{metrics['avg_mae_points']:.2f}", " pts")
    row("MFE media (mejor excursión a favor)",
        f"{metrics['avg_mfe_points']:.2f}", " pts")
    sharpe = metrics.get("trade_sharpe")
    row("Sharpe por trade", f"{sharpe:.3f}" if sharpe is not None else "n/d")

    print("\n  COSTOS")
    row("Costos totales",
        f"{metrics['total_costs_points']:.2f} pts / "
        f"{metrics['total_costs_usd']:,.2f} USD")

    print("\n  MOTIVOS DE SALIDA")
    for reason, count in sorted(
        metrics["exit_reasons"].items(), key=lambda kv: -kv[1]
    ):
        pct = count / metrics["trades_total"] * 100.0
        print(f"    {reason:.<36} {count:>4}  ({pct:.0f} %)")

    for warning in metrics.get("warnings", []):
        print(f"\n  [!] {warning}")

    print("=" * 68)


def print_trades_preview(trades: pd.DataFrame, limit: int = 10) -> None:
    if trades.empty:
        return
    print(f"\n  PRIMEROS {min(limit, len(trades))} TRADES "
          f"(la tabla completa va al CSV)")
    print("  " + "-" * 100)
    header = (
        f"  {'#':>3}  {'dir':<5}  {'entrada':<17} {'precio':>9}  "
        f"{'salida':<17} {'precio':>9}  {'pts':>8} {'%':>7}  {'motivo':<8}"
    )
    print(header)
    print("  " + "-" * 100)
    for _, t in trades.head(limit).iterrows():
        print(
            f"  {int(t['trade_id']):>3}  {t['direction']:<5}  "
            f"{t['entry_time']:%Y-%m-%d %H:%M}  {t['entry_price']:>9,.2f}  "
            f"{t['exit_time']:%Y-%m-%d %H:%M}  {t['exit_price']:>9,.2f}  "
            f"{t['points']:>+8.2f} {t['pct']:>+7.3f}  {t['exit_reason']:<8}"
        )
    print("  " + "-" * 100)


def run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    apply_cli_overrides(cfg, args)

    strategy_name = cfg.require("strategy.name")
    strategy = get_strategy(strategy_name, cfg.section("strategy").get("params", {}))

    print("\n" + "=" * 68)
    print("  BACKTEST")
    print("=" * 68)
    print(f"  Ticker     : {cfg.get('data.ticker')}")
    print(f"  Timeframe  : {cfg.get('data.interval')}")
    print(f"  Estrategia : {strategy.describe()}")
    print(f"  Riesgo     : TP {cfg.get('risk.take_profit')} / "
          f"SL {cfg.get('risk.stop_loss')} ({cfg.get('risk.mode')})")
    print(f"  Ejecución  : fill={cfg.get('execution.fill')}, "
          f"costos={cfg.get('execution.commission_points')}+"
          f"{cfg.get('execution.slippage_points')} pts por lado")
    print("-" * 68)

    # 1. Datos
    df = fetch_ohlcv(cfg, strict_limits=not args.allow_range_overflow)

    # 2. Indicadores
    df = add_indicators(df, cfg)
    warmup = warmup_bars(cfg)
    print(f"  [indicadores] warm-up = {warmup} velas "
          f"(no se opera antes de esa vela)")

    # 3. Backtest
    engine = Backtester(cfg, strategy)
    trades = engine.run(df)
    print(f"  [backtest] {len(trades)} trades cerrados")

    # 4. Equity + métricas
    initial = float(cfg.get("execution.initial_capital_usd", 25000.0))
    equity = build_equity_curve(trades, initial)
    metrics = compute_metrics(
        trades, equity,
        initial_capital=initial,
        ambiguous_bars=engine.ambiguous_bars,
        bars_total=len(df),
    )
    finalize_costs(
        metrics,
        float(cfg.get("execution.point_value_usd", 20.0)),
        int(cfg.get("execution.contracts", 1)),
    )
    metrics["config"] = {
        "ticker": cfg.get("data.ticker"),
        "interval": cfg.get("data.interval"),
        "strategy": strategy_name,
        "strategy_params": strategy.params,
        "risk": cfg.section("risk"),
        "execution": cfg.section("execution"),
        "data_start": str(df.index[0]),
        "data_end": str(df.index[-1]),
    }

    # 5. Exportar
    out = output_dir(cfg)
    trades_path = out / cfg.get("output.trades_csv", "trades.csv")
    equity_path = out / cfg.get("output.equity_csv", "equity.csv")
    metrics_path = out / cfg.get("output.metrics_json", "metrics.json")

    trades.to_csv(trades_path, index=False)
    equity.to_csv(equity_path, index=False)
    metrics_path.write_text(
        json.dumps(metrics, indent=2, default=str), encoding="utf-8"
    )

    # 6. Gráfico
    engine_name = cfg.get("output.chart_engine", "matplotlib")
    chart_path = out / cfg.get("output.chart_file", "chart.png")
    try:
        if engine_name == "plotly":
            from .plotting import plot_backtest_plotly
            chart_path = plot_backtest_plotly(df, trades, equity, cfg, chart_path)
        else:
            from .plotting import plot_backtest
            chart_path = plot_backtest(df, trades, equity, cfg, chart_path)
        chart_ok = True
    except Exception as exc:
        print(f"\n  [!] No se pudo generar el gráfico: {exc}")
        chart_ok = False

    # 7. Reporte
    print_trades_preview(trades)
    print_metrics(metrics)

    print("\n  ARCHIVOS GENERADOS")
    print(f"    trades   -> {trades_path}")
    print(f"    equity   -> {equity_path}")
    print(f"    métricas -> {metrics_path}")
    if chart_ok:
        print(f"    gráfico  -> {chart_path}")
    print()

    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.list_strategies:
        print("\nEstrategias registradas (src/strategy.py):\n")
        for name, cls in sorted(STRATEGY_REGISTRY.items()):
            doc = (cls.__doc__ or "").strip().split("\n")[0]
            print(f"  {name:<20} {doc}")
        print("\nUsala con: --strategy <nombre>, o strategy.name en config.yaml\n")
        return 0

    try:
        return run(args)
    except DataRangeError as exc:
        print(f"\n[RANGO INVÁLIDO]{exc}", file=sys.stderr)
        print(
            "Si querés correr igual con lo que Yahoo devuelva, agregá "
            "--allow-range-overflow\n",
            file=sys.stderr,
        )
        return 2
    except DataFetchError as exc:
        print(f"\n[ERROR DE DESCARGA]\n{exc}\n", file=sys.stderr)
        return 3
    except (KeyError, ValueError) as exc:
        print(f"\n[ERROR DE CONFIGURACIÓN]\n{exc}\n", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print("\nInterrumpido.\n", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
