"""Очередь полного прогона с resume."""

from __future__ import annotations

import json
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Callable

from config import (
    DEFAULT_CANDLE_LIMIT,
    DEFAULT_CASH,
    DEFAULT_FEE_RATE,
    DEFAULT_STOP_LOSS_PCT,
    DEFAULT_TIMEFRAMES,
    DEFAULT_WF_TRAIN_RATIO,
    STRATEGY_IDS,
)
from db import db_session, init_db, row_to_dict, rows_to_dicts
from engine.anti_ban import is_rate_error, job_pause, note_error
from engine.backtest import run_one
from engine.scheduler import add_active_seconds, snapshot as sched_snapshot, wait_for_budget
from engine.yahoo import universe_symbols

ProgressCb = Callable[[dict[str, Any]], None]


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pause_after_job(source: str | None) -> None:
    """Пауза только для сети. Локальный vero/local — без sleep, иначе 173k джобов спят сутками."""
    src = (source or "").lower()
    if src in ("vero", "local", "skip", ""):
        return
    time.sleep(job_pause(source=src))


class RunQueue:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.state: dict[str, Any] = {
            "status": "idle",
            "run_id": None,
            "tag": None,
            "done": 0,
            "total": 0,
            "ok": 0,
            "err": 0,
            "pct": 0.0,
            "message": "",
            "phase": "idle",
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            payload = dict(self.state)
        payload["scheduler"] = sched_snapshot()
        return payload

    def enriched_snapshot(self) -> dict[str, Any]:
        """Статус воркера + детали прогона из БД (для UI)."""
        payload = self.snapshot()
        alive = bool(self._thread and self._thread.is_alive())
        payload["worker_alive"] = alive

        with db_session() as conn:
            run_id = payload.get("run_id")
            row = None
            if run_id:
                row = conn.execute(
                    "SELECT * FROM runs WHERE id=?", (run_id,)
                ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
                ).fetchone()

            if not row:
                payload["run"] = None
                payload["last_run"] = None
                if not alive:
                    payload["status"] = "idle"
                return payload

            run = row_to_dict(row)
            run["timeframes"] = json.loads(run.get("timeframes") or "[]")
            run["strategies"] = json.loads(run.get("strategies") or "[]")
            rid = int(run["id"])

            total = int(run["total_jobs"] or 0)
            done = int(run["done_jobs"] or 0)
            ok = int(run["ok_jobs"] or 0)
            err = int(run["err_jobs"] or 0)
            # counters on runs — avoid GROUP BY over 200k+ run_jobs (blocks db lock)
            skipped = max(0, done - ok - err)
            pending = max(0, total - done)
            pct = round(done / total * 100, 1) if total else 0.0

            run_detail = {
                **run,
                "skipped_jobs": skipped,
                "pending_jobs": pending,
                "pct": pct,
                "strategies_count": len(run["strategies"]),
                "timeframes_count": len(run["timeframes"]),
            }

            db_status = run["status"]
            if alive:
                mem_status = payload.get("status") or db_status
                effective = mem_status
            else:
                effective = db_status if db_status != "running" else "paused"
                if db_status == "running":
                    run_detail["stale"] = True

            payload["run"] = run_detail
            payload["last_run"] = run_detail
            payload["run_id"] = rid
            payload["tag"] = run["tag"]
            payload["done"] = done
            payload["total"] = total
            payload["ok"] = ok
            payload["err"] = err
            payload["skipped"] = skipped
            payload["pending"] = pending
            payload["pct"] = pct

            if not alive:
                payload["status"] = effective
                payload["message"] = run.get("message") or effective
                if effective in ("paused", "daily_pause", "done", "error"):
                    payload["phase"] = effective
            elif payload.get("message"):
                run_detail["current_job"] = payload["message"]

        return payload

    def _daily_wait_tick(self, run_id: int, st: dict, wait: float) -> None:
        mode = st.get("session_mode", "daily")
        lbl = st.get("session_label", "")
        if mode == "fixed":
            msg = (
                f"session limit {st.get('budget_hours', '?')}h reached "
                f"({lbl}) — pause / ліміт сесії досягнуто"
            )
        else:
            msg = (
                f"daily limit {st.get('used_hours', 0):.1f}h / "
                f"{st.get('budget_hours', '?')}h — pause {int(wait // 60)}m"
            )
        self._set(status="daily_pause", phase="daily_pause", message=msg)
        with db_session() as conn:
            conn.execute(
                """
                UPDATE runs SET status='daily_pause', message=?, updated_at=?
                WHERE id=?
                """,
                (msg, _utc(), run_id),
            )

    def _set(self, **kwargs: Any) -> None:
        with self._lock:
            self.state.update(kwargs)

    def stop(self) -> None:
        self._stop.set()

    def start(
        self,
        *,
        universe: str = "full",
        timeframes: list[str] | None = None,
        strategies: list[str] | None = None,
        candle_limit: int = DEFAULT_CANDLE_LIMIT,
        cash: float = DEFAULT_CASH,
        fee_rate: float = DEFAULT_FEE_RATE,
        stop_loss_pct: float | None = DEFAULT_STOP_LOSS_PCT,
        resume_run_id: int | None = None,
        save_trades: bool = True,
        symbols_override: list[str] | None = None,
        tag_prefix: str = "",
    ) -> dict[str, Any]:
        if self._thread and self._thread.is_alive():
            raise RuntimeError("Run already in progress")
        self._stop.clear()
        init_db()

        if resume_run_id:
            run_id = resume_run_id
            with db_session() as conn:
                run = conn.execute(
                    "SELECT * FROM runs WHERE id=?", (run_id,)
                ).fetchone()
                if not run:
                    raise ValueError(f"run {run_id} not found")
                if run["status"] == "done":
                    raise ValueError("run already done")
                tag = run["tag"]
                total = run["total_jobs"]
                cursor = run["cursor_idx"]
                conn.execute(
                    "UPDATE runs SET status='running', updated_at=?, message=? WHERE id=?",
                    (_utc(), "resuming", run_id),
                )
        else:
            tfs = list(timeframes or DEFAULT_TIMEFRAMES)
            strats = list(strategies or STRATEGY_IDS)
            symbols = symbols_override or universe_symbols(universe)
            if not symbols:
                raise ValueError("empty universe")
            jobs = [
                (sym, tf, st)
                for sym in symbols
                for tf in tfs
                for st in strats
            ]
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            tag = f"{tag_prefix}{stamp}" if tag_prefix else stamp
            now = _utc()
            with db_session() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO runs(
                      tag, status, universe, timeframes, strategies,
                      candle_limit, cash, fee_rate, stop_loss_pct,
                      total_jobs, done_jobs, ok_jobs, err_jobs, cursor_idx,
                      message, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,0,0,0,0,?,?,?)
                    """,
                    (
                        tag,
                        "running",
                        universe,
                        json.dumps(tfs),
                        json.dumps(strats),
                        candle_limit,
                        cash,
                        fee_rate,
                        stop_loss_pct,
                        len(jobs),
                        "starting",
                        now,
                        now,
                    ),
                )
                run_id = int(cur.lastrowid)
                conn.executemany(
                    """
                    INSERT INTO run_jobs(run_id, idx, symbol, timeframe, strategy, status)
                    VALUES (?,?,?,?,?,'pending')
                    """,
                    [
                        (run_id, i, sym, tf, st)
                        for i, (sym, tf, st) in enumerate(jobs)
                    ],
                )
            total = len(jobs)
            cursor = 0

        self._set(
            status="running",
            run_id=run_id,
            tag=tag,
            done=cursor,
            total=total,
            ok=0,
            err=0,
            pct=round(cursor / total * 100, 1) if total else 0,
            message="running",
            phase="backtest",
        )

        self._thread = threading.Thread(
            target=self._worker,
            kwargs={
                "run_id": run_id,
                "candle_limit": candle_limit if not resume_run_id else None,
                "cash": cash,
                "fee_rate": fee_rate,
                "stop_loss_pct": stop_loss_pct,
                "save_trades": save_trades,
                "resume": bool(resume_run_id),
            },
            daemon=True,
        )
        self._thread.start()
        return self.snapshot()

    def _worker(
        self,
        *,
        run_id: int,
        candle_limit: int | None,
        cash: float,
        fee_rate: float,
        stop_loss_pct: float | None,
        save_trades: bool,
        resume: bool,
    ) -> None:
        try:
            with db_session() as conn:
                run = dict(conn.execute(
                    "SELECT * FROM runs WHERE id=?", (run_id,)
                ).fetchone())
            limit = candle_limit or int(run["candle_limit"])
            cash = float(run["cash"]) if resume else cash
            fee_rate = float(run["fee_rate"]) if resume else fee_rate
            sl = run["stop_loss_pct"] if resume else stop_loss_pct
            if sl is not None:
                sl = float(sl)

            with db_session() as conn:
                ok0 = conn.execute(
                    "SELECT ok_jobs, err_jobs, done_jobs FROM runs WHERE id=?",
                    (run_id,),
                ).fetchone()
            ok_n = int(ok0["ok_jobs"] or 0)
            err_n = int(ok0["err_jobs"] or 0)
            done_n = int(ok0["done_jobs"] or 0)
            total = int(run["total_jobs"])

            while True:
                with db_session() as conn:
                    job_row = conn.execute(
                        """
                        SELECT * FROM run_jobs
                        WHERE run_id=? AND status='pending'
                        ORDER BY idx
                        LIMIT 1
                        """,
                        (run_id,),
                    ).fetchone()
                if not job_row:
                    break
                job = dict(job_row)
                if self._stop.is_set():
                    with db_session() as conn:
                        conn.execute(
                            """
                            UPDATE runs SET status='paused', message=?, updated_at=?
                            WHERE id=?
                            """,
                            ("paused by user", _utc(), run_id),
                        )
                    self._set(status="paused", phase="paused", message="paused")
                    return

                if not wait_for_budget(
                    self._stop,
                    on_tick=lambda st, w: self._daily_wait_tick(run_id, st, w),
                ):
                    with db_session() as conn:
                        conn.execute(
                            """
                            UPDATE runs SET status='paused', message=?, updated_at=?
                            WHERE id=?
                            """,
                            ("paused by user", _utc(), run_id),
                        )
                    self._set(status="paused", phase="paused", message="paused")
                    return

                with db_session() as conn:
                    conn.execute(
                        """
                        UPDATE runs SET status='running', message=?, updated_at=?
                        WHERE id=? AND status IN ('paused','daily_pause','running')
                        """,
                        ("running", _utc(), run_id),
                    )

                sym = job["symbol"]
                tf = job["timeframe"]
                st = job["strategy"]
                idx = job["idx"]
                t0 = time.time()
                self._set(
                    message=f"{sym} {tf} {st}",
                    done=done_n,
                    total=total,
                    ok=ok_n,
                    err=err_n,
                    pct=round(done_n / total * 100, 1) if total else 0,
                    phase="backtest",
                    status="running",
                )

                job_source = "local"
                try:
                    out = run_one(
                        sym,
                        tf,
                        st,
                        limit=limit,
                        cash=cash,
                        fee_rate=fee_rate,
                        train_ratio=DEFAULT_WF_TRAIN_RATIO,
                        stop_loss_pct=sl,
                    )
                    if out.get("skipped"):
                        done_n += 1
                        job_source = out.get("source") or job_source
                        with db_session() as conn:
                            conn.execute(
                                "UPDATE run_jobs SET status='skipped', error=? WHERE id=?",
                                (out.get("error") or "skipped", job["id"]),
                            )
                            conn.execute(
                                """
                                UPDATE runs SET done_jobs=?, ok_jobs=?, err_jobs=?,
                                  cursor_idx=?, message=?, updated_at=?
                                WHERE id=?
                                """,
                                (
                                    done_n,
                                    ok_n,
                                    err_n,
                                    idx + 1,
                                    f"skip {sym}",
                                    _utc(),
                                    run_id,
                                ),
                            )
                        add_active_seconds(time.time() - t0)
                        self._set(
                            done=done_n,
                            ok=ok_n,
                            err=err_n,
                            pct=round(done_n / total * 100, 1) if total else 0,
                            scheduler=sched_snapshot(),
                        )
                        _pause_after_job(job_source)
                        continue

                    job_source = out.get("source") or job_source

                    if not out.get("ok"):
                        err = out.get("error") or "failed"
                        if is_rate_error(str(err)):
                            note_error(str(err))
                        raise RuntimeError(err)

                    with db_session() as conn:
                        cur = conn.execute(
                            """
                            INSERT INTO results(
                              run_id, symbol, timeframe, strategy,
                              return_pct, oos_return_pct, winrate_pct, oos_winrate_pct,
                              profit_factor, oos_profit_factor, max_dd_pct, oos_max_dd_pct,
                              trades, closed_rounds, oos_trades, oos_closed,
                              stop_exits, final_usd, candles, yahoo_symbol, extras, created_at
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(run_id, symbol, timeframe, strategy) DO UPDATE SET
                              return_pct=excluded.return_pct,
                              oos_return_pct=excluded.oos_return_pct,
                              winrate_pct=excluded.winrate_pct,
                              oos_winrate_pct=excluded.oos_winrate_pct,
                              profit_factor=excluded.profit_factor,
                              oos_profit_factor=excluded.oos_profit_factor,
                              max_dd_pct=excluded.max_dd_pct,
                              oos_max_dd_pct=excluded.oos_max_dd_pct,
                              trades=excluded.trades,
                              closed_rounds=excluded.closed_rounds,
                              oos_trades=excluded.oos_trades,
                              oos_closed=excluded.oos_closed,
                              stop_exits=excluded.stop_exits,
                              final_usd=excluded.final_usd,
                              candles=excluded.candles,
                              yahoo_symbol=excluded.yahoo_symbol,
                              extras=excluded.extras,
                              created_at=excluded.created_at
                            """,
                            (
                                run_id,
                                sym,
                                tf,
                                st,
                                out.get("return_pct"),
                                out.get("oos_return_pct"),
                                out.get("winrate_pct"),
                                out.get("oos_winrate_pct"),
                                out.get("profit_factor"),
                                out.get("oos_profit_factor"),
                                out.get("max_dd_pct"),
                                out.get("oos_max_dd_pct"),
                                out.get("trades"),
                                out.get("closed_rounds"),
                                out.get("oos_trades"),
                                out.get("oos_closed"),
                                out.get("stop_exits"),
                                out.get("final_usd"),
                                out.get("candles"),
                                out.get("yahoo_symbol"),
                                json.dumps({
                                    "features": out.get("features") or {},
                                    "source": out.get("source"),
                                    "mapped": out.get("yahoo_symbol"),
                                }),
                                _utc(),
                            ),
                        )
                        result_id = int(cur.lastrowid)
                        # ON CONFLICT update may not give lastrowid — resolve
                        if result_id <= 0:
                            row = conn.execute(
                                """
                                SELECT id FROM results
                                WHERE run_id=? AND symbol=? AND timeframe=? AND strategy=?
                                """,
                                (run_id, sym, tf, st),
                            ).fetchone()
                            result_id = int(row["id"])

                        if save_trades:
                            conn.execute(
                                "DELETE FROM trades WHERE result_id=?", (result_id,)
                            )
                            conn.executemany(
                                """
                                INSERT INTO trades(result_id, bar, timestamp, side, price, qty, fee, reason)
                                VALUES (?,?,?,?,?,?,?,?)
                                """,
                                [
                                    (
                                        result_id,
                                        t["bar"],
                                        t["timestamp"],
                                        t["side"],
                                        t["price"],
                                        t["qty"],
                                        t["fee"],
                                        t["reason"],
                                    )
                                    for t in out.get("trade_rows") or []
                                ],
                            )

                        # learn sample
                        feats = {
                            "return_pct": out.get("return_pct"),
                            "oos_return_pct": out.get("oos_return_pct"),
                            "winrate_pct": out.get("winrate_pct"),
                            "oos_winrate_pct": out.get("oos_winrate_pct"),
                            "profit_factor": out.get("profit_factor"),
                            "oos_profit_factor": out.get("oos_profit_factor"),
                            "max_dd_pct": out.get("max_dd_pct"),
                            "oos_max_dd_pct": out.get("oos_max_dd_pct"),
                            "closed_rounds": out.get("closed_rounds"),
                            "oos_closed": out.get("oos_closed"),
                            "candles": out.get("candles"),
                            "source": out.get("source"),
                            **(out.get("features") or {}),
                        }
                        label = _label(feats)
                        conn.execute(
                            """
                            INSERT INTO learn_samples(
                              result_id, run_id, symbol, timeframe, strategy,
                              features, label, cluster_id, created_at
                            ) VALUES (?,?,?,?,?,?,?,NULL,?)
                            """,
                            (
                                result_id,
                                run_id,
                                sym,
                                tf,
                                st,
                                json.dumps(feats),
                                label,
                                _utc(),
                            ),
                        )
                        conn.execute(
                            "UPDATE run_jobs SET status='ok', error=NULL WHERE id=?",
                            (job["id"],),
                        )
                        ok_n += 1
                        done_n += 1
                        conn.execute(
                            """
                            UPDATE runs SET done_jobs=?, ok_jobs=?, err_jobs=?,
                              cursor_idx=?, message=?, updated_at=?
                            WHERE id=?
                            """,
                            (done_n, ok_n, err_n, idx + 1, f"ok {sym}", _utc(), run_id),
                        )
                except Exception as e:
                    err_n += 1
                    done_n += 1
                    err_msg = str(e)
                    if is_rate_error(err_msg):
                        note_error(err_msg)
                    with db_session() as conn:
                        conn.execute(
                            "UPDATE run_jobs SET status='error', error=? WHERE id=?",
                            (str(e)[:500], job["id"]),
                        )
                        conn.execute(
                            """
                            UPDATE runs SET done_jobs=?, ok_jobs=?, err_jobs=?,
                              cursor_idx=?, message=?, updated_at=?
                            WHERE id=?
                            """,
                            (
                                done_n,
                                ok_n,
                                err_n,
                                idx + 1,
                                f"err {sym}: {e}"[:200],
                                _utc(),
                                run_id,
                            ),
                        )

                self._set(
                    done=done_n,
                    ok=ok_n,
                    err=err_n,
                    pct=round(done_n / total * 100, 1) if total else 0,
                    scheduler=add_active_seconds(time.time() - t0),
                )
                _pause_after_job(job_source)

            with db_session() as conn:
                conn.execute(
                    """
                    UPDATE runs SET status='done', message='completed',
                      finished_at=?, updated_at=?
                    WHERE id=?
                    """,
                    (_utc(), _utc(), run_id),
                )
            self._set(
                status="done",
                phase="done",
                message="completed",
                done=done_n,
                ok=ok_n,
                err=err_n,
                pct=100.0,
            )
        except Exception as e:
            traceback.print_exc()
            with db_session() as conn:
                conn.execute(
                    "UPDATE runs SET status='error', message=?, updated_at=? WHERE id=?",
                    (str(e)[:300], _utc(), run_id),
                )
            self._set(status="error", phase="error", message=str(e))


from learn.patterns import label_sample as _label


QUEUE = RunQueue()
