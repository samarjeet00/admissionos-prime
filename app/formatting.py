"""Markdown from the model -> Telegram HTML / WhatsApp text, split into message-sized chunks.

Both platforms cap a message at 4096 characters and neither renders markdown tables, so
tables and code blocks become monospaced blocks and headings become bold lines.
"""
from __future__ import annotations

import html
import re

LIMIT = 3900  # headroom under the 4096 cap for tags and joins

_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_SEP_ROW = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


# --- block splitting --------------------------------------------------------------
def split_blocks(md: str) -> list[tuple[str, str]]:
    """-> [(kind, text)] with kind in {'text', 'table', 'code'}."""
    lines = md.replace("\r\n", "\n").split("\n")
    blocks: list[tuple[str, str]] = []
    buf: list[str] = []
    i = 0

    def flush() -> None:
        if buf:
            blocks.append(("text", "\n".join(buf).strip("\n")))
            buf.clear()

    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            flush()
            j, code = i + 1, []
            while j < len(lines) and not lines[j].strip().startswith("```"):
                code.append(lines[j])
                j += 1
            blocks.append(("code", "\n".join(code)))
            i = j + 1
            continue
        if _TABLE_ROW.match(line):
            flush()
            rows = []
            while i < len(lines) and _TABLE_ROW.match(lines[i]):
                rows.append(lines[i])
                i += 1
            blocks.append(("table", render_table(rows)))
            continue
        buf.append(line)
        i += 1
    flush()
    return [b for b in blocks if b[1].strip()]


def render_table(rows: list[str]) -> str:
    parsed = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows if not _SEP_ROW.match(r)]
    if not parsed:
        return ""
    width = max(len(r) for r in parsed)
    parsed = [r + [""] * (width - len(r)) for r in parsed]
    cols = [max(len(r[c]) for r in parsed) for c in range(width)]
    out = [" | ".join(cell.ljust(cols[c]) for c, cell in enumerate(row)).rstrip() for row in parsed]
    out.insert(1, "-+-".join("-" * w for w in cols))
    return "\n".join(out)


# --- inline conversions -----------------------------------------------------------
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.M)
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])")
_ITALIC_U = re.compile(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])")
_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_BULLET = re.compile(r"^(\s*)[-*+]\s+", re.M)
_HR = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$", re.M)


def _telegram_inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = _HR.sub("", text)
    text = _HEADING.sub(r"<b>\1</b>", text)
    text = _LINK.sub(r'<a href="\2">\1</a>', text)
    text = _CODE.sub(r"<code>\1</code>", text)
    text = _BOLD.sub(r"<b>\1</b>", text)
    text = _ITALIC.sub(r"<i>\1</i>", text)
    text = _ITALIC_U.sub(r"<i>\1</i>", text)
    text = _BULLET.sub(r"\1• ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _whatsapp_inline(text: str) -> str:
    text = _HR.sub("", text)
    text = _HEADING.sub(r"*\1*", text)
    text = _LINK.sub(r"\1 (\2)", text)
    text = _CODE.sub(r"\1", text)
    text = _BOLD.sub(r"*\1*", text)
    text = _ITALIC_U.sub(r"_\1_", text)
    text = _BULLET.sub(r"\1• ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# --- chunking -------------------------------------------------------------------------
def _split_lines(text: str, limit: int) -> list[str]:
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:            # a single monster line
            parts.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + 1 + len(line) > limit:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts


def render(md: str, platform: str, limit: int = LIMIT) -> list[str]:
    """Convert markdown to a list of platform-formatted messages, each under `limit` chars."""
    pieces: list[str] = []
    for kind, text in split_blocks(md):
        if kind in ("table", "code"):
            for part in _split_lines(text, limit - 20):
                pieces.append(f"<pre>{html.escape(part, quote=False)}</pre>" if platform == "telegram" else f"```\n{part}\n```")
        else:
            rendered = _telegram_inline(text) if platform == "telegram" else _whatsapp_inline(text)
            pieces.extend(_split_lines(rendered, limit))

    chunks, cur = [], ""
    for piece in pieces:
        if cur and len(cur) + 2 + len(piece) > limit:
            chunks.append(cur)
            cur = piece
        else:
            cur = f"{cur}\n\n{piece}" if cur else piece
    if cur:
        chunks.append(cur)
    return chunks or ["(empty)"]
