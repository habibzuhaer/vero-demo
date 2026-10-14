"""PNG-график сигнала для Telegram."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from config import CHARTS_DIR, ensure_dirs


def _candle_width_days(ts: pd.Series) -> float:
    if len(ts) < 2:
        return 1 / 24  # 1 hour
    delta = ts.iloc[-1] - ts.iloc[0]
    sec = max(delta.total_seconds() / max(len(ts) - 1, 1), 60)
    return sec / 86400 * 0.72


def _draw_candles(ax, ts: pd.Series, tail: pd.DataFrame) -> None:
    import matplotlib.dates as mdates
    from matplotlib.patches import Rectangle

    o = tail["open"].astype(float)
    h = tail["high"].astype(float)
    lo = tail["low"].astype(float)
    c = tail["close"].astype(float)
    w = _candle_width_days(ts)

    for i in range(len(tail)):
        op, hi, low, cl = o.iloc[i], h.iloc[i], lo.iloc[i], c.iloc[i]
        bull = cl >= op
        color = "#3ecf8e" if bull else "#e85d5d"
        edge = "#2aa86a" if bull else "#c94444"
        x = mdates.date2num(ts.iloc[i])

        ax.vlines(x, low, hi, color=color, linewidth=0.9, alpha=0.95, zorder=2)

        body_bot = min(op, cl)
        body_h = abs(cl - op)
        if body_h < (hi - low) * 0.04:
            body_h = max((hi - low) * 0.04, hi * 1e-6)

        ax.add_patch(
            Rectangle(
                (x - w / 2, body_bot),
                w,
                body_h,
                facecolor=color,
                edgecolor=edge,
                linewidth=0.6,
                alpha=0.92,
                zorder=3,
            )
        )


def render_signal_chart(
    df: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    strategy: str,
    event_type: str,
    price: float,
    stop_price: float | None = None,
    bars: int = 80,
) -> Path | None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None

    ensure_dirs()
    tail = df.tail(bars).copy()
    if tail.empty:
        return None

    for col in ("open", "high", "low", "close"):
        if col not in tail.columns:
            return None

    ts = pd.to_datetime(tail["timestamp"], unit="ms", utc=True)

    fig, ax = plt.subplots(figsize=(10, 4.5), facecolor="#0e1412")
    ax.set_facecolor("#0e1412")

    _draw_candles(ax, ts, tail)

    if "EMA_FAST" in tail.columns and "EMA_SLOW" in tail.columns:
        ax.plot(
            ts,
            tail["EMA_FAST"].astype(float),
            color="#c8f542",
            alpha=0.75,
            linewidth=0.9,
            label="EMA9",
            zorder=4,
        )
        ax.plot(
            ts,
            tail["EMA_SLOW"].astype(float),
            color="#8aa396",
            alpha=0.75,
            linewidth=0.9,
            label="EMA21",
            zorder=4,
        )

    ax.scatter(
        [ts.iloc[-1]],
        [price],
        color="#c8f542" if event_type == "BUY" else "#e85d5d",
        s=80,
        zorder=6,
        edgecolors="#e6efe9",
        linewidths=0.6,
    )
    if stop_price and event_type == "BUY":
        ax.axhline(
            stop_price,
            color="#e85d5d",
            linestyle="--",
            linewidth=1,
            alpha=0.85,
            label=f"SL {stop_price}",
            zorder=1,
        )

    ax.set_title(
        f"{symbol} {timeframe} · {strategy} · {event_type}",
        color="#e6efe9",
        fontsize=11,
        pad=8,
    )
    ax.tick_params(colors="#8aa396", labelsize=8)
    for spine in ax.spines.values():
        spine.set_color("#24352e")
    ax.grid(True, alpha=0.12, color="#8aa396", zorder=0)
    ax.legend(facecolor="#15201c", edgecolor="#24352e", labelcolor="#e6efe9", fontsize=8)
    fig.autofmt_xdate()
    fig.tight_layout()

    safe = symbol.replace(".", "_")
    out = CHARTS_DIR / f"{safe}_{timeframe}_{strategy}_{event_type}_{int(price * 100)}.png"
    fig.savefig(out, dpi=120, facecolor=fig.get_facecolor())
    plt.close(fig)
    return out
