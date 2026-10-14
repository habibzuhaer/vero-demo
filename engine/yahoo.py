"""XTB → Yahoo OHLCV (адаптация логики MTB, без зависимости от MTB runtime)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

import httpx
import pandas as pd

from config import (
    CACHE_DIR,
    DEFAULT_CANDLE_LIMIT,
    MIN_CANDLES,
    REQUIRE_LOCAL_CANDLES,
    XTB_CACHE,
    XTB_QUALIFIED,
    XTB_TOP,
    YAHOO_PAUSE_SEC,
)

_YAHOO_CHART = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

_FOREX_USD_MAJORS = frozenset({
    "EURUSD", "GBPUSD", "AUDUSD", "NZDUSD",
    "USDJPY", "USDCHF", "USDCAD",
    "USDPLN", "USDSGD", "USDCNH", "USDTRY", "USDMXN",
})

# VERO: forex и прочие CFD вне акций/ETF — исключены
EXCLUDE_FOREX = True

EXCLUDED_MARKET_TYPES = frozenset({
    "forex",
    "index",
    "indices",
    "commodity",
    "commodities",
    "crypto",
    "crypto-cfd",
    "cryptocfd",
    "etn",
    "fund",
    "etc",
})


def is_forex_symbol(symbol: str, market_type: str = "") -> bool:
    s = (symbol or "").upper().strip()
    mtype = (market_type or "").lower()
    if mtype == "forex":
        return True
    if s in _FOREX_USD_MAJORS:
        return True
    # типичные FX без суффикса биржи: EURUSD, GBPJPY, …
    if len(s) == 6 and s.isalpha() and s[:3] != s[3:]:
        # не путать с тикерами акций без точки (редко) — только известные majors + USD*
        if s in _FOREX_USD_MAJORS or s.endswith(("USD", "JPY", "CHF", "CAD", "GBP", "AUD", "NZD", "EUR")):
            # EURUSD style
            if s[:3].isalpha() and s[3:].isalpha():
                return s in _FOREX_USD_MAJORS or (
                    s[:3] in {"EUR", "GBP", "AUD", "NZD", "USD"} and s[3:] in {
                        "USD", "JPY", "CHF", "CAD", "GBP", "AUD", "NZD", "EUR", "PLN", "SGD", "CNH", "TRY", "MXN",
                    }
                )
    return False


def is_excluded_symbol(symbol: str, market_type: str = "") -> bool:
    """Forex, индексы, сырьё, crypto CFD и прочее без биржевого суффикса."""
    if is_forex_symbol(symbol, market_type):
        return True
    mtype = (market_type or "").lower().replace("_", "-")
    if mtype in EXCLUDED_MARKET_TYPES:
        return True
    s = (symbol or "").upper().strip()
    if s in _YAHOO_SPECIAL:
        return True
    # индексы/commodities без точки: US500, GOLD, OIL…
    if "." not in s and (len(s) <= 8 or s.isdigit()):
        if mtype not in ("stock", "etf", "shares", "cashstocks", ""):
            return True
        if s in _YAHOO_SPECIAL or not s.endswith(
            (".US", ".DE", ".FR", ".PL", ".UK", ".BE", ".IT", ".ES", ".NL", ".CH")
        ):
            # чистые тикеры без суффикса — чаще индекс/CFD
            if len(s) <= 6 and s.isalpha():
                return True
    return False


_YAHOO_STOCK_SUFFIX = {
    "US": "", "UK": ".L", "DE": ".DE", "FR": ".PA", "PL": ".WA",
    "IT": ".MI", "ES": ".MC", "NL": ".AS", "CH": ".SW", "HK": ".HK",
    "JP": ".T", "AU": ".AX", "CA": ".TO", "SE": ".ST", "NO": ".OL",
    "DK": ".CO", "FI": ".HE", "BE": ".BR", "AT": ".VI", "PT": ".LS",
    "IE": ".IR", "CZ": ".PR", "HU": ".BD", "GR": ".AT", "TR": ".IS",
    "SG": ".SI", "KR": ".KS", "TW": ".TW", "IN": ".NS", "BR": ".SA",
    "MX": ".MX", "ZA": ".JO",
}

_YAHOO_SPECIAL = {
    "US500": "^GSPC", "US30": "^DJI", "US100": "^NDX", "US2000": "^RUT",
    "DE40": "^GDAXI", "DE30": "^GDAXI", "UK100": "^FTSE", "FR40": "^FCHI",
    "EU50": "^STOXX50E", "JP225": "^N225", "AU200": "^AXJO", "HK50": "^HSI",
    "GOLD": "GC=F", "SILVER": "SI=F", "OIL": "CL=F", "BRENT": "BZ=F",
    "NATGAS": "NG=F", "COPPER": "HG=F", "PLATINUM": "PL=F", "PALLADIUM": "PA=F",
    "WHEAT": "ZW=F", "CORN": "ZC=F", "COFFEE": "KC=F", "COCOA": "CC=F",
    "SUGAR": "SB=F", "COTTON": "CT=F",
}

_YAHOO_DAYS = {
    "30m": 59,
    "1h": 220,
    "4h": 650,
    "1d": 8000,
}

_client: httpx.Client | None = None
_crumb: str | None = None
_crumb_ts: float = 0.0


def _get_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(
            timeout=45.0,
            headers=_HTTP_HEADERS,
            follow_redirects=True,
        )
    return _client


def _refresh_crumb(force: bool = False) -> str:
    """Yahoo chart API требует cookie + crumb."""
    global _crumb, _crumb_ts
    if not force and _crumb and (time.time() - _crumb_ts) < 3600:
        return _crumb
    client = _get_client()
    # cookie bootstrap
    try:
        client.get("https://fc.yahoo.com")
    except Exception:
        pass
    resp = client.get("https://query2.finance.yahoo.com/v1/test/getcrumb")
    resp.raise_for_status()
    crumb = (resp.text or "").strip()
    if not crumb or "<" in crumb:
        raise RuntimeError(f"bad yahoo crumb: {crumb[:80]!r}")
    _crumb = crumb
    _crumb_ts = time.time()
    return crumb


def xtb_to_yahoo(symbol: str, market_type: str = "") -> Optional[str]:
    s = (symbol or "").upper().strip()
    if not s:
        return None
    if s in _YAHOO_SPECIAL:
        return _YAHOO_SPECIAL[s]
    if len(s) == 6 and s.isalpha():
        if s in _FOREX_USD_MAJORS:
            return f"{s}=X"
        return _YAHOO_SPECIAL.get(s)
    if "." in s:
        base, suffix = s.rsplit(".", 1)
        yf = _YAHOO_STOCK_SUFFIX.get(suffix)
        if yf is not None:
            return f"{base}{yf}" if yf else base
        return None
    mtype = (market_type or "").lower()
    if mtype in ("index", "indices", "commodity", "commodities"):
        return _YAHOO_SPECIAL.get(s)
    return None


def _days_for_tf(tf: str, limit: int) -> int:
    base = _YAHOO_DAYS.get(tf, 90)
    mins = {"30m": 30, "1h": 60, "4h": 240, "1d": 1440}.get(tf, 60)
    need_days = int(limit * mins / 60 / 24 * 1.15) + 5
    if tf == "30m":
        return min(59, max(need_days, base))
    if tf == "1h":
        return min(220, max(need_days, base))
    if tf == "4h":
        return min(650, max(need_days, base))
    if tf == "1d":
        return min(8000, max(need_days, base, limit + 30))
    return min(180, max(need_days, base))


def _parse_chart(data: dict) -> list[dict]:
    result = (data.get("chart") or {}).get("result")
    if not result:
        return []
    block = result[0]
    ts_list = block.get("timestamp") or []
    q = (block.get("indicators") or {}).get("quote") or [{}]
    q0 = q[0] if q else {}
    opens = q0.get("open") or []
    highs = q0.get("high") or []
    lows = q0.get("low") or []
    closes = q0.get("close") or []
    vols = q0.get("volume") or []
    out: list[dict] = []
    for i, ts in enumerate(ts_list):
        if ts is None:
            continue
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        if o is None or h is None or l is None or c is None:
            continue
        v = float(vols[i] or 0) if i < len(vols) and vols[i] is not None else 0.0
        out.append({
            "timestamp": int(ts) * 1000,
            "open": float(o),
            "high": float(h),
            "low": float(l),
            "close": float(c),
            "volume": v,
        })
    return out


def _cache_path(yahoo: str, tf: str, limit: int) -> Path:
    safe = yahoo.replace("/", "_").replace("^", "IDX_").replace("=", "_")
    return CACHE_DIR / f"yahoo_{safe}_{tf}_{limit}.csv"


def fetch_yahoo_ohlcv(
    yahoo_symbol: str,
    timeframe: str,
    limit: int = DEFAULT_CANDLE_LIMIT,
    *,
    use_cache: bool = True,
) -> tuple[pd.DataFrame, Optional[str]]:
    from engine.rate_limit import wait_yahoo

    wait_yahoo()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = _cache_path(yahoo_symbol, timeframe, limit)
    if use_cache and cache.exists():
        try:
            from config import YAHOO_CACHE_MAX_AGE_SEC

            age = time.time() - cache.stat().st_mtime
            if YAHOO_CACHE_MAX_AGE_SEC > 0 and age > YAHOO_CACHE_MAX_AGE_SEC:
                pass  # stale — refetch below
            else:
                df = pd.read_csv(cache)
                if len(df) >= MIN_CANDLES:
                    return df.tail(limit).reset_index(drop=True), None
        except Exception:
            pass

    now = int(time.time())
    period1 = now - _days_for_tf(timeframe, limit) * 86400
    client = _get_client()
    last_err: Optional[str] = None
    for attempt in range(4):
        try:
            crumb = _refresh_crumb(force=(attempt > 0))
            resp = client.get(
                _YAHOO_CHART.format(symbol=yahoo_symbol),
                params={
                    "interval": timeframe,
                    "period1": period1,
                    "period2": now,
                    "includePrePost": "false",
                    "crumb": crumb,
                },
            )
            if resp.status_code in (401, 403):
                from engine.anti_ban import note_error

                _refresh_crumb(force=True)
                last_err = f"{resp.status_code} Forbidden"
                note_error(last_err)
                time.sleep(0.5 * (attempt + 1))
                continue
            if resp.status_code == 429:
                from engine.anti_ban import note_error

                last_err = "429 Too Many Requests"
                note_error(last_err)
                time.sleep(1.5 * (attempt + 1))
                continue
            resp.raise_for_status()
            raw = _parse_chart(resp.json())
            if not raw:
                err = (resp.json().get("chart") or {}).get("error")
                return pd.DataFrame(), str(err or "empty chart")
            if len(raw) > limit:
                raw = raw[-limit:]
            df = pd.DataFrame(raw)
            if len(df) < MIN_CANDLES:
                return df, f"only {len(df)} bars"
            df.to_csv(cache, index=False)
            from engine.anti_ban import job_pause

            time.sleep(job_pause(source="yahoo"))
            return df, None
        except Exception as e:
            last_err = str(e)
            time.sleep(0.5 * (attempt + 1))
    return pd.DataFrame(), last_err


def fetch_xtb_ohlcv(
    symbol: str,
    timeframe: str,
    limit: int = DEFAULT_CANDLE_LIMIT,
    *,
    market_type: str = "",
    use_cache: bool = True,
) -> tuple[pd.DataFrame, Optional[str], Optional[str]]:
    yahoo = xtb_to_yahoo(symbol, market_type)
    if not yahoo:
        return pd.DataFrame(), None, "no yahoo mapping"
    df, err = fetch_yahoo_ohlcv(yahoo, timeframe, limit, use_cache=use_cache)
    return df, yahoo, err


def _load_catalog() -> dict[str, Any]:
    if not XTB_CACHE.exists():
        return {}
    return json.loads(XTB_CACHE.read_text(encoding="utf-8"))


def _load_catalog_meta() -> dict[str, dict]:
    data = _load_catalog()
    meta: dict[str, dict] = {}
    for sec in data.get("sections") or []:
        for it in sec.get("items") or []:
            sym = (it.get("symbol") or "").strip()
            if sym:
                meta[sym] = it
    return meta


def list_xtb_symbols(*, backtestable_only: bool = True) -> list[str]:
    from engine.local_candles import symbol_has_local

    data = _load_catalog()
    meta = _load_catalog_meta()
    out: list[str] = []
    for sec in data.get("sections") or []:
        for it in sec.get("items") or []:
            sym = (it.get("symbol") or "").strip()
            if not sym:
                continue
            mtype = (it.get("market_type") or it.get("xtb_type") or "").lower()
            if is_excluded_symbol(sym, mtype):
                continue
            out.append(sym)
    seen: set[str] = set()
    uniq: list[str] = []
    for s in out:
        if s in seen:
            continue
        seen.add(s)
        if backtestable_only:
            mtype = (meta.get(s) or {}).get("market_type") or ""
            if is_excluded_symbol(s, mtype):
                continue
            if REQUIRE_LOCAL_CANDLES and not symbol_has_local(s):
                continue
            if not xtb_to_yahoo(s, mtype) and REQUIRE_LOCAL_CANDLES:
                continue
        uniq.append(s)
    return uniq


def load_qualified_symbols() -> list[str]:
    if not XTB_QUALIFIED.exists():
        return []
    data = json.loads(XTB_QUALIFIED.read_text(encoding="utf-8"))
    return list(data.get("qualified_symbols") or [])


def load_top_symbols() -> list[str]:
    if not XTB_TOP.exists():
        return []
    data = json.loads(XTB_TOP.read_text(encoding="utf-8"))
    return list(data.get("symbols") or [])


def universe_symbols(kind: str) -> list[str]:
    kind = (kind or "full").lower()
    if kind == "qualified":
        return load_qualified_symbols()
    if kind == "top":
        return load_top_symbols()
    if kind == "seed":
        q = set(load_qualified_symbols())
        t = set(load_top_symbols())
        return sorted(q | t)
    return list_xtb_symbols(backtestable_only=True)


def catalog_stats() -> dict[str, Any]:
    from engine.local_candles import local_candles_available, local_symbol_count

    full = list_xtb_symbols(backtestable_only=True)
    raw = _load_catalog_meta()
    excluded = sum(
        1
        for sym, it in raw.items()
        if is_excluded_symbol(sym, (it.get("market_type") or it.get("xtb_type") or ""))
    )
    return {
        "full": len(full),
        "qualified": len(load_qualified_symbols()),
        "top": len(load_top_symbols()),
        "seed": len(universe_symbols("seed")),
        "excluded_non_stocks": excluded,
        "require_local": REQUIRE_LOCAL_CANDLES,
        "cache_path": str(XTB_CACHE),
        "cache_exists": XTB_CACHE.exists(),
        "local_available": local_candles_available(),
        "local_symbols_1h": local_symbol_count("1h") if local_candles_available() else 0,
    }
