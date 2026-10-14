"""Локальное хранилище VERO: OHLCV + индикаторы (своя SQLite, не MTB)."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

import pandas as pd

from config import MIN_CANDLES, PREFETCH_TARGET_BARS, VERO_BARS_DB, ensure_dirs

OHLCV_COLS = ("timestamp", "open", "high", "low", "close", "volume")
INDICATOR_COLS = (
    "RSI",
    "MACD",
    "MACD_SIGNAL",
    "MACD_HIST",
    "BB_MID",
    "BB_UPPER",
    "BB_LOWER",
    "BB_WIDTH",
    "STOCH_K",
    "STOCH_D",
    "EMA_FAST",
    "EMA_SLOW",
    "CCI",
    "WILLR",
)
ALL_BAR_COLS = OHLCV_COLS + INDICATOR_COLS

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
  symbol TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  timestamp INTEGER NOT NULL,
  open REAL, high REAL, low REAL, close REAL, volume REAL,
  RSI REAL, MACD REAL, MACD_SIGNAL REAL, MACD_HIST REAL,
  BB_MID REAL, BB_UPPER REAL, BB_LOWER REAL, BB_WIDTH REAL,
  STOCH_K REAL, STOCH_D REAL, EMA_FAST REAL, EMA_SLOW REAL,
  CCI REAL, WILLR REAL,
  source TEXT,
  PRIMARY KEY (symbol, timeframe, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_vero_bars_sym_tf
  ON bars(symbol, timeframe, timestamp);
CREATE TABLE IF NOT EXISTS sync_meta (
  symbol TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  bars INTEGER NOT NULL DEFAULT 0,
  source TEXT,
  last_ts INTEGER,
  updated_at INTEGER,
  PRIMARY KEY (symbol, timeframe)
);
"""


def ensure_store() -> Path:
    ensure_dirs()
    VERO_BARS_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(VERO_BARS_DB), timeout=60)
    try:
        con.executescript(_SCHEMA)
        con.commit()
    finally:
        con.close()
    return VERO_BARS_DB


def store_available() -> bool:
    if not VERO_BARS_DB.exists() or VERO_BARS_DB.stat().st_size < 64:
        return False
    try:
        with VERO_BARS_DB.open("rb") as f:
            return f.read(16).startswith(b"SQLite format 3")
    except Exception:
        return False


def _connect() -> sqlite3.Connection:
    ensure_store()
    con = sqlite3.connect(str(VERO_BARS_DB), timeout=120)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def _connect_ro() -> sqlite3.Connection:
    if not store_available():
        raise FileNotFoundError("vero bars db missing")
    return sqlite3.connect(
        f"file:{VERO_BARS_DB}?mode=ro",
        uri=True,
        timeout=60,
    )


def get_sync_meta(symbol: str, timeframe: str) -> dict[str, Any] | None:
    """Быстрый статус symbol×tf из sync_meta (без COUNT по bars)."""
    if not store_available():
        return None
    con = _connect_ro()
    try:
        row = con.execute(
            """
            SELECT bars, source, last_ts, updated_at
            FROM sync_meta
            WHERE symbol=? AND timeframe=?
            """,
            (symbol, timeframe),
        ).fetchone()
        if not row:
            return None
        return {
            "bars": int(row[0] or 0),
            "source": row[1],
            "last_ts": int(row[2]) if row[2] is not None else None,
            "updated_at": int(row[3]) if row[3] is not None else None,
        }
    finally:
        con.close()


def bar_count(symbol: str, timeframe: str) -> int:
    meta = get_sync_meta(symbol, timeframe)
    if meta is not None:
        return int(meta["bars"])
    if not store_available():
        return 0
    con = _connect_ro()
    try:
        row = con.execute(
            "SELECT COUNT(*) FROM bars WHERE symbol=? AND timeframe=?",
            (symbol, timeframe),
        ).fetchone()
        return int(row[0] or 0)
    finally:
        con.close()


def last_timestamp(symbol: str, timeframe: str) -> int | None:
    meta = get_sync_meta(symbol, timeframe)
    if meta is not None and meta.get("last_ts") is not None:
        return int(meta["last_ts"])
    if not store_available():
        return None
    con = _connect_ro()
    try:
        row = con.execute(
            """
            SELECT MAX(timestamp) FROM bars
            WHERE symbol=? AND timeframe=?
            """,
            (symbol, timeframe),
        ).fetchone()
        if not row or row[0] is None:
            return None
        return int(row[0])
    finally:
        con.close()


def fetch_vero_bars(
    symbol: str,
    timeframe: str,
    limit: int = PREFETCH_TARGET_BARS,
    *,
    with_indicators: bool = True,
) -> tuple[pd.DataFrame, str | None]:
    """Читает хвост истории. with_indicators=False → только OHLCV."""
    if not store_available():
        return pd.DataFrame(), "no vero bars db"
    cols = ALL_BAR_COLS if with_indicators else OHLCV_COLS
    col_sql = ", ".join(cols)
    con = _connect_ro()
    try:
        rows = con.execute(
            f"""
            SELECT {col_sql} FROM bars
            WHERE symbol=? AND timeframe=?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (symbol, timeframe, int(limit)),
        ).fetchall()
    finally:
        con.close()
    if not rows:
        return pd.DataFrame(), "no vero rows"
    df = pd.DataFrame(list(reversed(rows)), columns=list(cols))
    if len(df) < MIN_CANDLES:
        return df, f"only {len(df)} vero bars"
    return df.reset_index(drop=True), None


def load_ohlcv_only(
    symbol: str, timeframe: str, limit: int = PREFETCH_TARGET_BARS
) -> pd.DataFrame:
    df, _ = fetch_vero_bars(symbol, timeframe, limit, with_indicators=False)
    return df


def replace_bars(
    symbol: str,
    timeframe: str,
    df: pd.DataFrame,
    *,
    source: str,
    keep_limit: int | None = PREFETCH_TARGET_BARS,
) -> int:
    """Полная замена рядов symbol×tf (после merge+enrich). Возвращает число баров."""
    if df is None or df.empty:
        return 0
    work = df.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    if keep_limit and len(work) > keep_limit:
        work = work.tail(int(keep_limit))
    work = work.reset_index(drop=True)

    for c in OHLCV_COLS:
        if c not in work.columns:
            raise ValueError(f"missing column {c}")
    for c in INDICATOR_COLS:
        if c not in work.columns:
            work[c] = None

    now = int(time.time())
    rows = []
    for r in work.itertuples(index=False):
        rows.append(
            (
                symbol,
                timeframe,
                int(getattr(r, "timestamp")),
                float(getattr(r, "open")),
                float(getattr(r, "high")),
                float(getattr(r, "low")),
                float(getattr(r, "close")),
                float(getattr(r, "volume") or 0.0),
                _f(getattr(r, "RSI", None)),
                _f(getattr(r, "MACD", None)),
                _f(getattr(r, "MACD_SIGNAL", None)),
                _f(getattr(r, "MACD_HIST", None)),
                _f(getattr(r, "BB_MID", None)),
                _f(getattr(r, "BB_UPPER", None)),
                _f(getattr(r, "BB_LOWER", None)),
                _f(getattr(r, "BB_WIDTH", None)),
                _f(getattr(r, "STOCH_K", None)),
                _f(getattr(r, "STOCH_D", None)),
                _f(getattr(r, "EMA_FAST", None)),
                _f(getattr(r, "EMA_SLOW", None)),
                _f(getattr(r, "CCI", None)),
                _f(getattr(r, "WILLR", None)),
                source,
            )
        )

    con = _connect()
    try:
        con.execute(
            "DELETE FROM bars WHERE symbol=? AND timeframe=?",
            (symbol, timeframe),
        )
        con.executemany(
            """
            INSERT INTO bars(
              symbol, timeframe, timestamp,
              open, high, low, close, volume,
              RSI, MACD, MACD_SIGNAL, MACD_HIST,
              BB_MID, BB_UPPER, BB_LOWER, BB_WIDTH,
              STOCH_K, STOCH_D, EMA_FAST, EMA_SLOW, CCI, WILLR, source
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )
        last_ts = int(work["timestamp"].iloc[-1]) if len(work) else None
        con.execute(
            """
            INSERT INTO sync_meta(symbol, timeframe, bars, source, last_ts, updated_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(symbol, timeframe) DO UPDATE SET
              bars=excluded.bars,
              source=excluded.source,
              last_ts=excluded.last_ts,
              updated_at=excluded.updated_at
            """,
            (symbol, timeframe, len(rows), source, last_ts, now),
        )
        con.commit()
    finally:
        con.close()
    return len(rows)


def _f(v: Any) -> float | None:
    if v is None:
        return None
    try:
        import math

        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return None
        return x
    except Exception:
        return None


def merge_ohlcv(existing: pd.DataFrame, fresh: pd.DataFrame) -> pd.DataFrame:
    """Объединяет OHLCV по timestamp (докачка)."""
    parts = []
    if existing is not None and not existing.empty:
        parts.append(existing[list(OHLCV_COLS)])
    if fresh is not None and not fresh.empty:
        parts.append(fresh[list(OHLCV_COLS)])
    if not parts:
        return pd.DataFrame(columns=list(OHLCV_COLS))
    out = pd.concat(parts, ignore_index=True)
    out = out.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    return out.reset_index(drop=True)


def store_stats() -> dict[str, Any]:
    ensure_store()
    if not store_available():
        return {
            "available": False,
            "path": str(VERO_BARS_DB),
            "size_mb": 0,
            "symbols": 0,
            "rows": 0,
            "by_tf": {},
            "ready_symbols_by_tf": {},
            "target_bars": PREFETCH_TARGET_BARS,
        }
    con = _connect_ro()
    try:
        # sync_meta быстрее полного COUNT(*) по bars (БД уже >1GB)
        meta_rows = con.execute(
            """
            SELECT timeframe,
                   COUNT(*) AS symbols,
                   COALESCE(SUM(bars), 0) AS rows
            FROM sync_meta
            GROUP BY timeframe
            """
        ).fetchall()
        by_tf: dict[str, dict[str, int]] = {}
        rows = 0
        syms_set: set[str] = set()
        for tf, n_sym, n_rows in meta_rows:
            by_tf[str(tf)] = {"rows": int(n_rows or 0), "symbols": int(n_sym or 0)}
            rows += int(n_rows or 0)
        if meta_rows:
            for (s,) in con.execute("SELECT DISTINCT symbol FROM sync_meta"):
                syms_set.add(str(s))
        else:
            rows = int(con.execute("SELECT COUNT(*) FROM bars").fetchone()[0] or 0)
            syms_set = {
                str(r[0])
                for r in con.execute("SELECT DISTINCT symbol FROM bars").fetchall()
            }
            for tf, n, s in con.execute(
                """
                SELECT timeframe, COUNT(*), COUNT(DISTINCT symbol)
                FROM bars GROUP BY timeframe
                """
            ):
                by_tf[str(tf)] = {"rows": int(n), "symbols": int(s)}
        ready = {}
        for tf, n in con.execute(
            """
            SELECT timeframe, COUNT(*) FROM sync_meta
            WHERE bars >= ?
            GROUP BY timeframe
            """,
            (MIN_CANDLES,),
        ):
            ready[str(tf)] = int(n)
        if not ready and by_tf:
            # fallback если meta пуста
            for tf, n in con.execute(
                """
                SELECT timeframe, COUNT(*) FROM (
                  SELECT symbol, timeframe, COUNT(*) AS c
                  FROM bars GROUP BY symbol, timeframe
                  HAVING c >= ?
                ) GROUP BY timeframe
                """,
                (MIN_CANDLES,),
            ):
                ready[str(tf)] = int(n)
    finally:
        con.close()
    return {
        "available": True,
        "path": str(VERO_BARS_DB),
        "size_mb": round(VERO_BARS_DB.stat().st_size / 1e6, 2),
        "symbols": len(syms_set),
        "rows": rows,
        "by_tf": by_tf,
        "ready_symbols_by_tf": ready,
        "target_bars": PREFETCH_TARGET_BARS,
    }


def symbol_count(timeframe: str) -> int:
    if not store_available():
        return 0
    con = _connect_ro()
    try:
        n = con.execute(
            """
            SELECT COUNT(DISTINCT symbol) FROM bars WHERE timeframe=?
            """,
            (timeframe,),
        ).fetchone()[0]
        return int(n or 0)
    finally:
        con.close()


def list_symbols_covering(
    timeframes: list[str] | tuple[str, ...],
    *,
    min_bars: int = MIN_CANDLES,
) -> list[str]:
    """Тикеры, у которых в сторе есть все перечисленные ТФ (≥ min_bars)."""
    tfs = [str(t).strip() for t in timeframes if str(t).strip()]
    if not tfs or not store_available():
        return []
    placeholders = ",".join("?" * len(tfs))
    con = _connect_ro()
    try:
        rows = con.execute(
            f"""
            SELECT symbol FROM sync_meta
            WHERE timeframe IN ({placeholders}) AND bars >= ?
            GROUP BY symbol
            HAVING COUNT(DISTINCT timeframe) = ?
            ORDER BY symbol
            """,
            [*tfs, int(min_bars), len(tfs)],
        ).fetchall()
        return [str(r[0]) for r in rows]
    finally:
        con.close()


def list_symbols(timeframe: str | None = None) -> list[str]:
    if not store_available():
        return []
    con = _connect_ro()
    try:
        if timeframe:
            rows = con.execute(
                """
                SELECT DISTINCT symbol FROM bars
                WHERE timeframe=?
                ORDER BY symbol
                """,
                (timeframe,),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT DISTINCT symbol FROM bars ORDER BY symbol"
            ).fetchall()
        return [str(r[0]) for r in rows]
    finally:
        con.close()
