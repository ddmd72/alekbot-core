"""How a link from HTML is written into text a model reads: one rule for drive, chat and live Gmail bodies.

Owner decisions O-2 (b) and O-6 (2026-11-05): an external http(s) link keeps its full URL without `utm_*` query
parameters and without the fragment, cut at LINK_URL_MAX characters with "…". It is written as its text followed by
the URL in brackets ("Read more (https://example.org/a?id=7)"); a link whose text already holds its URL, a link with no
text, and any other link (relative, mailto:, javascript:, data:, malformed) keep their text only.

Callers: the drive parser's HTML reader (`src/adapters/drive_parser/html_text.py`, inside the sandbox) and the Gmail
adapter's HTML body (`src/adapters/gmail_provider_adapter.py`). Stdlib only: neither caller's image pulls a library
through it.

Public interface:
    LINK_URL_MAX
    external_url(href) -> str                 # cleaned http(s) URL, "" for anything else
    cut_url(url) -> str                       # at most LINK_URL_MAX characters, "…" when cut
    text_holds_url(text, url) -> bool
    link_with_url(text, href) -> str         # "text (url)", or "" when the link keeps its text only
"""
import re
from urllib.parse import urlsplit, urlunsplit

LINK_URL_MAX = 300                 # characters of an external link URL kept in the text (O-2 (b))

_SCHEME = re.compile(r"^https?://")


def external_url(href: str) -> str:
    """An external http(s) link cleaned: no `utm_*` query parameters (the others stay in order), no fragment; "" for
    a relative link, mailto:, javascript:, data: or a malformed one. A scheme-relative "//host/…" is read as https."""
    if href.startswith("//"):
        href = "https:" + href
    try:
        parts = urlsplit(href)
        host = parts.hostname
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https") or not host:
        return ""
    query = "&".join(pair for pair in parts.query.split("&")
                     if pair and not pair.lower().startswith("utm_"))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def cut_url(url: str) -> str:
    """`url` itself up to LINK_URL_MAX characters; past that, its first LINK_URL_MAX characters and "…"."""
    return url if len(url) <= LINK_URL_MAX else url[:LINK_URL_MAX] + "…"


def text_holds_url(text: str, url: str) -> bool:
    """The link's text already shows its URL (with or without the scheme, with or without a trailing slash)."""
    lowered = text.lower()
    bare = _SCHEME.sub("", url.lower())
    return url.lower() in lowered or bare in lowered or bare.rstrip("/") in lowered


def link_with_url(text: str, href: str) -> str:
    """The text an `<a>` with this (whitespace-collapsed) `text` and `href` becomes: "text (cleaned URL)" for an
    external link whose text does not already hold its URL; "" when the link keeps its text only."""
    full = external_url(href.strip())
    if full and text and not text_holds_url(text, full):
        return f"{text} ({cut_url(full)})"
    return ""
