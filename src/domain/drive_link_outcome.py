"""What the one-time link service answers (DRIVE_MCP_CONNECTOR_RFC §8).

Three small values of one flow, kept in one module on purpose (the one-public-class rule is waived here): an issued
link, a link's file as a HEAD sees it, and an opened link with its byte stream.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncIterator


@dataclass(frozen=True)
class IssuedLink:
    """`url` is `https://<host>/d/<raw id>`: a bearer secret, never logged or stored. `label` is the file's drive label
    (`drive_label`), what the client reads next to the link."""
    url: str = field(repr=False)
    label: str
    expires_at: datetime


@dataclass(frozen=True)
class LinkFile:
    """The file behind a link, as its headers name it."""
    name: str
    mime: str


@dataclass(frozen=True)
class OpenedLink:
    """A consumed, re-checked link: its file and its bytes. `chunks` is an async generator the route drains and
    closes; the first chunk was already fetched, so a source that fails before any byte never reaches the route."""
    name: str
    mime: str
    chunks: AsyncIterator[bytes] = field(repr=False)
