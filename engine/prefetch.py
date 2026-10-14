"""Докачка OHLCV → enrich → vero_bars.db с прогрессом и resume после рестарта."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from config import (
    ALLOWED_TIMEFRAMES,
    CACHE_DIR,
    MIN_CANDLES,
    PREFETCH_TARGET_BARS,
    PREFETCH_TIMEFRAMES,
    ensure_dirs,
)
from engine import indicators
from engine.anti_ban import job_pause
from engine.stooq import fetch_stooq_ohlcv
from engine.tv import fetch_tv_ohlcv
from engine.vero_store import (
    bar_count,
    ensure_store,
    last_timestamp,
    load_ohlcv_only,
    merge_ohlcv,
    replace_bars,
    store_stats,
)
from engine.yahoo import fetch_xtb_ohlcv, universe_symbols, xtb_to_yahoo

# Порядок сети для prefetch (без vero/local — они цель записи)
_NET_ORDER = ("tv", "yahoo", "stooq")
_STATE_PATH = CACHE_DIR / "prefetch_state.json"

def _catalog_tv(symbol: str) -> str | None:
    try:
        from engine.yahoo import _load_catalog_meta

        it = _load_catalog_meta().get(symbol) or {}
        tv = (it.get("tv_symbol") or "").strip()
        return tv or None
    except Exception:
        return None


def fetch_network_ohlcv(
    symbol: str,
    timeframe: str,
    limit: int,
) -> tuple[Any, str, str | None]:
    """Тянет сырые свечи с сети. Возвращает (df, source, error)."""
    import pandas as pd

    errors: list[str] = []
    for src in _NET_ORDER:
        if src == "tv":
            df, mapped, err = fetch_tv_ohlcv(
                symbol,
                timeframe,
                limit=limit,
                catalog_tv=_catalog_tv(symbol),
                use_cache=True,
            )
            if err or df is None or df.empty or len(df) < MIN_CANDLES:
                errors.append(f"tv: {err or 'empty'}")
                continue
            return df.reset_index(drop=True), "tv", None
        if src == "yahoo":
            df, mapped, err = fetch_xtb_ohlcv(
                symbol, timeframe, limit=limit, use_cache=True
            )
            if err or df is None or df.empty or len(df) < MIN_CANDLES:
                errors.append(f"yahoo: {err or 'empty'}")
                continue
            return df.reset_index(drop=True), "yahoo", None
        if src == "stooq":
            df, mapped, err = fetch_stooq_ohlcv(
                symbol, timeframe, limit=limit, use_cache=True
            )
            if err or df is None or df.empty or len(df) < MIN_CANDLES:
                errors.append(f"stooq: {err or 'empty'}")
                continue
            return df.reset_index(drop=True), "stooq", None
    return pd.DataFrame(), "skip", "; ".join(errors) if errors else "no ohlcv"


def sync_symbol_tf(
    symbol: str,
    timeframe: str,
    *,
    target_bars: int = PREFETCH_TARGET_BARS,
    force: bool = False,
) -> dict[str, Any]:
    """
    Докачка одного symbol×tf:
    merge с локальным → enrich → запись.
    Если уже >= target и бар свежий — skip без сети (resume/crash-safe).
    """
    ensure_store()
    have = bar_count(symbol, timeframe)
    last_ts = last_timestamp(symbol, timeframe)

    # Resume/crash-safe: уже набрали target — не трогаем сеть
    # (свежесть хвоста — отдельный force=True прогон)
    if not force and have >= target_bars:
        return {
            "ok": True,
            "skipped": True,
            "symbol": symbol,
            "timeframe": timeframe,
            "bars": have,
            "added": 0,
            "source": "vero",
            "message": "already local",
        }

    existing = load_ohlcv_only(
        symbol, timeframe, limit=max(target_bars * 2, target_bars)
    )
    have = len(existing)
    last_ts = last_timestamp(symbol, timeframe)

    fetch_limit = target_bars
    if have >= max(MIN_CANDLES, target_bars // 2) and not force:
        # докачка хвоста, не весь объём
        fetch_limit = min(target_bars, max(200, target_bars // 4))
    fresh, source, err = fetch_network_ohlcv(symbol, timeframe, fetch_limit)
    if err or fresh.empty:
        if have >= MIN_CANDLES:
            return {
                "ok": True,
                "skipped": True,
                "symbol": symbol,
                "timeframe": timeframe,
                "bars": have,
                "added": 0,
                "source": "vero",
                "message": f"kept local ({err})",
            }
        return {
            "ok": False,
            "symbol": symbol,
            "timeframe": timeframe,
            "bars": have,
            "added": 0,
            "source": source,
            "error": err or "empty",
        }

    merged = merge_ohlcv(existing, fresh)
    if target_bars and len(merged) > target_bars:
        merged = merged.tail(target_bars).reset_index(drop=True)

    if len(merged) < MIN_CANDLES:
        return {
            "ok": False,
            "symbol": symbol,
            "timeframe": timeframe,
            "bars": len(merged),
            "added": 0,
            "source": source,
            "error": f"only {len(merged)} bars after merge",
        }

    enriched = indicators.enrich(merged)
    n = replace_bars(
        symbol, timeframe, enriched, source=source, keep_limit=target_bars
    )
    added = 0
    if last_ts is not None and "timestamp" in fresh.columns:
        added = int((fresh["timestamp"] > last_ts).sum())
    else:
        added = n

    return {
        "ok": True,
        "skipped": False,
        "symbol": symbol,
        "timeframe": timeframe,
        "bars": n,
        "added": added,
        "source": source,
        "yahoo": xtb_to_yahoo(symbol),
    }


def _load_state() -> dict[str, Any] | None:
    try:
        if not _STATE_PATH.exists():
            return None
        return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_state(payload: dict[str, Any]) -> None:
    try:
        ensure_dirs()
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_PATH.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=0),
            encoding="utf-8",
        )
        tmp.replace(_STATE_PATH)
    except Exception:
        pass


class PrefetchQueue:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = self._idle_state()
        self._jobs: list[tuple[str, str]] = []
        self._cursor = 0

    def _idle_state(self) -> dict[str, Any]:
        return {
            "status": "idle",
            "universe": None,
            "timeframes": [],
            "target_bars": PREFETCH_TARGET_BARS,
            "total": 0,
            "done": 0,
            "ok": 0,
            "failed": 0,
            "skipped": 0,
            "bars_written": 0,
            "current": None,
            "message": "",
            "pct": 0.0,
            "started_at": None,
            "finished_at": None,
            "errors": [],
            "resumed": False,
            "store": {},
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            st = dict(self._state)
            st["store"] = store_stats()
            return st

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        with self._lock:
            if self._state.get("status") == "running":
                self._state["status"] = "stopping"
                self._state["message"] = "stopping…"
                self._persist_locked(status="stopping")
        return self.status()

    def _persist_locked(self, **extra: Any) -> None:
        payload = {
            "status": self._state.get("status"),
            "universe": self._state.get("universe"),
            "timeframes": self._state.get("timeframes"),
            "target_bars": self._state.get("target_bars"),
            "force": self._state.get("force", False),
            "symbols_count": self._state.get("symbols_count"),
            "total": self._state.get("total"),
            "done": self._state.get("done"),
            "ok": self._state.get("ok"),
            "failed": self._state.get("failed"),
            "skipped": self._state.get("skipped"),
            "bars_written": self._state.get("bars_written"),
            "cursor": self._cursor,
            "started_at": self._state.get("started_at"),
            "finished_at": self._state.get("finished_at"),
            "errors": self._state.get("errors") or [],
            "updated_at": time.time(),
        }
        payload.update(extra)
        _save_state(payload)

    def start(
        self,
        *,
        universe: str = "seed",
        timeframes: list[str] | None = None,
        target_bars: int | None = None,
        symbols: list[str] | None = None,
        force: bool = False,
        resume: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeError("prefetch already running")
            self._stop.clear()
            tfs = [
                t
                for t in (timeframes or list(PREFETCH_TIMEFRAMES))
                if t in ALLOWED_TIMEFRAMES
            ]
            if not tfs:
                raise ValueError("no timeframes")
            uni = universe if symbols is None else "custom"
            syms = list(symbols) if symbols else universe_symbols(universe)
            if not syms:
                raise ValueError("empty universe")
            limit = int(target_bars or PREFETCH_TARGET_BARS)
            jobs = [(s, tf) for s in syms for tf in tfs]

            cursor = 0
            ok = fail = skipped = bars_w = 0
            started_at = time.time()
            resumed = False
            saved = _load_state() if resume and not force and symbols is None else None
            if (
                saved
                and saved.get("universe") == uni
                and list(saved.get("timeframes") or []) == tfs
                and int(saved.get("target_bars") or 0) == limit
                and int(saved.get("total") or 0) == len(jobs)
                and saved.get("status") in ("running", "stopping", "stopped")
            ):
                cursor = max(0, min(int(saved.get("cursor") or 0), len(jobs)))
                ok = int(saved.get("ok") or 0)
                fail = int(saved.get("failed") or 0)
                skipped = int(saved.get("skipped") or 0)
                bars_w = int(saved.get("bars_written") or 0)
                started_at = float(saved.get("started_at") or started_at)
                resumed = cursor > 0
                # done в стейте = cursor (уже обработано)
                done0 = cursor
            else:
                done0 = 0

            self._jobs = jobs
            self._cursor = cursor
            self._state = {
                "status": "running",
                "universe": uni,
                "timeframes": tfs,
                "target_bars": limit,
                "symbols_count": len(syms),
                "total": len(jobs),
                "done": done0,
                "ok": ok,
                "failed": fail,
                "skipped": skipped,
                "bars_written": bars_w,
                "current": None,
                "message": (
                    f"resume @{cursor}/{len(jobs)}"
                    if resumed
                    else f"start {len(syms)} symbols × {len(tfs)} tf"
                ),
                "pct": round(100.0 * done0 / max(1, len(jobs)), 1),
                "started_at": started_at,
                "finished_at": None,
                "errors": list(saved.get("errors") or [])[-40:] if resumed and saved else [],
                "force": force,
                "resumed": resumed,
                "store": store_stats(),
            }
            self._persist_locked(status="running")
            self._thread = threading.Thread(
                target=self._run,
                args=(limit, force),
                daemon=True,
                name="vero-prefetch",
            )
            self._thread.start()
        return self.status()

    def try_auto_resume(self) -> dict[str, Any] | None:
        """После рестарта сервера: продолжить прерванный prefetch."""
        saved = _load_state()
        if not saved:
            return None
        if saved.get("status") not in ("running", "stopping"):
            return None
        if self._thread and self._thread.is_alive():
            return None
        uni = saved.get("universe") or "seed"
        if uni == "custom":
            return None
        try:
            return self.start(
                universe=str(uni),
                timeframes=list(saved.get("timeframes") or PREFETCH_TIMEFRAMES),
                target_bars=int(saved.get("target_bars") or PREFETCH_TARGET_BARS),
                force=bool(saved.get("force")),
                resume=True,
            )
        except Exception:
            return None

    def _set(self, **kw: Any) -> None:
        with self._lock:
            self._state.update(kw)
            total = max(1, int(self._state.get("total") or 1))
            done = int(self._state.get("done") or 0)
            self._state["pct"] = round(100.0 * done / total, 1)

    def _run(self, limit: int, force: bool) -> None:
        ensure_store()
        jobs = self._jobs
        i = self._cursor
        while i < len(jobs):
            if self._stop.is_set():
                with self._lock:
                    self._cursor = i
                    self._state["status"] = "stopped"
                    self._state["message"] = "stopped by user — resume later"
                    self._state["finished_at"] = time.time()
                    self._state["current"] = None
                    self._persist_locked(status="stopped")
                return

            sym, tf = jobs[i]
            label = f"{sym} {tf}"
            self._set(current=label, message=f"sync {label}")
            try:
                out = sync_symbol_tf(sym, tf, target_bars=limit, force=force)
            except Exception as e:
                out = {
                    "ok": False,
                    "symbol": sym,
                    "timeframe": tf,
                    "error": str(e)[:200],
                    "bars": 0,
                    "added": 0,
                }

            with self._lock:
                i += 1
                self._cursor = i
                self._state["done"] = i
                if out.get("ok"):
                    if out.get("skipped"):
                        self._state["skipped"] = int(self._state["skipped"]) + 1
                    else:
                        self._state["ok"] = int(self._state["ok"]) + 1
                        self._state["bars_written"] = int(
                            self._state["bars_written"]
                        ) + int(out.get("bars") or 0)
                else:
                    self._state["failed"] = int(self._state["failed"]) + 1
                    errs = list(self._state.get("errors") or [])
                    errs.append(
                        {
                            "symbol": sym,
                            "timeframe": tf,
                            "error": out.get("error"),
                        }
                    )
                    self._state["errors"] = errs[-40:]
                total = max(1, int(self._state.get("total") or 1))
                self._state["pct"] = round(100.0 * i / total, 1)
                self._state["message"] = (
                    f"{i}/{total} · ok={self._state['ok']} "
                    f"fail={self._state['failed']} skip={self._state['skipped']}"
                )
                self._persist_locked(status="running")

            if not out.get("skipped"):
                src = (out.get("source") or "tv").lower()
                job_pause(source=src if src in ("tv", "yahoo", "stooq") else "tv")

        with self._lock:
            self._state["status"] = "done"
            self._state["current"] = None
            self._state["message"] = "prefetch done"
            self._state["finished_at"] = time.time()
            self._persist_locked(status="done")


PREFETCH = PrefetchQueue()
