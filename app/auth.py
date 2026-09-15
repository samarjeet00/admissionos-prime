"""Identity + authorization for messaging channels.

Identity is the channel's own account: the Telegram user ID or the WhatsApp phone
number. Both are allow-listed in data/users.json by an administrator, so an unknown
sender gets ACCESS DENIED and nothing else. No passwords over chat - they would just
sit in the message history.

Authorization: each user carries a `tier`, which decides which portal tools the model
is allowed to see (see app/brain.py::tools_for_tier).
"""
from __future__ import annotations

import json
import re
from typing import Any

from . import config

TIERS = ("executive", "operational", "viewer")
CHANNEL_FIELD = {"telegram": "telegram_id", "whatsapp": "whatsapp"}


def load_users() -> list[dict[str, Any]]:
    if not config.USERS_FILE.exists():
        return []
    return json.loads(config.USERS_FILE.read_text("utf-8"))


def save_users(users: list[dict[str, Any]]) -> None:
    config.USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.USERS_FILE.write_text(json.dumps(users, indent=2), "utf-8")


def normalize(channel: str, external_id: Any) -> str:
    text = str(external_id or "").strip()
    if channel == "whatsapp":
        return re.sub(r"\D", "", text)          # "+91 98765 43210" -> "919876543210"
    return text


def resolve(channel: str, external_id: Any) -> dict[str, Any] | None:
    """Map a sender on a channel to an enrolled, active user - or None (ACCESS DENIED)."""
    field = CHANNEL_FIELD[channel]
    wanted = normalize(channel, external_id)
    if not wanted:
        return None
    for user in load_users():
        if not user.get("active", True) or not user.get(field):
            continue
        if normalize(channel, user[field]) == wanted:
            return {"handle": user["handle"], "name": user.get("name", user["handle"]),
                    "tier": user["tier"], "channel": channel, "id": wanted}
    return None
