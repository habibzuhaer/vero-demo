"""Rate-limit сетевых источников с jitter и backoff."""

from __future__ import annotations

import threading
import time

from engine.anti_ban import note_success, stooq_gap, tv_gap, wait_backoff, yahoo_gap

_lock = threading.Lock()
_last: dict[str, float] = {"yahoo": 0.0, "tv": 0.0, "stooq": 0.0}


def _wait_kind(kind: str, gap_fn) -> None:
    global _last
    wait_backoff()
    with _lock:
        now = time.time()
        gap = gap_fn() - (now - _last.get(kind, 0.0))
        if gap > 0:
            time.sleep(gap)
        _last[kind] = time.time()
    note_success()


def wait_yahoo() -> None:
    _wait_kind("yahoo", yahoo_gap)


def wait_tv() -> None:
    _wait_kind("tv", tv_gap)


def wait_stooq() -> None:
    _wait_kind("stooq", stooq_gap)
