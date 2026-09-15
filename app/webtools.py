"""Local (non-portal) tools available to the model on every tier: keyless web search and page fetch.

Used to verify public facts - exam / result / notification dates, competitor announcements -
against official sources instead of guessing. No API key, no cost.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from typing import Any
from urllib.parse import urlparse

import httpx2 as httpx

log = logging.getLogger("admissionos.web")

LOCAL_TOOLS: list[dict[str, Any]] = [
    {
        "name": "web_search",
        "description": ("Search the public web (DuckDuckGo, no key). Use it to confirm any public fact you do not have from the "
                        "portal, the reference sheet or memory - especially exam, result, notification and counselling dates, "
                        "competitor admission windows and scholarship announcements. Prefer official domains (upsc.gov.in, "
                        "nta.ac.in, cbse.gov.in, gseb.org, jeemain.nta.nic.in, mcc.nic.in, gujacpc.admissions.nic.in) and "
                        "then fetch_url the best result to read the exact wording."),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query, e.g. 'UPSC CDS 1 2027 exam date site:upsc.gov.in'"},
                "max_results": {"type": "integer", "description": "1-10, default 6"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "fetch_url",
        "description": ("Fetch a public web page and return its readable text (HTML stripped, scripts removed, truncated). "
                        "Use after web_search to read the exact date or wording from the source page. PDFs are not readable - "
                        "pick an HTML page instead."),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "http(s) URL"},
                "max_chars": {"type": "integer", "description": "Characters of text to return, default 12000"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "reference_sheet_tab",
        "description": ("Read one tab of the Admissions reference workbook 'Date Wise Performance Comparision - CCC' (read-only, "
                        "cached). Use it to compare a specific date or month across sessions, to see day-wise registrations or "
                        "admissions, or to check how past deadlines performed. Tabs: Domestic REG, Domestic ADM, Goa Campus REG, "
                        "Goa Campus ADM, Online - July Intake, Online - Jan Intake, Last Dates Performance, Board Exam & Result "
                        "Dates, Entrance Exam & Result Dates, CCC. Rows are returned as a markdown table with the sheet's own headers."),
        "input_schema": {
            "type": "object",
            "properties": {
                "tab": {"type": "string", "description": "Exact tab name, e.g. 'Domestic REG' or 'Last Dates Performance'"},
                "max_rows": {"type": "integer", "description": "Rows to return, default 120"},
            },
            "required": ["tab"],
        },
    },
]
LOCAL_TOOL_NAMES = {t["name"] for t in LOCAL_TOOLS}


async def reference_sheet_tab(tab: str, max_rows: int = 120) -> tuple[str, bool]:
    from .sheets import reference
    if reference.status != "ok":
        return (f"reference sheet not available: {reference.error}", True)
    wanted = (tab or "").strip().lower()
    match = next((t for t in reference.all_tabs if t.lower() == wanted), None) or \
        next((t for t in reference.all_tabs if wanted and wanted in t.lower()), None)
    if not match:
        return (f"No tab named '{tab}'. Available: {', '.join(reference.all_tabs)}", True)
    try:
        rows = await reference.get_tab(match)
    except Exception as exc:  # noqa: BLE001
        return (f"could not read tab '{match}': {type(exc).__name__}: {exc}", True)
    return (f"Tab: {match} ({len(rows)} rows)\n\n" + reference.rows_to_table(rows, max(10, min(int(max_rows or 120), 400))), False)

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AdmissionOSPrime/1.0"


def _blocked_host(host: str) -> bool:
    """Refuse localhost / private networks so the model cannot be steered into internal services."""
    if not host or host.lower() in ("localhost",):
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return True
    return False


def _search_sync(query: str, max_results: int) -> list[dict[str, str]]:
    from ddgs import DDGS
    return DDGS().text(query, max_results=max_results) or []


_google_exhausted_until = 0.0


async def _google_search(query: str, max_results: int) -> list[dict[str, str]] | None:
    """Google Programmable Search JSON API (free: 100 queries/day). None -> not configured / quota hit / error."""
    global _google_exhausted_until
    import time as _time
    if not (config.GOOGLE_CSE_KEY and config.GOOGLE_CSE_ID) or _time.time() < _google_exhausted_until:
        return None
    params = {"key": config.GOOGLE_CSE_KEY, "cx": config.GOOGLE_CSE_ID, "q": query, "num": min(max_results, 10), "gl": "in", "hl": "en"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0)) as http:
            r = await http.get("https://www.googleapis.com/customsearch/v1", params=params)
    except Exception as exc:  # noqa: BLE001
        log.warning("google search failed: %s", exc)
        return None
    if r.status_code == 429 or (r.status_code == 403 and "quota" in r.text.lower()):
        _google_exhausted_until = _time.time() + 3600          # daily quota hit - use DuckDuckGo for the next hour
        log.warning("google search quota exhausted - falling back to DuckDuckGo")
        return None
    if r.status_code >= 400:
        log.warning("google search HTTP %s: %s", r.status_code, r.text[:200])
        return None
    items = r.json().get("items") or []
    return [{"title": i.get("title", ""), "href": i.get("link", ""), "body": i.get("snippet", "")} for i in items]


async def web_search(query: str, max_results: int = 6) -> tuple[str, bool]:
    max_results = max(1, min(int(max_results or 6), 10))
    results = await _google_search(query, max_results)
    if results is None:
        try:
            results = await asyncio.wait_for(asyncio.to_thread(_search_sync, query, max_results), timeout=15)
        except asyncio.TimeoutError:
            return ("web_search timed out - try a shorter query", True)
        except Exception as exc:  # noqa: BLE001
            return (f"web_search failed: {type(exc).__name__}: {exc}", True)
    if not results:
        return ("No results. Try different words or drop the site: filter.", False)
    lines = [f"{i + 1}. {r.get('title', '').strip()}\n   {r.get('href', '')}\n   {(r.get('body') or '').strip()[:300]}"
             for i, r in enumerate(results)]
    return ("\n".join(lines), False)


_TAG_BLOCKS = re.compile(r"<(script|style|noscript|svg|nav|footer|header)[^>]*>.*?</\1>", re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_NL = re.compile(r"\n\s*\n+")


def html_to_text(html: str) -> str:
    html = _TAG_BLOCKS.sub(" ", html)
    html = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</tr>|</h[1-6]>", "\n", html, flags=re.I)
    text = _TAGS.sub(" ", html)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&#39;", "'"))
    text = _WS.sub(" ", text)
    return _NL.sub("\n", text).strip()


async def fetch_url(url: str, max_chars: int = 12000) -> tuple[str, bool]:
    max_chars = max(1000, min(int(max_chars or 12000), 40000))
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or _blocked_host(parsed.hostname or ""):
        return ("fetch_url: only public http(s) URLs are allowed", True)
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(12.0, connect=6.0), headers={"User-Agent": _UA}) as http:
            r = await http.get(url)
    except httpx.TimeoutException:
        return ("fetch_url: the page took too long (12s). Use the search snippets or try a different page.", True)
    except Exception as exc:  # noqa: BLE001
        return (f"fetch_url failed: {type(exc).__name__}: {exc}", True)
    ctype = r.headers.get("content-type", "")
    if r.status_code >= 400:
        return (f"fetch_url: HTTP {r.status_code} for {url}", True)
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        return ("fetch_url: this is a PDF, which cannot be read here. Search for an HTML page that reports the same information.", True)
    text = html_to_text(r.text[:600000])
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n[truncated at {max_chars} characters]"
    return (f"Source: {url}\n\n{text}", False)


async def call_local(name: str, args: dict[str, Any]) -> tuple[str, bool] | None:
    """Run a local tool; None if `name` is not a local tool."""
    if name == "web_search":
        return await web_search(str(args.get("query", "")), args.get("max_results") or 6)
    if name == "fetch_url":
        return await fetch_url(str(args.get("url", "")), args.get("max_chars") or 12000)
    if name == "reference_sheet_tab":
        return await reference_sheet_tab(str(args.get("tab", "")), args.get("max_rows") or 120)
    return None
