"""Telegram Bot API — фото + текст сигнала."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID


def telegram_configured() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def discover_chat_ids() -> dict[str, Any]:
    if not TELEGRAM_BOT_TOKEN:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN missing"}
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        r = httpx.get(url, timeout=15)
        data = r.json()
        if not data.get("ok"):
            return {"ok": False, "error": data.get("description", "getUpdates failed")}
        chats: list[dict[str, Any]] = []
        for upd in data.get("result", []):
            msg = upd.get("message") or upd.get("channel_post") or {}
            chat = msg.get("chat") or {}
            cid = chat.get("id")
            if cid is None:
                continue
            chats.append(
                {
                    "chat_id": str(cid),
                    "type": chat.get("type"),
                    "title": chat.get("title") or chat.get("username") or chat.get("first_name"),
                }
            )
        uniq = {c["chat_id"]: c for c in chats}
        return {"ok": True, "chats": list(uniq.values())}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def format_signal_caption(ev: dict[str, Any]) -> str:
    et = ev.get("event_type", "?")
    sym = ev.get("symbol", "?")
    tf = ev.get("timeframe", "?")
    st = ev.get("strategy", "?")
    price = ev.get("price", 0)
    oos = ev.get("oos_return_pct")

    if et == "BUY":
        icon = "🟢"
        uk = "ВХІД / LONG"
        en = "ENTRY / LONG"
    else:
        icon = "🔴"
        uk = "ВИХІД / CLOSE"
        en = "EXIT / CLOSE"

    lines: list[str] = []
    if ev.get("test"):
        lines.append("🧪 <b>TEST</b> · перевірка стратегії / strategy check")
    lines.extend([
        f"{icon} <b>{uk}</b> · {en}",
        f"<b>{sym}</b> · {tf}",
        f"Strategy: <code>{st}</code>",
        f"Price: <b>{price}</b>",
    ])
    if et == "BUY":
        sl = ev.get("stop_price")
        slp = ev.get("stop_loss_pct")
        if sl and slp:
            lines.append(f"Stop: <b>{sl}</b> (−{slp}%)")
        tp = ev.get("take_profit_note") or ""
        if tp:
            lines.append(f"TP: {tp}")
    if oos is not None:
        lines.append(f"Backtest OOS: <b>{oos}%</b>")
    lines.append("")
    lines.append("📄 Paper trade · VERO")
    return "\n".join(lines)


def send_signal(ev: dict[str, Any], chart_path: Path | None = None) -> dict[str, Any]:
    if not TELEGRAM_BOT_TOKEN:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN missing"}
    if not TELEGRAM_CHAT_ID:
        return {"ok": False, "error": "TELEGRAM_CHAT_ID missing — message your bot, then GET /api/telegram/discover"}

    caption = format_signal_caption(ev)
    chat_id = TELEGRAM_CHAT_ID

    try:
        if chart_path and chart_path.exists():
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            with chart_path.open("rb") as f:
                r = httpx.post(
                    url,
                    data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
                    files={"photo": (chart_path.name, f, "image/png")},
                    timeout=30,
                )
        else:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            r = httpx.post(
                url,
                json={"chat_id": chat_id, "text": caption, "parse_mode": "HTML"},
                timeout=20,
            )
        data = r.json()
        if not data.get("ok"):
            return {"ok": False, "error": data.get("description", r.text[:200])}
        msg = data.get("result") or {}
        return {"ok": True, "message_id": msg.get("message_id")}
    except Exception as e:
        return {"ok": False, "error": str(e)}
