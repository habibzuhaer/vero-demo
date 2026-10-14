"""Сканер watchlist → сигналы → Telegram + paper."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from config import (
    SIGNAL_MIN_OOS,
    SIGNAL_WATCH,
    TOP_MIN_TRADES,
)
from db import _utc, db_session, rows_to_dicts
from engine.chart_signal import render_signal_chart
from engine.paper import check_stops, execute_paper, portfolio_status
from engine.signal_eval import evaluate_pair
from engine.telegram_notify import send_signal, telegram_configured

_scan_lock = threading.Lock()
_scan_thread: threading.Thread | None = None
_stop = threading.Event()


def _best_oos(symbol: str, timeframe: str | None, strategy: str | None) -> float | None:
    with db_session() as conn:
        where = ["symbol=?", "oos_return_pct IS NOT NULL", "closed_rounds >= ?"]
        args: list[Any] = [symbol, TOP_MIN_TRADES]
        if timeframe:
            where.append("timeframe=?")
            args.append(timeframe)
        if strategy:
            where.append("strategy=?")
            args.append(strategy)
        row = conn.execute(
            f"""
            SELECT oos_return_pct FROM results
            WHERE {' AND '.join(where)}
            ORDER BY oos_return_pct DESC LIMIT 1
            """,
            args,
        ).fetchone()
    return float(row["oos_return_pct"]) if row else None


def get_watchlist() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(sym: str, tf: str | None, st: str | None, src: str) -> None:
        tf = tf or "1h"
        st = st or "rsi_mean_reversion"
        key = (sym, tf, st)
        if key in seen:
            return
        oos = _best_oos(sym, tf, st)
        if oos is not None and oos < SIGNAL_MIN_OOS:
            return
        seen.add(key)
        items.append({"symbol": sym, "timeframe": tf, "strategy": st, "source": src, "oos_return_pct": oos})

    with db_session() as conn:
        if SIGNAL_WATCH in ("user", "both"):
            for r in conn.execute("SELECT * FROM user_top ORDER BY score DESC").fetchall():
                add(r["symbol"], r["timeframe"], r["strategy"], "user_top")

        if SIGNAL_WATCH in ("auto", "both"):
            from engine.truth import auto_top_truth

            truth = auto_top_truth(limit=38)
            if truth.get("truth") and truth.get("tickers"):
                for r in truth["tickers"]:
                    if float(r.get("oos_return_pct") or 0) >= SIGNAL_MIN_OOS:
                        add(r["symbol"], r["timeframe"], r["strategy"], "auto_top")
            else:
                rows = rows_to_dicts(
                    conn.execute(
                        """
                        SELECT symbol, timeframe, strategy, oos_return_pct, closed_rounds
                        FROM results
                        WHERE oos_return_pct IS NOT NULL AND closed_rounds >= ?
                        ORDER BY oos_return_pct DESC
                        LIMIT 500
                        """,
                        (TOP_MIN_TRADES,),
                    ).fetchall()
                )
                best: dict[str, dict] = {}
                for r in rows:
                    s = r["symbol"]
                    if s not in best or float(r["oos_return_pct"] or -999) > float(
                        best[s]["oos_return_pct"] or -999
                    ):
                        best[s] = r
                for r in best.values():
                    if float(r["oos_return_pct"] or 0) >= SIGNAL_MIN_OOS:
                        add(r["symbol"], r["timeframe"], r["strategy"], "auto_top")

    return items


def _already_processed(dedupe_key: str) -> bool:
    with db_session() as conn:
        row = conn.execute(
            "SELECT id FROM signal_events WHERE dedupe_key=?", (dedupe_key,)
        ).fetchone()
    return row is not None


def _persist_signal(ev: dict[str, Any], chart_path: str | None, tg: dict[str, Any]) -> int:
    now = _utc()
    with db_session() as conn:
        cur = conn.execute(
            """
            INSERT INTO signal_events(
              symbol, timeframe, strategy, event_type, signal_from, signal_to,
              price, stop_loss_pct, stop_price, take_profit_note, bar_ts,
              indicators, chart_path, oos_return_pct, telegram_sent, telegram_error,
              dedupe_key, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ev["symbol"],
                ev["timeframe"],
                ev["strategy"],
                ev["event_type"],
                ev.get("signal_from"),
                ev.get("signal_to"),
                ev["price"],
                ev.get("stop_loss_pct"),
                ev.get("stop_price"),
                ev.get("take_profit_note"),
                ev["bar_ts"],
                json.dumps(ev.get("indicators") or {}),
                chart_path,
                ev.get("oos_return_pct"),
                1 if tg.get("ok") else 0,
                tg.get("error"),
                ev["dedupe_key"],
                now,
            ),
        )
        sid = int(cur.lastrowid)
        conn.execute(
            """
            INSERT INTO signal_bar_state(symbol, timeframe, strategy, last_bar_ts, last_signal, updated_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(symbol, timeframe, strategy) DO UPDATE SET
              last_bar_ts=excluded.last_bar_ts,
              last_signal=excluded.last_signal,
              updated_at=excluded.updated_at
            """,
            (
                ev["symbol"],
                ev["timeframe"],
                ev["strategy"],
                ev["bar_ts"],
                ev.get("signal_to", 0),
                now,
            ),
        )
    return sid


def process_watch_item(item: dict[str, Any], *, send_tg: bool = True) -> dict[str, Any]:
    ev = evaluate_pair(
        item["symbol"],
        item["timeframe"],
        item["strategy"],
    )
    if not ev.get("ok"):
        return {"ok": False, **ev, "watch": item}
    if not ev.get("event_type"):
        return {"ok": True, "skipped": True, "reason": "no transition", "watch": item}

    ev["oos_return_pct"] = item.get("oos_return_pct")
    if _already_processed(ev["dedupe_key"]):
        return {"ok": True, "skipped": True, "reason": "duplicate", "watch": item}

    chart_path = None
    ohlcv = ev.pop("ohlcv", None)
    if ohlcv is not None:
        p = render_signal_chart(
            ohlcv,
            symbol=ev["symbol"],
            timeframe=ev["timeframe"],
            strategy=ev["strategy"],
            event_type=ev["event_type"],
            price=ev["price"],
            stop_price=ev.get("stop_price"),
        )
        chart_path = str(p) if p else None

    tg: dict[str, Any] = {"ok": False, "skipped": True}
    if send_tg:
        from pathlib import Path

        tg = send_signal(ev, Path(chart_path) if chart_path else None)

    sid = _persist_signal(ev, chart_path, tg)
    paper = execute_paper(ev, sid)

    return {
        "ok": True,
        "signal_id": sid,
        "event": ev,
        "telegram": tg,
        "paper": paper,
        "chart": chart_path,
    }


def run_scan(*, send_telegram: bool = True) -> dict[str, Any]:
    stop_hits = _process_stop_losses(send_telegram=send_telegram)
    watch = get_watchlist()
    fired: list[dict[str, Any]] = list(stop_hits)
    skipped = 0
    errors = 0
    for item in watch:
        try:
            out = process_watch_item(item, send_tg=send_telegram)
            if not out.get("ok"):
                errors += 1
            elif out.get("skipped"):
                skipped += 1
            elif out.get("signal_id"):
                fired.append(out)
        except Exception as e:
            errors += 1
            fired.append({"ok": False, "error": str(e), "watch": item})
    return {
        "ok": True,
        "watch_count": len(watch),
        "fired": len([f for f in fired if f.get("signal_id")]),
        "stops": len(stop_hits),
        "skipped": skipped,
        "errors": errors,
        "telegram_configured": telegram_configured(),
        "portfolio": portfolio_status(),
        "details": fired[:20],
    }


def _process_stop_losses(*, send_telegram: bool = True) -> list[dict[str, Any]]:
    """Закрытие paper-позиций по stop_price."""
    from engine.signal_eval import evaluate_pair

    hits: list[dict[str, Any]] = []
    with db_session() as conn:
        positions = [
            dict(r) for r in conn.execute("SELECT * FROM paper_positions").fetchall()
        ]
    prices: dict[tuple[str, str, str], float] = {}
    for pos in positions:
        ev = evaluate_pair(pos["symbol"], pos["timeframe"], pos["strategy"])
        if ev.get("ok"):
            prices[(pos["symbol"], pos["timeframe"], pos["strategy"])] = float(ev["price"])

    for ev in check_stops(prices):
        ev["dedupe_key"] = (
            f"{ev['symbol']}|{ev['timeframe']}|{ev['strategy']}|stop|{ev['price']}"
        )
        if _already_processed(ev["dedupe_key"]):
            continue
        tg: dict[str, Any] = {"ok": False, "skipped": True}
        if send_telegram:
            tg = send_signal(ev, None)
        sid = _persist_signal(ev, None, tg)
        paper = execute_paper(ev, sid)
        hits.append({"ok": True, "signal_id": sid, "event": ev, "telegram": tg, "paper": paper})
    return hits


def recent_signals(limit: int = 50) -> list[dict[str, Any]]:
    with db_session() as conn:
        rows = rows_to_dicts(
            conn.execute(
                "SELECT * FROM signal_events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        )
    for r in rows:
        if r.get("indicators"):
            try:
                r["indicators"] = json.loads(r["indicators"])
            except json.JSONDecodeError:
                pass
    return rows


def _scan_loop(interval: int) -> None:
    while not _stop.is_set():
        try:
            run_scan(send_telegram=True)
        except Exception:
            pass
        _stop.wait(interval)


def start_background_scanner(interval: int | None = None) -> bool:
    global _scan_thread
    from config import SIGNAL_SCAN_INTERVAL_SEC

    sec = interval or SIGNAL_SCAN_INTERVAL_SEC
    with _scan_lock:
        if _scan_thread and _scan_thread.is_alive():
            return False
        _stop.clear()
        _scan_thread = threading.Thread(
            target=_scan_loop, args=(sec,), daemon=True, name="vero-signal-scan"
        )
        _scan_thread.start()
    return True


def stop_background_scanner() -> None:
    _stop.set()
