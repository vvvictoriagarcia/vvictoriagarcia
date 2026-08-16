#!/usr/bin/env python3
"""Genera un dashboard HTML estático con el estado del sistema.

Para qué sirve
--------------
Es la cara visible del sistema: una página que muestra el estado del mercado
ahora, la señal vigente, la curva de equity del backtest y los últimos trades.
Pensada para publicarse en GitHub Pages y regenerarse sola con GitHub Actions.

Decisiones de diseño
--------------------
* **HTML autocontenido, sin dependencias externas.** Los gráficos son SVG
  generados acá, no una librería de charts por CDN. Así la página funciona
  con cualquier CSP, carga instantáneo y no se rompe si un CDN se cae.
* **Sin JavaScript para los datos.** Todo se renderiza en build time. El único
  JS es un contador de "hace cuánto se actualizó".
* **Tema claro y oscuro**, siguiendo el del sistema operativo.
* **La identidad nunca depende solo del color:** long/short llevan flecha,
  ganador/perdedor llevan signo, el estado del mercado lleva texto.

Uso:
    python tools/build_dashboard.py
    python tools/build_dashboard.py --out docs_site --ticker NQ=F
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from src.backtester import (  # noqa: E402
    Backtester, build_equity_curve, compute_metrics, finalize_costs,
)
from src.config import load_config  # noqa: E402
from src.data_fetcher import fetch_ohlcv  # noqa: E402
from src.indicators import add_indicators  # noqa: E402
from src.strategy import Bar, Context, get_strategy  # noqa: E402

MARKET_TZ = ZoneInfo("America/New_York")
LOCAL_TZ = ZoneInfo("America/Argentina/Buenos_Aires")


# ---------------------------------------------------------------------------
# Gráficos SVG
# ---------------------------------------------------------------------------

def _points_to_path(values: list[float], width: int, height: int, pad: int = 4) -> str:
    """Convierte una serie en el atributo `points` de un <polyline>."""
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    inner_w, inner_h = width - 2 * pad, height - 2 * pad
    step = inner_w / (len(values) - 1)
    return " ".join(
        f"{pad + i * step:.2f},{pad + inner_h - ((v - lo) / span) * inner_h:.2f}"
        for i, v in enumerate(values)
    )


def equity_svg(equity: pd.DataFrame, width: int = 760, height: int = 230) -> str:
    """Curva de equity con relleno de drawdown y etiquetas de escala.

    Sin las etiquetas el gráfico es decorativo: se ve la forma pero no se sabe
    si el eje recorre 500 dólares o 50.000. Etiquetamos los extremos del eje
    y el valor final, que es la información que realmente se busca acá.
    """
    if equity.empty or len(equity) < 2:
        return '<p class="empty">Sin trades para graficar.</p>'

    values = equity["equity_usd"].tolist()
    peaks = equity["peak_usd"].tolist()
    pad_l, pad_r, pad_y = 8, 96, 14   # margen derecho para las etiquetas
    lo = min(min(values), min(peaks))
    hi = max(max(values), max(peaks))
    span = (hi - lo) or 1.0
    inner_w, inner_h = width - pad_l - pad_r, height - 2 * pad_y
    step = inner_w / (len(values) - 1)

    def y(v: float) -> float:
        return pad_y + inner_h - ((v - lo) / span) * inner_h

    dd_top = " ".join(f"{pad_l + i*step:.2f},{y(p):.2f}" for i, p in enumerate(peaks))
    dd_bottom = " ".join(
        f"{pad_l + i*step:.2f},{y(v):.2f}" for i, v in reversed(list(enumerate(values)))
    )
    line = " ".join(f"{pad_l + i*step:.2f},{y(v):.2f}" for i, v in enumerate(values))

    initial = float(equity["equity_usd"].iloc[0] - equity["pnl_usd"].iloc[0])
    final = values[-1]
    end_x, end_y = pad_l + inner_w, y(final)

    return f"""
<svg viewBox="0 0 {width} {height}" class="chart" role="img"
     aria-label="Curva de equity: arranca en {initial:,.0f} y termina en {final:,.0f} dólares">
  <polygon points="{dd_top} {dd_bottom}" class="dd-area"/>
  <line x1="{pad_l}" y1="{y(initial):.2f}" x2="{end_x:.2f}" y2="{y(initial):.2f}"
        class="baseline"/>
  <polyline points="{line}" class="equity-line"/>
  <circle cx="{end_x:.2f}" cy="{end_y:.2f}" r="4" class="end-dot"/>
  <text x="{end_x + 8:.2f}" y="{end_y + 4:.2f}" class="lbl lbl-strong">${final:,.0f}</text>
  <text x="{pad_l}" y="{pad_y - 4}" class="lbl">${hi:,.0f}</text>
  <text x="{pad_l}" y="{height - 3}" class="lbl">${lo:,.0f}</text>
  <text x="{end_x + 8:.2f}" y="{y(initial) + 4:.2f}" class="lbl">inicio ${initial:,.0f}</text>
</svg>"""


def price_svg(closes: list[float], width: int = 760, height: int = 130) -> str:
    """Sparkline del precio, con máximo, mínimo y último valor etiquetados."""
    if len(closes) < 2:
        return ""
    up = closes[-1] >= closes[0]
    cls = "spark-up" if up else "spark-down"
    pad_l, pad_r, pad_y = 8, 74, 14
    lo, hi = min(closes), max(closes)
    span = (hi - lo) or 1.0
    inner_w, inner_h = width - pad_l - pad_r, height - 2 * pad_y
    step = inner_w / (len(closes) - 1)

    def y(v: float) -> float:
        return pad_y + inner_h - ((v - lo) / span) * inner_h

    pts = " ".join(f"{pad_l + i*step:.2f},{y(v):.2f}" for i, v in enumerate(closes))
    end_x, end_y = pad_l + inner_w, y(closes[-1])

    return f"""
<svg viewBox="0 0 {width} {height}" class="chart" role="img"
     aria-label="Precio reciente entre {lo:,.2f} y {hi:,.2f}, {'subiendo' if up else 'bajando'}">
  <polyline points="{pts}" class="{cls}"/>
  <circle cx="{end_x:.2f}" cy="{end_y:.2f}" r="4" class="end-dot"/>
  <text x="{end_x + 8:.2f}" y="{end_y + 4:.2f}" class="lbl lbl-strong">{closes[-1]:,.0f}</text>
  <text x="{pad_l}" y="{pad_y - 4}" class="lbl">máx {hi:,.0f}</text>
  <text x="{pad_l}" y="{height - 3}" class="lbl">mín {lo:,.0f}</text>
</svg>"""


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def stat_tile(label: str, value: str, sub: str = "", tone: str = "") -> str:
    tone_cls = f" tone-{tone}" if tone else ""
    sub_html = f'<div class="stat-sub">{html.escape(sub)}</div>' if sub else ""
    return f"""<div class="stat{tone_cls}">
      <div class="stat-label">{html.escape(label)}</div>
      <div class="stat-value">{html.escape(value)}</div>
      {sub_html}
    </div>"""


def build_html(ctx: dict) -> str:
    m = ctx["metrics"]
    trades = ctx["trades"]

    # --- Estado actual -----------------------------------------------------
    sig = ctx["signal"]
    if sig["type"] == "LONG":
        signal_tone, signal_text = "good", "▲ LONG"
    elif sig["type"] == "SHORT":
        signal_tone, signal_text = "critical", "▼ SHORT"
    else:
        signal_tone, signal_text = "neutral", "— sin señal"

    market_cls = "open" if ctx["market_open"] else "closed"
    market_text = "Mercado abierto" if ctx["market_open"] else "Mercado cerrado"

    # --- Tiles de estado ---------------------------------------------------
    ind = ctx["indicators"]
    spread = ind.get("ema_spread")
    live_tiles = "".join([
        stat_tile("Último precio", f"{ctx['last_price']:,.2f}",
                  f"vela {ctx['last_bar_time']}"),
        stat_tile("Señal actual", signal_text,
                  sig["reason"] or "no se cumplen las condiciones", signal_tone),
        stat_tile("RSI", f"{ind['rsi']:.1f}" if ind.get("rsi") else "—",
                  f"filtro long {ctx['rsi_band_long']}"),
        stat_tile(
            "EMA rápida − lenta",
            f"{spread:+.2f}" if spread is not None else "—",
            "alcista" if (spread or 0) > 0 else "bajista",
            "good" if (spread or 0) > 0 else "critical",
        ),
    ])

    # --- Tiles del backtest ------------------------------------------------
    pf = m.get("profit_factor")
    total_pts = m.get("total_points", 0.0)
    bt_tiles = "".join([
        stat_tile("Trades", f"{m.get('trades_total', 0)}",
                  f"{m.get('trades_long', 0)} long · {m.get('trades_short', 0)} short"),
        stat_tile("Win rate", f"{m.get('win_rate_pct', 0):.1f} %",
                  f"{m.get('wins', 0)} ganadores / {m.get('losses', 0)} perdedores"),
        stat_tile("Profit factor", f"{pf:.3f}" if pf else "—",
                  "bruto ganado / bruto perdido",
                  "good" if (pf or 0) > 1 else "critical"),
        stat_tile("Resultado neto", f"{total_pts:+,.1f} pts",
                  f"{m.get('total_pnl_usd', 0):+,.0f} USD",
                  "good" if total_pts > 0 else "critical"),
        stat_tile("Drawdown máximo", f"{m.get('max_drawdown_pct', 0):.1f} %",
                  f"{m.get('max_drawdown_usd', 0):+,.0f} USD", "critical"),
        stat_tile("Costos", f"{m.get('total_costs_points', 0):,.1f} pts",
                  f"{m.get('total_costs_usd', 0):,.0f} USD"),
    ])

    # --- Tabla de trades ---------------------------------------------------
    rows = []
    for _, t in trades.tail(15).iloc[::-1].iterrows():
        won = t["points"] > 0
        arrow = "▲" if t["direction"] == "long" else "▼"
        rows.append(f"""<tr>
          <td class="mono">{t['entry_time']:%d-%m %H:%M}</td>
          <td>{arrow} {html.escape(t['direction'])}</td>
          <td class="mono num">{t['entry_price']:,.2f}</td>
          <td class="mono">{t['exit_time']:%d-%m %H:%M}</td>
          <td class="mono num">{t['exit_price']:,.2f}</td>
          <td class="mono num {'pos' if won else 'neg'}">{t['points']:+.2f}</td>
          <td class="mono num {'pos' if won else 'neg'}">{t['pct']:+.3f}%</td>
          <td><span class="tag tag-{html.escape(str(t['exit_reason']).lower())}">
            {html.escape(str(t['exit_reason']))}</span></td>
        </tr>""")
    trade_rows = "".join(rows) or '<tr><td colspan="8" class="empty">Sin trades</td></tr>'

    warnings_html = ""
    if m.get("warnings"):
        items = "".join(f"<li>{html.escape(w)}</li>" for w in m["warnings"])
        warnings_html = f'<div class="warnings"><strong>Advertencias del backtest</strong><ul>{items}</ul></div>'

    return f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="180">
<title>{html.escape(ctx['ticker'])} · Panel de estrategia</title>
<style>
  :root {{
    color-scheme: light;
    --surface: #fcfcfb;  --panel: #ffffff;  --page: #f9f9f7;
    --ink: #0b0b0b;      --ink-2: #52514e;  --muted: #898781;
    --grid: #e1e0d9;     --line: #c3c2b7;
    --blue: #2a78d6;     --orange: #eb6834;
    --good: #0ca30c;     --critical: #d03b3b;
    --good-soft: rgba(12,163,12,.10);
    --critical-soft: rgba(208,59,59,.10);
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      color-scheme: dark;
      --surface: #1a1a19; --panel: #212120; --page: #0d0d0d;
      --ink: #ffffff;     --ink-2: #c3c2b7; --muted: #898781;
      --grid: #2c2c2a;    --line: #383835;
      --blue: #3987e5;    --orange: #d95926;
      --good: #0ca30c;    --critical: #d03b3b;
      --good-soft: rgba(12,163,12,.16);
      --critical-soft: rgba(208,59,59,.16);
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 24px 16px 48px;
    background: var(--page); color: var(--ink);
    font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
  }}
  .wrap {{ max-width: 1080px; margin: 0 auto; }}
  header {{ margin-bottom: 24px; }}
  h1 {{ font-size: 22px; margin: 0 0 6px; letter-spacing: -.01em; }}
  .sub {{ color: var(--ink-2); font-size: 13px; }}
  .pill {{
    display: inline-flex; align-items: center; gap: 6px;
    padding: 3px 10px; border-radius: 999px;
    font-size: 12px; font-weight: 600; margin-right: 8px;
  }}
  .pill.open  {{ background: var(--good-soft); color: var(--good); }}
  .pill.closed{{ background: var(--grid); color: var(--ink-2); }}
  .dot {{ width: 7px; height: 7px; border-radius: 50%; background: currentColor; }}

  section {{ margin-top: 28px; }}
  h2 {{
    font-size: 12px; text-transform: uppercase; letter-spacing: .07em;
    color: var(--muted); margin: 0 0 12px; font-weight: 600;
  }}
  .panel {{
    background: var(--panel); border: 1px solid var(--grid);
    border-radius: 12px; padding: 18px;
  }}
  .grid {{ display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(215px, 1fr)); }}
  .stat {{
    background: var(--panel); border: 1px solid var(--grid);
    border-radius: 10px; padding: 14px 16px;
  }}
  .stat-label {{ font-size: 11px; text-transform: uppercase; letter-spacing: .06em;
                 color: var(--muted); margin-bottom: 6px; }}
  .stat-value {{ font-size: 24px; font-weight: 650; letter-spacing: -.02em; }}
  .stat-sub {{ font-size: 12px; color: var(--ink-2); margin-top: 4px; }}
  .tone-good .stat-value {{ color: var(--good); }}
  .tone-critical .stat-value {{ color: var(--critical); }}

  .chart {{ width: 100%; height: auto; display: block; }}
  .equity-line {{ fill: none; stroke: var(--blue); stroke-width: 2;
                  stroke-linejoin: round; stroke-linecap: round; }}
  .dd-area {{ fill: var(--critical); opacity: .13; }}
  .baseline {{ stroke: var(--line); stroke-width: 1; stroke-dasharray: 4 4; }}
  .spark-up {{ fill: none; stroke: var(--blue); stroke-width: 1.8; }}
  .spark-down {{ fill: none; stroke: var(--orange); stroke-width: 1.8; }}
  .end-dot {{ fill: var(--blue); stroke: var(--panel); stroke-width: 2; }}
  /* Las etiquetas van en tinta, no en el color de la serie: el color lo
     lleva la línea, el texto se lee. */
  .lbl {{ fill: var(--muted); font-size: 11px;
          font-family: system-ui, -apple-system, sans-serif;
          font-variant-numeric: tabular-nums; }}
  .lbl-strong {{ fill: var(--ink); font-weight: 650; font-size: 12px; }}

  .scroll {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{
    text-align: left; font-size: 11px; text-transform: uppercase;
    letter-spacing: .05em; color: var(--muted); font-weight: 600;
    padding: 8px 10px; border-bottom: 1px solid var(--grid); white-space: nowrap;
  }}
  td {{ padding: 8px 10px; border-bottom: 1px solid var(--grid); white-space: nowrap; }}
  tr:last-child td {{ border-bottom: none; }}
  .mono {{ font-variant-numeric: tabular-nums; }}
  .num {{ text-align: right; }}
  .pos {{ color: var(--good); }}
  .neg {{ color: var(--critical); }}
  .tag {{ font-size: 11px; padding: 2px 7px; border-radius: 5px;
          background: var(--grid); color: var(--ink-2); font-weight: 600; }}
  .tag-tp {{ background: var(--good-soft); color: var(--good); }}
  .tag-sl {{ background: var(--critical-soft); color: var(--critical); }}

  .warnings {{
    margin-top: 14px; padding: 12px 16px; border-radius: 10px;
    background: var(--critical-soft); border: 1px solid var(--grid);
    font-size: 13px; color: var(--ink-2);
  }}
  .warnings ul {{ margin: 6px 0 0; padding-left: 18px; }}
  .empty {{ color: var(--muted); text-align: center; padding: 20px; }}
  footer {{ margin-top: 36px; padding-top: 18px; border-top: 1px solid var(--grid);
            color: var(--muted); font-size: 12px; }}
  code {{ background: var(--grid); padding: 1px 5px; border-radius: 4px; font-size: 12px; }}
</style>
</head>
<body>
<div class="wrap">

  <header>
    <h1>{html.escape(ctx['ticker'])} · {html.escape(ctx['interval'])}</h1>
    <div class="sub">
      <span class="pill {market_cls}"><span class="dot"></span>{market_text}</span>
      Estrategia <code>{html.escape(ctx['strategy_name'])}</code> ·
      actualizado <span id="ago" data-ts="{ctx['generated_iso']}">recién</span>
      · {html.escape(ctx['generated_local'])} (Buenos Aires)
    </div>
  </header>

  <section>
    <h2>Estado ahora</h2>
    <div class="grid">{live_tiles}</div>
  </section>

  <section>
    <h2>Precio · últimas {ctx['spark_bars']} velas</h2>
    <div class="panel">{ctx['price_chart']}</div>
  </section>

  <section>
    <h2>Backtest · {html.escape(ctx['period'])}</h2>
    <div class="grid">{bt_tiles}</div>
    {warnings_html}
  </section>

  <section>
    <h2>Curva de equity · {m.get('trades_total', 0)} trades</h2>
    <div class="panel">{ctx['equity_chart']}</div>
  </section>

  <section>
    <h2>Últimos trades</h2>
    <div class="panel scroll">
      <table>
        <thead><tr>
          <th>Entrada</th><th>Dir</th><th class="num">Precio</th>
          <th>Salida</th><th class="num">Precio</th>
          <th class="num">Puntos</th><th class="num">%</th><th>Motivo</th>
        </tr></thead>
        <tbody>{trade_rows}</tbody>
      </table>
    </div>
  </section>

  <footer>
    Panel generado automáticamente desde datos de Yahoo Finance. La página se
    recarga sola cada 3 minutos y se regenera con GitHub Actions en horario de
    mercado. <strong>Es una herramienta de análisis:</strong> no ejecuta órdenes
    ni se conecta a ningún broker, y no constituye asesoramiento financiero.
    La estrategia cargada es un placeholder para validar el sistema.
  </footer>

</div>
<script>
  // Único JS de la página: "hace cuánto" se generó, para que se note si el
  // pipeline se cortó. Un panel congelado que no lo dice es peor que no tenerlo.
  (function () {{
    var el = document.getElementById('ago');
    if (!el) return;
    function tick() {{
      var mins = Math.floor((Date.now() - new Date(el.dataset.ts).getTime()) / 60000);
      el.textContent = mins < 1 ? 'recién'
        : mins < 60 ? 'hace ' + mins + ' min'
        : 'hace ' + Math.floor(mins / 60) + ' h ' + (mins % 60) + ' min';
      if (mins > 90) el.style.color = 'var(--critical)';
    }}
    tick(); setInterval(tick, 30000);
  }})();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Genera el dashboard HTML estático")
    ap.add_argument("--config", default=None)
    ap.add_argument("--ticker", default=None)
    ap.add_argument("--interval", default=None)
    ap.add_argument("--lookback", type=int, default=None)
    ap.add_argument("--out", default="site", help="Carpeta de salida")
    ap.add_argument(
        "--no-cache", action="store_true",
        help="Ignorar la caché en disco. Usalo en CI: el runner es efímero, "
             "así que cachear no sirve y evita depender de pyarrow.",
    )
    args = ap.parse_args()

    cfg = load_config(args.config)
    for flag, key in (("ticker", "data.ticker"), ("interval", "data.interval")):
        if getattr(args, flag):
            cfg.set(key, getattr(args, flag))
    if args.lookback:
        cfg.set("data.lookback_days", args.lookback)
    if args.no_cache:
        cfg.set("data.use_cache", False)

    print(f"  [dashboard] {cfg.get('data.ticker')} @ {cfg.get('data.interval')}")

    df = fetch_ohlcv(cfg, strict_limits=False)
    df = add_indicators(df, cfg)

    strategy = get_strategy(
        cfg.require("strategy.name"), cfg.section("strategy").get("params", {})
    )
    engine = Backtester(cfg, strategy)
    trades = engine.run(df)

    initial = float(cfg.get("execution.initial_capital_usd", 25000.0))
    equity = build_equity_curve(trades, initial)
    metrics = compute_metrics(
        trades, equity, initial_capital=initial,
        ambiguous_bars=engine.ambiguous_bars, bars_total=len(df),
    )
    finalize_costs(
        metrics,
        float(cfg.get("execution.point_value_usd", 20.0)),
        int(cfg.get("execution.contracts", 1)),
    )

    # --- Señal sobre la última vela cerrada -------------------------------
    def mk_bar(i: int) -> Bar:
        ts, row = df.index[i], df.iloc[i]
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

    bar, prev = mk_bar(-1), mk_bar(-2)
    signal = strategy.evaluate(
        Context(bar=bar, prev=prev, position=None, params=strategy.params)
    )

    now_utc = datetime.now(timezone.utc)
    now_market = now_utc.astimezone(MARKET_TZ)
    market_open = (
        now_market.weekday() < 5
        and "09:30" <= now_market.strftime("%H:%M") < "16:00"
    )

    params = strategy.params
    spark = df["close"].tail(180).tolist()

    ctx = {
        "ticker": str(cfg.get("data.ticker")),
        "interval": str(cfg.get("data.interval")),
        "strategy_name": str(cfg.get("strategy.name")),
        "metrics": metrics,
        "trades": trades,
        "last_price": float(df["close"].iloc[-1]),
        "last_bar_time": df.index[-1].strftime("%d-%m %H:%M"),
        "indicators": {
            "rsi": bar.get("rsi"),
            "ema_spread": bar.get("ema_spread"),
        },
        "rsi_band_long": (
            f"({params.get('rsi_long_min', 50):g}, {params.get('rsi_long_max', 70):g})"
        ),
        "signal": {"type": signal.type, "reason": signal.reason},
        "market_open": market_open,
        "period": f"{df.index[0]:%d-%m-%Y} → {df.index[-1]:%d-%m-%Y}",
        "spark_bars": len(spark),
        "price_chart": price_svg(spark),
        "equity_chart": equity_svg(equity),
        "generated_iso": now_utc.isoformat(),
        "generated_local": now_utc.astimezone(LOCAL_TZ).strftime("%d-%m-%Y %H:%M"),
    }

    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    index = out_dir / "index.html"
    index.write_text(build_html(ctx), encoding="utf-8")

    # JSON al lado, por si querés consumirlo desde otro lado.
    (out_dir / "state.json").write_text(
        json.dumps(
            {
                "ticker": ctx["ticker"], "interval": ctx["interval"],
                "generated": ctx["generated_iso"],
                "market_open": market_open,
                "last_price": ctx["last_price"],
                "last_bar": str(df.index[-1]),
                "signal": ctx["signal"],
                "indicators": ctx["indicators"],
                "metrics": {
                    k: v for k, v in metrics.items()
                    if k not in ("config", "warnings", "exit_reasons")
                },
            },
            indent=2, default=str,
        ),
        encoding="utf-8",
    )

    size_kb = index.stat().st_size / 1024
    print(f"  [dashboard] {index} ({size_kb:.0f} KB)")
    print(f"  [dashboard] señal actual: {signal.type or 'NONE'} · "
          f"mercado {'abierto' if market_open else 'cerrado'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
