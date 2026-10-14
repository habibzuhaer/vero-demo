"""Загрузка OHLCV: vero → local → TradingView → Yahoo → Stooq."""

from __future__ import annotations

import threading
from typing import Callable

import pandas as pd

from config import MIN_CANDLES, OHLCV_SOURCE_ORDER
from engine.local_candles import fetch_local_ohlcv
from engine.stooq import fetch_stooq_ohlcv
from engine.tv import fetch_tv_ohlcv
from engine.vero_store import fetch_vero_bars
from engine.yahoo import fetch_xtb_ohlcv, xtb_to_yahoo

_mem_lock = threading.Lock()
_mem: dict[tuple[str, str, int], tuple[pd.DataFrame, str, str, str | None]] = {}


def _catalog_tv(symbol: str) -> str | None:
    try:
        from engine.yahoo import _load_catalog_meta

        it = _load_catalog_meta().get(symbol) or {}
        tv = (it.get("tv_symbol") or "").strip()
        return tv or None
    except Exception:
        return None


def _try_vero(symbol: str, timeframe: str, limit: int, **_kw):
    df, err = fetch_vero_bars(symbol, timeframe, limit=limit, with_indicators=True)
    yahoo = xtb_to_yahoo(symbol) or ""
    if err or df.empty:
        return pd.DataFrame(), yahoo, err or "no vero bars"
    return df, yahoo, None


def _try_local(symbol: str, timeframe: str, limit: int, **_kw):
    df, err = fetch_local_ohlcv(symbol, timeframe, limit=limit)
    yahoo = xtb_to_yahoo(symbol) or ""
    if err or df.empty:
        return pd.DataFrame(), yahoo, err or "no local candles"
    return df, yahoo, None


def _try_tv(symbol: str, timeframe: str, limit: int, *, use_cache: bool = True, **_kw):
    df, mapped, err = fetch_tv_ohlcv(
        symbol,
        timeframe,
        limit=limit,
        catalog_tv=_catalog_tv(symbol),
        use_cache=use_cache,
    )
    return df, mapped or "", err


def _try_yahoo(symbol: str, timeframe: str, limit: int, *, use_cache: bool = True, **_kw):
    df, mapped, err = fetch_xtb_ohlcv(
        symbol, timeframe, limit=limit, use_cache=use_cache
    )
    return df, mapped or xtb_to_yahoo(symbol) or "", err


def _try_stooq(symbol: str, timeframe: str, limit: int, *, use_cache: bool = True, **_kw):
    df, mapped, err = fetch_stooq_ohlcv(
        symbol, timeframe, limit=limit, use_cache=use_cache
    )
    return df, mapped or "", err


_FETCHERS: dict[str, Callable[..., tuple[pd.DataFrame, str, str | None]]] = {
    "vero": _try_vero,
    "local": _try_local,
    "tv": _try_tv,
    "tradingview": _try_tv,
    "yahoo": _try_yahoo,
    "finance": _try_yahoo,
    "stooq": _try_stooq,
}


def load_ohlcv(
    symbol: str,
    timeframe: str,
    limit: int,
    *,
    use_cache: bool = True,
) -> tuple[pd.DataFrame, str, str, str | None]:
    """
    Возвращает (df, mapped_symbol, source, error).
    source: local | tv | yahoo | stooq | skip
    """
    key = (symbol, timeframe, int(limit))
    if use_cache:
        with _mem_lock:
            hit = _mem.get(key)
        if hit is not None:
            df, mapped, source, err = hit
            return df.copy(), mapped, source, err

    errors: list[str] = []
    yahoo_fallback = xtb_to_yahoo(symbol) or ""

    for src in OHLCV_SOURCE_ORDER:
        fn = _FETCHERS.get(src)
        if fn is None:
            continue
        df, mapped, err = fn(symbol, timeframe, limit, use_cache=use_cache)
        if err or df is None or df.empty or len(df) < MIN_CANDLES:
            errors.append(f"{src}: {err or 'empty'}")
            continue
        mapped = mapped or yahoo_fallback
        out = (df.reset_index(drop=True), mapped, src if src != "tradingview" else "tv", None)
        if src == "finance":
            out = (out[0], out[1], "yahoo", None)
        if use_cache:
            with _mem_lock:
                _mem[key] = out
                if len(_mem) > 64:
                    # drop oldest-ish
                    for old in list(_mem.keys())[:16]:
                        _mem.pop(old, None)
        return out

    err = "; ".join(errors) if errors else "no ohlcv"
    out = (pd.DataFrame(), yahoo_fallback, "skip", err)
    if use_cache:
        with _mem_lock:
            _mem[key] = out
    return out
