"""Случайные паузы и backoff — снижение риска бана Yahoo/API."""

from __future__ import annotations

import random
import re
import time
from typing import Any

from config import (
    PAUSE_LOCAL_MAX,
    PAUSE_LOCAL_MIN,
    PAUSE_STOOQ_MAX,
    PAUSE_STOOQ_MIN,
    PAUSE_TV_MAX,
    PAUSE_TV_MIN,
    PAUSE_YAHOO_MAX,
    PAUSE_YAHOO_MIN,
    STOOQ_MIN_INTERVAL_MAX,
    STOOQ_MIN_INTERVAL_MIN,
    TV_MIN_INTERVAL_MAX,
    TV_MIN_INTERVAL_MIN,
    YAHOO_BACKOFF_BASE_SEC,
    YAHOO_BACKOFF_MAX_SEC,
    YAHOO_MIN_INTERVAL_MAX,
    YAHOO_MIN_INTERVAL_MIN,
)

_rng = random.Random()
_backoff_until: float = 0.0
_consecutive_rate_errors: int = 0


def _jitter(lo: float, hi: float) -> float:
    """Равномерная пауза в диапазоне [lo, hi]."""
    if hi <= lo:
        return lo
    return _rng.uniform(lo, hi)


def job_pause(*, source: str = "local") -> float:
    """Случайная пауза между jobs (local быстрее, сеть медленнее)."""
    src = (source or "local").lower()
    if src in ("tv", "tradingview"):
        return _jitter(PAUSE_TV_MIN, PAUSE_TV_MAX)
    if src in ("yahoo", "finance"):
        return _jitter(PAUSE_YAHOO_MIN, PAUSE_YAHOO_MAX)
    if src == "stooq":
        return _jitter(PAUSE_STOOQ_MIN, PAUSE_STOOQ_MAX)
    return _jitter(PAUSE_LOCAL_MIN, PAUSE_LOCAL_MAX)


def yahoo_gap() -> float:
    """Случайный минимальный интервал между Yahoo-запросами."""
    return _jitter(YAHOO_MIN_INTERVAL_MIN, YAHOO_MIN_INTERVAL_MAX)


def tv_gap() -> float:
    return _jitter(TV_MIN_INTERVAL_MIN, TV_MIN_INTERVAL_MAX)


def stooq_gap() -> float:
    return _jitter(STOOQ_MIN_INTERVAL_MIN, STOOQ_MIN_INTERVAL_MAX)


def wait_backoff() -> float:
    """Ждёт если активен backoff после rate-limit; возвращает секунд ожидания."""
    global _backoff_until
    now = time.time()
    if now >= _backoff_until:
        return 0.0
    gap = _backoff_until - now
    time.sleep(gap)
    return gap


def note_error(error: str | None) -> dict[str, Any]:
    """
    Увеличивает backoff при 429/403/Too Many Requests/Forbidden.
    Экспоненциально: base * 2^n, cap max.
    """
    global _backoff_until, _consecutive_rate_errors
    msg = (error or "").lower()
    rate_hit = any(
        x in msg
        for x in ("429", "403", "too many requests", "forbidden", "rate limit")
    )
    if not rate_hit:
        _consecutive_rate_errors = max(0, _consecutive_rate_errors - 1)
        return {"backoff_sec": 0, "consecutive": _consecutive_rate_errors}

    _consecutive_rate_errors += 1
    delay = min(
        YAHOO_BACKOFF_MAX_SEC,
        YAHOO_BACKOFF_BASE_SEC * (2 ** (_consecutive_rate_errors - 1)),
    )
    delay += _jitter(0.5, 3.0)
    _backoff_until = max(_backoff_until, time.time() + delay)
    return {
        "backoff_sec": round(delay, 2),
        "consecutive": _consecutive_rate_errors,
        "until": round(_backoff_until, 1),
    }


def note_success() -> None:
    """Постепенно сбрасывает счётчик после успешных запросов."""
    global _consecutive_rate_errors
    if _consecutive_rate_errors > 0:
        _consecutive_rate_errors -= 1


def is_rate_error(error: str | None) -> bool:
    msg = (error or "").lower()
    return bool(re.search(r"429|403|too many|forbidden|rate.?limit", msg))


def daily_budget_seconds() -> int:
    """Случайный дневной бюджет активной работы (часы из config min..max)."""
    from config import DAILY_RUN_HOURS_MAX, DAILY_RUN_HOURS_MIN

    hours = _rng.uniform(DAILY_RUN_HOURS_MIN, DAILY_RUN_HOURS_MAX)
    return int(hours * 3600)


def anti_ban_snapshot() -> dict[str, Any]:
    return {
        "backoff_active": time.time() < _backoff_until,
        "backoff_until_in_sec": max(0.0, round(_backoff_until - time.time(), 1)),
        "consecutive_rate_errors": _consecutive_rate_errors,
        "pause_local": [PAUSE_LOCAL_MIN, PAUSE_LOCAL_MAX],
        "pause_yahoo": [PAUSE_YAHOO_MIN, PAUSE_YAHOO_MAX],
        "pause_tv": [PAUSE_TV_MIN, PAUSE_TV_MAX],
        "pause_stooq": [PAUSE_STOOQ_MIN, PAUSE_STOOQ_MAX],
    }
