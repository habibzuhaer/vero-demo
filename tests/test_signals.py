"""Telegram signals + paper trading."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setattr("config.DB_PATH", db)
    monkeypatch.setattr("db.DB_PATH", db)
    from db import init_db

    init_db(db)
    return db


def test_format_signal_caption_buy():
    from engine.telegram_notify import format_signal_caption

    text = format_signal_caption(
        {
            "event_type": "BUY",
            "symbol": "AAPL.US",
            "timeframe": "1h",
            "strategy": "rsi_mean_reversion",
            "price": 150.25,
            "stop_price": 148.0,
            "stop_loss_pct": 1.5,
            "take_profit_note": "TP = SELL",
            "oos_return_pct": 12.3,
        }
    )
    assert "AAPL.US" in text
    assert "150.25" in text
    assert "Stop" in text


def test_paper_buy_sell_cycle(tmp_db):
    from engine.paper import execute_paper, portfolio_status

    ev_buy = {
        "event_type": "BUY",
        "symbol": "TEST.US",
        "timeframe": "1h",
        "strategy": "rsi_mean_reversion",
        "price": 100.0,
        "bar_ts": 1,
        "stop_loss_pct": 1.5,
        "stop_price": 98.5,
        "take_profit_note": "TP",
        "indicators": {"RSI": 28.0},
        "oos_return_pct": 5.0,
    }
    out = execute_paper(ev_buy, signal_id=1)
    assert out["ok"] is True
    assert out["action"] == "BUY"

    st = portfolio_status()
    assert st["open_count"] == 1
    assert st["cash"] == 0.0

    ev_sell = {
        "event_type": "SELL",
        "symbol": "TEST.US",
        "timeframe": "1h",
        "strategy": "rsi_mean_reversion",
        "price": 105.0,
        "bar_ts": 2,
        "indicators": {"RSI": 72.0},
    }
    out2 = execute_paper(ev_sell, signal_id=2)
    assert out2["ok"] is True
    assert out2["action"] == "SELL"
    assert out2["pnl_pct"] > 0

    st2 = portfolio_status()
    assert st2["open_count"] == 0
    assert st2["cash"] > 0

    with __import__("db").db_session() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM learn_samples").fetchone()["n"]
    assert n == 1


def test_signal_dedupe(tmp_db, monkeypatch):
    from engine import signal_scanner

    df = pd.DataFrame(
        {
            "timestamp": [1, 2],
            "open": [10.0, 10.0],
            "high": [10.5, 10.5],
            "low": [9.5, 9.5],
            "close": [10.0, 10.2],
            "volume": [100.0, 100.0],
            "signal": [0, 1],
        }
    )
    ev = {
        "ok": True,
        "symbol": "X.US",
        "timeframe": "1h",
        "strategy": "rsi_mean_reversion",
        "event_type": "BUY",
        "price": 10.2,
        "bar_ts": 2,
        "stop_loss_pct": 1.5,
        "stop_price": 10.05,
        "take_profit_note": "TP",
        "indicators": {},
        "dedupe_key": "X.US|1h|rsi_mean_reversion|2|BUY",
        "ohlcv": df,
    }
    monkeypatch.setattr(signal_scanner, "evaluate_pair", lambda *a, **k: dict(ev))
    monkeypatch.setattr(signal_scanner, "send_signal", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(signal_scanner, "render_signal_chart", lambda *a, **k: None)

    item = {"symbol": "X.US", "timeframe": "1h", "strategy": "rsi_mean_reversion"}
    r1 = signal_scanner.process_watch_item(item, send_tg=False)
    r2 = signal_scanner.process_watch_item(item, send_tg=False)
    assert r1.get("signal_id")
    assert r2.get("skipped") is True


def test_check_stops(tmp_db):
    from db import db_session, _utc
    from engine.paper import check_stops, execute_paper

    ev_buy = {
        "event_type": "BUY",
        "symbol": "STOP.US",
        "timeframe": "1h",
        "strategy": "ema_crossover",
        "price": 200.0,
        "bar_ts": 1,
        "stop_loss_pct": 1.5,
        "stop_price": 197.0,
        "indicators": {},
    }
    execute_paper(ev_buy, signal_id=10)

    hits = check_stops({("STOP.US", "1h", "ema_crossover"): 196.5})
    assert len(hits) == 1
    assert hits[0]["reason"] == "stop_loss"
