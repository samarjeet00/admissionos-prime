"""Telegram channel - long polling against the Bot API, so no public URL is needed.

Only private chats are served: group members would otherwise see each other's data.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx2 as httpx

from . import access, auth, brain, config, engine, formatting

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

    async def send(self, chat_id: int, text: str, parse_mode: str | None = "HTML",
                   keyboard: list[list[dict[str, str]]] | None = None) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if keyboard:
            extra["reply_markup"] = {"inline_keyboard": keyboard}
        try:
            return await self.call("sendMessage", chat_id=chat_id, text=text, parse_mode=parse_mode,
                                   disable_web_page_preview=True, **extra)
        except RuntimeError as exc:
            if parse_mode and "parse" in str(exc).lower():   # malformed HTML from an odd model output - fall back to plain
                return await self.call("sendMessage", chat_id=chat_id, text=text, disable_web_page_preview=True, **extra)
            raise

    async def _notify_admin(self, chat_id: int, text: str, keyboard: list[list[dict[str, str]]]) -> None:
        await self.send(chat_id, text, keyboard=keyboard)

    async def _reply_external(self, external_id: str, text: str) -> None:
        await self.send(int(external_id), text, parse_mode=None)

    # --- main loop -----------------------------------------------------------
    async def run(self) -> None:
        me = await self.call("getMe")
        self.username = me.get("username", "")
        commands = [{"command": name, "description": spec["title"][:256]} for name, spec in brain.COMMANDS.items()]
        commands += [{"command": "cancel", "description": "Stop the request that is running"},
                     {"command": "reset", "description": "Start a fresh conversation"},
                     {"command": "reload", "description": "Re-read the exam-dates reference sheet"},
                     {"command": "help", "description": "List commands"}]
        await self.call("setMyCommands", commands=commands)
        access.register_notifier(self._notify_admin)
        access.register_replier("telegram", self._reply_external)
        log.info("Telegram bot @%s polling (admins: %s)", self.username, ", ".join(a["handle"] for a in access.admins()) or "none")
        offset: int | None = None
        while True:
            try:
                updates = await self.call("getUpdates", offset=offset, timeout=50, allowed_updates=["message", "callback_query"])
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
        if "callback_query" in update:
            await self._handle_callback(update["callback_query"])
            return
        msg = update.get("message") or {}
        text = msg.get("text")
        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        if not text or chat.get("type") != "private":
            return
        chat_id, tg_id = chat["id"], sender.get("id")
        user = auth.resolve("telegram", tg_id)
        if not user:
            display = " ".join(p for p in (sender.get("first_name"), sender.get("last_name")) if p) or "Unknown"
            if sender.get("username"):
                display += f" (@{sender['username']})"
            status = await access.request("telegram", tg_id, display, text)
            if status == "new":
                reply = ("🔐 This is a private system. Your access request has been sent to the AdmissionOS administrator - "
                         "you will get a message here as soon as it is approved.")
            elif status == "pending":
                reply = "⏳ Your access request is still waiting for the administrator. You will be notified here."
            else:
                reply = f"ACCESS DENIED. This is a private system and no administrator is reachable. Your Telegram ID is {tg_id}."
            await self.send(chat_id, reply, parse_mode=None)
            log.warning("access request from telegram id %s (%s): %s", tg_id, sender.get("username"), status)
            return
        if self.username and text.startswith("/"):
            text = text.replace(f"@{self.username}", "", 1)   # "/daily@BotName focus" -> "/daily focus"
        if text.strip().lower() in ("/cancel", "cancel", "stop"):
            # Cancel must bypass the busy check: run the engine's cancel path directly and reply.
            reply = ""
            async for ev in engine.handle(user, f"telegram:{tg_id}", "/cancel"):
                if ev.kind == "final":
                    reply = ev.text
            await self.send(chat_id, reply or "OK.", parse_mode=None)
            return

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
                elif ev.kind == "status":
                    progress.append(f"⚠️ {ev.text}")
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
        except asyncio.CancelledError:
            typing.cancel()
            try:
                await self.call("editMessageText", chat_id=chat_id, message_id=status_id, text="⛔ Cancelled.")
            except RuntimeError:
                pass
            return
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

    async def _handle_callback(self, cq: dict[str, Any]) -> None:
        """An administrator pressed Viewer / Operational / Executive / Deny on an access request."""
        cq_id = cq.get("id")
        data = cq.get("data") or ""
        presser = cq.get("from") or {}
        msg = cq.get("message") or {}
        try:
            if not data.startswith("acc|"):
                await self.call("answerCallbackQuery", callback_query_id=cq_id)
                return
            if not access.is_admin(presser.get("id")):
                await self.call("answerCallbackQuery", callback_query_id=cq_id, text="Only an administrator can decide this.", show_alert=True)
                return
            _, key, decision = data.split("|", 2)
            by = " ".join(p for p in (presser.get("first_name"), presser.get("last_name")) if p) or "administrator"
            outcome = await access.decide(key, decision, by)
            await self.call("answerCallbackQuery", callback_query_id=cq_id, text=outcome[:180])
            if msg.get("message_id"):
                original = msg.get("text") or ""
                await self.call("editMessageText", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
                                text=f"{original}\n\n{outcome}")
        except Exception:  # noqa: BLE001
            log.exception("callback handling failed")
            try:
                await self.call("answerCallbackQuery", callback_query_id=cq_id, text="Something went wrong - see the log.")
            except RuntimeError:
                pass

    async def _keep_typing(self, chat_id: int) -> None:
        try:
            while True:
                await self.call("sendChatAction", chat_id=chat_id, action="typing")
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            pass
