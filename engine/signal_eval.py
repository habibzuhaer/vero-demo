"""Оценка последнего бара: переход сигнала стратегии."""

from __future__ import annotations

import json
from typing import Any

from config import MIN_CANDLES, SIGNAL_CANDLE_LIMIT, SIGNAL_DEFAULT_STOP_LOSS_PCT
from engine import indicators, strategies
from engine.ohlcv import load_ohlcv
from engine.yahoo import is_excluded_symbol


def _snapshot_row(row: Any) -> dict[str, Any]:
    keys = (
        "close", "RSI", "MACD", "MACD_SIGNAL", "MACD_HIST",
        "BB_UPPER", "BB_MID", "BB_LOWER", "BB_WIDTH",
        "STOCH_K", "STOCH_D", "EMA_FAST", "EMA_SLOW", "CCI", "WILLR",
    )
    out: dict[str, Any] = {}
    for k in keys:
        if k not in row.index:
            continue
        v = row[k]
        if v is not None and v == v:
            try:
                out[k] = round(float(v), 4)
            except (TypeError, ValueError):
                pass
    return out


def evaluate_pair(
    symbol: str,
    timeframe: str,
    strategy: str,
    *,
    limit: int | None = None,
    stop_loss_pct: float | None = None,
) -> dict[str, Any]:
    """Возвращает переход сигнала на последнем закрытом баре (prev→curr)."""
    if is_excluded_symbol(symbol):
        return {"ok": False, "error": "excluded symbol"}

    lim = limit or SIGNAL_CANDLE_LIMIT
    sl = stop_loss_pct if stop_loss_pct is not None else SIGNAL_DEFAULT_STOP_LOSS_PCT

    df, _, err = load_ohlcv(symbol, timeframe, limit=lim)
    if err or df.empty or len(df) < MIN_CANDLES + 2:
        return {"ok": False, "error": err or "insufficient candles"}

    enriched = indicators.enrich(df)
    signaled = strategies.generate(enriched, strategy)
    if len(signaled) < 2:
        return {"ok": False, "error": "too few bars"}

    prev_row = signaled.iloc[-2]
    curr_row = signaled.iloc[-1]
    prev_sig = int(prev_row["signal"])
    curr_sig = int(curr_row["signal"])
    price = float(curr_row["close"])
    bar_ts = int(curr_row["timestamp"])
    event_type: str | None = None

    if prev_sig != 1 and curr_sig == 1:
        event_type = "BUY"
    elif prev_sig == 1 and curr_sig == -1:
        event_type = "SELL"
    elif prev_sig == 1 and curr_sig == 0:
        event_type = "SELL"

    stop_price = None
    if event_type == "BUY" and sl and sl > 0:
        stop_price = round(price * (1.0 - sl / 100.0), 4)

    tp_note = "TP = SELL signal / TP = сигнал SELL стратегії"

    return {
        "ok": True,
        "symbol": symbol,
        "timeframe": timeframe,
        "strategy": strategy,
        "event_type": event_type,
        "signal_from": prev_sig,
        "signal_to": curr_sig,
        "price": price,
        "bar_ts": bar_ts,
        "stop_loss_pct": sl if event_type == "BUY" else None,
        "stop_price": stop_price,
        "take_profit_note": tp_note if event_type == "BUY" else "",
        "indicators": _snapshot_row(curr_row),
        "ohlcv": signaled,
        "dedupe_key": f"{symbol}|{timeframe}|{strategy}|{bar_ts}|{event_type or 'none'}",
    }
