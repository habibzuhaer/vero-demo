"""Предустановленные режимы запуска VERO."""

from __future__ import annotations

from typing import Any

from config import (
    DEFAULT_CANDLE_LIMIT,
    DEFAULT_TIMEFRAMES,
    STRATEGY_IDS,
)

MODES: dict[str, dict[str, Any]] = {
    "smoke": {
        "id": "smoke",
        "title": "Smoke",
        "desc": "Быстрая проверка: seed-тикеры, 1h, 2 стратегии (~минуты).",
        "icon": "⚡",
        "universe": "seed",
        "timeframes": ["1h"],
        "strategies": ["rsi_mean_reversion", "ema_crossover"],
        "candle_limit": 200,
        "save_trades": False,
    },
    "daily": {
        "id": "daily",
        "title": "Daily",
        "desc": "Стабильный прогон: full universe, анти-бан, лимит 4–8 ч/день.",
        "icon": "📅",
        "universe": "full",
        "timeframes": list(DEFAULT_TIMEFRAMES),
        "strategies": list(STRATEGY_IDS),
        "candle_limit": DEFAULT_CANDLE_LIMIT,
        "save_trades": True,
    },
    "full": {
        "id": "full",
        "title": "Full catalog",
        "desc": "Полный каталог акций/ETF, 1h/4h/1d × стратегии.",
        "icon": "🔄",
        "universe": "full",
        "timeframes": list(DEFAULT_TIMEFRAMES),
        "strategies": list(STRATEGY_IDS),
        "candle_limit": DEFAULT_CANDLE_LIMIT,
        "save_trades": True,
    },
    "qualified": {
        "id": "qualified",
        "title": "Qualified",
        "desc": "Только qualified из MTB-скринера (~12 тикеров).",
        "icon": "✓",
        "universe": "qualified",
        "timeframes": list(DEFAULT_TIMEFRAMES),
        "strategies": list(STRATEGY_IDS),
        "candle_limit": DEFAULT_CANDLE_LIMIT,
        "save_trades": True,
    },
    "top": {
        "id": "top",
        "title": "Top",
        "desc": "Только top-пары из скринера.",
        "icon": "★",
        "universe": "top",
        "timeframes": list(DEFAULT_TIMEFRAMES),
        "strategies": list(STRATEGY_IDS),
        "candle_limit": DEFAULT_CANDLE_LIMIT,
        "save_trades": True,
    },
    "learn": {
        "id": "learn",
        "title": "Learn",
        "desc": "Построить кластеры и правила риска по накопленным samples.",
        "icon": "🧠",
        "action": "learn",
    },
    "preflight": {
        "id": "preflight",
        "title": "Preflight",
        "desc": "Проверка всех модулей без бэктеста.",
        "icon": "🔍",
        "action": "preflight",
    },
    "bootstrap": {
        "id": "bootstrap",
        "title": "Bootstrap data",
        "desc": "Скопировать каталог и связать свечи в data/ (автономия).",
        "icon": "📦",
        "action": "bootstrap",
    },
    "prefetch_seed": {
        "id": "prefetch_seed",
        "title": "Prefetch seed",
        "desc": "~20 тикерів · 1h/4h/1d · 1600 барів + індикатори (докачка).",
        "icon": "⬇",
        "action": "prefetch",
        "universe": "seed",
        "timeframes": list(DEFAULT_TIMEFRAMES),
        "candle_limit": DEFAULT_CANDLE_LIMIT,
    },
    "prefetch_full": {
        "id": "prefetch_full",
        "title": "Prefetch full",
        "desc": "Повний каталог · 1h/4h/1d · 1600 + індикатори (дні).",
        "icon": "⬇⬇",
        "action": "prefetch",
        "universe": "full",
        "timeframes": list(DEFAULT_TIMEFRAMES),
        "candle_limit": DEFAULT_CANDLE_LIMIT,
    },
}


def list_modes() -> list[dict[str, Any]]:
    out = []
    for m in MODES.values():
        item = {k: v for k, v in m.items() if k != "icon"}
        item["icon"] = m.get("icon", "")
        out.append(item)
    return out


def get_mode(mode_id: str) -> dict[str, Any] | None:
    return MODES.get((mode_id or "").lower())
