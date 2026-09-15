"""Telegram channel - long polling against the Bot API, so no public URL is needed.

Only private chats are served: group members would otherwise see each other's data.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx2 as httpx

from . import auth, brain, config, engine, formatting

log = logging.getLogger("admissionos.telegram")


class TelegramBot:
    def __init__(self, token: str) -> None:
        self.api = f"https://api.telegram.org/bot{token}"
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(75.0, connect=15.0))
        self.username = ""

    async def call(self, method: str, **params: Any) -> Any:
        response = await self.http.post(f"{self.api}/{method}", json=params)
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {data.get('description', response.text)}")
        return data["result"]

    async def send(self, chat_id: int, text: str, parse_mode: str | None = "HTML") -> dict[str, Any]:
        try:
            return await self.call("sendMessage", chat_id=chat_id, text=text, parse_mode=parse_mode,
                                   disable_web_page_preview=True)
        except RuntimeError as exc:
            if parse_mode and "parse" in str(exc).lower():   # malformed HTML from an odd model output - fall back to plain
                return await self.call("sendMessage", chat_id=chat_id, text=text, disable_web_page_preview=True)
            raise

    # --- main loop -----------------------------------------------------------
    async def run(self) -> None:
        me = await self.call("getMe")
        self.username = me.get("username", "")
        commands = [{"command": name, "description": spec["title"][:256]} for name, spec in brain.COMMANDS.items()]
        commands += [{"command": "reset", "description": "Start a fresh conversation"},
                     {"command": "reload", "description": "Re-read the exam-dates reference sheet"},
                     {"command": "help", "description": "List commands"}]
        await self.call("setMyCommands", commands=commands)
        log.info("Telegram bot @%s polling", self.username)
        offset: int | None = None
        while True:
            try:
                updates = await self.call("getUpdates", offset=offset, timeout=50, allowed_updates=["message"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("getUpdates failed: %s", exc)
                await asyncio.sleep(3)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                asyncio.create_task(self._safe_handle(update))

    async def _safe_handle(self, update: dict[str, Any]) -> None:
        try:
            await self.handle(update)
        except Exception:  # noqa: BLE001
            log.exception("update %s failed", update.get("update_id"))

    async def handle(self, update: dict[str, Any]) -> None:
        msg = update.get("message") or {}
        text = msg.get("text")
        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        if not text or chat.get("type") != "private":
            return
        chat_id, tg_id = chat["id"], sender.get("id")
        user = auth.resolve("telegram", tg_id)
        if not user:
            await self.send(chat_id, "<b>ACCESS DENIED</b>\n\nThis is a private system.\n"
                                     f"Your Telegram ID is <code>{tg_id}</code> - ask the AdmissionOS administrator to enrol it.")
            log.warning("denied telegram id %s (%s)", tg_id, sender.get("username"))
            return
        if self.username and text.startswith("/"):
            text = text.replace(f"@{self.username}", "", 1)   # "/daily@BotName focus" -> "/daily focus"

        typing = asyncio.create_task(self._keep_typing(chat_id))
        status = await self.send(chat_id, "🔎 Working on it…")
        status_id = status["message_id"]
        progress: list[str] = []
        running: dict[str, int] = {}
        last_edit = 0.0
        final_text = ""
        notices: list[str] = []
        started = time.monotonic()
        tools_used: list[str] = []

        async def refresh(force: bool = False) -> None:
            nonlocal last_edit
            if not force and time.monotonic() - last_edit < 1.5:
                return
            body = "🔎 Working on it…\n" + "\n".join(progress[-12:])
            try:
                await self.call("editMessageText", chat_id=chat_id, message_id=status_id, text=body[:4000])
                last_edit = time.monotonic()
            except RuntimeError:
                pass  # "message is not modified" and friends

        try:
            async for ev in engine.handle(user, f"telegram:{tg_id}", text):
                if ev.kind == "command":
                    progress.append(f"▶ {ev.text}")
                    await refresh(force=True)
                elif ev.kind == "tool_start":
                    progress.append(f"… {ev.name}")
                    running[ev.name] = len(progress) - 1
                    await refresh()
                elif ev.kind == "tool_result":
                    idx = running.pop(ev.name, None)
                    if ev.name not in tools_used:
                        tools_used.append(ev.name)
                    line = f"{'✓' if ev.ok else '✕'} {ev.name} ({ev.seconds}s)"
                    if idx is not None:
                        progress[idx] = line
                    else:
                        progress.append(line)
                    await refresh()
                elif ev.kind in ("notice", "error"):
                    notices.append(ev.text)
                elif ev.kind == "final":
                    final_text = ev.text
        finally:
            typing.cancel()

        try:
            await self.call("deleteMessage", chat_id=chat_id, message_id=status_id)
        except RuntimeError:
            pass
        if notices:
            final_text = (final_text + "\n\n" if final_text else "") + "\n".join(f"⚠️ {n}" for n in notices)
        footer = f"_⏱ {time.monotonic() - started:.0f}s · {engine.model_name()}"
        if tools_used:
            footer += " · tools: " + ", ".join(tools_used[:6])
        final_text = f"{final_text}\n\n{footer}_"
        for chunk in formatting.render(final_text, "telegram"):
            await self.send(chat_id, chunk)

    async def _keep_typing(self, chat_id: int) -> None:
        try:
            while True:
                await self.call("sendChatAction", chat_id=chat_id, action="typing")
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            pass
