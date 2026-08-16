"""Gráficos del backtest: precio con marcadores de entrada/salida + equity.

Reglas de diseño aplicadas (para que el gráfico se LEA, no solo se vea):

  * El precio es contexto, no una serie protagonista: va en gris fino. Las EMAs
    (las que toman la decisión) van en color. Si todo grita, nada se lee.
  * La identidad nunca depende solo del color: long/short se distinguen por
    FORMA (triángulo arriba / abajo), TP/SL por forma además de color. Así
    funciona en blanco y negro y para daltónicos.
  * Sin doble eje Y. Precio y equity son magnitudes distintas -> paneles
    separados, cada uno con su escala.
  * Grilla y ejes recesivos (hairline), etiquetas en tinta apagada.
  * EJE X ORDINAL, NO TEMPORAL. Un gráfico intradiario con eje de tiempo real
    dedica ~17 de cada 24 horas a la sesión overnight que filtramos, así que
    los huecos se comen el ancho y cada sesión queda en una franja ilegible.
    Dibujamos contra el índice de vela (0..N-1) y etiquetamos los ticks con el
    timestamp real — que es lo que hacen TradingView y cualquier plataforma.

La paleta es la de referencia, verificada con el validador de contraste/CVD:
las dos EMAs dan ΔE 24.7 bajo simulación de protanopia (mínimo exigido: 8).
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # backend sin display: necesario en servidores/CI

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .config import Config

# --- Paleta ---------------------------------------------------------------
SURFACE = "#fcfcfb"
INK_PRIMARY = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRIDLINE = "#e1e0d9"
BASELINE = "#c3c2b7"

SERIES_1 = "#2a78d6"   # azul   -> EMA rápida
SERIES_2 = "#eb6834"   # naranja-> EMA lenta
PRICE = "#898781"      # el precio es contexto

STATUS_GOOD = "#0ca30c"      # salida por Take Profit
STATUS_CRITICAL = "#d03b3b"  # salida por Stop Loss
STATUS_NEUTRAL = "#52514e"   # salida por señal / cierre de sesión


def _style_axis(ax) -> None:
    """Grilla y ejes recesivos."""
    ax.set_facecolor(SURFACE)
    ax.grid(True, color=GRIDLINE, linewidth=0.6, alpha=0.9)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(BASELINE)
        ax.spines[side].set_linewidth(0.8)
    ax.tick_params(colors=INK_MUTED, labelsize=8, length=3, width=0.8)


def _ordinal_ticks(ax, index: pd.DatetimeIndex, max_ticks: int = 12) -> None:
    """Etiqueta un eje ordinal con los timestamps reales.

    Prioriza poner un tick al comienzo de cada sesión (cambio de día); si hay
    más sesiones que `max_ticks`, muestrea de forma pareja.
    """
    if len(index) == 0:
        return

    session_starts = [0]
    for i in range(1, len(index)):
        if index[i].date() != index[i - 1].date():
            session_starts.append(i)

    if len(session_starts) > max_ticks:
        step = len(session_starts) / max_ticks
        positions = [session_starts[int(k * step)] for k in range(max_ticks)]
    elif len(session_starts) >= 3:
        positions = session_starts
    else:
        # Una o dos sesiones: los ticks marcan horas dentro del día.
        positions = list(np.linspace(0, len(index) - 1, max_ticks, dtype=int))

    # Descartar ticks demasiado juntos: tras un fin de semana o un feriado,
    # dos inicios de sesión pueden caer a pocas velas de distancia y sus
    # etiquetas se pisan.
    min_gap = max(1, len(index) // (max_ticks * 2))
    spaced: list[int] = []
    for p in positions:
        if not spaced or p - spaced[-1] >= min_gap:
            spaced.append(p)
    positions = spaced

    multi_day = len({index[p].date() for p in positions}) > 1
    fmt = "%d-%b\n%H:%M" if not multi_day else "%d-%b"

    ax.set_xticks(positions)
    ax.set_xticklabels([index[p].strftime(fmt) for p in positions])
    ax.set_xlim(-1, len(index))


def _session_separators(ax, index: pd.DatetimeIndex) -> None:
    """Línea vertical tenue en cada cambio de sesión."""
    for i in range(1, len(index)):
        if index[i].date() != index[i - 1].date():
            ax.axvline(i - 0.5, color=GRIDLINE, linewidth=0.8, zorder=0)


def plot_backtest(
    df: pd.DataFrame,
    trades: pd.DataFrame,
    equity: pd.DataFrame,
    cfg: Config,
    out_path: Path,
) -> Path:
    """Gráfico estático (PNG): precio + EMAs + marcadores, y curva de equity."""
    max_bars = int(cfg.get("output.chart_max_bars", 800))
    ticker = cfg.get("data.ticker")
    interval = cfg.get("data.interval")

    # Recortamos a las últimas N velas: con miles de velas de 5m el PNG queda
    # ilegible y los marcadores se pisan.
    view = df.tail(max_bars) if len(df) > max_bars else df
    t0, t1 = view.index[0], view.index[-1]

    # Eje ordinal: posición de cada vela. `pos` traduce timestamp -> índice.
    x = np.arange(len(view))
    pos = {ts: i for i, ts in enumerate(view.index)}

    shown = trades[
        (trades["entry_time"] >= t0) & (trades["entry_time"] <= t1)
    ] if not trades.empty else trades

    fig, (ax_price, ax_eq) = plt.subplots(
        2, 1,
        figsize=(15, 9),
        gridspec_kw={"height_ratios": [3, 1], "hspace": 0.32},
        facecolor=SURFACE,
    )

    # ------------------------------------------------------------------
    # Panel 1 — precio
    # ------------------------------------------------------------------
    _session_separators(ax_price, view.index)

    ax_price.plot(
        x, view["close"].to_numpy(),
        color=PRICE, linewidth=0.9, label="Precio (close)", zorder=1,
    )
    if "ema_fast" in view:
        ax_price.plot(
            x, view["ema_fast"].to_numpy(),
            color=SERIES_1, linewidth=2.0,
            label=f"EMA {cfg.get('indicators.ema_fast')}", zorder=3,
        )
    if "ema_slow" in view:
        ax_price.plot(
            x, view["ema_slow"].to_numpy(),
            color=SERIES_2, linewidth=2.0,
            label=f"EMA {cfg.get('indicators.ema_slow')}", zorder=3,
        )

    if not shown.empty:
        # --- Línea entrada->salida, teñida por resultado (debajo de todo) ---
        for _, tr in shown.iterrows():
            xe, xx = pos.get(tr["entry_time"]), pos.get(tr["exit_time"])
            if xe is None or xx is None:
                continue
            ax_price.plot(
                [xe, xx], [tr["entry_price"], tr["exit_price"]],
                color=STATUS_GOOD if tr["points"] > 0 else STATUS_CRITICAL,
                linewidth=1.3, alpha=0.5, zorder=4,
            )

        # --- Marcadores de entrada: la FORMA indica la dirección ---
        for direction, marker, color, label in (
            ("long", "^", SERIES_1, "Entrada long"),
            ("short", "v", SERIES_2, "Entrada short"),
        ):
            sub = shown[shown["direction"] == direction]
            xs = [pos[t] for t in sub["entry_time"] if t in pos]
            ys = [p for t, p in zip(sub["entry_time"], sub["entry_price"]) if t in pos]
            if xs:
                ax_price.scatter(
                    xs, ys, marker=marker, s=150, color=color,
                    edgecolors=SURFACE, linewidths=2.0, zorder=6, label=label,
                )

        # --- Marcadores de salida: color + forma indican el motivo ---
        exit_styles = {
            "TP":          ("o", STATUS_GOOD,     "Salida TP"),
            "SL":          ("X", STATUS_CRITICAL, "Salida SL"),
            "SIGNAL":      ("s", STATUS_NEUTRAL,  "Salida por señal"),
            "SESSION_END": ("D", STATUS_NEUTRAL,  "Cierre de sesión"),
            "EOD_DATA":    ("D", STATUS_NEUTRAL,  "Fin de datos"),
        }
        for reason, group in shown.groupby("exit_reason"):
            marker, color, label = exit_styles.get(
                str(reason), ("o", STATUS_NEUTRAL, str(reason))
            )
            xs = [pos[t] for t in group["exit_time"] if t in pos]
            ys = [p for t, p in zip(group["exit_time"], group["exit_price"]) if t in pos]
            if xs:
                ax_price.scatter(
                    xs, ys, marker=marker, s=110, color=color,
                    edgecolors=SURFACE, linewidths=1.8, zorder=6, label=label,
                )

    ax_price.set_title(
        f"{ticker} · {interval} · {len(shown)} trades en vista "
        f"({t0:%d-%b %H:%M} → {t1:%d-%b %H:%M}, {len(view)} velas)",
        color=INK_PRIMARY, fontsize=13, fontweight="bold", loc="left", pad=12,
    )
    ax_price.set_ylabel("Precio", color=INK_SECONDARY, fontsize=9)
    _style_axis(ax_price)
    _ordinal_ticks(ax_price, view.index)

    legend = ax_price.legend(
        loc="upper left", fontsize=8, framealpha=0.95,
        facecolor=SURFACE, edgecolor=GRIDLINE, ncol=2,
    )
    for text in legend.get_texts():
        text.set_color(INK_SECONDARY)

    # ------------------------------------------------------------------
    # Panel 2 — curva de equity (TODOS los trades, no solo los de la vista).
    # También en eje ordinal: un trade por posición, sin huecos de calendario.
    # ------------------------------------------------------------------
    if not equity.empty:
        eq_x = np.arange(len(equity))
        ax_eq.plot(
            eq_x, equity["equity_usd"].to_numpy(),
            color=SERIES_1, linewidth=2.0, zorder=3,
        )
        ax_eq.fill_between(
            eq_x, equity["peak_usd"].to_numpy(), equity["equity_usd"].to_numpy(),
            color=STATUS_CRITICAL, alpha=0.13, zorder=2, label="Drawdown",
        )
        initial = float(cfg.get("execution.initial_capital_usd", 25000.0))
        ax_eq.axhline(initial, color=BASELINE, linewidth=1.0, linestyle="--", zorder=1)

        # Etiqueta directa del valor final, en tinta — no en el color de la serie.
        final = float(equity["equity_usd"].iloc[-1])
        ax_eq.annotate(
            f"${final:,.0f}",
            xy=(eq_x[-1], final), xytext=(8, 0), textcoords="offset points",
            va="center", fontsize=9, fontweight="bold", color=INK_PRIMARY,
        )
        ax_eq.set_xlabel(
            "Trades en orden de cierre (ticks = fecha de salida)",
            color=INK_SECONDARY, fontsize=9,
        )
        _ordinal_ticks(
            ax_eq, pd.DatetimeIndex(equity["exit_time"]), max_ticks=10
        )
        eq_legend = ax_eq.legend(
            loc="upper left", fontsize=8, framealpha=0.95,
            facecolor=SURFACE, edgecolor=GRIDLINE,
        )
        for text in eq_legend.get_texts():
            text.set_color(INK_SECONDARY)
    else:
        ax_eq.text(
            0.5, 0.5, "Sin trades",
            ha="center", va="center", transform=ax_eq.transAxes,
            color=INK_MUTED, fontsize=11,
        )

    ax_eq.set_title(
        f"Curva de equity · {len(equity)} trades cerrados "
        f"(USD, marcada al cierre de cada trade)",
        color=INK_PRIMARY, fontsize=10, fontweight="bold", loc="left", pad=8,
    )
    ax_eq.set_ylabel("Equity USD", color=INK_SECONDARY, fontsize=9)
    _style_axis(ax_eq)

    fig.savefig(out_path, dpi=140, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)
    return out_path


def plot_backtest_plotly(
    df: pd.DataFrame,
    trades: pd.DataFrame,
    equity: pd.DataFrame,
    cfg: Config,
    out_path: Path,
) -> Path:
    """Versión interactiva (HTML) con velas japonesas y hover.

    Se activa con output.chart_engine: "plotly" en config.yaml.
    """
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError as exc:
        raise ImportError(
            "chart_engine='plotly' requiere plotly: pip install plotly"
        ) from exc

    max_bars = int(cfg.get("output.chart_max_bars", 800))
    view = df.tail(max_bars) if len(df) > max_bars else df
    t0, t1 = view.index[0], view.index[-1]
    shown = trades[
        (trades["entry_time"] >= t0) & (trades["entry_time"] <= t1)
    ] if not trades.empty else trades

    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=False,
        row_heights=[0.72, 0.28], vertical_spacing=0.10,
        subplot_titles=(
            f"{cfg.get('data.ticker')} · {cfg.get('data.interval')}",
            "Curva de equity (USD)",
        ),
    )

    fig.add_trace(
        go.Candlestick(
            x=view.index, open=view["open"], high=view["high"],
            low=view["low"], close=view["close"], name="Precio",
            increasing_line_color=STATUS_GOOD, decreasing_line_color=STATUS_CRITICAL,
            increasing_fillcolor=STATUS_GOOD, decreasing_fillcolor=STATUS_CRITICAL,
            line_width=1,
        ),
        row=1, col=1,
    )

    for col, color, label in (
        ("ema_fast", SERIES_1, f"EMA {cfg.get('indicators.ema_fast')}"),
        ("ema_slow", SERIES_2, f"EMA {cfg.get('indicators.ema_slow')}"),
    ):
        if col in view:
            fig.add_trace(
                go.Scatter(
                    x=view.index, y=view[col], name=label, mode="lines",
                    line=dict(color=color, width=2),
                ),
                row=1, col=1,
            )

    if not shown.empty:
        for direction, symbol, color in (
            ("long", "triangle-up", SERIES_1),
            ("short", "triangle-down", SERIES_2),
        ):
            sub = shown[shown["direction"] == direction]
            if sub.empty:
                continue
            fig.add_trace(
                go.Scatter(
                    x=sub["entry_time"], y=sub["entry_price"], mode="markers",
                    name=f"Entrada {direction}",
                    marker=dict(symbol=symbol, size=13, color=color,
                                line=dict(color=SURFACE, width=1.5)),
                    customdata=sub[["trade_id", "points", "pct", "exit_reason"]],
                    hovertemplate=(
                        "<b>Trade %{customdata[0]}</b><br>"
                        "Entrada: %{x|%Y-%m-%d %H:%M}<br>"
                        "Precio: %{y:.2f}<br>"
                        "Resultado: %{customdata[1]:.2f} pts "
                        "(%{customdata[2]:.2f}%)<br>"
                        "Salida por: %{customdata[3]}<extra></extra>"
                    ),
                ),
                row=1, col=1,
            )

        for reason, symbol, color in (
            ("TP", "circle", STATUS_GOOD),
            ("SL", "x", STATUS_CRITICAL),
        ):
            sub = shown[shown["exit_reason"] == reason]
            if sub.empty:
                continue
            fig.add_trace(
                go.Scatter(
                    x=sub["exit_time"], y=sub["exit_price"], mode="markers",
                    name=f"Salida {reason}",
                    marker=dict(symbol=symbol, size=11, color=color,
                                line=dict(color=SURFACE, width=1.5)),
                ),
                row=1, col=1,
            )
        others = shown[~shown["exit_reason"].isin(["TP", "SL"])]
        if not others.empty:
            fig.add_trace(
                go.Scatter(
                    x=others["exit_time"], y=others["exit_price"], mode="markers",
                    name="Salida señal/sesión",
                    marker=dict(symbol="square", size=10, color=STATUS_NEUTRAL,
                                line=dict(color=SURFACE, width=1.5)),
                ),
                row=1, col=1,
            )

    if not equity.empty:
        fig.add_trace(
            go.Scatter(
                x=equity["exit_time"], y=equity["equity_usd"],
                name="Equity", mode="lines",
                line=dict(color=SERIES_1, width=2),
                hovertemplate="%{x|%Y-%m-%d %H:%M}<br>$%{y:,.0f}<extra></extra>",
            ),
            row=2, col=1,
        )

    fig.update_layout(
        template="plotly_white",
        paper_bgcolor=SURFACE, plot_bgcolor=SURFACE,
        font=dict(family="system-ui, -apple-system, Segoe UI, sans-serif",
                  color=INK_SECONDARY, size=11),
        height=900, hovermode="x unified",
        xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", y=1.06, x=0),
        margin=dict(l=60, r=40, t=80, b=50),
    )
    fig.update_xaxes(gridcolor=GRIDLINE, linecolor=BASELINE)
    fig.update_yaxes(gridcolor=GRIDLINE, linecolor=BASELINE)

    # Mismo problema que en matplotlib: sin esto, la sesión overnight y los
    # fines de semana ocupan la mayor parte del ancho. Plotly lo resuelve con
    # rangebreaks en vez de un eje ordinal.
    breaks = [dict(bounds=["sat", "mon"])]
    if cfg.get("data.regular_hours_only", False):
        breaks.append(
            dict(
                bounds=[
                    str(cfg.get("data.session_end", "16:00")),
                    str(cfg.get("data.session_start", "09:30")),
                ]
            )
        )
    fig.update_xaxes(rangebreaks=breaks, row=1, col=1)

    out_html = out_path.with_suffix(".html")
    fig.write_html(str(out_html), include_plotlyjs="cdn")
    return out_html
