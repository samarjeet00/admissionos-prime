"""Read-only Google Sheets reference workbook ("Date Wise Performance Comparision - CCC").

Access model
- Authenticates as a Google service account whose JSON key sits outside version control.
- Requests ONLY the `spreadsheets.readonly` scope, so the token cannot modify the sheet.
- The tabs in REFERENCE_SHEET_TABS are kept in the model's context permanently; every other
  tab is fetched on demand through the `reference_sheet_tab` tool and cached.
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

# What each tab holds - shown to the model so it knows where to look.
TAB_GUIDE: dict[str, str] = {
    "Domestic REG": "Day-wise domestic registrations. Rows = day of month (1-31) + Total; columns = month for each session 2023-24, 2024-25, 2025-26, 2026-27 (row 3 = session, row 4 = month label like May-25).",
    "Domestic ADM": "Day-wise domestic admissions, same layout as Domestic REG.",
    "Goa Campus REG": "Day-wise Goa campus registrations, sessions 2025-26 and 2026-27.",
    "Goa Campus ADM": "Day-wise Goa campus admissions, sessions 2025-26 and 2026-27.",
    "Online - July Intake": "Day-wise registrations for online programmes, July intake, two sessions side by side with a Growth column and Grand Total row.",
    "Online - Jan Intake": "Day-wise registrations for online programmes, January intake, two sessions side by side with Growth.",
    "Last Dates Performance": "Every application deadline ('last date') set per month per session and the number of registrations received ON that day, including extensions. The authority on how past deadlines performed.",
    "Board Exam & Result Dates": "Board exam start and result dates per board per session (2023-24 to 2027-28 tentative); left block is a date-wise list.",
    "Entrance Exam & Result Dates": "Entrance exam and result dates per exam per session; columns N-S also hold a monthly leads-by-campus table.",
    "CCC": "Per month per session: admissions and CCC confirmations (two columns) with the % yield row; Total columns compare Sep-Apr year on year.",
}


class ReferenceSheet:
    def __init__(self) -> None:
        self.tabs: dict[str, list[list[str]]] = {}          # in-context tabs
        self.cache: dict[str, tuple[float, list[list[str]]]] = {}   # on-demand tabs -> (fetched_at, rows)
        self.all_tabs: list[str] = []
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
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
        if self._creds is None:
            self._creds = service_account.Credentials.from_service_account_file(config.GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        if not self._creds.valid:
            self._creds.refresh(Request())
        return self._creds.token

    @property
    def service_account_email(self) -> str | None:
        return getattr(self._creds, "service_account_email", None)

    async def _get(self, http: httpx.AsyncClient, path: str) -> dict[str, Any]:
        token = await asyncio.to_thread(self._token)
        response = await http.get(f"{API}/{config.REFERENCE_SHEET_ID}{path}", headers={"Authorization": f"Bearer {token}"})
        if response.status_code == 400 and "Unable to parse range" in response.text:
            raise RuntimeError("tab not found in the sheet - check the exact tab name")
        if response.status_code == 403:
            detail = (response.json().get("error") or {}).get("message", "") if "json" in response.headers.get("content-type", "") else ""
            if "has not been used" in detail or "is disabled" in detail:
                raise RuntimeError(f"Google Sheets API is not enabled for the service account's project - {detail}")
            raise RuntimeError(f"no access to the sheet - share it (Viewer) with {self.service_account_email}")
        response.raise_for_status()
        return response.json()

    async def _values(self, http: httpx.AsyncClient, tab: str) -> list[list[str]]:
        data = await self._get(http, f"/values/{quote(chr(39) + tab + chr(39), safe='')}?majorDimension=ROWS")
        return [[str(c).strip() for c in row] for row in data.get("values", [])]

    # --- fetch ------------------------------------------------------------------
    async def refresh(self) -> bool:
        if not self.configured:
            self.status = "not_configured"
            self.error = (f"Put the service-account key at {config.GOOGLE_SERVICE_ACCOUNT_FILE} and share the sheet "
                          "with that account as Viewer.")
            return False
        async with self._lock:
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
                    meta = await self._get(http, "?fields=sheets.properties.title")
                    self.all_tabs = [s["properties"]["title"] for s in meta.get("sheets", [])]
                    fetched = {tab: await self._values(http, tab) for tab in config.REFERENCE_SHEET_TABS}
                self.tabs, self.fetched_at, self.status, self.error = fetched, time.time(), "ok", None
                self.cache.clear()
                log.info("reference sheet loaded: %s | on-demand tabs: %d", ", ".join(f"{k} ({len(v)} rows)" for k, v in fetched.items()), len(self.all_tabs))
                return True
            except Exception as exc:  # noqa: BLE001
                self.status, self.error = "error", f"{type(exc).__name__}: {exc}"
                log.error("reference sheet refresh failed: %s", self.error)
                return False

    async def get_tab(self, tab: str) -> list[list[str]]:
        """Any tab of the workbook, fetched on demand and cached for REFERENCE_REFRESH_HOURS."""
        if tab in self.tabs:
            return self.tabs[tab]
        hit = self.cache.get(tab)
        if hit and time.time() - hit[0] < config.REFERENCE_REFRESH_HOURS * 3600:
            return hit[1]
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
            rows = await self._values(http, tab)
        self.cache[tab] = (time.time(), rows)
        return rows

    async def refresh_forever(self) -> None:
        while True:
            await asyncio.sleep(config.REFERENCE_REFRESH_HOURS * 3600)
            await self.refresh()

    # --- render -----------------------------------------------------------------
    @staticmethod
    def rows_to_table(rows: list[list[str]], max_rows: int = 120) -> str:
        rows = [r for r in rows if any(r)][:max_rows]
        if not rows:
            return "(empty)"
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        out = ["| " + " | ".join(c.replace("|", "/").replace("\n", " ") for c in rows[0]) + " |", "|" + "---|" * width]
        out += ["| " + " | ".join(c.replace("|", "/").replace("\n", " ") for c in r) + " |" for r in rows[1:]]
        return "\n".join(out)

    def as_markdown(self, max_chars: int = 70000) -> str:
        if self.status != "ok" or not self.tabs:
            return ""
        parts = ["# REFERENCE WORKBOOK: 'Date Wise Performance Comparision - CCC' (read-only)",
                 "The tabs below are loaded in full. Treat their dates and counts as [sheet] verified history. "
                 "Other tabs are available through the reference_sheet_tab tool:",
                 ""]
        for tab in self.all_tabs:
            marker = "(loaded below)" if tab in self.tabs else "(use reference_sheet_tab)"
            parts.append(f"- {tab} {marker}: {TAB_GUIDE.get(tab, '')}")
        parts.append("")
        for tab, rows in self.tabs.items():
            parts.append(f"## {tab}")
            parts.append(self.rows_to_table(rows))
            parts.append("")
        text = "\n".join(parts)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n\n[Reference truncated - the sheet is larger than the context budget; ask the administrator to trim old rows.]"
        return text


reference = ReferenceSheet()
