"""Read-only Google Sheets reference data (historical board / entrance exam and result dates).

Access model
- Authenticates as a Google service account whose JSON key sits outside version control.
- Requests ONLY the `spreadsheets.readonly` scope, so the token cannot modify the sheet even
  if the sheet were shared with edit rights.
- Reads ONLY the tabs listed in REFERENCE_SHEET_TABS; nothing else in the workbook is touched.
- Values are cached in memory and refreshed on a timer (or via /reload).
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx2 as httpx

from . import config

log = logging.getLogger("admissionos.sheets")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
API = "https://sheets.googleapis.com/v4/spreadsheets"


class ReferenceSheet:
    def __init__(self) -> None:
        self.tabs: dict[str, list[list[str]]] = {}
        self.fetched_at: float | None = None
        self.status = "not_configured"          # not_configured | ok | error
        self.error: str | None = None
        self._creds: Any = None
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(config.REFERENCE_SHEET_ID) and Path(config.GOOGLE_SERVICE_ACCOUNT_FILE).exists()

    # --- auth -------------------------------------------------------------------
    def _token(self) -> str:
        """Runs in a worker thread (google-auth's transport is synchronous)."""
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account

        if self._creds is None:
            self._creds = service_account.Credentials.from_service_account_file(
                config.GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES
            )
        if not self._creds.valid:
            self._creds.refresh(Request())
        return self._creds.token

    @property
    def service_account_email(self) -> str | None:
        return getattr(self._creds, "service_account_email", None)

    # --- fetch ------------------------------------------------------------------
    async def refresh(self) -> bool:
        if not self.configured:
            self.status = "not_configured"
            self.error = (f"Put the service-account key at {config.GOOGLE_SERVICE_ACCOUNT_FILE} and share the sheet "
                          "with that account as Viewer.")
            return False
        async with self._lock:
            try:
                token = await asyncio.to_thread(self._token)
                headers = {"Authorization": f"Bearer {token}"}
                fetched: dict[str, list[list[str]]] = {}
                async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
                    for tab in config.REFERENCE_SHEET_TABS:
                        rng = quote(f"'{tab}'", safe="")
                        url = f"{API}/{config.REFERENCE_SHEET_ID}/values/{rng}?majorDimension=ROWS"
                        response = await http.get(url, headers=headers)
                        if response.status_code == 400 and "Unable to parse range" in response.text:
                            raise RuntimeError(f"tab '{tab}' not found in the sheet - check the exact tab name")
                        if response.status_code == 403:
                            raise RuntimeError(f"no access to the sheet - share it (Viewer) with {self.service_account_email}")
                        response.raise_for_status()
                        rows = response.json().get("values", [])
                        fetched[tab] = [[str(c).strip() for c in row] for row in rows]
                self.tabs, self.fetched_at, self.status, self.error = fetched, time.time(), "ok", None
                log.info("reference sheet loaded: %s", ", ".join(f"{k} ({len(v)} rows)" for k, v in fetched.items()))
                return True
            except Exception as exc:  # noqa: BLE001
                self.status, self.error = "error", f"{type(exc).__name__}: {exc}"
                log.error("reference sheet refresh failed: %s", self.error)
                return False

    async def refresh_forever(self) -> None:
        while True:
            await asyncio.sleep(config.REFERENCE_REFRESH_HOURS * 3600)
            await self.refresh()

    # --- render -----------------------------------------------------------------
    def as_markdown(self, max_chars: int = 60000) -> str:
        if self.status != "ok" or not self.tabs:
            return ""
        parts = ["# HISTORICAL REFERENCE: EXAM AND RESULT DATES",
                 "Source: the Admissions reference Google Sheet (read-only). These are recorded actual dates from "
                 "previous years - treat them as [verified] history and use them to estimate this year's windows "
                 "(same week-of-year pattern) when an official notification is not yet out. Say when you are "
                 "extrapolating from history versus quoting a confirmed date.", ""]
        for tab, rows in self.tabs.items():
            rows = [r for r in rows if any(r)]
            parts.append(f"## {tab}")
            if not rows:
                parts.append("(empty)\n")
                continue
            width = max(len(r) for r in rows)
            rows = [r + [""] * (width - len(r)) for r in rows]
            header, body = rows[0], rows[1:]
            parts.append("| " + " | ".join(c.replace("|", "/") for c in header) + " |")
            parts.append("|" + "---|" * width)
            for r in body:
                parts.append("| " + " | ".join(c.replace("|", "/") for c in r) + " |")
            parts.append("")
        text = "\n".join(parts)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n\n[Reference truncated - the sheet is larger than the context budget; ask the administrator to trim old rows.]"
        return text


reference = ReferenceSheet()
