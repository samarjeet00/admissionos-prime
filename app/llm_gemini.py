"""Google Gemini backend (Gemini Developer API, free tier) with function calling.

Auth: GEMINI_API_KEY if set, otherwise an OAuth token from the Sheets service account.
Speed: a startup health-probe picks the first responsive model in the ranked list, the
choice is sticky, overloaded (503/429) models are skipped instantly, and dead ones (404)
are dropped for good. Tool schemas from the portal are sanitised down to the OpenAPI
subset Gemini accepts.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import httpx2 as httpx

from . import config

log = logging.getLogger("admissionos.gemini")

API = "https://generativelanguage.googleapis.com/v1beta"
SCOPES = ["https://www.googleapis.com/auth/generative-language"]
_ALLOWED_FORMATS = {"string": {"enum", "date-time"}, "number": {"float", "double"}, "integer": {"int32", "int64"}}
_RETRY_STATUS = {429, 500, 502, 503, 504}
PROBE_INTERVAL = 3600      # background health probe at most hourly, and only when the bot has been idle
IDLE_BEFORE_PROBE = 1800   # skip the probe if a real request succeeded in the last 30 minutes
_RETRY_IN = re.compile(r"retry in (\d+(?:\.\d+)?)s", re.I)


class GeminiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


# --- schema sanitising --------------------------------------------------------------
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
        if any(isinstance(v, dict) and v.get("type") == "null" for v in schema["anyOf"]):
            nullable = True
        if len(variants) == 1:
            out = dict(variants[0])
            if nullable:
                out["nullable"] = True
            if schema.get("description") and "description" not in out:
                out["description"] = str(schema["description"])[:800]
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
        out["items"] = sanitize_schema(schema.get("items")) or {"type": "string"}
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


# --- model ranking ----------------------------------------------------------------------
def rank_models(names: list[str]) -> list[str]:
    """Newest plain '-flash' models first (free-tier friendly), then flash-latest, then pro models."""
    def version(n: str) -> float:
        m = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
        return float(m.group(1)) if m else 0.0
    bare = sorted((n for n in names if re.fullmatch(r"models/gemini-\d+(?:\.\d+)?-flash", n)), key=version, reverse=True)
    latest = [n for n in names if n == "models/gemini-flash-latest"]
    lite = sorted((n for n in names if re.fullmatch(r"models/gemini-\d+(?:\.\d+)?-flash-lite", n)), key=version, reverse=True)
    lite_latest = [n for n in names if n == "models/gemini-flash-lite-latest"]
    pro = sorted((n for n in names if re.fullmatch(r"models/gemini-\d+(?:\.\d+)?-pro", n)), key=version, reverse=True)
    return [n.replace("models/", "") for n in bare + latest + lite[:2] + lite_latest + pro]


def pick_model(names: list[str]) -> str | None:
    ranked = rank_models(names)
    return "models/" + ranked[0] if ranked else None


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


# --- client ---------------------------------------------------------------------------------
class GeminiClient:
    def __init__(self) -> None:
        self._creds: Any = None
        pinned = config.GEMINI_MODEL if config.GEMINI_MODEL and config.GEMINI_MODEL != "auto" else None
        self.model: str | None = pinned
        self.candidates: list[str] = [pinned] if pinned else []
        self.unavailable: set[str] = set()
        self.cooldown: dict[str, float] = {}       # model -> time until which we avoid it (recent 503)
        self.last_probe = 0.0
        self.last_success = 0.0
        self._sent: list[float] = []               # timestamps of requests in the last minute (self-imposed budget)
        self._budget_lock = asyncio.Lock()
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
        if config.GEMINI_API_KEY:
            return {}, {"key": config.GEMINI_API_KEY}
        token = await asyncio.to_thread(self._bearer)
        headers = {"Authorization": f"Bearer {token}"}
        if config.GEMINI_PROJECT_ID:
            headers["x-goog-user-project"] = config.GEMINI_PROJECT_ID
        return headers, {}

    # --- HTTP ---------------------------------------------------------------------
    async def _take_budget(self) -> None:
        """Never exceed GEMINI_RPM_BUDGET requests per rolling minute - the free tier's shared cap is ~20."""
        async with self._budget_lock:
            while True:
                now = time.monotonic()
                self._sent = [t for t in self._sent if now - t < 60]
                if len(self._sent) < config.GEMINI_RPM_BUDGET:
                    self._sent.append(now)
                    return
                wait = 60 - (now - self._sent[0]) + 0.5
                log.info("Gemini budget: %d requests in the last minute - waiting %.0fs", len(self._sent), wait)
                await asyncio.sleep(wait)

    async def _request(self, method: str, path: str, max_attempts: int = 3, **kw: Any) -> dict[str, Any]:
        if ":generateContent" in path:
            await self._take_budget()
        headers, params = await self._auth()
        headers.update(kw.pop("headers", {}))
        params.update(kw.pop("params", {}))
        delay = 4.0
        for attempt in range(max_attempts):
            r = await self.http.request(method, f"{API}/{path}", headers=headers, params=params, **kw)
            if r.status_code in _RETRY_STATUS and attempt < max_attempts - 1:
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

    # --- model selection ----------------------------------------------------------
    async def list_candidates(self) -> list[str]:
        if self.candidates and self.candidates != [self.model] or (self.candidates and config.GEMINI_MODEL != "auto"):
            return self.candidates
        data = await self._request("GET", "models", params={"pageSize": 200})
        names = [m["name"] for m in data.get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])]
        self.candidates = rank_models(names)
        if not self.candidates:
            raise GeminiError(404, "no Gemini model with generateContent is available to this project")
        return self.candidates

    def _order(self) -> list[str]:
        now = time.monotonic()
        cands = [m for m in self.candidates if m not in self.unavailable]
        healthy = [m for m in cands if self.cooldown.get(m, 0) <= now]
        cands = healthy or cands                      # only fall back to cooled-down models if nothing else is left
        if self.model and self.model in cands:
            cands.remove(self.model)
            cands.insert(0, self.model)
        return cands[:8]

    def _mark_busy(self, model: str, seconds: float = 300.0) -> None:
        self.cooldown[model] = time.monotonic() + seconds

    async def probe(self) -> str | None:
        """Send a realistic-size request down the ranked list and pin the first model that answers.
        Run at startup and on a timer so the first user message does not pay for the search."""
        await self.list_candidates()
        filler = ("Admissions context paragraph for load testing. " * 40 + "\n") * 12   # ~4k tokens, like a real turn
        body = {"contents": [{"role": "user", "parts": [{"text": filler + "\nReply with OK"}]}],
                "generationConfig": {"maxOutputTokens": 5, "thinkingConfig": {"thinkingBudget": 0}}}
        for m in self._order()[:3]:                  # at most three probe requests - they count against the quota
            t0 = time.monotonic()
            try:
                await self._request("POST", f"models/{m}:generateContent", max_attempts=1, json=body)
                if m != self.model:
                    log.info("Gemini model pinned: %s (%.1fs)", m, time.monotonic() - t0)
                self.model = m
                self.last_probe = time.monotonic()
                return m
            except GeminiError as exc:
                if exc.status == 404:
                    self.unavailable.add(m)
                elif exc.status in _RETRY_STATUS:
                    self._mark_busy(m)
                log.info("probe: %s -> %s", m, exc.status)
        return None

    async def probe_forever(self) -> None:
        while True:
            await asyncio.sleep(PROBE_INTERVAL)
            if time.monotonic() - self.last_success < IDLE_BEFORE_PROBE:
                continue                                  # recently working - do not spend quota on a probe
            try:
                await self.probe()
            except Exception:  # noqa: BLE001
                log.exception("Gemini probe failed")

    @staticmethod
    def _retry_seconds(exc: "GeminiError") -> float | None:
        m = _RETRY_IN.search(exc.message or "")
        return float(m.group(1)) if m else None

    async def ensure_model(self) -> str:
        if self.model and self.candidates:
            return self.model
        await self.list_candidates()
        if not self.model:
            self.model = (await self.probe()) or self.candidates[0]
        return self.model

    # --- generation ----------------------------------------------------------------
    async def generate(self, system: str, contents: list[dict[str, Any]], tools: list[dict[str, Any]],
                       thinking: str | None = None) -> dict[str, Any]:
        await self.ensure_model()
        gen: dict[str, Any] = {"temperature": 0.3, "maxOutputTokens": config.ANTHROPIC_MAX_TOKENS}
        if thinking:
            gen["thinkingConfig"] = {"thinkingLevel": thinking}     # "low" for quick lookups, "high" for strategy
        body: dict[str, Any] = {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents, "generationConfig": gen}
        if tools:
            body["tools"] = [{"functionDeclarations": to_function_declarations(tools)}]

        last: GeminiError | None = None
        quota_waits = 0
        for sweep in range(2):                       # two passes over the list; short pause between them
            hops = 0
            for m in self._order():
                if hops >= config.GEMINI_MAX_HOPS:   # every hop is a request against the shared quota
                    break
                try:
                    result = await self._request("POST", f"models/{m}:generateContent", max_attempts=1, json=body)
                    if m != self.model:
                        log.warning("Gemini failover: %s -> %s", self.model, m)
                        self.model = m
                    self.last_success = time.monotonic()
                    return result
                except GeminiError as exc:
                    if exc.status == 404:
                        self.unavailable.add(m)
                        continue
                    if exc.status == 400 and "thinking" in exc.message.lower() and "thinkingConfig" in gen:
                        gen.pop("thinkingConfig")      # model does not support thinkingLevel - resend without it
                        return await self.generate(system, contents, tools, thinking=None)
                    if exc.status not in _RETRY_STATUS:
                        raise
                    last = exc
                    if exc.status == 429:
                        # Free-tier per-minute quota is shared across models: hopping does not help, waiting does.
                        wait = self._retry_seconds(exc)
                        if wait is not None and wait <= 65 and quota_waits < 2:
                            quota_waits += 1
                            log.warning("Gemini quota: waiting %.0fs as requested (wait %d/2)", wait + 1, quota_waits)
                            await asyncio.sleep(wait + 1)
                            result = await self._retry_same(m, body)
                            if result is not None:
                                return result
                            continue
                        raise
                    self._mark_busy(m)                 # 5xx: skip this model for the next few minutes
                    hops += 1
                    log.warning("Gemini %s on %s - trying next", exc.status, m)
            if sweep == 0:
                await asyncio.sleep(8)
        raise last or GeminiError(503, "all Gemini models busy")

    async def _retry_same(self, model: str, body: dict[str, Any]) -> dict[str, Any] | None:
        try:
            result = await self._request("POST", f"models/{model}:generateContent", max_attempts=1, json=body)
            self.model = model
            self.last_success = time.monotonic()
            return result
        except GeminiError as exc:
            if exc.status in _RETRY_STATUS:
                return None
            raise
