"""Тест Telegram: по одному сообщению на каждую стратегию."""

from __future__ import annotations

import time
from typing import Any

from config import SIGNAL_DEFAULT_STOP_LOSS_PCT, STRATEGY_IDS, TOP_MIN_TRADES
from db import db_session
from engine.chart_signal import render_signal_chart
from engine.signal_eval import evaluate_pair
from engine.telegram_notify import send_signal, telegram_configured


def _best_pair(strategy: str) -> dict[str, Any] | None:
    with db_session() as conn:
        row = conn.execute(
            """
            SELECT symbol, timeframe, oos_return_pct
            FROM results
            WHERE strategy=? AND oos_return_pct IS NOT NULL AND closed_rounds >= ?
            ORDER BY oos_return_pct DESC LIMIT 1
            """,
            (strategy, TOP_MIN_TRADES),
        ).fetchone()
    return dict(row) if row else None


def test_strategies_telegram(*, delay_sec: float = 1.5) -> dict[str, Any]:
    """Отправляет 7 TEST-сигналов (BUY + chart) — по одному на стратегию."""
    if not telegram_configured():
        return {"ok": False, "error": "Telegram not configured (token + chat_id)"}

    sl = SIGNAL_DEFAULT_STOP_LOSS_PCT
    sent: list[dict[str, Any]] = []

    for st in STRATEGY_IDS:
        pair = _best_pair(st)
        if not pair:
            sent.append({"strategy": st, "ok": False, "error": "no backtest pair"})
            continue

        sym = pair["symbol"]
        tf = pair["timeframe"]
        oos = float(pair["oos_return_pct"])
        ev = evaluate_pair(sym, tf, st)
        if not ev.get("ok"):
            sent.append({"strategy": st, "symbol": sym, "ok": False, "error": ev.get("error")})
            continue

        price = float(ev["price"])
        stop_price = round(price * (1.0 - sl / 100.0), 4) if sl else None
        test_ev = {
            "symbol": sym,
            "timeframe": tf,
            "strategy": st,
            "event_type": "BUY",
            "price": price,
            "stop_loss_pct": sl,
            "stop_price": stop_price,
            "take_profit_note": "TP = SELL signal / TP = сигнал SELL стратегії",
            "oos_return_pct": round(oos, 2),
            "indicators": ev.get("indicators") or {},
            "test": True,
        }

        chart_path = None
        ohlcv = ev.get("ohlcv")
        if ohlcv is not None:
            p = render_signal_chart(
                ohlcv,
                symbol=sym,
                timeframe=tf,
                strategy=st,
                event_type="BUY",
                price=price,
                stop_price=stop_price,
            )
            chart_path = p

        tg = send_signal(test_ev, chart_path)
        sent.append(
            {
                "strategy": st,
                "symbol": sym,
                "timeframe": tf,
                "price": price,
                "chart": str(chart_path) if chart_path else None,
                **tg,
            }
        )
        if delay_sec > 0 and st != STRATEGY_IDS[-1]:
            time.sleep(delay_sec)

    ok_n = sum(1 for x in sent if x.get("ok"))
    return {
        "ok": ok_n > 0,
        "total": len(STRATEGY_IDS),
        "sent": ok_n,
        "failed": len(sent) - ok_n,
        "details": sent,
    }
