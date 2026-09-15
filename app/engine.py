"""Channel-independent conversation engine: takes a message from an enrolled user,
runs the model + tools loop, and yields progress events and the final answer.

Two interchangeable brains (LLM_PROVIDER): Claude (Anthropic API or Vertex AI) and Gemini
(Google's free-tier Developer API). Tools = portal tools for the user's tier + local web
tools (search / fetch) for everyone. Conversation history is kept per user in the format
of whichever brain is active.
"""
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

from . import brain, config, webtools
from .mcp_bridge import bridge

log = logging.getLogger("admissionos.engine")
IST = ZoneInfo("Asia/Kolkata")
MAX_HISTORY_MESSAGES = 60

PROVIDER = config.LLM_PROVIDER
conversations: dict[str, list[dict[str, Any]]] = defaultdict(list)
locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
active_tasks: dict[str, asyncio.Task | None] = {}     # key -> the task currently answering (for /cancel)


def _make_claude_client() -> Any:
    if PROVIDER == "vertex":
        import os
        from anthropic import AsyncAnthropicVertex
        os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", config.GOOGLE_SERVICE_ACCOUNT_FILE)
        if not config.VERTEX_PROJECT_ID:
            raise RuntimeError("LLM_PROVIDER=vertex needs VERTEX_PROJECT_ID in .env")
        log.info("Brain: Claude %s via Vertex AI (project %s, %s)", config.ANTHROPIC_MODEL, config.VERTEX_PROJECT_ID, config.VERTEX_REGION)
        return AsyncAnthropicVertex(project_id=config.VERTEX_PROJECT_ID, region=config.VERTEX_REGION)
    log.info("Brain: Claude %s via Anthropic API", config.ANTHROPIC_MODEL)
    return anthropic.AsyncAnthropic()


from . import llm_openai_compat

gemini = None
claude = None
compat = None
if PROVIDER == "gemini":
    from . import llm_gemini
    gemini = llm_gemini.GeminiClient()
    log.info("Brain: Gemini via Google Developer API (%s)", "API key" if config.GEMINI_API_KEY else "service account")
elif PROVIDER in ("mistral", "openai_compat"):
    compat = llm_openai_compat.make_client(PROVIDER)
    if compat is None:
        raise RuntimeError(f"LLM_PROVIDER={PROVIDER} but its API key / model is not set in .env")
    log.info("Brain: %s (%s) via OpenAI-compatible API", PROVIDER, compat.model)
else:
    claude = _make_claude_client()
USE_FALLBACKS = config.ANTHROPIC_FALLBACKS and PROVIDER == "anthropic"   # server-side fallbacks are Claude API only

# Secondary brains, tried in order when the primary's quota is exhausted or it is overloaded.
FALLBACKS = [p for p in config.LLM_FALLBACK_PROVIDERS if p != PROVIDER]
FALLBACK = FALLBACKS[0] if FALLBACKS else None
_fallback_clients: dict[str, Any] = {}


def _fallback_claude(provider: str = "anthropic") -> Any:
    if provider not in _fallback_clients:
        import os
        if provider == "vertex":
            from anthropic import AsyncAnthropicVertex
            os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", config.GOOGLE_SERVICE_ACCOUNT_FILE)
            _fallback_clients[provider] = AsyncAnthropicVertex(project_id=config.VERTEX_PROJECT_ID, region=config.VERTEX_REGION)
        else:
            _fallback_clients[provider] = anthropic.AsyncAnthropic()
    return _fallback_clients[provider]


def _fallback_available(provider: str) -> bool:
    if provider in ("mistral", "openai_compat"):
        return llm_openai_compat.make_client(provider) is not None
    if provider == "anthropic":
        import os
        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    if provider == "vertex":
        return bool(config.VERTEX_PROJECT_ID)
    return False


async def _run_fallbacks(user: dict[str, Any], key: str, message: str, transcript: str, reason: str) -> AsyncIterator[Event]:
    """Try each configured secondary brain for this one question; the first that answers wins."""
    for provider in FALLBACKS:
        if not _fallback_available(provider):
            continue
        label = {"openai_compat": "NVIDIA backup (slower, 3-5 min)", "mistral": "Mistral backup", "anthropic": "Claude backup", "vertex": "Claude backup"}.get(provider, provider)
        yield Event("status", text=f"{reason} - switching to {label}")
        events: list[Event] = []
        if provider in ("mistral", "openai_compat"):
            runner = _run_turn_openai(user, key, message, llm_openai_compat.make_client(provider), transcript=transcript)
        else:
            runner = _run_turn_claude(user, key, message, transcript=transcript, client=_fallback_claude(provider))
        async for ev in runner:
            events.append(ev)
        if any(ev.kind == "final" for ev in events):
            yield Event("notice", f"{reason} - this answer came from {provider}.")
            for ev in events:
                yield ev
            return
        log.warning("fallback %s did not answer: %s", provider, [e.text for e in events if e.kind == "error"][:1])
    yield Event("error", f"{reason}, and no backup brain could answer (Mistral not configured or Claude has no credits). "
                         "Please resend in about a minute.")


def _gemini_history_as_text(history: list[dict[str, Any]], limit: int = 12000) -> str:
    """Flatten a Gemini-format conversation into a transcript so the fallback brain has the context."""
    lines = []
    for turn in history:
        role = "User" if turn.get("role") == "user" else "AdmissionOS"
        for part in turn.get("parts", []):
            if "text" in part:
                lines.append(f"{role}: {part['text']}")
            elif "functionCall" in part:
                lines.append(f"{role} called tool {part['functionCall'].get('name')}")
            elif "functionResponse" in part:
                res = str((part['functionResponse'].get('response') or {}).get('result', ''))[:1500]
                lines.append(f"Tool {part['functionResponse'].get('name')} returned: {res}")
    text = "\n".join(lines)
    return text[-limit:]


def model_name() -> str:
    if gemini:
        return gemini.model or "gemini"
    if compat:
        return compat.model
    return config.ANTHROPIC_MODEL


@dataclass
class Event:
    kind: str                 # command | status | tool_start | tool_result | notice | error | final
    text: str = ""
    name: str = ""
    ok: bool = True
    seconds: float = 0.0


def help_text(user: dict[str, Any]) -> str:
    lines = [f"**AdmissionOS Prime** - signed in as {user['name']} ({user['tier']} tier).",
             "", "Ask anything about admissions in plain English, or use a command:", ""]
    for name, spec in brain.COMMANDS.items():
        lines.append(f"/{name} - {spec['description']}")
    lines += ["", "/reset - start a fresh conversation", "/reload - re-read the exam-dates reference sheet", "/help - this list",
              "", "Add a focus after any command, e.g. `/deadline B.Pharm`."]
    return "\n".join(lines)


def _envelope(text: str, user: dict[str, Any]) -> str:
    now = datetime.now(IST)
    return (f"{text}\n\n---\n(Context: today is {now:%A, %d %B %Y, %H:%M} IST. User: {user['name']}, "
            f"access tier: {user['tier']}, channel: {user['channel']} - keep answers compact enough to read on a phone.)")


def _trim(history: list[dict[str, Any]]) -> None:
    if len(history) > MAX_HISTORY_MESSAGES:
        del history[: len(history) - MAX_HISTORY_MESSAGES]
        while history and history[0]["role"] != "user":
            del history[0]


def _tools_for(user: dict[str, Any]) -> tuple[list[dict[str, Any]], set[str]]:
    tools = brain.tools_for_tier(bridge.tools, user["tier"]) + webtools.LOCAL_TOOLS
    return tools, {t["name"] for t in tools}


_turn_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))   # key -> tool -> calls this turn
_BUDGETS = {"web_search": lambda: config.WEB_SEARCH_BUDGET, "fetch_url": lambda: config.FETCH_BUDGET}


async def _execute_tool(name: str, args: dict[str, Any], allowed: set[str], user: dict[str, Any], key: str = "") -> dict[str, Any]:
    t0 = time.monotonic()
    if name not in allowed:
        text, is_error = (f"Access denied: `{name}` is not available to the {user['tier']} tier.", True)
    elif name in _BUDGETS and _turn_counts[key][name] >= _BUDGETS[name]():
        text, is_error = (f"{name} budget for this answer is used up ({_BUDGETS[name]()}). Answer now with the evidence you already have "
                          "and mark anything unconfirmed as [estimate].", True)
    else:
        _turn_counts[key][name] += 1
        local = await webtools.call_local(name, dict(args or {}))
        text, is_error = local if local is not None else await bridge.call(name, dict(args or {}))
    return {"name": name, "text": text, "is_error": is_error, "seconds": round(time.monotonic() - t0, 1)}


# --- entry point ----------------------------------------------------------------------
async def handle(user: dict[str, Any], key: str, text: str) -> AsyncIterator[Event]:
    """Entry point for every channel. `key` identifies the conversation (e.g. 'telegram:12345')."""
    stripped = text.strip()
    lowered = stripped.lower()
    if lowered in ("/start", "/help", "help"):
        yield Event("final", help_text(user))
        return
    if lowered in ("/cancel", "cancel", "stop"):
        task = active_tasks.get(key)
        if task and not task.done():
            task.cancel()
            yield Event("final", "Cancelled the previous request. Ask again whenever you are ready.")
        else:
            yield Event("final", "Nothing is running right now.")
        return
    if lowered in ("/reset", "/new"):
        conversations.pop(key, None)
        yield Event("final", "Conversation cleared. What should we look at?")
        return
    if lowered == "/reload":
        from .sheets import reference
        ok = await reference.refresh()
        if ok:
            counts = ", ".join(f"{tab}: {len(rows)} rows" for tab, rows in reference.tabs.items())
            yield Event("final", f"Reference sheet reloaded ({counts}). Institutional memory files are re-read on every message.")
        else:
            yield Event("final", f"Reference sheet not loaded - {reference.error}")
        return
    if stripped.startswith("/") and stripped[1:].split(" ", 1)[0].lower() not in brain.COMMANDS:
        yield Event("final", f"Unknown command `{stripped.split(' ', 1)[0]}`.\n\n" + help_text(user))
        return
    lock = locks[key]
    if lock.locked():
        yield Event("error", "Still working on your previous request - I will reply as soon as it finishes, or send /cancel to stop it.")
        return
    active_tasks[key] = asyncio.current_task()
    _turn_counts.pop(key, None)          # fresh tool budgets for this answer
    async with lock:
        if PROVIDER == "gemini":
            runner = _run_turn_gemini(user, key, stripped)
        elif compat is not None:
            runner = _run_turn_openai(user, key, stripped, compat)
        else:
            runner = _run_turn_claude(user, key, stripped)
        async for event in runner:
            yield event


# --- Claude ----------------------------------------------------------------------------
def _serialize_content(blocks: list[Any]) -> list[dict[str, Any]]:
    return [b.model_dump(mode="json", exclude_none=True) for b in blocks]


async def _run_turn_claude(user: dict[str, Any], key: str, message: str,
                           transcript: str | None = None, client: Any = None) -> AsyncIterator[Event]:
    """Primary Claude path, or (with `transcript` + `client`) a one-off fallback turn that does not touch stored history."""
    fallback_mode = transcript is not None
    history = [] if fallback_mode else conversations[key]
    prompt, command = brain.expand_command(message)
    if command and not fallback_mode:
        yield Event("command", name=command, text=brain.COMMANDS[command]["title"])
    if fallback_mode and transcript:
        prompt = f"Earlier conversation (for context):\n{transcript}\n\n---\nCurrent request:\n{prompt}"
    start_len = len(history)
    history.append({"role": "user", "content": [{"type": "text", "text": _envelope(prompt, user)}]})
    _trim(history)
    start_len = min(start_len, len(history) - 1)

    api_client = client or claude
    tools, allowed = _tools_for(user)
    request: dict[str, Any] = dict(
        model=config.ANTHROPIC_MODEL,
        max_tokens=config.ANTHROPIC_MAX_TOKENS,
        system=brain.build_system_prompt(),
        thinking={"type": "adaptive"},
        output_config={"effort": config.ANTHROPIC_EFFORT if command else "medium"},
        tools=tools,
    )
    use_server_fallbacks = USE_FALLBACKS or (fallback_mode and FALLBACK == "anthropic" and config.ANTHROPIC_FALLBACKS)
    if use_server_fallbacks:
        request["betas"] = ["server-side-fallback-2026-07-01"]
        request["extra_body"] = {"fallbacks": "default"}
    messages_api = api_client.beta.messages if use_server_fallbacks else api_client.messages

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
            msg = (exc.message or "").lower()
            if "credit balance" in msg or "billing" in msg:
                text = "The Anthropic account behind this bot has run out of credits. The administrator needs to top up at console.anthropic.com -> Plans & Billing; then just resend your message."
            elif exc.status_code == 401:
                text = "The bot's Anthropic API key was rejected. The administrator needs to check ANTHROPIC_API_KEY in .env."
            elif exc.status_code == 529 or exc.status_code >= 500:
                text = "Claude is temporarily overloaded. Please try again in a minute."
            else:
                text = f"Claude API error {exc.status_code}. The administrator has the details in the log."
            yield Event("error", text)
            return
        except anthropic.APIConnectionError as exc:
            del history[start_len:]
            yield Event("error", f"Could not reach the Claude API: {exc}")
            return

        history.append({"role": "assistant", "content": _serialize_content(final.content)})

        if final.stop_reason == "tool_use":
            calls = [b for b in final.content if b.type == "tool_use"]
            for b in calls:
                yield Event("tool_start", name=b.name)
            results = await asyncio.gather(*(_execute_tool(b.name, b.input or {}, allowed, user, key) for b in calls))
            for b, r in zip(calls, results):
                r["id"] = b.id
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


# --- OpenAI-compatible (Mistral etc.) -------------------------------------------------------
def _openai_history_as_text(history: list[dict[str, Any]], limit: int = 12000) -> str:
    lines = []
    for m in history:
        role = m.get("role")
        if role == "user":
            lines.append(f"User: {m.get('content', '')}")
        elif role == "assistant":
            if m.get("content"):
                lines.append(f"AdmissionOS: {m['content']}")
            for tc in m.get("tool_calls") or []:
                lines.append(f"AdmissionOS called tool {(tc.get('function') or {}).get('name')}")
        elif role == "tool":
            lines.append(f"Tool returned: {str(m.get('content', ''))[:1500]}")
    return "\n".join(lines)[-limit:]


async def _run_turn_openai(user: dict[str, Any], key: str, message: str, client: Any,
                           transcript: str | None = None) -> AsyncIterator[Event]:
    fallback_mode = transcript is not None
    history = [] if fallback_mode else conversations[key]
    prompt, command = brain.expand_command(message)
    if command and not fallback_mode:
        yield Event("command", name=command, text=brain.COMMANDS[command]["title"])
    if fallback_mode and transcript:
        prompt = f"Earlier conversation (for context):\n{transcript}\n\n---\nCurrent request:\n{prompt}"
    start_len = len(history)
    history.append({"role": "user", "content": _envelope(prompt, user)})
    _trim(history)
    start_len = min(start_len, len(history) - 1)

    tools, allowed = _tools_for(user)
    system = "\n\n".join(block["text"] for block in brain.build_system_prompt())
    messages_prefix = [{"role": "system", "content": system}]

    for _round in range(config.MAX_TOOL_ROUNDS):
        try:
            response = await client.chat(messages_prefix + history, tools, config.ANTHROPIC_MAX_TOKENS)
            echo, calls, text, finish = llm_openai_compat.extract(response)
        except llm_openai_compat.CompatError as exc:
            del history[start_len:]
            log.error("%s error %s: %s", client.name, exc.status, exc.message)
            if exc.status in (429, 503, 500, 502, 504) and not fallback_mode and FALLBACKS:
                reason = f"{client.name} is rate-limited" if exc.status == 429 else f"{client.name} is overloaded"
                async for ev in _run_fallbacks(user, key, message, _openai_history_as_text(history), reason):
                    yield ev
                return
            if exc.status == 401:
                text = f"The {client.name} API key was rejected. The administrator needs to check it in .env."
            elif exc.status == 429:
                text = f"{client.name} is rate-limited right now - wait a minute and resend."
            else:
                text = f"{client.name} API error {exc.status}. The administrator has the details in the log."
            yield Event("error", text)
            return
        except Exception as exc:  # noqa: BLE001
            del history[start_len:]
            log.exception("%s request failed", client.name)
            yield Event("error", f"Could not reach {client.name}: {type(exc).__name__}: {exc}")
            return

        history.append(echo)
        if calls:
            for c in calls:
                yield Event("tool_start", name=c["name"])
            results = await asyncio.gather(*(_execute_tool(c["name"], c["args"], allowed, user, key) for c in calls))
            for c, r in zip(calls, results):
                yield Event("tool_result", name=r["name"], ok=not r["is_error"], seconds=r["seconds"])
                history.append({"role": "tool", "tool_call_id": c["id"], "name": c["name"], "content": r["text"]})
            continue
        if finish == "length":
            yield Event("notice", "The answer hit the length limit - send 'continue' for the rest.")
        yield Event("final", text.strip() or "(no answer produced)")
        return
    yield Event("final", f"Stopped after {config.MAX_TOOL_ROUNDS} tool rounds without a conclusion - narrow the question.")


# --- Gemini ------------------------------------------------------------------------------
async def _run_turn_gemini(user: dict[str, Any], key: str, message: str) -> AsyncIterator[Event]:
    from . import llm_gemini
    history = conversations[key]
    prompt, command = brain.expand_command(message)
    if command:
        yield Event("command", name=command, text=brain.COMMANDS[command]["title"])
    start_len = len(history)
    history.append({"role": "user", "parts": [{"text": _envelope(prompt, user)}]})
    _trim(history)
    start_len = min(start_len, len(history) - 1)

    tools, allowed = _tools_for(user)
    system = "\n\n".join(block["text"] for block in brain.build_system_prompt())
    # Deep reasoning only where it pays: daily report and forecast; briefs medium; lookups fast.
    thinking = "high" if command in ("daily", "forecast") else ("medium" if command else "low")

    for _round in range(config.MAX_TOOL_ROUNDS):
        try:
            response = await gemini.generate(system, history, tools, thinking=thinking)
            parts, calls, text, finish = llm_gemini.extract(response)
        except llm_gemini.GeminiError as exc:
            del history[start_len:]
            log.error("Gemini error %s: %s", exc.status, exc.message)
            if exc.status in (429, 503, 500, 502, 504) and FALLBACKS:
                # Quota exhausted or Google overloaded: answer this question on a secondary brain instead.
                reason = "Gemini's free quota is exhausted for the moment" if exc.status == 429 else "Gemini is overloaded"
                async for ev in _run_fallbacks(user, key, message, _gemini_history_as_text(history), reason):
                    yield ev
                return
            if exc.status == 429:
                text = "The free Gemini quota is busy right now - wait a minute and resend."
            elif exc.status in (503, 500, 502, 504):
                text = "Google's Gemini service is overloaded at the moment (all fallback models busy). Please resend in a minute."
            elif exc.status == 403:
                text = "Gemini API access is not enabled for the bot's Google project. The administrator needs to enable it in Google Cloud Console."
            elif exc.status == 200:
                text = f"The model declined this request ({exc.message}). Rephrase or narrow it."
            else:
                text = f"Gemini API error {exc.status}. The administrator has the details in the log."
            yield Event("error", text)
            return
        except Exception as exc:  # noqa: BLE001
            del history[start_len:]
            log.exception("Gemini request failed")
            yield Event("error", f"Could not reach Gemini: {type(exc).__name__}: {exc}")
            return

        history.append({"role": "model", "parts": parts})

        if calls:
            for c in calls:
                yield Event("tool_start", name=c.get("name", "?"))
            results = await asyncio.gather(*(_execute_tool(c.get("name", ""), c.get("args") or {}, allowed, user, key) for c in calls))
            responses = []
            for c, r in zip(calls, results):
                yield Event("tool_result", name=r["name"], ok=not r["is_error"], seconds=r["seconds"])
                fr: dict[str, Any] = {"name": r["name"], "response": {"result": r["text"], "is_error": r["is_error"]}}
                if c.get("id"):
                    fr["id"] = c["id"]          # newer Gemini models pair responses to calls by id
                responses.append({"functionResponse": fr})
            history.append({"role": "user", "parts": responses})
            continue

        if finish == "MAX_TOKENS":
            yield Event("notice", "The answer hit the length limit - send 'continue' for the rest.")
        elif finish in ("SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST"):
            yield Event("error", f"The model declined this request ({finish}). Rephrase or narrow it.")
        yield Event("final", text.strip() or "(no answer produced)")
        return
    yield Event("final", f"Stopped after {config.MAX_TOOL_ROUNDS} tool rounds without a conclusion - narrow the question.")
