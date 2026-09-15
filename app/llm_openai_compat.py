"""OpenAI-compatible chat-completions backend with tool calling.

Used for Mistral's free "Experiment" plan (https://api.mistral.ai/v1) and for any other
OpenAI-compatible endpoint (Groq, Cerebras, OpenRouter, a local server) via OPENAI_COMPAT_*.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx2 as httpx

from . import config
from .llm_gemini import sanitize_schema

log = logging.getLogger("admissionos.openai_compat")
_RETRY_STATUS = {429, 500, 502, 503, 504}


class CompatError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


def to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for t in tools:
        params = sanitize_schema(t.get("input_schema")) or {"type": "object", "properties": {}}
        if params.get("type") != "object":
            params = {"type": "object", "properties": {}}
        out.append({"type": "function", "function": {"name": t["name"], "description": (t.get("description") or t["name"])[:1000],
                                                     "parameters": params}})
    return out


class OpenAICompatClient:
    def __init__(self, base_url: str, api_key: str, model: str, name: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.name = name
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(20.0, read=150.0))   # backup brains must fail fast

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], max_tokens: int) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": 0.3, "max_tokens": max_tokens}
        if tools:
            body["tools"] = to_openai_tools(tools)
            body["tool_choice"] = "auto"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        delay = 3.0
        for attempt in range(4):
            r = await self.http.post(f"{self.base_url}/chat/completions", headers=headers, json=body)
            if r.status_code in _RETRY_STATUS and attempt < 3:
                wait = delay
                ra = r.headers.get("retry-after")
                if ra and re.fullmatch(r"\d+(\.\d+)?", ra):
                    wait = max(wait, float(ra) + 0.5)
                log.warning("%s %s -> retrying in %.0fs", self.name, r.status_code, wait)
                await asyncio.sleep(wait)
                delay = min(delay * 2, 30)
                continue
            if r.status_code >= 400:
                try:
                    msg = r.json().get("message") or (r.json().get("error") or {}).get("message") or r.text
                except Exception:  # noqa: BLE001
                    msg = r.text
                raise CompatError(r.status_code, str(msg)[:500])
            return r.json()
        raise CompatError(429, "rate limited")


def extract(response: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], str, str]:
    """-> (assistant message to echo back, tool calls [{id, name, args}], text, finish reason)"""
    choice = (response.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    calls = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        raw = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except json.JSONDecodeError:
            args = {}
        calls.append({"id": tc.get("id") or fn.get("name"), "name": fn.get("name", ""), "args": args})
    content = msg.get("content")
    if isinstance(content, list):                      # some providers return content parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    echo = {"role": "assistant", "content": content or ""}
    if msg.get("tool_calls"):
        echo["tool_calls"] = msg["tool_calls"]
    return echo, calls, (content or ""), choice.get("finish_reason", "")


def make_client(provider: str) -> OpenAICompatClient | None:
    if provider == "mistral" and config.MISTRAL_API_KEY:
        return OpenAICompatClient("https://api.mistral.ai/v1", config.MISTRAL_API_KEY, config.MISTRAL_MODEL, "mistral")
    if provider == "openai_compat" and config.OPENAI_COMPAT_BASE_URL and config.OPENAI_COMPAT_API_KEY and config.OPENAI_COMPAT_MODEL:
        return OpenAICompatClient(config.OPENAI_COMPAT_BASE_URL, config.OPENAI_COMPAT_API_KEY, config.OPENAI_COMPAT_MODEL, "openai_compat")
    return None
