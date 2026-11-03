"""The drive connector's one-time download link (DRIVE_MCP_CONNECTOR_RFC §8, E18): its state, limits and headers.

A link is `https://<host>/d/<id>`: a 128-bit opaque id (`secrets.token_urlsafe(16)`, 22 characters), never a JWT. The
raw id is never stored or logged: the store keys the link by `keyed_digest(digest_key, LINK_DIGEST_PURPOSE, raw_id)`
(SECRETS_AT_REST, as the MCP authorization codes). The server keeps the state (`DriveLink`); a GET consumes one read,
a HEAD none. Every byte streams through the main service: there is no redirect to the provider's URL (owner
2026-11-03).

`LINK_MAX_READS` is the reads one link allows within `LINK_TTL_S`: 1 (owner 2026-11-03). It is raised, never above 3,
only after a client's pre-fetch has been measured live; a test pins both bounds.

Headers (§8, DELTA 4): `no-store`, `no-referrer`, `nosniff` and a CSP sandbox on every response; inline only for
PNG, JPEG, GIF, WebP and PDF; any type that runs script in a browser (HTML, SVG, XML, JavaScript) is served as
`application/octet-stream`, always as an attachment.

The module holds several public names on purpose: one value, its constants and its two pure helpers.
"""
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, FrozenSet, Optional
from urllib.parse import quote, urlparse

LINK_TTL_S = 300
LINK_MAX_READS = 1          # owner 2026-11-03: raise (never above 3) only after a client's pre-fetch is measured
LINK_PURGE_AFTER_S = 86_400
LINK_PATH = "/d/"
LINK_DIGEST_PURPOSE = "drive_link"
LINK_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{22}$")

INLINE_MIME_TYPES: FrozenSet[str] = frozenset({
    "image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf",
})
ACTIVE_MIME_TYPES: FrozenSet[str] = frozenset({
    "text/html", "application/xhtml+xml", "image/svg+xml", "text/xml", "application/xml", "text/javascript",
    "application/javascript", "application/ecmascript", "text/ecmascript",
})
OCTET_STREAM = "application/octet-stream"

FOLDER_LINK_LINE = "'{path}' is a folder; a download link is for one file."

SAFETY_HEADERS: Dict[str, str] = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox; default-src 'none'",
}

_ASCII_UNSAFE = re.compile(r'[^\x20-\x7e]|["\\%;]')


@dataclass(frozen=True)
class DriveLink:
    """One issued link's server state; `link_key` is the keyed digest of the raw id (the store's doc id)."""
    link_key: str
    user_id: str
    account_id: str
    connection_id: str
    ref_tag: str
    item_id: str
    grant_id: str
    created_at: datetime
    expires_at: datetime
    purge_after: datetime
    reads: int = 0
    used_at: Optional[datetime] = None

    def live(self, now: datetime, max_reads: int) -> bool:
        """Before its expiry (exclusive) and with a read left."""
        return now < self.expires_at and self.reads < max_reads


def link_base_url(resource_uri: str) -> str:
    """`scheme://host[:port]` of the MCP resource URI: the links' host (E4, no new deploy key)."""
    parsed = urlparse(resource_uri)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("the MCP resource URI has no scheme or host")
    return f"{parsed.scheme}://{parsed.netloc}"


def served_type(mime: str) -> str:
    """The Content-Type the link serves: the file's own type, `application/octet-stream` for an active type."""
    base = (mime or "").split(";", 1)[0].strip().lower()
    if not base or base in ACTIVE_MIME_TYPES or base.endswith("+xml"):
        return OCTET_STREAM
    return base


def link_headers(name: str, mime: str) -> Dict[str, str]:
    """The headers of a link's 200 (GET and HEAD): type, disposition and the safety set."""
    content_type = served_type(mime)
    disposition = "inline" if content_type in INLINE_MIME_TYPES else "attachment"
    fallback = _ASCII_UNSAFE.sub("_", name).strip() or "file"
    encoded = quote(name, safe="")
    return {
        "Content-Type": content_type,
        "Content-Disposition": f"{disposition}; filename=\"{fallback}\"; filename*=UTF-8''{encoded}",
        **SAFETY_HEADERS,
    }
