"""Channel-independent conversation engine: takes a message from an enrolled user,
runs the Claude + portal-tools loop, and yields progress events and the final answer."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncIterator
from zoneinfo import ZoneInfo

import anthropic

from . import brain, config
from .mcp_bridge import bridge

log = logging.getLogger("admissionos.engine")
IST = ZoneInfo("Asia/Kolkata")
MAX_HISTORY_MESSAGES = 60

client = anthropic.AsyncAnthropic()
conversations: dict[str, list[dict[str, Any]]] = defaultdict(list)
locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


@dataclass
class Event:
    kind: str                 # command | tool_start | tool_result | notice | error | final
    text: str = ""
    name: str = ""
    ok: bool = True
    seconds: float = 0.0


def help_text(user: dict[str, Any]) -> str:
    lines = [f"**AdmissionOS Prime** - signed in as {user['name']} ({user['tier']} tier).",
             "", "Ask anything about admissions in plain English, or use a command:", ""]
    for name, spec in brain.COMMANDS.items():
        lines.append(f"/{name} - {spec['description']}")
    lines += ["", "/reset - start a fresh conversation", "/help - this list",
              "", "Add a focus after any command, e.g. `/deadline B.Pharm`."]
    return "\n".join(lines)


def _serialize_content(blocks: list[Any]) -> list[dict[str, Any]]:
    return [b.model_dump(mode="json", exclude_none=True) for b in blocks]


def _user_turn(text: str, user: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(IST)
    envelope = (f"\n\n---\n(Context: today is {now:%A, %d %B %Y, %H:%M} IST. User: {user['name']}, "
                f"access tier: {user['tier']}, channel: {user['channel']} - keep answers compact enough to read on a phone.)")
    return {"role": "user", "content": [{"type": "text", "text": text + envelope}]}


async def handle(user: dict[str, Any], key: str, text: str) -> AsyncIterator[Event]:
    """Entry point for every channel. `key` identifies the conversation (e.g. 'telegram:12345')."""
    stripped = text.strip()
    lowered = stripped.lower()
    if lowered in ("/start", "/help", "help"):
        yield Event("final", help_text(user))
        return
    if lowered in ("/reset", "/new"):
        conversations.pop(key, None)
        yield Event("final", "Conversation cleared. What should we look at?")
        return
    if stripped.startswith("/") and stripped[1:].split(" ", 1)[0].lower() not in brain.COMMANDS:
        yield Event("final", f"Unknown command `{stripped.split(' ', 1)[0]}`.\n\n" + help_text(user))
        return
    lock = locks[key]
    if lock.locked():
        yield Event("error", "Still working on your previous request - I will reply as soon as it finishes.")
        return
    async with lock:
        async for event in _run_turn(user, key, stripped):
            yield event


async def _run_turn(user: dict[str, Any], key: str, message: str) -> AsyncIterator[Event]:
    history = conversations[key]
    prompt, command = brain.expand_command(message)
    if command:
        yield Event("command", name=command, text=brain.COMMANDS[command]["title"])
    start_len = len(history)
    history.append(_user_turn(prompt, user))
    if len(history) > MAX_HISTORY_MESSAGES:
        del history[: len(history) - MAX_HISTORY_MESSAGES]
        while history and history[0]["role"] != "user":
            del history[0]
        start_len = 0

    tools = brain.tools_for_tier(bridge.tools, user["tier"])
    allowed = {t["name"] for t in tools}
    request: dict[str, Any] = dict(
        model=config.ANTHROPIC_MODEL,
        max_tokens=config.ANTHROPIC_MAX_TOKENS,
        system=brain.build_system_prompt(),
        thinking={"type": "adaptive"},
        output_config={"effort": config.ANTHROPIC_EFFORT},
    )
    if tools:
        request["tools"] = tools
    if config.ANTHROPIC_FALLBACKS:
        request["betas"] = ["server-side-fallback-2026-07-01"]
        request["extra_body"] = {"fallbacks": "default"}
    messages_api = client.beta.messages if config.ANTHROPIC_FALLBACKS else client.messages

    for _round in range(config.MAX_TOOL_ROUNDS):
        try:
            async with messages_api.stream(messages=history, **request) as stream:
                async for event in stream:
                    if event.type == "content_block_start" and event.content_block.type == "tool_use":
                        yield Event("tool_start", name=event.content_block.name)
                final = await stream.get_final_message()
        except anthropic.RateLimitError:
            del history[start_len:]
            yield Event("error", "Claude rate limit hit - try again in a minute.")
            return
        except anthropic.APIStatusError as exc:
            del history[start_len:]
            log.error("Claude API error %s: %s", exc.status_code, exc.message)
            yield Event("error", f"Claude API error {exc.status_code}. The administrator has the details in the log.")
            return
        except anthropic.APIConnectionError as exc:
            del history[start_len:]
            yield Event("error", f"Could not reach the Claude API: {exc}")
            return

        history.append({"role": "assistant", "content": _serialize_content(final.content)})

        if final.stop_reason == "tool_use":
            calls = [b for b in final.content if b.type == "tool_use"]

            async def execute(block: Any) -> dict[str, Any]:
                t0 = time.monotonic()
                if block.name not in allowed:
                    text, is_error = (f"Access denied: `{block.name}` is not available to the {user['tier']} tier.", True)
                else:
                    text, is_error = await bridge.call(block.name, dict(block.input or {}))
                return {"id": block.id, "name": block.name, "text": text, "is_error": is_error,
                        "seconds": round(time.monotonic() - t0, 1)}

            results = await asyncio.gather(*(execute(b) for b in calls))
            for r in results:
                yield Event("tool_result", name=r["name"], ok=not r["is_error"], seconds=r["seconds"])
            history.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": r["id"], "content": r["text"], "is_error": r["is_error"]}
                for r in results
            ]})
            continue
        if final.stop_reason == "pause_turn":
            continue

        text = "\n\n".join(b.text for b in final.content if b.type == "text").strip()
        if final.stop_reason == "refusal":
            detail = getattr(final, "stop_details", None)
            why = f" ({detail.category})" if detail and getattr(detail, "category", None) else ""
            yield Event("error", f"The model declined this request{why}. Rephrase or narrow it.")
        elif final.stop_reason == "max_tokens":
            yield Event("notice", "The answer hit the length limit - send 'continue' for the rest.")
        if any(b.type == "fallback" for b in final.content):
            yield Event("notice", "Primary model declined; a fallback model answered.")
        yield Event("final", text or "(no answer produced)")
        return
    yield Event("final", f"Stopped after {config.MAX_TOOL_ROUNDS} tool rounds without a conclusion - narrow the question.")
