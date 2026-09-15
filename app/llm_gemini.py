"""Google Gemini backend (Gemini Developer API, free tier) with function calling.

Auth: GEMINI_API_KEY if set, otherwise an OAuth token from the Sheets service account
(scope generative-language) for the project in GEMINI_PROJECT_ID. Tool schemas from the
portal are sanitised down to the OpenAPI subset Gemini accepts.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx2 as httpx

from . import config

log = logging.getLogger("admissionos.gemini")

API = "https://generativelanguage.googleapis.com/v1beta"
SCOPES = ["https://www.googleapis.com/auth/generative-language"]
_ALLOWED_FORMATS = {"string": {"enum", "date-time"}, "number": {"float", "double"}, "integer": {"int32", "int64"}}
_RETRY_STATUS = {429, 500, 502, 503, 504}


def sanitize_schema(schema: Any) -> dict[str, Any] | None:
    """Reduce a JSON schema to what Gemini's function declarations accept."""
    if not isinstance(schema, dict):
        return None
    out: dict[str, Any] = {}
    stype = schema.get("type")
    nullable = bool(schema.get("nullable"))
    if isinstance(stype, list):
        non_null = [t for t in stype if t != "null"]
        nullable = nullable or len(non_null) < len(stype)
        stype = non_null[0] if non_null else None
    if stype is None and "anyOf" in schema:
        variants = [sanitize_schema(v) for v in schema["anyOf"] if isinstance(v, dict) and v.get("type") != "null"]
        variants = [v for v in variants if v]
        if any(v.get("type") == "null" for v in schema["anyOf"] if isinstance(v, dict)):
            nullable = True
        if len(variants) == 1:
            out = dict(variants[0])
            if nullable:
                out["nullable"] = True
            if schema.get("description") and "description" not in out:
                out["description"] = schema["description"]
            return out
        if variants:
            out["anyOf"] = variants
            if schema.get("description"):
                out["description"] = str(schema["description"])[:800]
            return out
    if stype is None:
        stype = "object" if "properties" in schema else "string"
    if stype == "object":
        props = {k: s for k, v in (schema.get("properties") or {}).items() if (s := sanitize_schema(v))}
        if not props:
            return None  # Gemini rejects OBJECT without properties - callers drop empty parameter blocks
        out["type"] = "object"
        out["properties"] = props
        req = [r for r in schema.get("required", []) if r in props]
        if req:
            out["required"] = req
    elif stype == "array":
        out["type"] = "array"
        items = sanitize_schema(schema.get("items")) or {"type": "string"}
        out["items"] = items
    else:
        out["type"] = stype if stype in ("string", "number", "integer", "boolean") else "string"
        if "enum" in schema and out["type"] == "string":
            out["enum"] = [str(e) for e in schema["enum"]]
        fmt = schema.get("format")
        if fmt in _ALLOWED_FORMATS.get(out["type"], set()):
            out["format"] = fmt
    if schema.get("description"):
        out["description"] = str(schema["description"])[:800]
    if nullable:
        out["nullable"] = True
    return out


def to_function_declarations(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    decls = []
    for t in tools:
        decl: dict[str, Any] = {"name": t["name"], "description": (t.get("description") or t["name"])[:1000]}
        params = sanitize_schema(t.get("input_schema"))
        if params and params.get("type") == "object":
            decl["parameters"] = params
        decls.append(decl)
    return decls


def pick_model(names: list[str]) -> str | None:
    """Prefer the newest plain '-flash' model (free-tier friendly), then any flash, then pro."""
    def version(n: str) -> float:
        m = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
        return float(m.group(1)) if m else 0.0
    bare = [n for n in names if re.fullmatch(r"models/gemini-\d+(?:\.\d+)?-flash", n)]
    if bare:
        return max(bare, key=version)
    flash = [n for n in names if "flash" in n and not any(x in n for x in ("lite", "image", "tts", "live", "audio", "8b", "exp"))]
    if flash:
        return max(flash, key=version)
    pro = [n for n in names if re.fullmatch(r"models/gemini-\d+(?:\.\d+)?-pro", n)]
    return max(pro, key=version) if pro else None


class GeminiClient:
    def __init__(self) -> None:
        self._creds: Any = None
        self.model: str | None = config.GEMINI_MODEL if config.GEMINI_MODEL and config.GEMINI_MODEL != "auto" else None
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0))

    # --- auth -----------------------------------------------------------------
    def _bearer(self) -> str:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
        if self._creds is None:
            self._creds = service_account.Credentials.from_service_account_file(config.GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        if not self._creds.valid:
            self._creds.refresh(Request())
        return self._creds.token

    async def _auth(self) -> tuple[dict[str, str], dict[str, str]]:
        """-> (headers, query params)"""
        if config.GEMINI_API_KEY:
            return {}, {"key": config.GEMINI_API_KEY}
        token = await asyncio.to_thread(self._bearer)
        headers = {"Authorization": f"Bearer {token}"}
        if config.GEMINI_PROJECT_ID:
            headers["x-goog-user-project"] = config.GEMINI_PROJECT_ID
        return headers, {}

    # --- API ---------------------------------------------------------------------
    async def _request(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        headers, params = await self._auth()
        headers.update(kw.pop("headers", {}))
        params.update(kw.pop("params", {}))
        delay = 10.0
        for attempt in range(5):
            r = await self.http.request(method, f"{API}/{path}", headers=headers, params=params, **kw)
            if r.status_code in _RETRY_STATUS and attempt < 4:
                wait = delay
                try:
                    for d in (r.json().get("error") or {}).get("details", []):
                        if "retryDelay" in d:
                            wait = max(wait, float(str(d["retryDelay"]).rstrip("s")) + 1)
                except Exception:  # noqa: BLE001
                    pass
                log.warning("Gemini %s -> retrying in %.0fs (attempt %d)", r.status_code, wait, attempt + 1)
                await asyncio.sleep(wait)
                delay = min(delay * 2, 60)
                continue
            if r.status_code >= 400:
                try:
                    msg = (r.json().get("error") or {}).get("message", r.text)
                except Exception:  # noqa: BLE001
                    msg = r.text
                raise GeminiError(r.status_code, str(msg)[:500])
            return r.json()
        raise GeminiError(429, "rate limited")

    async def ensure_model(self) -> str:
        if self.model:
            return self.model
        data = await self._request("GET", "models", params={"pageSize": 200})
        names = [m["name"] for m in data.get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])]
        choice = pick_model(names)
        if not choice:
            raise GeminiError(404, "no Gemini model with generateContent is available to this project")
        self.model = choice.replace("models/", "")
        log.info("Gemini model: %s (from %d available)", self.model, len(names))
        return self.model

    async def generate(self, system: str, contents: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        model = await self.ensure_model()
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": config.ANTHROPIC_MAX_TOKENS},
        }
        if tools:
            body["tools"] = [{"functionDeclarations": to_function_declarations(tools)}]
        return await self._request("POST", f"models/{model}:generateContent", json=body)


class GeminiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


def extract(response: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, str]:
    """-> (model parts to echo back, function calls, text, finish reason)"""
    cands = response.get("candidates") or []
    if not cands:
        block = (response.get("promptFeedback") or {}).get("blockReason", "no candidates")
        raise GeminiError(200, f"response blocked: {block}")
    cand = cands[0]
    parts = (cand.get("content") or {}).get("parts") or []
    calls = [p["functionCall"] for p in parts if "functionCall" in p]
    text = "".join(p.get("text", "") for p in parts if "text" in p and not p.get("thought"))
    return parts, calls, text, cand.get("finishReason", "")
