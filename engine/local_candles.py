"""Локальные OHLCV — только из VERO/data/candles/."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from config import CANDLES_DB, DEFAULT_CANDLE_LIMIT, MIN_CANDLES, ensure_dirs

ensure_dirs()


def local_candles_available() -> bool:
    if not CANDLES_DB.exists() or CANDLES_DB.stat().st_size < 64:
        return False
    try:
        with CANDLES_DB.open("rb") as f:
            return f.read(16).startswith(b"SQLite format 3")
    except Exception:
        return False


def _connect_ro() -> sqlite3.Connection:
    # immutable=1 — не трогает WAL/shm рядом с MTB DB
    return sqlite3.connect(
        f"file:{CANDLES_DB}?mode=ro&immutable=1",
        uri=True,
        timeout=60,
    )


def get_max_local_bars(timeframe: str | None = None) -> int:
    """Макс. число свечей в локальной БД (для UI «max»)."""
    if not local_candles_available():
        return 0
    try:
        con = _connect_ro()
        if timeframe:
            row = con.execute(
                """
                SELECT MAX(cnt) FROM (
                  SELECT COUNT(*) AS cnt FROM candles
                  WHERE exchange='xtb' AND timeframe=?
                  GROUP BY symbol
                )
                """,
                (timeframe,),
            ).fetchone()
        else:
            row = con.execute(
                """
                SELECT MAX(cnt) FROM (
                  SELECT COUNT(*) AS cnt FROM candles
                  WHERE exchange='xtb'
                  GROUP BY symbol, timeframe
                )
                """
            ).fetchone()
        con.close()
        return int(row[0] or 0)
    except Exception:
        return 0


def fetch_local_ohlcv(
    symbol: str,
    timeframe: str,
    limit: int = DEFAULT_CANDLE_LIMIT,
    *,
    exchange: str | None = "xtb",
) -> tuple[pd.DataFrame, str | None]:
    """Читает свечи из MTB DB. timestamp в ms."""
    if not local_candles_available():
        return pd.DataFrame(), "no local candles db"

    try:
        con = _connect_ro()
    except Exception as e:
        return pd.DataFrame(), f"db open: {e}"

    try:
        # prefer exchange=xtb, else any
        if exchange:
            rows = con.execute(
                """
                SELECT timestamp, open, high, low, close, volume
                FROM candles
                WHERE symbol=? AND timeframe=? AND exchange=?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (symbol, timeframe, exchange, limit),
            ).fetchall()
        else:
            rows = []
        if not rows:
            rows = con.execute(
                """
                SELECT timestamp, open, high, low, close, volume
                FROM candles
                WHERE symbol=? AND timeframe=?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (symbol, timeframe, limit),
            ).fetchall()
    finally:
        con.close()

    if not rows:
        return pd.DataFrame(), "no local rows"

    # DESC → ASC
    rows = list(reversed(rows))
    df = pd.DataFrame(
        rows, columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    # normalize ts to ms
    ts0 = int(df["timestamp"].iloc[0])
    if ts0 < 10_000_000_000:  # seconds
        df["timestamp"] = df["timestamp"].astype("int64") * 1000
    if len(df) < MIN_CANDLES:
        return df, f"only {len(df)} local bars"
    return df.reset_index(drop=True), None


def local_symbol_count(timeframe: str = "1h") -> int:
    if not local_candles_available():
        return 0
    con = _connect_ro()
    try:
        n = con.execute(
            """
            SELECT COUNT(DISTINCT symbol) FROM candles
            WHERE timeframe=? AND exchange='xtb'
            """,
            (timeframe,),
        ).fetchone()[0]
    finally:
        con.close()
    return int(n)


def symbol_has_local(symbol: str, timeframe: str | None = None) -> bool:
    """Есть ли хотя бы один TF с достаточной историей в MTB DB."""
    if not local_candles_available():
        return False
    con = _connect_ro()
    try:
        if timeframe:
            n = con.execute(
                """
                SELECT COUNT(*) FROM candles
                WHERE symbol=? AND timeframe=? AND exchange='xtb'
                """,
                (symbol, timeframe),
            ).fetchone()[0]
            return int(n) >= MIN_CANDLES
        row = con.execute(
            """
            SELECT MAX(cnt) FROM (
              SELECT COUNT(*) AS cnt FROM candles
              WHERE symbol=? AND exchange='xtb'
              GROUP BY timeframe
            )
            """,
            (symbol,),
        ).fetchone()
        return int(row[0] or 0) >= MIN_CANDLES
    finally:
        con.close()
