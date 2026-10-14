"""Бумажная торговля + learn samples."""

from __future__ import annotations

import json
from typing import Any

from config import PAPER_FEE_RATE, PAPER_INITIAL_CASH
from db import _utc, db_session
from learn.patterns import FEATURE_KEYS, label_sample


def _ensure_account(conn) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
    if row:
        return dict(row)
    now = _utc()
    conn.execute(
        "INSERT INTO paper_account(id, cash, initial_cash, updated_at) VALUES(1,?,?,?)",
        (PAPER_INITIAL_CASH, PAPER_INITIAL_CASH, now),
    )
    return {"cash": PAPER_INITIAL_CASH, "initial_cash": PAPER_INITIAL_CASH}


def portfolio_status() -> dict[str, Any]:
    with db_session() as conn:
        acc = _ensure_account(conn)
        positions = [
            dict(r)
            for r in conn.execute("SELECT * FROM paper_positions ORDER BY opened_at DESC").fetchall()
        ]
        recent = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM paper_trades ORDER BY id DESC LIMIT 30"
            ).fetchall()
        ]
    return {
        "cash": acc["cash"],
        "initial_cash": acc["initial_cash"],
        "positions": positions,
        "recent_trades": recent,
        "open_count": len(positions),
    }


def _paper_features(ev: dict[str, Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    feats = dict(extra or {})
    ind = ev.get("indicators") or {}
    if ind.get("RSI") is not None:
        feats["avg_rsi"] = ind["RSI"]
    if ind.get("MACD_HIST") is not None:
        feats["avg_macd_hist"] = ind["MACD_HIST"]
    if ind.get("BB_WIDTH") is not None:
        feats["avg_bb_width"] = ind["BB_WIDTH"]
    if ind.get("STOCH_K") is not None:
        feats["avg_stoch_k"] = ind["STOCH_K"]
    if ind.get("CCI") is not None:
        feats["avg_cci"] = ind["CCI"]
    if ind.get("WILLR") is not None:
        feats["avg_willr"] = ind["WILLR"]
    if ev.get("oos_return_pct") is not None:
        feats["oos_return_pct"] = ev["oos_return_pct"]
    return feats


def execute_paper(ev: dict[str, Any], signal_id: int) -> dict[str, Any]:
    """BUY открывает позицию, SELL закрывает."""
    et = ev.get("event_type")
    if et not in ("BUY", "SELL"):
        return {"ok": False, "skipped": True, "reason": "no event"}

    sym = ev["symbol"]
    tf = ev["timeframe"]
    st = ev["strategy"]
    price = float(ev["price"])
    fee_rate = PAPER_FEE_RATE
    now = _utc()

    with db_session() as conn:
        acc = _ensure_account(conn)
        cash = float(acc["cash"])
        pos = conn.execute(
            """
            SELECT * FROM paper_positions
            WHERE symbol=? AND timeframe=? AND strategy=?
            """,
            (sym, tf, st),
        ).fetchone()

        if et == "BUY":
            if pos:
                return {"ok": False, "skipped": True, "reason": "position exists"}
            fee = cash * fee_rate
            invest = cash - fee
            if invest <= 0:
                return {"ok": False, "error": "no cash"}
            qty = invest / price
            cash_after = 0.0
            conn.execute(
                "UPDATE paper_account SET cash=?, updated_at=? WHERE id=1",
                (cash_after, now),
            )
            cur = conn.execute(
                """
                INSERT INTO paper_trades(
                  symbol, timeframe, strategy, side, price, qty, fee, reason,
                  stop_loss_pct, stop_price, take_profit_note, signal_id, features, bar_ts, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    sym, tf, st, "BUY", price, qty, fee, "signal",
                    ev.get("stop_loss_pct"), ev.get("stop_price"),
                    ev.get("take_profit_note"), signal_id,
                    json.dumps(_paper_features(ev)), ev.get("bar_ts"), now,
                ),
            )
            trade_id = int(cur.lastrowid)
            conn.execute(
                """
                INSERT INTO paper_positions(
                  symbol, timeframe, strategy, qty, entry_price, entry_ts,
                  stop_loss_pct, stop_price, take_profit_note, signal_id, features, opened_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    sym, tf, st, qty, price, ev.get("bar_ts"),
                    ev.get("stop_loss_pct"), ev.get("stop_price"),
                    ev.get("take_profit_note"), signal_id,
                    json.dumps(_paper_features(ev)), now,
                ),
            )
            return {"ok": True, "action": "BUY", "trade_id": trade_id, "qty": qty}

        # SELL
        if not pos:
            return {"ok": False, "skipped": True, "reason": "no position"}
        qty = float(pos["qty"])
        entry = float(pos["entry_price"])
        gross = qty * price
        fee = gross * fee_rate
        cash_after = gross - fee
        pnl_usd = cash_after - (qty * entry)
        pnl_pct = (price / entry - 1.0) * 100.0 if entry else 0.0

        conn.execute(
            "UPDATE paper_account SET cash=?, updated_at=? WHERE id=1",
            (cash_after, now),
        )
        feats = _paper_features(ev)
        feats["return_pct"] = round(pnl_pct, 4)
        feats["closed_rounds"] = 1
        feats["avg_trade_ret_pct"] = round(pnl_pct, 4)

        cur = conn.execute(
            """
            INSERT INTO paper_trades(
              symbol, timeframe, strategy, side, price, qty, fee, reason,
              pnl_pct, pnl_usd, stop_loss_pct, stop_price, take_profit_note,
              signal_id, features, bar_ts, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                sym, tf, st, "SELL", price, qty, fee, ev.get("reason", "signal"),
                round(pnl_pct, 4), round(pnl_usd, 4),
                pos["stop_loss_pct"], pos["stop_price"], pos["take_profit_note"],
                signal_id, json.dumps(feats), ev.get("bar_ts"), now,
            ),
        )
        trade_id = int(cur.lastrowid)
        conn.execute(
            "DELETE FROM paper_positions WHERE id=?", (pos["id"],),
        )
        lab = label_sample(feats)
        sample_feats = {k: feats.get(k) for k in FEATURE_KEYS}
        for k, v in feats.items():
            if k not in sample_feats and v is not None:
                sample_feats[k] = v
        conn.execute(
            """
            INSERT INTO learn_samples(
              result_id, run_id, symbol, timeframe, strategy, features, label, created_at
            ) VALUES (NULL, NULL, ?, ?, ?, ?, ?, ?)
            """,
            (sym, tf, st, json.dumps(sample_feats), lab, now),
        )
        return {
            "ok": True,
            "action": "SELL",
            "trade_id": trade_id,
            "pnl_pct": round(pnl_pct, 4),
            "pnl_usd": round(pnl_usd, 4),
            "label": lab,
        }


def check_stops(current_prices: dict[tuple[str, str, str], float]) -> list[dict[str, Any]]:
    """Проверка стопов по текущей цене (low/close)."""
    closed: list[dict[str, Any]] = []
    with db_session() as conn:
        rows = conn.execute("SELECT * FROM paper_positions").fetchall()
        for pos in rows:
            key = (pos["symbol"], pos["timeframe"], pos["strategy"])
            px = current_prices.get(key)
            if px is None:
                continue
            stop = pos["stop_price"]
            if stop and px <= float(stop):
                ev = {
                    "symbol": pos["symbol"],
                    "timeframe": pos["timeframe"],
                    "strategy": pos["strategy"],
                    "event_type": "SELL",
                    "price": float(stop),
                    "bar_ts": 0,
                    "reason": "stop_loss",
                    "indicators": json.loads(pos["features"] or "{}"),
                }
                closed.append(ev)
    return closed
