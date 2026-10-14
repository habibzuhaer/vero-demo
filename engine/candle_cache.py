"""Общий CSV-кэш OHLCV — чтобы не бить TV/Yahoo по 7 раз на символ×TF."""

from __future__ import annotations

import time
from pathlib import Path

import pandas as pd

from config import CACHE_DIR, MIN_CANDLES, YAHOO_CACHE_MAX_AGE_SEC, ensure_dirs

ensure_dirs()


def cache_path(prefix: str, symbol: str, timeframe: str, limit: int) -> Path:
    safe = (
        str(symbol)
        .replace("/", "_")
        .replace("^", "IDX_")
        .replace("=", "_")
        .replace(":", "_")
    )
    return CACHE_DIR / f"{prefix}_{safe}_{timeframe}_{limit}.csv"


def read_cache(path: Path, *, limit: int, max_age_sec: int | None = None) -> pd.DataFrame | None:
    if not path.exists():
        return None
    age_limit = YAHOO_CACHE_MAX_AGE_SEC if max_age_sec is None else max_age_sec
    try:
        if age_limit > 0:
            age = time.time() - path.stat().st_mtime
            if age > age_limit:
                return None
        df = pd.read_csv(path)
        if len(df) < MIN_CANDLES:
            return None
        return df.tail(limit).reset_index(drop=True)
    except Exception:
        return None


def write_cache(path: Path, df: pd.DataFrame) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(path, index=False)
    except Exception:
        pass
