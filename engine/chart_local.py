"""PNG-график из локальной vero_bars.db (свечи + индикаторы)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from config import CHARTS_DIR, ensure_dirs
from engine.chart_signal import _draw_candles
from engine.vero_store import fetch_vero_bars, store_stats


def render_local_chart(
    symbol: str,
    timeframe: str,
    *,
    bars: int = 160,
) -> tuple[Path | None, dict[str, Any]]:
    """
    Рисует свечи + EMA/BB + RSI из локального стора.
    Возвращает (path, meta).
    """
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        return None, {"ok": False, "error": "matplotlib not installed"}

    limit = max(40, min(int(bars), 800))
    df, err = fetch_vero_bars(symbol, timeframe, limit=limit, with_indicators=True)
    if err or df is None or df.empty:
        return None, {"ok": False, "error": err or "no local bars", "symbol": symbol}

    ensure_dirs()
    tail = df.tail(limit).copy()
    ts = pd.to_datetime(tail["timestamp"], unit="ms", utc=True)

    fig, (ax, ax_rsi) = plt.subplots(
        2,
        1,
        figsize=(11, 6.2),
        facecolor="#0e1412",
        gridspec_kw={"height_ratios": [3.2, 1.0], "hspace": 0.12},
        sharex=True,
    )
    ax.set_facecolor("#0e1412")
    ax_rsi.set_facecolor("#0e1412")

    _draw_candles(ax, ts, tail)

    if "BB_UPPER" in tail.columns and "BB_LOWER" in tail.columns:
        ax.plot(
            ts,
            tail["BB_UPPER"].astype(float),
            color="#5b8def",
            alpha=0.45,
            linewidth=0.8,
            label="BB",
            zorder=4,
        )
        ax.plot(
            ts,
            tail["BB_MID"].astype(float),
            color="#5b8def",
            alpha=0.35,
            linewidth=0.7,
            linestyle="--",
            zorder=4,
        )
        ax.plot(
            ts,
            tail["BB_LOWER"].astype(float),
            color="#5b8def",
            alpha=0.45,
            linewidth=0.8,
            zorder=4,
        )

    if "EMA_FAST" in tail.columns and "EMA_SLOW" in tail.columns:
        ax.plot(
            ts,
            tail["EMA_FAST"].astype(float),
            color="#c8f542",
            alpha=0.85,
            linewidth=1.0,
            label="EMA9",
            zorder=5,
        )
        ax.plot(
            ts,
            tail["EMA_SLOW"].astype(float),
            color="#8aa396",
            alpha=0.85,
            linewidth=1.0,
            label="EMA21",
            zorder=5,
        )

    last = float(tail["close"].iloc[-1])
    ax.set_title(
        f"{symbol} · {timeframe} · local · {len(tail)} bars · last {last:.4g}",
        color="#e6efe9",
        fontsize=11,
        pad=8,
    )
    ax.tick_params(colors="#8aa396", labelsize=8)
    ax.grid(True, alpha=0.12, color="#8aa396", zorder=0)
    for spine in ax.spines.values():
        spine.set_color("#24352e")
    ax.legend(
        facecolor="#15201c",
        edgecolor="#24352e",
        labelcolor="#e6efe9",
        fontsize=8,
        loc="upper left",
    )

    if "RSI" in tail.columns:
        ax_rsi.plot(
            ts,
            tail["RSI"].astype(float),
            color="#c8f542",
            linewidth=1.0,
            label="RSI",
        )
        ax_rsi.axhline(70, color="#e85d5d", linestyle="--", linewidth=0.7, alpha=0.6)
        ax_rsi.axhline(30, color="#3ecf8e", linestyle="--", linewidth=0.7, alpha=0.6)
        ax_rsi.set_ylim(0, 100)
        ax_rsi.set_ylabel("RSI", color="#8aa396", fontsize=8)
    ax_rsi.tick_params(colors="#8aa396", labelsize=8)
    ax_rsi.grid(True, alpha=0.12, color="#8aa396", zorder=0)
    for spine in ax_rsi.spines.values():
        spine.set_color("#24352e")

    ax_rsi.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    fig.autofmt_xdate()
    fig.subplots_adjust(left=0.07, right=0.98, top=0.93, bottom=0.12, hspace=0.12)

    safe = symbol.replace(".", "_").replace("/", "_")
    out = CHARTS_DIR / f"local_{safe}_{timeframe}_{len(tail)}.png"
    fig.savefig(out, dpi=120, facecolor=fig.get_facecolor())
    plt.close(fig)

    return out, {
        "ok": True,
        "symbol": symbol,
        "timeframe": timeframe,
        "bars": len(tail),
        "path": str(out),
        "url": f"/api/charts/{out.name}",
        "last_close": last,
        "store": store_stats(),
    }
