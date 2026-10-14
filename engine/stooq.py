"""Stooq CSV — запасной HTTP-источник (акции EU/US), с паузами."""

from __future__ import annotations

from typing import Optional

import httpx
import pandas as pd

from config import DEFAULT_CANDLE_LIMIT, MIN_CANDLES, STOOQ_ENABLED
from engine.candle_cache import cache_path, read_cache, write_cache

_STOOQ = "https://stooq.com/q/d/l/"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,text/plain,*/*",
}

# Stooq: i=h часовые; дневки — без i / i=d. 30m нет; 4h = ресэмпл с 1h.
_STOOQ_INTERVAL = {"1h": "h", "4h": "h", "1d": "d"}

_SUF_TO_STOOQ = {
    "US": "us",
    "UK": "uk",
    "DE": "de",
    "FR": "fr",
    "PL": "pl",
    "IT": "it",
    "ES": "es",
    "NL": "nl",
    "BE": "be",
    "CH": "ch",
    "HK": "hk",
    "JP": "jp",
    "AU": "au",
    "CA": "us",  # часто нет; пусть пробует .us не будем — stooq ca = .to иногда
}


def xtb_to_stooq(symbol: str) -> Optional[str]:
    s = (symbol or "").upper().strip()
    if not s or "." not in s:
        return None
    base, suf = s.rsplit(".", 1)
    tag = _SUF_TO_STOOQ.get(suf)
    if not tag:
        return None
    return f"{base.lower()}.{tag}"


def _resample_4h(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    ts = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    x = df.set_index(ts)
    agg = x.resample("4h").agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    )
    agg = agg.dropna(subset=["open", "high", "low", "close"])
    if agg.empty:
        return pd.DataFrame()
    out = agg.reset_index()
    time_col = out.columns[0]
    out["timestamp"] = (
        pd.to_datetime(out[time_col], utc=True).astype("int64") // 1_000_000
    ).astype("int64")
    return out[["timestamp", "open", "high", "low", "close", "volume"]]


def fetch_stooq_ohlcv(
    xtb_symbol: str,
    timeframe: str,
    limit: int = DEFAULT_CANDLE_LIMIT,
    *,
    use_cache: bool = True,
) -> tuple[pd.DataFrame, Optional[str], Optional[str]]:
    if not STOOQ_ENABLED:
        return pd.DataFrame(), None, "stooq disabled"
    if timeframe == "30m":
        return pd.DataFrame(), None, "stooq has no 30m"
    interval = _STOOQ_INTERVAL.get(timeframe)
    if not interval:
        return pd.DataFrame(), None, f"stooq no interval for {timeframe}"
    stooq = xtb_to_stooq(xtb_symbol)
    if not stooq:
        return pd.DataFrame(), None, "no stooq mapping"

    if use_cache:
        cached = read_cache(cache_path("stooq", stooq, timeframe, limit), limit=limit)
        if cached is not None:
            return cached, stooq, None

    from engine.rate_limit import wait_stooq

    wait_stooq()
    params: dict[str, str] = {"s": stooq}
    if interval != "d":
        params["i"] = interval
    try:
        resp = httpx.get(
            _STOOQ,
            params=params,
            headers=_HEADERS,
            timeout=30.0,
            follow_redirects=True,
        )
        if resp.status_code in (401, 403, 429):
            from engine.anti_ban import note_error

            err = f"{resp.status_code} stooq"
            note_error(err)
            return pd.DataFrame(), stooq, err
        resp.raise_for_status()
        text = resp.text.strip()
        if not text or "No data" in text[:80] or "<html" in text[:40].lower():
            return pd.DataFrame(), stooq, "empty stooq"
        from io import StringIO

        raw = pd.read_csv(StringIO(text))
    except Exception as e:
        return pd.DataFrame(), stooq, str(e)[:200]

    if raw.empty or "Close" not in raw.columns:
        return pd.DataFrame(), stooq, "bad stooq csv"

    date_col = "Date" if "Date" in raw.columns else raw.columns[0]
    dt = pd.to_datetime(raw[date_col], utc=True, errors="coerce")
    df = pd.DataFrame(
        {
            "timestamp": (dt.astype("int64") // 1_000_000).astype("int64"),
            "open": pd.to_numeric(raw.get("Open"), errors="coerce"),
            "high": pd.to_numeric(raw.get("High"), errors="coerce"),
            "low": pd.to_numeric(raw.get("Low"), errors="coerce"),
            "close": pd.to_numeric(raw.get("Close"), errors="coerce"),
            "volume": pd.to_numeric(raw.get("Volume"), errors="coerce").fillna(0.0),
        }
    ).dropna(subset=["open", "high", "low", "close"])
    df = df[df["timestamp"] > 0].sort_values("timestamp")
    if timeframe == "4h":
        df = _resample_4h(df)
    if df.empty or len(df) < MIN_CANDLES:
        return df, stooq, f"only {len(df)} stooq bars"
    if len(df) > limit:
        df = df.tail(limit)
    df = df.reset_index(drop=True)
    write_cache(cache_path("stooq", stooq, timeframe, limit), df)
    return df, stooq, None
