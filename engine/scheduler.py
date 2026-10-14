"""Дневной бюджет (случайный 4–8 ч) + учёт активного времени."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from config import DAILY_RUN_HOURS_MAX, DAILY_RUN_HOURS_MIN
from engine.anti_ban import anti_ban_snapshot, daily_budget_seconds

_BUDGET_KEY = "daily_budget"
_DAY_KEY = "daily_day"
_USED_KEY = "daily_used_sec"
_SESSION_MODE_KEY = "session_mode"  # fixed | unlimited | daily
_SESSION_LABEL_KEY = "session_label"

# ~1 year — practical «until cancel»
UNLIMITED_BUDGET_SEC = 365 * 24 * 3600


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_table(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS scheduler_state (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        """
    )


def _read_raw() -> dict[str, str]:
    from db import db_session

    with db_session() as conn:
        _ensure_table(conn)
        rows = conn.execute("SELECT key, value FROM scheduler_state").fetchall()
    return {r["key"]: r["value"] for r in rows}


def _write_keys(**kwargs: str) -> None:
    from db import db_session, init_db

    init_db()
    now = _utc()
    with db_session() as conn:
        _ensure_table(conn)
        for key, value in kwargs.items():
            conn.execute(
                """
                INSERT INTO scheduler_state(key, value, updated_at) VALUES (?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, value, now),
            )


def _roll_new_day() -> dict[str, Any]:
    """Новый UTC-день: сброс used + случайный budget."""
    day = _today_utc()
    budget = daily_budget_seconds()
    _write_keys(
        **{
            _DAY_KEY: day,
            _USED_KEY: "0",
            _BUDGET_KEY: str(budget),
            _SESSION_MODE_KEY: "daily",
            _SESSION_LABEL_KEY: "daily",
        }
    )
    return {"day": day, "used_sec": 0.0, "budget_sec": float(budget)}


def set_session_budget(hours: float | None, *, label: str = "") -> dict[str, Any]:
    """
    Задать бюджет текущей сессии.
    hours: 2/4/6/8 — фиксированный лимит; None или <=0 — без лимита (цикл).
    """
    day = _today_utc()
    if hours is None or hours <= 0:
        budget = float(UNLIMITED_BUDGET_SEC)
        mode = "unlimited"
        lbl = label or "cycle"
    else:
        budget = float(hours) * 3600.0
        mode = "fixed"
        lbl = label or f"{int(hours)}h"
    _write_keys(
        **{
            _DAY_KEY: day,
            _USED_KEY: "0",
            _BUDGET_KEY: str(int(budget)),
            _SESSION_MODE_KEY: mode,
            _SESSION_LABEL_KEY: lbl,
        }
    )
    return snapshot()


def _read_state() -> dict[str, Any]:
    data = _read_raw()
    today = _today_utc()
    day = data.get(_DAY_KEY) or ""
    if day != today:
        base = _roll_new_day()
    else:
        base = {
            "day": day,
            "used_sec": float(data.get(_USED_KEY) or 0),
            "budget_sec": float(data.get(_BUDGET_KEY) or daily_budget_seconds()),
        }
    remaining = max(0.0, base["budget_sec"] - base["used_sec"])
    mode = data.get(_SESSION_MODE_KEY) or "daily"
    lbl = data.get(_SESSION_LABEL_KEY) or mode
    return {
        **base,
        "session_mode": mode,
        "session_label": lbl,
        "unlimited": mode == "unlimited",
        "budget_hours_min": DAILY_RUN_HOURS_MIN,
        "budget_hours_max": DAILY_RUN_HOURS_MAX,
        "budget_hours": round(base["budget_sec"] / 3600, 2),
        "remaining_sec": remaining,
    }


def snapshot() -> dict[str, Any]:
    st = _read_state()
    rem = st["remaining_sec"]
    st["remaining_hours"] = round(rem / 3600, 2)
    st["used_hours"] = round(st["used_sec"] / 3600, 2)
    st["exhausted"] = rem <= 0
    st["anti_ban"] = anti_ban_snapshot()
    return st


def add_active_seconds(seconds: float) -> dict[str, Any]:
    if seconds <= 0:
        return snapshot()
    st = _read_state()
    used = st["used_sec"] + seconds
    _write_keys(**{_DAY_KEY: st["day"], _USED_KEY: str(round(used, 3)), _BUDGET_KEY: str(int(st["budget_sec"]))})
    return snapshot()


def seconds_until_next_day_utc() -> float:
    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(60.0, (tomorrow - now).total_seconds())


def wait_for_budget(stop_event, *, on_tick=None) -> bool:
    """Ждёт бюджет. fixed-сессия при исчерпании → pause; daily → ждёт UTC-день."""
    while True:
        st = snapshot()
        if st["remaining_sec"] > 0:
            return True
        if stop_event.is_set():
            return False
        if st.get("session_mode") == "fixed":
            return False
        wait = min(300.0, seconds_until_next_day_utc())
        if on_tick:
            on_tick(st, wait)
        end = time.time() + wait
        while time.time() < end:
            if stop_event.is_set():
                return False
            time.sleep(min(5.0, end - time.time()))
        _roll_new_day()
