"""Telegram bridge — the bot texts Brady a summary of each self-change it wants to make, with
Approve / Deny buttons, and reads his taps. No webhook / public URL needed: the always-on trader
long-polls getUpdates each loop tick. Degrades to a SILENT no-op when the creds aren't set, so the
deploy is always safe whether or not Telegram is wired.

Setup (Railway vars on the tentacle-trader service):
  TELEGRAM_BOT_TOKEN   from @BotFather  (/newbot)
  TELEGRAM_CHAT_ID     your numeric chat id (message @userinfobot, it replies with your id)
"""
from __future__ import annotations

import os

try:
    import requests
    _HAVE = True
except Exception:  # pragma: no cover
    _HAVE = False

from .persist import load_json as _load, save_json as _save

API = "https://api.telegram.org/bot{token}/{method}"
OFFSET_PATH = "state/evolution/tg_offset.json"


def _token() -> str | None:
    return os.getenv("TELEGRAM_BOT_TOKEN")


def _chat() -> str | None:
    return os.getenv("TELEGRAM_CHAT_ID")


def enabled() -> bool:
    return bool(_HAVE and _token() and _chat())


def esc(s) -> str:
    """Escape for Telegram HTML parse mode."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _call(method: str, payload: dict, timeout: float = 15):
    if not _HAVE or not _token():
        return None
    try:
        r = requests.post(API.format(token=_token(), method=method), json=payload, timeout=timeout)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def send(text: str, buttons=None) -> int | None:
    """Send a message; buttons = list of (label, callback_data). Returns message_id or None."""
    if not enabled():
        return None
    payload = {"chat_id": _chat(), "text": text[:4000], "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": l, "callback_data": d} for l, d in buttons]]}
    r = _call("sendMessage", payload)
    try:
        return r["result"]["message_id"] if r and r.get("ok") else None
    except Exception:
        return None


def poll(timeout: int = 0) -> list[dict]:
    """Return new button taps since last poll, acking the offset. timeout=0 -> non-blocking."""
    if not enabled():
        return []
    off = _load(OFFSET_PATH, {"offset": 0}).get("offset", 0)
    r = _call("getUpdates", {"offset": off, "timeout": timeout,
                             "allowed_updates": ["callback_query"]}, timeout=timeout + 12)
    out: list[dict] = []
    if not (r and r.get("ok")):
        return out
    max_id = off
    for u in r["result"]:
        max_id = max(max_id, u["update_id"] + 1)
        cq = u.get("callback_query")
        if not cq:
            continue
        msg = cq.get("message") or {}
        out.append({"data": cq.get("data", ""), "query_id": cq.get("id"),
                    "message_id": msg.get("message_id"),
                    "chat_id": (msg.get("chat") or {}).get("id")})
    if max_id != off:
        _save(OFFSET_PATH, {"offset": max_id})
    return out


def ack(query_id: str | None, text: str = ""):
    if query_id:
        _call("answerCallbackQuery", {"callback_query_id": query_id, "text": text[:180]})


def edit(chat_id, message_id, text: str):
    if enabled() and message_id:
        _call("editMessageText", {"chat_id": chat_id or _chat(), "message_id": message_id,
                                  "text": text[:4000], "parse_mode": "HTML",
                                  "disable_web_page_preview": True})
