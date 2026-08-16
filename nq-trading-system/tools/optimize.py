#!/usr/bin/env python3
"""Optimización de parámetros con validación fuera de muestra.

Por qué este script existe
--------------------------
El README te dice "testeá fuera de muestra" y sin una herramienta eso no se
hace. Peor: probar parámetros a mano hasta que el resultado da lindo es la
forma más rápida de construir una estrategia que funciona perfecto en el
pasado y pierde plata en vivo.

Qué hace distinto a un grid search común
----------------------------------------
1. **Parte la serie en dos.** Optimiza sobre el primer 70 % (in-sample) y
   después mide esos mismos parámetros sobre el 30 % final (out-of-sample),
   que nunca vio. Si el resultado se desploma, era curve fitting.

2. **Busca mesetas, no picos.** El mejor combo de una grilla casi siempre es
   ruido: un valor que dio bien por casualidad rodeado de vecinos malos. Un
   parámetro con edge real forma una MESETA — sus vecinos también dan bien.
   El script calcula un "score de robustez" promediando cada celda con sus
   vecinas y lo reporta al lado del resultado crudo.

3. **Descarta muestras chicas.** Un combo con 6 trades y profit factor 8.0 no
   dice nada. Se filtra por cantidad mínima de trades.

4. **Da un veredicto explícito** sobre si hubo sobreajuste, en vez de dejarte
   interpretar una tabla.

Uso:
    python tools/optimize.py
    python tools/optimize.py --tp 20,30,40,60 --sl 10,15,20 --min-trades 30
    python tools/optimize.py --ema-fast 5,9 --ema-slow 21,34 --split 0.6

ADVERTENCIA: optimizar sobre 55 días de datos encuentra ruido, no edge. Esta
herramienta sirve para DESCARTAR parámetros malos y detectar sobreajuste, no
para elegir "los mejores". Si el out-of-sample no acompaña, la respuesta es
tirar la estrategia, no seguir buscando parámetros.
"""

from __future__ import annotations

import argparse
import itertools
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from src.backtester import Backtester, build_equity_curve, compute_metrics  # noqa: E402
from src.config import load_config, output_dir  # noqa: E402
from src.data_fetcher import fetch_ohlcv  # noqa: E402
from src.indicators import add_indicators  # noqa: E402
from src.strategy import get_strategy  # noqa: E402


def parse_list(raw: str, cast=float) -> list:
    return [cast(x.strip()) for x in raw.split(",") if x.strip()]


def run_segment(cfg, df_segment: pd.DataFrame) -> dict:
    """Backtestea un tramo ya con indicadores calculados."""
    strategy = get_strategy(
        cfg.require("strategy.name"), cfg.section("strategy").get("params", {})
    )
    engine = Backtester(cfg, strategy)
    try:
        trades = engine.run(df_segment)
    except ValueError:
        # Tramo demasiado corto para el warm-up de estos parámetros.
        return {"trades_total": 0}

    initial = float(cfg.get("execution.initial_capital_usd", 25000.0))
    equity = build_equity_curve(trades, initial)
    return compute_metrics(
        trades, equity,
        initial_capital=initial,
        ambiguous_bars=engine.ambiguous_bars,
        bars_total=len(df_segment),
    )


def neighbourhood_score(
    results: pd.DataFrame, axes: list[str], metric: str
) -> pd.Series:
    """Promedia cada combo con sus vecinos inmediatos en la grilla.

    Un pico aislado (bueno pero rodeado de malos) baja su score; una meseta
    (bueno y con vecinos buenos) lo mantiene. Es la diferencia entre haber
    encontrado ruido y haber encontrado algo estable.

    Vecino = mismo combo salvo en UN eje, y en ese eje una posición adyacente
    dentro de los valores probados.
    """
    if results.empty:
        return pd.Series(dtype=float)

    # Índice posicional de cada valor probado, por eje.
    order = {ax: sorted(results[ax].unique()) for ax in axes}
    index_of = {ax: {v: i for i, v in enumerate(order[ax])} for ax in axes}

    key_to_value = {
        tuple(row[ax] for ax in axes): row[metric]
        for _, row in results.iterrows()
    }

    scores = []
    for _, row in results.iterrows():
        coords = [index_of[ax][row[ax]] for ax in axes]
        gathered = [row[metric]]

        for dim, ax in enumerate(axes):
            for step in (-1, 1):
                pos = coords[dim] + step
                if not (0 <= pos < len(order[ax])):
                    continue
                neighbour = list(coords)
                neighbour[dim] = pos
                key = tuple(order[a][neighbour[d]] for d, a in enumerate(axes))
                if key in key_to_value:
                    gathered.append(key_to_value[key])

        scores.append(float(np.mean(gathered)))

    return pd.Series(scores, index=results.index)


def plot_heatmap(results: pd.DataFrame, out_path: Path, metric: str = "oos_profit_factor"):
    """Mapa de calor TP × SL del profit factor fuera de muestra.

    Escala divergente centrada en 1.0, que es el punto neutro real del profit
    factor: por encima gana, por debajo pierde. Una escala secuencial acá
    mentiría, porque escondería dónde está la línea entre ganar y perder.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm

    SURFACE, INK, INK_SEC, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
    BLUE, GRAY, RED = "#2a78d6", "#f0efec", "#d03b3b"

    pivot = results.pivot_table(
        index="stop_loss", columns="take_profit", values=metric, aggfunc="mean"
    )
    if pivot.empty or pivot.isna().all().all():
        return None

    cmap = LinearSegmentedColormap.from_list("pf", [RED, GRAY, BLUE])
    values = pivot.to_numpy(dtype=float)
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return None

    vmin, vmax = float(np.nanmin(finite)), float(np.nanmax(finite))
    # TwoSlopeNorm exige vmin < centro < vmax.
    norm = TwoSlopeNorm(vmin=min(vmin, 0.99), vcenter=1.0, vmax=max(vmax, 1.01))

    fig, ax = plt.subplots(figsize=(9, 6), facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    im = ax.imshow(values, cmap=cmap, norm=norm, aspect="auto", origin="lower")

    ax.set_xticks(range(len(pivot.columns)))
    ax.set_xticklabels([f"{c:g}" for c in pivot.columns])
    ax.set_yticks(range(len(pivot.index)))
    ax.set_yticklabels([f"{i:g}" for i in pivot.index])
    ax.set_xlabel("Take profit (puntos)", color=INK_SEC, fontsize=10)
    ax.set_ylabel("Stop loss (puntos)", color=INK_SEC, fontsize=10)
    ax.set_title(
        "Profit factor FUERA DE MUESTRA por combinación TP/SL\n"
        "azul = gana · gris = 1.0 (punto muerto) · rojo = pierde",
        color=INK, fontsize=12, fontweight="bold", loc="left", pad=14,
    )
    ax.tick_params(colors=MUTED, labelsize=9, length=0)
    for side in ax.spines.values():
        side.set_visible(False)

    # Etiqueta directa en cada celda: la grilla es chica, el número exacto
    # importa más que adivinarlo del color.
    for i in range(values.shape[0]):
        for j in range(values.shape[1]):
            v = values[i, j]
            if not np.isfinite(v):
                continue
            ax.text(j, i, f"{v:.2f}", ha="center", va="center",
                    fontsize=9, color=INK,
                    fontweight="bold" if v > 1.0 else "normal")

    cbar = fig.colorbar(im, ax=ax, fraction=0.04, pad=0.03)
    cbar.set_label("Profit factor (OOS)", color=INK_SEC, fontsize=9)
    cbar.ax.tick_params(colors=MUTED, labelsize=8)
    cbar.outline.set_visible(False)

    fig.savefig(out_path, dpi=140, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)
    return out_path


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Grid search con validación out-of-sample y análisis de robustez",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--config", default=None)
    ap.add_argument("--ticker", default=None)
    ap.add_argument("--interval", default=None)
    ap.add_argument("--lookback", type=int, default=None)
    ap.add_argument("--ema-fast", default="5,9,13", help="Valores de EMA rápida")
    ap.add_argument("--ema-slow", default="21,34", help="Valores de EMA lenta")
    ap.add_argument("--tp", default="20,30,40,60", help="Take profits (puntos)")
    ap.add_argument("--sl", default="10,15,20,30", help="Stop losses (puntos)")
    ap.add_argument("--split", type=float, default=0.7,
                    help="Fracción in-sample (default 0.7 = 70/30)")
    ap.add_argument("--min-trades", type=int, default=20,
                    help="Descartar combos con menos trades que esto (por tramo)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.ticker:
        cfg.set("data.ticker", args.ticker)
    if args.interval:
        cfg.set("data.interval", args.interval)
    if args.lookback:
        cfg.set("data.lookback_days", args.lookback)

    ema_fast_vals = parse_list(args.ema_fast, int)
    ema_slow_vals = parse_list(args.ema_slow, int)
    tp_vals = parse_list(args.tp, float)
    sl_vals = parse_list(args.sl, float)

    combos = [
        (ef, es, tp, sl)
        for ef, es, tp, sl in itertools.product(
            ema_fast_vals, ema_slow_vals, tp_vals, sl_vals
        )
        if ef < es  # una EMA "rápida" más lenta que la "lenta" no tiene sentido
    ]

    print("=" * 72)
    print("  OPTIMIZACIÓN CON VALIDACIÓN FUERA DE MUESTRA")
    print("=" * 72)
    print(f"  Ticker      : {cfg.get('data.ticker')} @ {cfg.get('data.interval')}")
    print(f"  Estrategia  : {cfg.get('strategy.name')}")
    print(f"  Grilla      : {len(combos)} combinaciones")
    print(f"                EMA rápida {ema_fast_vals} × lenta {ema_slow_vals}")
    print(f"                TP {tp_vals} × SL {sl_vals}")
    print(f"  Split       : {args.split:.0%} in-sample / {1-args.split:.0%} out-of-sample")
    print(f"  Mín. trades : {args.min_trades} por tramo")
    print("-" * 72)

    raw = fetch_ohlcv(cfg, strict_limits=False)

    split_at = int(len(raw) * args.split)
    print(f"  [split] in-sample : {raw.index[0]:%Y-%m-%d %H:%M} → "
          f"{raw.index[split_at-1]:%Y-%m-%d %H:%M}  ({split_at} velas)")
    print(f"  [split] out-sample: {raw.index[split_at]:%Y-%m-%d %H:%M} → "
          f"{raw.index[-1]:%Y-%m-%d %H:%M}  ({len(raw)-split_at} velas)")
    print("-" * 72)

    rows = []
    start = time.time()
    last_indicator_key = None
    df_ind = None
    is_tty = sys.stdout.isatty()

    # Ordenamos por (ema_fast, ema_slow) para recalcular los indicadores solo
    # cuando cambian de verdad: TP y SL no los afectan.
    for n, (ef, es, tp, sl) in enumerate(
        sorted(combos, key=lambda c: (c[0], c[1])), start=1
    ):
        cfg.set("indicators.ema_fast", ef)
        cfg.set("indicators.ema_slow", es)
        cfg.set("risk.take_profit", tp)
        cfg.set("risk.stop_loss", sl)

        key = (ef, es)
        if key != last_indicator_key:
            # Indicadores sobre la serie COMPLETA y después se parte. Como
            # todos son causales (el valor en i solo usa velas <= i), esto no
            # filtra información del futuro hacia el in-sample.
            df_ind = add_indicators(raw, cfg)
            last_indicator_key = key

        is_metrics = run_segment(cfg, df_ind.iloc[:split_at])
        oos_metrics = run_segment(cfg, df_ind.iloc[split_at:])

        rows.append({
            "ema_fast": ef, "ema_slow": es,
            "take_profit": tp, "stop_loss": sl,
            "rr_ratio": round(tp / sl, 2),
            "is_trades": is_metrics.get("trades_total", 0),
            "is_win_rate": is_metrics.get("win_rate_pct", 0.0),
            "is_profit_factor": is_metrics.get("profit_factor") or 0.0,
            "is_points": is_metrics.get("total_points", 0.0),
            "is_max_dd_pct": is_metrics.get("max_drawdown_pct", 0.0),
            "oos_trades": oos_metrics.get("trades_total", 0),
            "oos_win_rate": oos_metrics.get("win_rate_pct", 0.0),
            "oos_profit_factor": oos_metrics.get("profit_factor") or 0.0,
            "oos_points": oos_metrics.get("total_points", 0.0),
            "oos_max_dd_pct": oos_metrics.get("max_drawdown_pct", 0.0),
        })

        if n % 10 == 0 or n == len(combos):
            elapsed = time.time() - start
            remaining = (elapsed / n) * (len(combos) - n)
            line = (f"  [{n:>3}/{len(combos)}] {elapsed:5.1f}s transcurridos · "
                    f"~{remaining:5.1f}s restantes")
            # Con \r en una terminal la línea se reescribe sola; si la salida
            # está redirigida a un archivo o a un pipe, \r no borra nada y
            # queda todo concatenado en un renglón ilegible.
            if is_tty:
                print(line, end="\r", flush=True)
            elif n == len(combos):
                print(line.strip())

    if is_tty:
        print(" " * 72, end="\r")
    results = pd.DataFrame(rows)

    # --- Filtro de muestra mínima ---------------------------------------
    valid = results[
        (results["is_trades"] >= args.min_trades)
        & (results["oos_trades"] >= max(5, args.min_trades // 3))
    ].copy()

    print(f"  {len(results)} combos evaluados en {time.time()-start:.1f}s · "
          f"{len(valid)} pasan el filtro de muestra mínima")

    out = output_dir(cfg)
    results_path = out / "optimization_results.csv"
    results.to_csv(results_path, index=False)

    if valid.empty:
        print("\n  Ningún combo alcanzó el mínimo de trades. Bajá --min-trades "
              "o ampliá el rango de fechas.")
        print(f"  Resultados crudos en {results_path}\n")
        return 0

    # --- Robustez: ¿pico aislado o meseta? --------------------------------
    axes = ["ema_fast", "ema_slow", "take_profit", "stop_loss"]
    valid["is_robustness"] = neighbourhood_score(valid, axes, "is_profit_factor")
    valid["oos_robustness"] = neighbourhood_score(valid, axes, "oos_profit_factor")
    valid["overfit_gap"] = valid["is_profit_factor"] - valid["oos_profit_factor"]

    valid = valid.sort_values("is_profit_factor", ascending=False)
    valid.to_csv(out / "optimization_ranked.csv", index=False)

    # --- Reporte ----------------------------------------------------------
    print("\n" + "=" * 72)
    print("  TOP 10 SEGÚN IN-SAMPLE — y qué pasó después, fuera de muestra")
    print("=" * 72)
    header = (
        f"  {'EMA':>7} {'TP/SL':>9} {'R:R':>5} │ "
        f"{'IS PF':>6} {'IS tr':>6} │ {'OOS PF':>7} {'OOS tr':>7} │ "
        f"{'meseta':>7} {'caída':>7}"
    )
    print(header)
    print("  " + "─" * 70)
    for _, r in valid.head(10).iterrows():
        holds = "✓" if r["oos_profit_factor"] >= 1.0 else "✗"
        print(
            f"  {int(r['ema_fast']):>3}/{int(r['ema_slow']):<3} "
            f"{r['take_profit']:>4.0f}/{r['stop_loss']:<4.0f} "
            f"{r['rr_ratio']:>5.2f} │ "
            f"{r['is_profit_factor']:>6.2f} {int(r['is_trades']):>6} │ "
            f"{r['oos_profit_factor']:>6.2f}{holds} {int(r['oos_trades']):>7} │ "
            f"{r['oos_robustness']:>7.2f} {r['overfit_gap']:>+7.2f}"
        )

    # --- Veredicto --------------------------------------------------------
    best_is = valid.iloc[0]
    n_profitable_is = int((valid["is_profit_factor"] > 1.0).sum())
    n_profitable_oos = int((valid["oos_profit_factor"] > 1.0).sum())
    top10 = valid.head(10)
    n_top10_hold = int((top10["oos_profit_factor"] >= 1.0).sum())

    corr = None
    if len(valid) > 3 and valid["is_profit_factor"].std() > 0:
        corr = float(valid["is_profit_factor"].corr(valid["oos_profit_factor"]))

    print("\n" + "=" * 72)
    print("  VEREDICTO")
    print("=" * 72)
    print(f"  Combos rentables in-sample .......... "
          f"{n_profitable_is}/{len(valid)} ({n_profitable_is/len(valid):.0%})")
    print(f"  Combos rentables out-of-sample ...... "
          f"{n_profitable_oos}/{len(valid)} ({n_profitable_oos/len(valid):.0%})")
    print(f"  Del top 10 in-sample, aguantan OOS .. {n_top10_hold}/10")
    if corr is not None:
        print(f"  Correlación IS ↔ OOS del PF ......... {corr:+.3f}")

    print(f"\n  Mejor combo in-sample: EMA {int(best_is['ema_fast'])}/"
          f"{int(best_is['ema_slow'])}, TP {best_is['take_profit']:.0f} / "
          f"SL {best_is['stop_loss']:.0f}")
    print(f"    in-sample  : PF {best_is['is_profit_factor']:.2f} · "
          f"{int(best_is['is_trades'])} trades · "
          f"{best_is['is_points']:+.1f} pts")
    print(f"    out-sample : PF {best_is['oos_profit_factor']:.2f} · "
          f"{int(best_is['oos_trades'])} trades · "
          f"{best_is['oos_points']:+.1f} pts")

    print("\n  Lectura:")
    if n_top10_hold <= 3:
        print("    ✗ SOBREAJUSTE. Lo que mejor anduvo in-sample se desarma fuera")
        print("      de muestra. Los parámetros están describiendo el ruido de")
        print("      este tramo puntual, no una regularidad del mercado.")
        print("      No sirve buscar más parámetros: el problema es la estrategia.")
    elif corr is not None and corr < 0.2:
        print("    ✗ El resultado in-sample no predice el out-of-sample")
        print(f"      (correlación {corr:+.2f}). Elegir parámetros por el")
        print("      backtest, con esta estrategia, es tirar una moneda.")
    elif n_profitable_oos / len(valid) < 0.3:
        print("    ⚠ Pocos combos sobreviven fuera de muestra. Si alguno anda,")
        print("      fijate que forme MESETA (columna 'meseta' alta), no que sea")
        print("      un pico aislado.")
    else:
        print("    ✓ Buena parte de los combos aguanta fuera de muestra. Elegí")
        print("      por la columna 'meseta', no por el mejor PF in-sample:")
        print("      un pico aislado casi siempre es ruido.")

    print("\n  Recordá: son ~55 días de datos. Ni el mejor resultado de acá es")
    print("  evidencia suficiente. Esto sirve para DESCARTAR, no para elegir.")

    # --- Gráfico ----------------------------------------------------------
    chart_path = None
    try:
        best_ema = valid.iloc[0]
        subset = valid[
            (valid["ema_fast"] == best_ema["ema_fast"])
            & (valid["ema_slow"] == best_ema["ema_slow"])
        ]
        if len(subset) >= 4:
            chart_path = plot_heatmap(subset, out / "optimization_heatmap.png")
    except Exception as exc:
        print(f"\n  [!] No pude generar el mapa de calor: {exc}")

    print("\n  ARCHIVOS GENERADOS")
    print(f"    todos los combos -> {results_path}")
    print(f"    ranking          -> {out / 'optimization_ranked.csv'}")
    if chart_path:
        print(f"    mapa de calor    -> {chart_path}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
