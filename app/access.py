"""Access requests: unknown senders ask, administrators approve from their own Telegram chat.

Flow
1. An un-enrolled person messages the bot (Telegram or WhatsApp).
2. The bot records a pending request (data/access_requests.json) and tells the person it has been
   forwarded. Repeat messages do not re-notify (one reminder per hour at most).
3. Every administrator (users with "admin": true in users.json) gets a Telegram message with buttons:
   Viewer / Operational / Executive / Deny.
4. Approval enrols the person with that tier and notifies them; denial notifies them too.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Awaitable, Callable

from . import auth, config

log = logging.getLogger("admissionos.access")

REQUESTS_FILE = config.DATA_DIR / "access_requests.json"
RENOTIFY_SECONDS = 3600

# Set by the Telegram bot at startup: async fn(chat_id, text, keyboard) -> None
_notify: Callable[[int, str, list[list[dict[str, str]]]], Awaitable[Any]] | None = None
# Set by each channel so approvals can message the requester back: channel -> async fn(external_id, text)
_reply_to: dict[str, Callable[[str, str], Awaitable[Any]]] = {}


def register_notifier(fn: Callable[[int, str, list[list[dict[str, str]]]], Awaitable[Any]]) -> None:
    global _notify
    _notify = fn


def register_replier(channel: str, fn: Callable[[str, str], Awaitable[Any]]) -> None:
    _reply_to[channel] = fn


def _load() -> dict[str, dict[str, Any]]:
    if REQUESTS_FILE.exists():
        try:
            return json.loads(REQUESTS_FILE.read_text("utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save(data: dict[str, dict[str, Any]]) -> None:
    REQUESTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    REQUESTS_FILE.write_text(json.dumps(data, indent=2), "utf-8")


def admins() -> list[dict[str, Any]]:
    users = [u for u in auth.load_users() if u.get("active", True) and u.get("telegram_id")]
    flagged = [u for u in users if u.get("admin")]
    return flagged or [u for u in users if u.get("tier") == "executive"]   # fall back to executives if nobody is flagged


def is_admin(telegram_id: Any) -> bool:
    wanted = auth.normalize("telegram", telegram_id)
    return any(auth.normalize("telegram", a.get("telegram_id")) == wanted for a in admins())


async def request(channel: str, external_id: Any, display: str, first_message: str) -> str:
    """Record a request and notify admins. Returns 'new', 'pending' (already waiting) or 'no_admin'."""
    ext = auth.normalize(channel, external_id)
    key = f"{channel}:{ext}"
    data = _load()
    now = time.time()
    entry = data.get(key)
    status = "new"
    if entry and entry.get("status") == "pending":
        status = "pending"
        if now - entry.get("notified_at", 0) < RENOTIFY_SECONDS:
            return status
    entry = {"channel": channel, "id": ext, "display": display, "message": first_message[:300],
             "status": "pending", "created_at": entry.get("created_at", now) if entry else now, "notified_at": now}
    data[key] = entry
    _save(data)
    if _notify is None or not admins():
        return "no_admin"
    where = "Telegram" if channel == "telegram" else "WhatsApp"
    text = (f"🔐 <b>Access request</b>\n{where}: <b>{display}</b> (id <code>{ext}</code>)\n"
            f"First message: <i>{first_message[:200]}</i>\n\nGrant which level?")
    keyboard = [[{"text": "Viewer", "callback_data": f"acc|{key}|viewer"},
                 {"text": "Operational", "callback_data": f"acc|{key}|operational"},
                 {"text": "Executive", "callback_data": f"acc|{key}|executive"}],
                [{"text": "❌ Deny", "callback_data": f"acc|{key}|deny"}]]
    for admin in admins():
        try:
            await _notify(int(admin["telegram_id"]), text, keyboard)
        except Exception:  # noqa: BLE001
            log.exception("could not notify admin %s", admin.get("handle"))
    return status


async def decide(key: str, decision: str, by_name: str) -> str:
    """Apply an admin's button press. Returns a short status line for the admin's message."""
    data = _load()
    entry = data.get(key)
    if not entry:
        return "This request is no longer on file."
    if entry.get("status") != "pending":
        return f"Already {entry.get('status')} by {entry.get('decided_by', 'an administrator')}."
    channel, ext = entry["channel"], entry["id"]
    if decision == "deny":
        entry.update(status="denied", decided_by=by_name, decided_at=time.time())
        _save(data)
        await _tell(channel, ext, "Your access request for AdmissionOS Prime was declined by the administrator.")
        return f"❌ Denied by {by_name}."
    if decision not in auth.TIERS:
        return "Unknown decision."
    users = auth.load_users()
    field = auth.CHANNEL_FIELD[channel]
    existing = next((u for u in users if auth.normalize(channel, u.get(field)) == ext), None)
    if existing:
        existing.update(tier=decision, active=True)
        handle = existing["handle"]
    else:
        base = "tg" if channel == "telegram" else "wa"
        handle = f"{base}{ext}"
        users.append({"handle": handle, "name": entry.get("display") or handle, "tier": decision, "active": True, field: ext})
    auth.save_users(users)
    entry.update(status="approved", tier=decision, decided_by=by_name, decided_at=time.time())
    _save(data)
    await _tell(channel, ext, f"✅ You now have access to AdmissionOS Prime ({decision} tier). Send /help to see what I can do.")
    log.info("access granted: %s -> %s by %s", key, decision, by_name)
    return f"✅ Approved as {decision} by {by_name}."


async def _tell(channel: str, ext: str, text: str) -> None:
    fn = _reply_to.get(channel)
    if fn is None:
        return
    try:
        await fn(ext, text)
    except Exception:  # noqa: BLE001
        log.exception("could not notify requester %s:%s", channel, ext)
