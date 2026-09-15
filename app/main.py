"""AdmissionOS Prime - messaging bot entrypoint.

    python -m app.main

Starts the portal (MCP) bridge, the Telegram poller if TELEGRAM_BOT_TOKEN is set, and the
WhatsApp webhook server if the WhatsApp Cloud API variables are set. One process, one loop.
"""
from __future__ import annotations

import asyncio
import logging

import uvicorn

from . import config
from .mcp_bridge import bridge

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("admissionos")


async def main() -> None:
    await bridge.start()
    tasks: list[asyncio.Task] = []

    if config.TELEGRAM_BOT_TOKEN:
        from .telegram_bot import TelegramBot
        tasks.append(asyncio.create_task(TelegramBot(config.TELEGRAM_BOT_TOKEN).run(), name="telegram"))
    else:
        log.info("Telegram disabled (TELEGRAM_BOT_TOKEN not set)")

    if config.WHATSAPP_TOKEN and config.WHATSAPP_PHONE_NUMBER_ID and config.WHATSAPP_VERIFY_TOKEN:
        from .whatsapp_bot import app as whatsapp_app
        server = uvicorn.Server(uvicorn.Config(whatsapp_app, host=config.WEBHOOK_HOST, port=config.WEBHOOK_PORT, log_level="info"))
        tasks.append(asyncio.create_task(server.serve(), name="whatsapp"))
        log.info("WhatsApp webhook on http://%s:%d/webhook", config.WEBHOOK_HOST, config.WEBHOOK_PORT)
    else:
        log.info("WhatsApp disabled (set WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_VERIFY_TOKEN)")

    if not tasks:
        log.error("No channel configured - set TELEGRAM_BOT_TOKEN and/or the WHATSAPP_* variables in .env")
        await bridge.stop()
        return
    try:
        await asyncio.gather(*tasks)
    finally:
        await bridge.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
