"""
Deep research drive copy — naming the report's copy on the user's drive
(docs/04_solution_strategy/decisions/deep_research_drive_copy.md).

Pure and deterministic: the copy is `YYYY-MM-DD <title>.html`, the title read from the report's <title>,
else the research question shortened; sanitized like every drive name and capped in UTF-8 bytes
(a Cyrillic character costs 2) on a character boundary.
"""
from __future__ import annotations

import html as html_lib
import re
from datetime import date

from .user_drive import sanitize_drive_filename

# Cloud Task context key set by deliver_deep_research on the HtmlPageGenerator task: its presence marks the
# delivered HTML as a deep research report, its value is the research question (the name fallback).
DEEP_RESEARCH_REPORT_CONTEXT_KEY = "deep_research_report_query"
MAX_MARKER_QUERY_CHARS = 500

# The whole name, extension included. OneDrive allows 255 characters per name; bytes are the stricter
# measure for non-Latin titles, and a margin leaves room for the drive's own " 1" clash suffix.
MAX_REPORT_NAME_BYTES = 200
MAX_QUERY_TITLE_CHARS = 80
REPORT_EXTENSION = ".html"
REPORT_CONTENT_TYPE = "text/html; charset=utf-8"
_FALLBACK_TITLE = "report"

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_WS_RE = re.compile(r"\s+")


def report_title_from_html(html: str) -> str:
    """The report's <title> text, entities unescaped and whitespace collapsed; "" when absent."""
    match = _TITLE_RE.search(html or "")
    if not match:
        return ""
    return _WS_RE.sub(" ", html_lib.unescape(match.group(1))).strip()


def marker_query(query: str) -> str:
    """The research question as carried in the task context (bounded: it rides in a Cloud Task payload)."""
    return (query or "")[:MAX_MARKER_QUERY_CHARS]


def _shorten_query(query: str) -> str:
    text = _WS_RE.sub(" ", query or "").strip()
    if len(text) <= MAX_QUERY_TITLE_CHARS:
        return text
    cut = text[:MAX_QUERY_TITLE_CHARS]
    head = cut.rsplit(" ", 1)[0]
    if len(head) >= MAX_QUERY_TITLE_CHARS // 2:
        cut = head
    return cut.rstrip(" .,;:-") + "…"


def _truncate_utf8(text: str, limit: int) -> str:
    """At most `limit` UTF-8 bytes, never splitting a character."""
    return text.encode("utf-8")[:max(limit, 0)].decode("utf-8", errors="ignore")


def deep_research_drive_filename(day: date, title: str, query: str) -> str:
    """`YYYY-MM-DD <title>.html`, drive-safe and at most MAX_REPORT_NAME_BYTES bytes."""
    subject = _WS_RE.sub(" ", title or "").strip() or _shorten_query(query) or _FALLBACK_TITLE
    prefix = f"{day.isoformat()} "
    budget = MAX_REPORT_NAME_BYTES - len(prefix.encode("utf-8")) - len(REPORT_EXTENSION.encode("utf-8"))
    # sanitize, cap, then sanitize again: the cut may leave a trailing space or dot.
    stem = sanitize_drive_filename(_truncate_utf8(sanitize_drive_filename(subject), budget))
    return f"{prefix}{stem}{REPORT_EXTENSION}"
