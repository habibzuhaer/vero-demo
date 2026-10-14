"""TradingView history (неофициальный WS) — аккуратно, с паузами и кэшем."""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Optional

import pandas as pd

from config import CACHE_DIR, DEFAULT_CANDLE_LIMIT, MIN_CANDLES, TV_ENABLED
from engine.candle_cache import cache_path, read_cache, write_cache

_TV_WS = "wss://data.tradingview.com/socket.io/websocket"
_TV_ORIGIN = "https://www.tradingview.com"
_TV_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
_TV_INTERVAL = {"30m": "30", "1h": "60", "4h": "240", "1d": "1D"}
_MAX_CANDIDATES = 2
_MAP_PATH = CACHE_DIR / "tv_symbol_map.json"

# XTB-суффикс страны → биржи TradingView (порядок = приоритет)
_TV_EXCHANGES: dict[str, tuple[str, ...]] = {
    "US": ("NASDAQ", "NYSE", "AMEX", "NYSEARCA"),
    "UK": ("LSE",),
    "DE": ("XETR", "FWB"),
    "FR": ("EURONEXT",),
    "PL": ("GPW",),
    "IT": ("MIL",),
    "ES": ("BME",),
    "NL": ("EURONEXT",),
    "BE": ("EURONEXT",),
    "CH": ("SIX",),
    "SE": ("OMXSTO",),
    "NO": ("OSL",),
    "DK": ("OMXCOP",),
    "FI": ("OMXHEX",),
    "AT": ("VIE",),
    "PT": ("EURONEXT",),
    "IE": ("ISE",),
    "HK": ("HKEX",),
    "JP": ("TSE",),
    "AU": ("ASX",),
    "CA": ("TSX",),
    "CZ": ("PRA",),
    "HU": ("BUD",),
    "GR": ("ATHEX",),
    "TR": ("BIST",),
    "SG": ("SGX",),
    "KR": ("KRX",),
    "TW": ("TWSE",),
    "IN": ("NSE",),
    "BR": ("BMFBOVESPA",),
    "MX": ("BMV",),
    "ZA": ("JSE",),
}


def xtb_to_tv_candidates(symbol: str, catalog_tv: str | None = None) -> list[str]:
    """Список TV-инструментов EXCHANGE:SYMBOL для XTB-тикера."""
    s = (symbol or "").upper().strip()
    out: list[str] = []
    seen: set[str] = set()

    def add(item: str) -> None:
        v = (item or "").strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)

    mapped = _load_map().get(s)
    if mapped:
        add(mapped)

    if catalog_tv:
        add(catalog_tv.upper())

    if "." in s:
        base, suf = s.rsplit(".", 1)
        for ex in _TV_EXCHANGES.get(suf, ()):
            add(f"{ex}:{base}")
        add(f"XTB:{s}")
    elif s:
        add(f"NASDAQ:{s}")
        add(f"NYSE:{s}")

    return out


def _load_map() -> dict[str, str]:
    if not _MAP_PATH.exists():
        return {}
    try:
        data = json.loads(_MAP_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_map_entry(xtb: str, tv: str) -> None:
    mp = _load_map()
    mp[xtb.upper().strip()] = tv
    try:
        _MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
        _MAP_PATH.write_text(json.dumps(mp, indent=0), encoding="utf-8")
    except Exception:
        pass


def _pack(method: str, params: list) -> str:
    payload = json.dumps({"m": method, "p": params}, separators=(",", ":"))
    return f"~m~{len(payload)}~m~{payload}"


def _split_frames(raw: str) -> list[str]:
    parts: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        if not raw.startswith("~m~", i):
            break
        i += 3
        j = raw.find("~m~", i)
        if j < 0:
            break
        try:
            ln = int(raw[i:j])
        except ValueError:
            break
        i = j + 3
        parts.append(raw[i : i + ln])
        i += ln
    return parts


def _bars_from_msg(msg: dict) -> list[list]:
    rows: list[list] = []
    if msg.get("m") not in ("du", "timescale_update"):
        return rows
    payload = msg.get("p")
    if not isinstance(payload, list) or len(payload) < 2:
        return rows
    block = payload[1]
    if not isinstance(block, dict):
        return rows
    for series in block.values():
        if not isinstance(series, dict):
            continue
        for bar in series.get("s") or []:
            v = bar.get("v") if isinstance(bar, dict) else None
            if not v or len(v) < 5:
                continue
            rows.append(list(v[:6]))
    return rows


async def _ws_history(tv_symbol: str, interval: str, n_bars: int) -> pd.DataFrame:
    import websockets

    cs = "cs_" + secrets.token_hex(6)
    qs = "qs_" + secrets.token_hex(6)
    resolve = json.dumps({"symbol": tv_symbol, "adjustment": "splits"})
    bars: list[list] = []
    done = False
    err: str | None = None

    async with websockets.connect(
        _TV_WS,
        origin=_TV_ORIGIN,
        user_agent_header=_TV_UA,
        open_timeout=18,
        close_timeout=4,
        ping_interval=None,
        max_size=2**22,
    ) as ws:
        await ws.send(_pack("set_auth_token", ["unauthorized_user_token"]))
        await ws.send(_pack("set_locale", ["en", "US"]))
        await ws.send(_pack("chart_create_session", [cs, ""]))
        await ws.send(_pack("quote_create_session", [qs]))
        await ws.send(_pack("resolve_symbol", [cs, "sds_sym_1", f"={resolve}"]))
        await ws.send(
            _pack("create_series", [cs, "sds_1", "s1", "sds_sym_1", interval, n_bars])
        )

        deadline = asyncio.get_running_loop().time() + 22.0
        while asyncio.get_running_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=8.0)
            except TimeoutError:
                break
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            for frame in _split_frames(raw):
                if frame.startswith("~h~"):
                    await ws.send(f"~m~{len(frame)}~m~{frame}")
                    continue
                try:
                    msg = json.loads(frame)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                m = msg.get("m")
                if m in ("du", "timescale_update"):
                    bars.extend(_bars_from_msg(msg))
                elif m == "series_completed":
                    done = True
                elif m in ("symbol_error", "series_error"):
                    err = str(msg.get("p") or m)
                    done = True
            if done:
                break

    if err and not bars:
        return pd.DataFrame()
    if not bars:
        return pd.DataFrame()

    rows = []
    seen_ts: set[int] = set()
    for v in bars:
        ts = int(float(v[0]))
        if ts in seen_ts:
            continue
        seen_ts.add(ts)
        vol = float(v[5]) if len(v) > 5 and v[5] is not None else 0.0
        o, h, l, c = v[1], v[2], v[3], v[4]
        if None in (o, h, l, c):
            continue
        rows.append(
            {
                "timestamp": ts * 1000 if ts < 10_000_000_000 else ts,
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
                "volume": vol,
            }
        )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).sort_values("timestamp").drop_duplicates("timestamp")
    return df.reset_index(drop=True)


def fetch_tv_ohlcv(
    xtb_symbol: str,
    timeframe: str,
    limit: int = DEFAULT_CANDLE_LIMIT,
    *,
    catalog_tv: str | None = None,
    use_cache: bool = True,
) -> tuple[pd.DataFrame, Optional[str], Optional[str]]:
    """
    Возвращает (df, tv_symbol, error).
    Сеть только если TV включён и кэш пуст.
    """
    if not TV_ENABLED:
        return pd.DataFrame(), None, "tv disabled"
    interval = _TV_INTERVAL.get(timeframe)
    if not interval:
        return pd.DataFrame(), None, f"tv no interval for {timeframe}"

    candidates = xtb_to_tv_candidates(xtb_symbol, catalog_tv=catalog_tv)[:_MAX_CANDIDATES]
    if not candidates:
        return pd.DataFrame(), None, "no tv mapping"

    if use_cache:
        for tv_sym in candidates:
            cached = read_cache(cache_path("tv", tv_sym, timeframe, limit), limit=limit)
            if cached is not None:
                return cached, tv_sym, None

    from engine.rate_limit import wait_tv

    last_err = "empty tv"
    for tv_sym in candidates:
        wait_tv()
        try:
            df = asyncio.run(_ws_history(tv_sym, interval, max(limit, MIN_CANDLES)))
        except Exception as e:
            last_err = str(e)[:200]
            from engine.anti_ban import note_error, is_rate_error

            if is_rate_error(last_err):
                note_error(last_err)
            continue
        if df is None or df.empty or len(df) < MIN_CANDLES:
            last_err = f"only {0 if df is None or df.empty else len(df)} tv bars"
            continue
        if len(df) > limit:
            df = df.tail(limit).reset_index(drop=True)
        write_cache(cache_path("tv", tv_sym, timeframe, limit), df)
        _save_map_entry(xtb_symbol, tv_sym)
        return df, tv_sym, None

    return pd.DataFrame(), None, last_err
