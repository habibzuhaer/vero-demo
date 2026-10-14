"""Честный прогон: SL 1.5% локально, топ-38, сверка стоп/тейк по свечам."""

from __future__ import annotations

from typing import Any

import pandas as pd

from config import CHARTS_DIR, DEFAULT_RISK, TOP_MIN_TRADES, ensure_dirs
from db import db_session, rows_to_dicts

TRUTH_TAG_PREFIX = "truth_sl15_"
TRUTH_SL_PCT = float(DEFAULT_RISK["default_stop_loss_pct"])  # 1.5
TRUTH_TFS: tuple[str, ...] = ("1h", "4h", "1d")
TRUTH_TOP_N = 38
TRUTH_UNIVERSE = "full"


def sl_price(entry: float, sl_pct: float = TRUTH_SL_PCT) -> float:
    return float(entry) * (1.0 - float(sl_pct) / 100.0)


def pair_rounds(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """BUY→SELL круги из сохранённых сделок."""
    rounds: list[dict[str, Any]] = []
    buy: dict[str, Any] | None = None
    for t in trades:
        side = (t.get("side") or "").upper()
        if side == "BUY":
            buy = t
            continue
        if side == "SELL" and buy is not None:
            entry = float(buy["price"])
            exit_px = float(t["price"])
            pnl = (exit_px / entry - 1.0) * 100.0 if entry else 0.0
            reason = t.get("reason") or "signal"
            kind = "stop" if reason == "stop_loss" else "take"
            rounds.append(
                {
                    "entry_bar": buy.get("bar"),
                    "exit_bar": t.get("bar"),
                    "entry_ts": buy.get("timestamp"),
                    "exit_ts": t.get("timestamp"),
                    "entry": entry,
                    "exit": exit_px,
                    "reason": reason,
                    "kind": kind,
                    "pnl_pct": round(pnl, 4),
                    "stop_px": round(sl_price(entry), 6),
                }
            )
            buy = None
    return rounds


def summarize_rounds(rounds: list[dict[str, Any]]) -> dict[str, Any]:
    stops = [r for r in rounds if r["kind"] == "stop"]
    takes = [r for r in rounds if r["kind"] == "take"]
    avg_take = (
        round(sum(r["pnl_pct"] for r in takes) / len(takes), 4) if takes else None
    )
    avg_stop = (
        round(sum(r["pnl_pct"] for r in stops) / len(stops), 4) if stops else None
    )
    return {
        "n_rounds": len(rounds),
        "stops": len(stops),
        "takes": len(takes),
        "avg_take_pct": avg_take,
        "avg_stop_pct": avg_stop,
    }


def find_truth_run(conn=None) -> dict[str, Any] | None:
    sql = """
      SELECT * FROM runs
      WHERE tag LIKE ? OR (stop_loss_pct IS NOT NULL AND ABS(stop_loss_pct - ?) < 0.001)
      ORDER BY id DESC LIMIT 1
    """
    args = (f"{TRUTH_TAG_PREFIX}%", TRUTH_SL_PCT)

    def _one(c) -> dict[str, Any] | None:
        row = c.execute(sql, args).fetchone()
        return dict(row) if row else None

    if conn is not None:
        return _one(conn)
    with db_session() as c:
        return _one(c)


def prefetch_ready(
    prefetch: dict[str, Any] | None,
    *,
    store_symbols: int | None = None,
) -> tuple[bool, str]:
    st = (prefetch or {}).get("status") or "idle"
    if st in ("running", "stopping"):
        return False, "prefetch ещё качает свечи"
    if st == "done":
        return True, "prefetch done"
    total = int((prefetch or {}).get("total") or 0)
    done = int((prefetch or {}).get("done") or 0)
    if st in ("stopped", "idle") and total > 0 and done >= total:
        return True, "prefetch finished"
    if store_symbols is None:
        try:
            from engine.vero_store import store_stats

            store_symbols = int(store_stats().get("symbols") or 0)
        except Exception:
            store_symbols = 0
    if int(store_symbols or 0) >= 100:
        return True, f"локальный стор {int(store_symbols)} тикеров"
    if st == "stopped":
        return False, "prefetch остановлен до конца — сначала докачай Full catalog"
    return False, "нужен prefetch full 1h/4h/1d до статуса done"


def _idx_by_ts(df: pd.DataFrame, ts: Any) -> int | None:
    if df is None or df.empty or ts is None:
        return None
    try:
        ts_i = int(ts)
    except (TypeError, ValueError):
        return None
    hits = df.index[df["timestamp"].astype("int64") == ts_i].tolist()
    if hits:
        return int(hits[0])
    # nearest within 1 bar-ish
    delta = (df["timestamp"].astype("int64") - ts_i).abs()
    j = int(delta.idxmin())
    if abs(int(df.loc[j, "timestamp"]) - ts_i) <= 3 * 24 * 3600 * 1000:
        return j
    return None


def first_stop_hit(
    df: pd.DataFrame,
    *,
    entry_i: int,
    exit_i: int,
    stop_px: float,
) -> dict[str, Any] | None:
    """Первый бар после входа, где low коснулся стопа (как в execution: не бар входа)."""
    lo = df["low"].astype(float)
    op = df["open"].astype(float)
    start = min(entry_i + 1, len(df) - 1)
    end = min(max(exit_i, start), len(df) - 1)
    if start < 0 or start >= len(df):
        return None
    for i in range(start, end + 1):
        low = float(lo.iloc[i])
        if low <= stop_px:
            open_px = float(op.iloc[i])
            return {
                "bar": int(i),
                "timestamp": int(df.iloc[i]["timestamp"]),
                "low": round(low, 6),
                "open": round(open_px, 6),
                "gap": open_px < stop_px,
            }
    return None


def audit_round(rnd: dict[str, Any], df: pd.DataFrame) -> dict[str, Any]:
    out = dict(rnd)
    if df is None or df.empty:
        out["ok"] = False
        out["check"] = "no_ohlc"
        out["note"] = "нет локальных свечей"
        return out
    entry_i = _idx_by_ts(df, rnd.get("entry_ts"))
    exit_i = _idx_by_ts(df, rnd.get("exit_ts"))
    if entry_i is None or exit_i is None:
        out["ok"] = False
        out["check"] = "ts_mismatch"
        out["note"] = "бар входа/выхода не найден в vero_bars"
        return out
    stop_px = float(rnd["stop_px"])
    hit = first_stop_hit(df, entry_i=entry_i, exit_i=exit_i, stop_px=stop_px)
    out["entry_i"] = entry_i
    out["exit_i"] = exit_i
    out["hit"] = hit
    if rnd["kind"] == "stop":
        if hit is None:
            out["ok"] = False
            out["check"] = "stop_without_touch"
            out["note"] = "в БД стоп, но low не касался SL 1.5%"
        else:
            out["ok"] = True
            out["check"] = "gap" if hit.get("gap") else "stop_ok"
            out["note"] = (
                "гэп через стоп — fill в движке по цене SL, не по open"
                if hit.get("gap")
                else "low коснулся SL 1.5%"
            )
    else:
        if hit is not None:
            out["ok"] = False
            out["check"] = "take_after_sl_path"
            out["note"] = (
                f"тейк {rnd['pnl_pct']:+.2f}%, но по пути low уже был ≤ SL "
                f"(бар {hit['bar']})"
            )
        else:
            out["ok"] = True
            out["check"] = "take_ok"
            out["note"] = f"тейк {rnd['pnl_pct']:+.2f}% — стоп по пути не задет"
    return out


def _load_ohlc(symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    from engine.vero_store import fetch_vero_bars

    df, _err = fetch_vero_bars(
        symbol, timeframe, limit=max(limit, 200), with_indicators=False
    )
    return df if df is not None else pd.DataFrame()


def _trade_stats_for_ids(conn, result_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not result_ids:
        return {}
    q = ",".join("?" * len(result_ids))
    rows = rows_to_dicts(
        conn.execute(
            f"""
            SELECT result_id, bar, timestamp, side, price, reason
            FROM trades WHERE result_id IN ({q}) ORDER BY result_id, id
            """,
            result_ids,
        ).fetchall()
    )
    by: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        by.setdefault(int(r["result_id"]), []).append(r)
    return {rid: summarize_rounds(pair_rounds(trs)) for rid, trs in by.items()}


def auto_top_truth(
    *,
    limit: int = TRUTH_TOP_N,
    min_trades: int = TOP_MIN_TRADES,
    run_id: int | None = None,
) -> dict[str, Any]:
    with db_session() as conn:
        run = None
        if run_id is not None:
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            run = dict(row) if row else None
        if run is None:
            run = find_truth_run(conn)
        if run is None:
            return {
                "truth": False,
                "count": 0,
                "tickers": [],
                "run": None,
                "message": "Нет прогона SL 1.5%. Дождись prefetch done и нажми «Прогон правда».",
            }
        rid = int(run["id"])
        rows = rows_to_dicts(
            conn.execute(
                """
                SELECT * FROM results
                WHERE run_id=? AND closed_rounds >= ? AND oos_return_pct IS NOT NULL
                ORDER BY oos_return_pct DESC
                LIMIT ?
                """,
                (rid, min_trades, max(limit * 8, 80)),
            ).fetchall()
        )
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
        stats = _trade_stats_for_ids(conn, [int(r["id"]) for r in ranked])
        for i, r in enumerate(ranked, 1):
            r["rank"] = i
            extra = stats.get(int(r["id"])) or {}
            r["stops"] = extra.get("stops", r.get("stop_exits") or 0)
            r["takes"] = extra.get("takes")
            if r["takes"] is None:
                cr = int(r.get("closed_rounds") or 0)
                r["takes"] = max(0, cr - int(r.get("stop_exits") or 0))
            r["avg_take_pct"] = extra.get("avg_take_pct")
            r["avg_stop_pct"] = extra.get("avg_stop_pct")
            r["n_rounds"] = extra.get("n_rounds", r.get("closed_rounds"))
            r["sl_pct"] = TRUTH_SL_PCT
        return {
            "truth": True,
            "count": len(ranked),
            "tickers": ranked,
            "run": {
                "id": rid,
                "tag": run.get("tag"),
                "status": run.get("status"),
                "stop_loss_pct": run.get("stop_loss_pct"),
                "done_jobs": run.get("done_jobs"),
                "total_jobs": run.get("total_jobs"),
                "ok_jobs": run.get("ok_jobs"),
            },
            "sl_pct": TRUTH_SL_PCT,
            "message": None,
        }


def audit_top(*, limit: int = TRUTH_TOP_N, with_charts: bool = False) -> dict[str, Any]:
    top = auto_top_truth(limit=limit)
    if not top.get("truth"):
        return {**top, "items": []}
    items: list[dict[str, Any]] = []
    ok_n = fail_n = 0
    with db_session() as conn:
        for row in top["tickers"]:
            rid = int(row["id"])
            trades = rows_to_dicts(
                conn.execute(
                    """
                    SELECT bar, timestamp, side, price, reason
                    FROM trades WHERE result_id=? ORDER BY id
                    """,
                    (rid,),
                ).fetchall()
            )
            rounds = pair_rounds(trades)
            candles = int(row.get("candles") or 1600)
            df = _load_ohlc(row["symbol"], row["timeframe"], candles)
            audited = [audit_round(rnd, df) for rnd in rounds]
            bad = sum(1 for a in audited if not a.get("ok"))
            good = len(audited) - bad
            ok_n += good
            fail_n += bad
            chart_url = None
            if with_charts and not df.empty:
                chart_url = render_audit_chart(
                    df,
                    symbol=row["symbol"],
                    timeframe=row["timeframe"],
                    strategy=row["strategy"],
                    rounds=audited,
                )
            items.append(
                {
                    "rank": row.get("rank"),
                    "result_id": rid,
                    "symbol": row["symbol"],
                    "timeframe": row["timeframe"],
                    "strategy": row["strategy"],
                    "oos_return_pct": row.get("oos_return_pct"),
                    "ohlc_bars": int(len(df)),
                    "ok": bad == 0 and len(audited) > 0,
                    "rounds_ok": good,
                    "rounds_fail": bad,
                    "rounds": audited,
                    "chart_url": chart_url,
                }
            )
    return {
        "truth": True,
        "sl_pct": TRUTH_SL_PCT,
        "run": top.get("run"),
        "count": len(items),
        "rounds_ok": ok_n,
        "rounds_fail": fail_n,
        "items": items,
    }


def render_audit_chart(
    df: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    strategy: str,
    rounds: list[dict[str, Any]],
    bars: int = 220,
) -> str | None:
    if df is None or df.empty:
        return None
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        return None

    from engine.chart_signal import _draw_candles

    ensure_dirs()
    idxs = []
    for r in rounds:
        if r.get("entry_i") is not None:
            idxs.append(int(r["entry_i"]))
        if r.get("exit_i") is not None:
            idxs.append(int(r["exit_i"]))
    if idxs:
        lo = max(0, min(idxs) - 20)
        hi = min(len(df), max(idxs) + 20)
        tail = df.iloc[lo:hi].copy()
        offset = lo
    else:
        tail = df.tail(bars).copy()
        offset = max(0, len(df) - len(tail))
    if tail.empty:
        return None
    ts = pd.to_datetime(tail["timestamp"], unit="ms", utc=True)

    fig, ax = plt.subplots(figsize=(11, 5.2), facecolor="#0e1412")
    ax.set_facecolor("#0e1412")
    _draw_candles(ax, ts, tail)

    sl_drawn = False
    for r in rounds:
        ei, xi = r.get("entry_i"), r.get("exit_i")
        if ei is None:
            continue
        rel_e = ei - offset
        if 0 <= rel_e < len(tail):
            ax.scatter(
                [ts.iloc[rel_e]],
                [r["entry"]],
                color="#c8f542",
                s=42,
                zorder=6,
                edgecolors="#e6efe9",
                linewidths=0.4,
            )
        if not sl_drawn:
            ax.axhline(
                r["stop_px"],
                color="#e85d5d",
                linestyle="--",
                linewidth=0.9,
                alpha=0.75,
                label=f"SL {TRUTH_SL_PCT:g}%",
                zorder=1,
            )
            sl_drawn = True
        if xi is not None:
            rel_x = xi - offset
            if 0 <= rel_x < len(tail):
                col = "#e85d5d" if r.get("kind") == "stop" else "#3ecf8e"
                ax.scatter(
                    [ts.iloc[rel_x]],
                    [r["exit"]],
                    color=col,
                    s=42,
                    zorder=6,
                    marker="x",
                    linewidths=1.2,
                )

    ax.set_title(
        f"{symbol} · {timeframe} · {strategy} · SL {TRUTH_SL_PCT:g}% audit",
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
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    fig.autofmt_xdate()
    fig.tight_layout()
    safe = f"{symbol}_{timeframe}_{strategy}".replace(".", "_").replace("/", "_")
    out = CHARTS_DIR / f"audit_{safe}.png"
    fig.savefig(out, dpi=110, facecolor=fig.get_facecolor())
    plt.close(fig)
    return f"/api/charts/{out.name}"


def render_one_audit_chart(result_id: int) -> dict[str, Any]:
    with db_session() as conn:
        row = conn.execute("SELECT * FROM results WHERE id=?", (result_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "result not found"}
        r = dict(row)
        trades = rows_to_dicts(
            conn.execute(
                """
                SELECT bar, timestamp, side, price, reason
                FROM trades WHERE result_id=? ORDER BY id
                """,
                (result_id,),
            ).fetchall()
        )
    df = _load_ohlc(r["symbol"], r["timeframe"], int(r.get("candles") or 1600))
    audited = [audit_round(rnd, df) for rnd in pair_rounds(trades)]
    url = render_audit_chart(
        df,
        symbol=r["symbol"],
        timeframe=r["timeframe"],
        strategy=r["strategy"],
        rounds=audited,
    )
    if not url:
        return {"ok": False, "error": "chart failed", "rounds": audited}
    return {"ok": True, "url": url, "symbol": r["symbol"], "rounds": audited}
