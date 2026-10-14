#!/usr/bin/env python3
"""VERO web app — XTB backtests + learn. http://127.0.0.1:8900"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import (  # noqa: E402
    ALLOWED_TIMEFRAMES,
    CANDLE_LIMIT_PRESETS,
    CANDLE_LIMIT_UI_MAX,
    CANDLE_LIMIT_UI_MIN,
    CHARTS_DIR,
    DEFAULT_CANDLE_LIMIT,
    DEFAULT_CASH,
    DEFAULT_FEE_RATE,
    DEFAULT_SAVE_TRADES,
    HOST,
    PORT,
    REQUIRE_LOCAL_CANDLES,
    ROOT,
    SIGNAL_SCAN_ENABLED,
    SIGNAL_SCAN_INTERVAL_SEC,
    STRATEGY_IDS,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    WEB_DIR,
    YAHOO_CACHE_MAX_AGE_SEC,
    YAHOO_FALLBACK,
    YAHOO_FIRST,
    TV_ENABLED,
    STOOQ_ENABLED,
    OHLCV_SOURCE_ORDER,
    PAUSE_YAHOO_MAX,
    PAUSE_YAHOO_MIN,
    PAUSE_TV_MAX,
    PAUSE_TV_MIN,
    PAUSE_STOOQ_MAX,
    PAUSE_STOOQ_MIN,
    ensure_dirs,
)
from bootstrap import autonomy_status, bootstrap, repair_candles_link  # noqa: E402
from db import db_session, init_db, rows_to_dicts  # noqa: E402
from engine.queue import QUEUE  # noqa: E402
from engine.local_candles import (  # noqa: E402
    get_max_local_bars,
    local_candles_available,
    local_symbol_count,
)
from engine.scheduler import set_session_budget, snapshot as sched_snapshot  # noqa: E402
from engine.yahoo import catalog_stats, universe_symbols  # noqa: E402
from engine.prefetch import PREFETCH  # noqa: E402
from engine.vero_store import list_symbols_covering, store_stats  # noqa: E402
from engine.truth import (  # noqa: E402
    TRUTH_SL_PCT,
    TRUTH_TAG_PREFIX,
    TRUTH_TFS,
    TRUTH_TOP_N,
    TRUTH_UNIVERSE,
    audit_top,
    auto_top_truth,
    find_truth_run,
    prefetch_ready,
    render_one_audit_chart,
)
from modes import get_mode, list_modes  # noqa: E402
from learn.patterns import (  # noqa: E402
    build_clusters,
    learn_summary,
    list_clusters,
    list_rules,
    upsert_rule,
)
from engine.signal_scanner import (  # noqa: E402
    get_watchlist,
    recent_signals,
    run_scan,
    start_background_scanner,
    stop_background_scanner,
)
from engine.telegram_notify import discover_chat_ids, telegram_configured  # noqa: E402
from engine.paper import portfolio_status  # noqa: E402


def _resume_active_backtest() -> None:
    """Авто-Resume только если прогон был running/daily_pause (не manual paused)."""
    import threading

    def _go() -> None:
        from engine.queue import QUEUE
        from engine.scheduler import set_session_budget

        with db_session() as conn:
            row = conn.execute(
                """
                SELECT id FROM runs
                WHERE status IN ('running', 'daily_pause')
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
        if not row:
            return
        if QUEUE._thread and QUEUE._thread.is_alive():
            return
        keep_id = int(row["id"])
        with db_session() as conn:
            conn.execute(
                """
                UPDATE runs SET status='paused', message=?, updated_at=?
                WHERE status IN ('running','daily_pause') AND id!=?
                """,
                (
                    "paused: stale worker, continuing latest run",
                    __import__("datetime").datetime.now(
                        __import__("datetime").timezone.utc
                    ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    keep_id,
                ),
            )
        set_session_budget(None, label="cycle")
        try:
            QUEUE.start(resume_run_id=keep_id, save_trades=True)
        except RuntimeError:
            pass

    threading.Thread(target=_go, daemon=True, name="vero-resume").start()

app = FastAPI(title="VERO", docs_url="/api/docs")
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")


def resolve_candle_limit(value: int | None, *, use_max: bool = False) -> int:
    """1000–8000 или max (=8000 cap, фактически min(available, cap))."""
    if use_max or value == 0:
        return CANDLE_LIMIT_UI_MAX
    n = int(value if value is not None else DEFAULT_CANDLE_LIMIT)
    if n < CANDLE_LIMIT_UI_MIN:
        return CANDLE_LIMIT_UI_MIN
    if n > CANDLE_LIMIT_UI_MAX:
        return CANDLE_LIMIT_UI_MAX
    return n


def offline_meta() -> dict[str, Any]:
    offline = not YAHOO_FALLBACK and REQUIRE_LOCAL_CANDLES and not YAHOO_FIRST and not TV_ENABLED
    if "tv" in OHLCV_SOURCE_ORDER and TV_ENABLED:
        label_uk = "local → TradingView → Yahoo → Stooq"
        label_en = "local → TradingView → Yahoo → Stooq"
    elif YAHOO_FIRST:
        label_uk = "yahoo-first · живі дані"
        label_en = "yahoo-first · live data"
    elif offline:
        label_uk = "offline · без мережі"
        label_en = "offline · no network"
    else:
        label_uk = "online · Yahoo fallback"
        label_en = "online · Yahoo fallback"
    return {
        "offline": offline,
        "yahoo_first": YAHOO_FIRST,
        "yahoo_fallback": YAHOO_FALLBACK,
        "require_local": REQUIRE_LOCAL_CANDLES,
        "label_uk": label_uk,
        "label_en": label_en,
        "order": list(OHLCV_SOURCE_ORDER),
    }


def data_source_meta() -> dict[str, Any]:
    return {
        "yahoo_first": YAHOO_FIRST,
        "yahoo_fallback": YAHOO_FALLBACK,
        "require_local": REQUIRE_LOCAL_CANDLES,
        "tv": TV_ENABLED,
        "stooq": STOOQ_ENABLED,
        "order": list(OHLCV_SOURCE_ORDER),
        "cache_max_age_sec": YAHOO_CACHE_MAX_AGE_SEC,
        "pause_yahoo_sec": [PAUSE_YAHOO_MIN, PAUSE_YAHOO_MAX],
        "pause_tv_sec": [PAUSE_TV_MIN, PAUSE_TV_MAX],
        "pause_stooq_sec": [PAUSE_STOOQ_MIN, PAUSE_STOOQ_MAX],
        "default_save_trades": DEFAULT_SAVE_TRADES,
    }


def candle_meta() -> dict[str, Any]:
    loc_max = get_max_local_bars() if local_candles_available() else 0
    by_tf = {}
    if local_candles_available():
        for tf in ALLOWED_TIMEFRAMES:
            by_tf[tf] = get_max_local_bars(tf)
    return {
        "default": DEFAULT_CANDLE_LIMIT,
        "min": CANDLE_LIMIT_UI_MIN,
        "max": CANDLE_LIMIT_UI_MAX,
        "presets": list(CANDLE_LIMIT_PRESETS),
        "local_max_bars": loc_max,
        "local_max_by_tf": by_tf,
        "effective_max": min(loc_max, CANDLE_LIMIT_UI_MAX) if loc_max else CANDLE_LIMIT_UI_MAX,
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    st = autonomy_status()
    return {"ok": True, "app": "VERO", "port": PORT, "autonomous": st["ready"]}


@app.on_event("startup")
def _startup() -> None:
    ensure_dirs()
    repair_candles_link()
    init_db()
    st = autonomy_status()
    if not st["ready"]:
        bootstrap()
    if SIGNAL_SCAN_ENABLED:
        start_background_scanner(SIGNAL_SCAN_INTERVAL_SEC)
    _resume_active_backtest()
    # Prefetch: только если в state остался running/stopping (падение/рестарт)
    try:
        PREFETCH.try_auto_resume()
    except Exception:
        pass


@app.on_event("shutdown")
def _shutdown() -> None:
    stop_background_scanner()


@app.get("/api/autonomy")
def api_autonomy() -> dict[str, Any]:
    return autonomy_status()


@app.post("/api/bootstrap")
def api_bootstrap() -> dict[str, Any]:
    return bootstrap()


@app.get("/api/modes")
def api_modes() -> dict[str, Any]:
    return {"modes": list_modes(), "autonomy": autonomy_status()}


class RunModeOpts(BaseModel):
    candle_limit: int | None = None
    candle_max: bool = False


@app.post("/api/run/mode/{mode_id}")
def run_mode(mode_id: str, body: RunModeOpts | None = None) -> dict[str, Any]:
    mode = get_mode(mode_id)
    if not mode:
        raise HTTPException(404, f"unknown mode {mode_id}")

    action = mode.get("action")
    if action == "preflight":
        from agents.control import run_preflight
        return {"action": "preflight", **run_preflight()}
    if action == "bootstrap":
        return {"action": "bootstrap", **bootstrap()}
    if action == "learn":
        return {"action": "learn", **build_clusters()}
    if action == "prefetch":
        try:
            st = PREFETCH.start(
                universe=mode.get("universe", "seed"),
                timeframes=mode.get("timeframes"),
                target_bars=int(mode.get("candle_limit") or DEFAULT_CANDLE_LIMIT),
            )
            return {"action": "prefetch", **st}
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    st = autonomy_status()
    if not st["ready"]:
        raise HTTPException(
            503,
            "Данные не готовы. Нажми Bootstrap или ./start.sh bootstrap",
        )

    opts = body or RunModeOpts()
    if mode_id == "smoke":
        limit = mode.get("candle_limit", 200)
    elif opts.candle_limit is not None or opts.candle_max:
        limit = resolve_candle_limit(opts.candle_limit, use_max=opts.candle_max)
    else:
        limit = resolve_candle_limit(mode.get("candle_limit"))

    try:
        return {
            "mode": mode_id,
            "candle_limit": limit,
            **QUEUE.start(
                universe=mode["universe"],
                timeframes=mode["timeframes"],
                strategies=mode["strategies"],
                candle_limit=int(limit),
                save_trades=mode.get("save_trades", False),
                stop_loss_pct=mode.get("stop_loss_pct"),
                tag_prefix=mode.get("tag_prefix") or "",
            ),
        }
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/meta")
def meta() -> dict[str, Any]:
    return {
        "app": "VERO",
        "root": str(ROOT),
        "autonomy": autonomy_status(),
        "modes": [m["id"] for m in list_modes()],
        "timeframes": list(ALLOWED_TIMEFRAMES),
        "strategies": list(STRATEGY_IDS),
        "catalog": catalog_stats(),
        "local_candles": {
            "available": local_candles_available(),
            "symbols_1h": local_symbol_count("1h") if local_candles_available() else 0,
            "symbols_30m": local_symbol_count("30m") if local_candles_available() else 0,
            "symbols_4h": local_symbol_count("4h") if local_candles_available() else 0,
        },
        "vero_store": store_stats(),
        "prefetch": PREFETCH.status(),
        "defaults": {
            "candle_limit": DEFAULT_CANDLE_LIMIT,
            "cash": DEFAULT_CASH,
            "fee_rate": DEFAULT_FEE_RATE,
        },
        "offline": offline_meta(),
        "data_source": data_source_meta(),
        "candles": candle_meta(),
        "job": QUEUE.snapshot(),
    }


@app.get("/api/scheduler")
def api_scheduler() -> dict[str, Any]:
    return sched_snapshot()


class LaunchRun(BaseModel):
    """Запуск full backtest с выбранной длительностью сессии."""
    hours: float | None = Field(
        None,
        description="2|4|6|8 — лимит часов; null/0 — цикл до Stop",
    )
    universe: str = "full"
    resume: bool = False
    candle_limit: int | None = None
    candle_max: bool = False
    save_trades: bool | None = None


@app.post("/api/run/launch")
def run_launch(body: LaunchRun) -> dict[str, Any]:
    st = autonomy_status()
    if not st["ready"]:
        raise HTTPException(
            503,
            "Дані не готові / Data not ready — Bootstrap first",
        )

    allowed_hours = {2.0, 4.0, 6.0, 8.0}
    hours = body.hours
    if hours is not None and hours > 0 and hours not in allowed_hours:
        raise HTTPException(400, "hours must be 2, 4, 6, 8 or null (cycle)")

    if hours is None or hours <= 0:
        sched = set_session_budget(None, label="cycle")
        launch_label = "cycle"
    else:
        sched = set_session_budget(hours, label=f"{int(hours)}h")
        launch_label = f"{int(hours)}h"

    mode = get_mode("daily")
    limit = resolve_candle_limit(body.candle_limit, use_max=body.candle_max)
    save_trades = body.save_trades if body.save_trades is not None else DEFAULT_SAVE_TRADES
    try:
        if body.resume:
            with db_session() as conn:
                row = conn.execute(
                    """
                    SELECT id FROM runs
                    WHERE status IN ('paused','daily_pause','running')
                    ORDER BY id DESC LIMIT 1
                    """
                ).fetchone()
            if not row:
                raise HTTPException(404, "no run to resume")
            out = QUEUE.start(resume_run_id=int(row["id"]), save_trades=True)
        else:
            out = QUEUE.start(
                universe=body.universe or mode["universe"],
                timeframes=mode["timeframes"],
                strategies=mode["strategies"],
                candle_limit=limit,
                save_trades=save_trades,
            )
        return {
            "launch": launch_label,
            "candle_limit": limit,
            "candle_max": body.candle_max,
            "save_trades": save_trades,
            "data_source": data_source_meta(),
            "scheduler": sched,
            **out,
        }
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


class StartRun(BaseModel):
    universe: str = "full"  # full | qualified | top | seed
    timeframes: list[str] = Field(default_factory=lambda: list(ALLOWED_TIMEFRAMES))
    strategies: list[str] = Field(default_factory=lambda: list(STRATEGY_IDS))
    candle_limit: int = DEFAULT_CANDLE_LIMIT
    cash: float = DEFAULT_CASH
    fee_rate: float = DEFAULT_FEE_RATE
    stop_loss_pct: float | None = None
    save_trades: bool = True
    symbols: str | None = None  # optional CSV override


@app.post("/api/run/start")
def run_start(body: StartRun) -> dict[str, Any]:
    tfs = [t for t in body.timeframes if t in ALLOWED_TIMEFRAMES]
    strats = [s for s in body.strategies if s in STRATEGY_IDS]
    if not tfs or not strats:
        raise HTTPException(400, "need timeframes and strategies")
    limit = resolve_candle_limit(body.candle_limit)
    override = None
    if body.symbols:
        override = [s.strip() for s in body.symbols.split(",") if s.strip()]
    try:
        return QUEUE.start(
            universe=body.universe,
            timeframes=tfs,
            strategies=strats,
            candle_limit=limit,
            cash=body.cash,
            fee_rate=body.fee_rate,
            stop_loss_pct=body.stop_loss_pct,
            save_trades=body.save_trades,
            symbols_override=override,
        )
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


class ResumeRun(BaseModel):
    run_id: int


@app.post("/api/run/resume")
def run_resume(body: ResumeRun) -> dict[str, Any]:
    try:
        return {
            **QUEUE.start(resume_run_id=body.run_id, save_trades=True),
            "worker_alive": bool(QUEUE._thread and QUEUE._thread.is_alive()),
        }
    except (RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/run/stop")
def run_stop() -> dict[str, Any]:
    QUEUE.stop()
    return {"ok": True, "job": QUEUE.snapshot()}


@app.get("/api/run/status")
def run_status() -> dict[str, Any]:
    return QUEUE.enriched_snapshot()


@app.get("/api/runs")
def list_runs(limit: int = 50) -> dict[str, Any]:
    with db_session() as conn:
        rows = rows_to_dicts(
            conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        )
    for r in rows:
        r["timeframes"] = json.loads(r["timeframes"] or "[]")
        r["strategies"] = json.loads(r["strategies"] or "[]")
    return {"runs": rows}


@app.get("/api/results")
def results(
    run_id: int | None = None,
    latest: bool = False,
    symbol: str | None = None,
    timeframe: str | None = None,
    strategy: str | None = None,
    min_oos: float | None = None,
    min_pf: float | None = None,
    min_wr: float | None = None,
    max_dd: float | None = None,
    min_n: int | None = None,
    sort: str = "oos_return_pct",
    order: str = "desc",
    limit: int = 300,
    offset: int = 0,
) -> dict[str, Any]:
    allowed_sort = {
        "symbol",
        "timeframe",
        "strategy",
        "oos_return_pct",
        "return_pct",
        "profit_factor",
        "oos_profit_factor",
        "winrate_pct",
        "oos_winrate_pct",
        "max_dd_pct",
        "oos_max_dd_pct",
        "closed_rounds",
        "oos_closed",
        "trades",
    }
    if sort not in allowed_sort:
        sort = "oos_return_pct"
    direction = "DESC" if order.lower() != "asc" else "ASC"
    limit = max(1, min(int(limit), 2000))
    offset = max(0, int(offset))
    where = ["1=1"]
    args: list[Any] = []
    resolved_run: int | None = run_id
    with db_session() as conn:
        if latest and resolved_run is None:
            row = conn.execute("SELECT id FROM runs ORDER BY id DESC LIMIT 1").fetchone()
            if row:
                resolved_run = int(row["id"])
        if resolved_run is not None:
            where.append("run_id=?")
            args.append(resolved_run)
        if symbol:
            where.append("symbol LIKE ?")
            args.append(f"%{symbol.strip()}%")
        if timeframe:
            where.append("timeframe=?")
            args.append(timeframe)
        if strategy:
            where.append("strategy=?")
            args.append(strategy)
        if min_oos is not None:
            where.append("oos_return_pct >= ?")
            args.append(min_oos)
        if min_pf is not None:
            where.append("COALESCE(oos_profit_factor, profit_factor) >= ?")
            args.append(min_pf)
        if min_wr is not None:
            where.append("COALESCE(oos_winrate_pct, winrate_pct) >= ?")
            args.append(min_wr)
        if max_dd is not None:
            where.append("COALESCE(oos_max_dd_pct, max_dd_pct) <= ?")
            args.append(max_dd)
        if min_n is not None:
            where.append("COALESCE(oos_closed, closed_rounds, 0) >= ?")
            args.append(int(min_n))
        sql = f"""
          SELECT * FROM results
          WHERE {' AND '.join(where)}
          ORDER BY {sort} IS NULL, {sort} {direction}
          LIMIT ? OFFSET ?
        """
        rows = rows_to_dicts(conn.execute(sql, [*args, limit, offset]).fetchall())
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM results WHERE {' AND '.join(where)}",
            args,
        ).fetchone()["n"]
    return {
        "total": total,
        "rows": rows,
        "limit": limit,
        "offset": offset,
        "sort": sort,
        "order": direction.lower(),
        "run_id": resolved_run,
    }


@app.get("/api/top/auto")
def top_auto(
    run_id: int | None = None,
    limit: int = TRUTH_TOP_N,
    min_trades: int = 4,
    legacy: bool = False,
) -> dict[str, Any]:
    """Топ по честному SL-прогону. legacy=1 — старый рейтинг без стопа."""
    if not legacy:
        out = auto_top_truth(limit=limit, min_trades=min_trades, run_id=run_id)
        if out.get("truth") or run_id is not None:
            return out
        return out
    where = ["closed_rounds >= ?", "oos_return_pct IS NOT NULL"]
    args: list[Any] = [min_trades]
    if run_id is not None:
        where.append("run_id=?")
        args.append(run_id)
    sql = f"""
      SELECT * FROM results
      WHERE {' AND '.join(where)}
      ORDER BY oos_return_pct DESC
      LIMIT ?
    """
    args.append(limit * 5)
    with db_session() as conn:
        rows = rows_to_dicts(conn.execute(sql, args).fetchall())
    best: dict[str, dict] = {}
    for r in rows:
        s = r["symbol"]
        cur = best.get(s)
        if cur is None or float(r["oos_return_pct"] or -999) > float(
            cur["oos_return_pct"] or -999
        ):
            best[s] = r
    ranked = sorted(
        best.values(),
        key=lambda x: float(x.get("oos_return_pct") or -999),
        reverse=True,
    )[:limit]
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
    return {"truth": False, "legacy": True, "count": len(ranked), "tickers": ranked}


class UserTopItem(BaseModel):
    symbol: str
    timeframe: str | None = None
    strategy: str | None = None
    note: str = ""
    score: float = 0


@app.get("/api/top/user")
def top_user() -> dict[str, Any]:
    with db_session() as conn:
        rows = rows_to_dicts(
            conn.execute(
                "SELECT * FROM user_top ORDER BY score DESC, id DESC"
            ).fetchall()
        )
    return {"items": rows}


@app.post("/api/top/user")
def top_user_add(body: UserTopItem) -> dict[str, Any]:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db_session() as conn:
        conn.execute(
            """
            INSERT INTO user_top(symbol, timeframe, strategy, note, score, created_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(symbol, timeframe, strategy) DO UPDATE SET
              note=excluded.note, score=excluded.score
            """,
            (
                body.symbol.strip().upper(),
                body.timeframe,
                body.strategy,
                body.note,
                body.score,
                now,
            ),
        )
    return top_user()


@app.delete("/api/top/user/{item_id}")
def top_user_del(item_id: int) -> dict[str, Any]:
    with db_session() as conn:
        conn.execute("DELETE FROM user_top WHERE id=?", (item_id,))
    return top_user()


@app.get("/api/truth/status")
def api_truth_status() -> dict[str, Any]:
    pf = PREFETCH.status()
    ready, reason = prefetch_ready(pf)
    run = find_truth_run()
    q = QUEUE.snapshot()
    worker = bool(q.get("status") == "running")
    run_status = (run or {}).get("status")
    can_resume = bool(
        run
        and run_status in ("paused", "daily_pause", "running")
        and not worker
        and run_status != "done"
    )
    # running in this worker
    if run and worker and q.get("run_id") == run.get("id"):
        can_resume = False
    can_start = ready and not worker and (run is None or run_status == "done")
    if run and run_status in ("paused", "daily_pause") and ready and not worker:
        can_start = False
        can_resume = True
    can_verify = bool(run and int(run.get("ok_jobs") or 0) > 0)
    return {
        "sl_pct": TRUTH_SL_PCT,
        "timeframes": list(TRUTH_TFS),
        "top_n": TRUTH_TOP_N,
        "prefetch": {
            "status": pf.get("status"),
            "done": pf.get("done"),
            "total": pf.get("total"),
            "pct": pf.get("pct"),
            "ok": pf.get("ok"),
            "failed": pf.get("failed"),
            "skipped": pf.get("skipped"),
        },
        "prefetch_ready": ready,
        "reason": reason,
        "run": None
        if not run
        else {
            "id": run.get("id"),
            "tag": run.get("tag"),
            "status": run.get("status"),
            "done_jobs": run.get("done_jobs"),
            "total_jobs": run.get("total_jobs"),
            "ok_jobs": run.get("ok_jobs"),
            "err_jobs": run.get("err_jobs"),
            "stop_loss_pct": run.get("stop_loss_pct"),
        },
        "queue_status": q.get("status"),
        "can_start": can_start,
        "can_resume": can_resume,
        "can_verify": can_verify,
    }


class TruthRunReq(BaseModel):
    resume: bool = True
    candle_limit: int | None = None


@app.post("/api/run/truth")
def run_truth(body: TruthRunReq | None = None) -> dict[str, Any]:
    """Ручной локальный прогон full × 1h/4h/1d × 7 strat, SL 1.5%, save_trades."""
    req = body or TruthRunReq()
    pf = PREFETCH.status()
    ready, reason = prefetch_ready(pf)
    if not ready:
        raise HTTPException(409, reason)

    existing = find_truth_run()
    set_session_budget(None, label="truth_sl15")
    local_syms = list_symbols_covering(TRUTH_TFS)
    if not local_syms:
        raise HTTPException(409, "в сторе нет тикеров с 1h/4h/1d")

    try:
        if req.resume and existing and existing.get("status") in (
            "paused",
            "daily_pause",
            "running",
        ):
            out = QUEUE.start(resume_run_id=int(existing["id"]), save_trades=True)
            return {"action": "truth_resume", "sl_pct": TRUTH_SL_PCT, **out}

        out = QUEUE.start(
            universe=TRUTH_UNIVERSE,
            timeframes=list(TRUTH_TFS),
            strategies=list(STRATEGY_IDS),
            candle_limit=resolve_candle_limit(req.candle_limit),
            stop_loss_pct=TRUTH_SL_PCT,
            save_trades=True,
            tag_prefix=TRUTH_TAG_PREFIX,
            symbols_override=local_syms,
        )
        return {
            "action": "truth_start",
            "sl_pct": TRUTH_SL_PCT,
            "symbols": len(local_syms),
            **out,
        }
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


class TruthVerifyReq(BaseModel):
    with_charts: bool = False
    limit: int = TRUTH_TOP_N


@app.post("/api/top/verify")
def api_top_verify(body: TruthVerifyReq | None = None) -> dict[str, Any]:
    req = body or TruthVerifyReq()
    return audit_top(limit=min(req.limit, TRUTH_TOP_N), with_charts=req.with_charts)


@app.get("/api/top/audit-chart")
def api_top_audit_chart(result_id: int = Query(...)) -> dict[str, Any]:
    out = render_one_audit_chart(result_id)
    if not out.get("ok"):
        raise HTTPException(404, out.get("error") or "chart failed")
    return out


@app.get("/api/learn/summary")
def api_learn_summary() -> dict[str, Any]:
    return learn_summary()


class ClusterReq(BaseModel):
    n_clusters: int = 8
    run_id: int | None = None


@app.post("/api/learn/cluster")
def api_cluster(body: ClusterReq) -> dict[str, Any]:
    return build_clusters(n_clusters=body.n_clusters, run_id=body.run_id)


@app.get("/api/learn/clusters")
def api_clusters(version: str | None = None) -> dict[str, Any]:
    return list_clusters(version)


@app.get("/api/learn/rules")
def api_rules() -> dict[str, Any]:
    return {"rules": list_rules()}


class RuleReq(BaseModel):
    scope: str
    scope_key: str = ""
    name: str
    payload: dict[str, Any]
    enabled: bool = True


@app.post("/api/learn/rules")
def api_rule_upsert(body: RuleReq) -> dict[str, Any]:
    return upsert_rule(
        scope=body.scope,
        scope_key=body.scope_key,
        name=body.name,
        payload=body.payload,
        enabled=body.enabled,
    )


@app.get("/api/preflight")
def api_preflight() -> dict[str, Any]:
    from agents.control import run_preflight

    return run_preflight()


class SignalScanReq(BaseModel):
    send_telegram: bool = True


@app.post("/api/signals/scan")
def api_signal_scan(body: SignalScanReq | None = None) -> dict[str, Any]:
    opts = body or SignalScanReq()
    return run_scan(send_telegram=opts.send_telegram)


@app.get("/api/signals/recent")
def api_signals_recent(limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    return {"signals": recent_signals(limit)}


@app.get("/api/signals/watchlist")
def api_signals_watchlist() -> dict[str, Any]:
    items = get_watchlist()
    return {"items": items, "count": len(items)}


@app.get("/api/paper/status")
def api_paper_status() -> dict[str, Any]:
    return portfolio_status()


@app.get("/api/telegram/discover")
def api_telegram_discover() -> dict[str, Any]:
    return discover_chat_ids()


@app.get("/api/telegram/status")
def api_telegram_status() -> dict[str, Any]:
    return {
        "configured": telegram_configured(),
        "has_token": bool(TELEGRAM_BOT_TOKEN),
        "has_chat_id": bool(TELEGRAM_CHAT_ID),
        "scan_enabled": SIGNAL_SCAN_ENABLED,
        "scan_interval_sec": SIGNAL_SCAN_INTERVAL_SEC,
    }


@app.get("/api/charts/{name}")
def api_chart(name: str) -> FileResponse:
    safe = Path(name).name
    path = CHARTS_DIR / safe
    if not path.exists():
        raise HTTPException(404, "chart not found")
    return FileResponse(path, media_type="image/png")


@app.get("/api/universe/{kind}")
def api_universe(kind: str, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    syms = universe_symbols(kind)
    return {"kind": kind, "total": len(syms), "sample": syms[:limit]}


class PrefetchReq(BaseModel):
    universe: str = "seed"
    timeframes: list[str] = Field(default_factory=lambda: ["1h", "4h", "1d"])
    candle_limit: int = DEFAULT_CANDLE_LIMIT
    force: bool = False
    resume: bool = True
    symbols: list[str] | None = None


@app.get("/api/prefetch/status")
def api_prefetch_status() -> dict[str, Any]:
    return PREFETCH.status()


@app.post("/api/prefetch/start")
def api_prefetch_start(body: PrefetchReq | None = None) -> dict[str, Any]:
    req = body or PrefetchReq()
    tfs = [t for t in req.timeframes if t in ALLOWED_TIMEFRAMES]
    limit = resolve_candle_limit(req.candle_limit)
    try:
        return PREFETCH.start(
            universe=req.universe,
            timeframes=tfs,
            target_bars=limit,
            symbols=req.symbols,
            force=req.force,
            resume=req.resume,
        )
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/prefetch/stop")
def api_prefetch_stop() -> dict[str, Any]:
    return PREFETCH.stop()


@app.get("/api/vero/store")
def api_vero_store() -> dict[str, Any]:
    return store_stats()


@app.get("/api/vero/symbols")
def api_vero_symbols(timeframe: str | None = None) -> dict[str, Any]:
    from engine.vero_store import list_symbols

    tf = timeframe if timeframe in ALLOWED_TIMEFRAMES else None
    syms = list_symbols(tf)
    return {"symbols": syms, "count": len(syms), "timeframe": tf}


@app.get("/api/chart/local")
def api_chart_local(
    symbol: str = Query(...),
    timeframe: str = Query("4h"),
    bars: int = Query(160, ge=40, le=800),
) -> dict[str, Any]:
    if timeframe not in ALLOWED_TIMEFRAMES:
        raise HTTPException(400, f"bad timeframe {timeframe}")
    from engine.chart_local import render_local_chart

    path, meta = render_local_chart(symbol.strip().upper(), timeframe, bars=bars)
    if not meta.get("ok") or path is None:
        raise HTTPException(404, meta.get("error") or "chart failed")
    return meta


def main() -> None:
    init_db()
    uvicorn.run("server:app", host=HOST, port=PORT, reload=False)


if __name__ == "__main__":
    main()
