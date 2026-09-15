"""WhatsApp channel - Meta WhatsApp Cloud API webhook.

Meta POSTs incoming messages to /webhook (this needs a public HTTPS URL - a reverse proxy,
or a tunnel such as cloudflared / ngrok while testing). Replies go out through the Graph API.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from collections import OrderedDict
from typing import Any

import httpx2 as httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from . import access, auth, config, engine, formatting

log = logging.getLogger("admissionos.whatsapp")

app = FastAPI(title="AdmissionOS Prime - WhatsApp webhook", docs_url=None, redoc_url=None)
http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
_seen: "OrderedDict[str, None]" = OrderedDict()      # Meta retries deliveries; do not answer twice


def _graph_url() -> str:
    return f"https://graph.facebook.com/{config.WHATSAPP_API_VERSION}/{config.WHATSAPP_PHONE_NUMBER_ID}/messages"


async def _post(payload: dict[str, Any]) -> None:
    response = await http.post(_graph_url(), json=payload, headers={"Authorization": f"Bearer {config.WHATSAPP_TOKEN}"})
    if response.status_code >= 400:
        log.error("WhatsApp send failed %s: %s", response.status_code, response.text[:500])


async def send_text(to: str, text: str) -> None:
    await _post({"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text, "preview_url": False}})


async def mark_read(message_id: str, typing: bool = True) -> None:
    payload: dict[str, Any] = {"messaging_product": "whatsapp", "status": "read", "message_id": message_id}
    if typing:
        payload["typing_indicator"] = {"type": "text"}
    await _post(payload)


@app.get("/webhook")
async def verify(request: Request) -> PlainTextResponse:
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == config.WHATSAPP_VERIFY_TOKEN:
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(status_code=403, detail="verification failed")


@app.post("/webhook")
async def receive(request: Request) -> dict[str, str]:
    raw = await request.body()
    if config.WHATSAPP_APP_SECRET:
        signature = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(config.WHATSAPP_APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise HTTPException(status_code=403, detail="bad signature")
    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="bad json")
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for message in (change.get("value") or {}).get("messages", []) or []:
                if message.get("type") != "text":
                    continue
                message_id = message.get("id", "")
                if message_id in _seen:
                    continue
                _seen[message_id] = None
                while len(_seen) > 2000:
                    _seen.popitem(last=False)
                asyncio.create_task(_safe_process(message["from"], message_id, message["text"]["body"]))
    return {"status": "ok"}          # answer fast; Meta retries anything slower than a few seconds


async def _safe_process(sender: str, message_id: str, text: str) -> None:
    try:
        await process(sender, message_id, text)
    except Exception:  # noqa: BLE001
        log.exception("message %s failed", message_id)
        await send_text(sender, "Something went wrong on my side. Please try again in a minute.")


async def process(sender: str, message_id: str, text: str) -> None:
    user = auth.resolve("whatsapp", sender)
    if not user:
        access.register_replier("whatsapp", send_text)
        status = await access.request("whatsapp", sender, f"+{sender}", text)
        if status == "new":
            reply = ("🔐 This is a private system. Your access request has been sent to the AdmissionOS administrator - "
                     "you will get a message here as soon as it is approved.")
        elif status == "pending":
            reply = "⏳ Your access request is still waiting for the administrator. You will be notified here."
        else:
            reply = f"ACCESS DENIED. This is a private system and no administrator is reachable (+{sender})."
        await send_text(sender, reply)
        log.warning("access request from whatsapp number %s: %s", sender, status)
        return
    await mark_read(message_id, typing=True)
    quick = text.strip().lower() in ("/start", "/help", "help", "/reset", "/new", "hi", "hello")
    if not quick:
        await send_text(sender, "🔎 Working on it - pulling live portal data. This can take a minute or two.")

    final_text, notices, calls = "", [], 0
    async for ev in engine.handle(user, f"whatsapp:{sender}", text):
        if ev.kind == "tool_result":
            calls += 1
        elif ev.kind in ("notice", "error"):
            notices.append(ev.text)
        elif ev.kind == "final":
            final_text = ev.text
    if notices:
        final_text = (final_text + "\n\n" if final_text else "") + "\n".join(f"⚠️ {n}" for n in notices)
    for chunk in formatting.render(final_text, "whatsapp"):
        await send_text(sender, chunk)
