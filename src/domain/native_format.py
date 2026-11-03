"""
What a model reads natively: one file format with its caps (docs/10_rfcs/EXTERNAL_FILES_RFC.md §4.3, P5). Pure.

An adapter declares a tuple of these per model (`LLMPort.native_formats`). An engine runner sets the declaration on a
copy of its context under `NATIVE_FORMATS_KEY`, in the JSON-plain form `to_plain` gives (a list of dicts: the Cloud Task
encoder handles no dataclass); the open path reads it back with `from_plain`. No key, or an empty list, means text only.

A format is refused at build when its `max_bytes` is above `document_limits.native_ceiling` for its kind (5 MiB an
image, 20 MiB a PDF; any other mime type has a ceiling of 0, so it can never be declared). `max_pages` limits a PDF;
an image is one page.

Public interface:
    NATIVE_FORMATS_KEY
    NativeFormat(mime, max_bytes, max_pages)   frozen; ValueError on a bad mime, a cap above the ceiling or below 1
    to_plain(formats) -> List[Dict[str, Any]]
    from_plain(value) -> Tuple[NativeFormat, ...]
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Tuple

from .document_kind import mime_kind
from .document_limits import native_ceiling

NATIVE_FORMATS_KEY = "native_formats"

_FIELDS = ("mime", "max_bytes", "max_pages")


def _kind(mime: str) -> str:
    """The kind `native_ceiling` is keyed by: "pdf", "image" or "other"."""
    if mime_kind(mime) == "pdf":
        return "pdf"
    if mime.startswith("image/"):
        return "image"
    return "other"


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class NativeFormat:
    """One format a model reads natively: its bare, lower-case mime type, the most raw bytes one file may have, and
    the most pages (a PDF's; 1 for an image)."""

    mime: str
    max_bytes: int
    max_pages: int

    def __post_init__(self) -> None:
        if not isinstance(self.mime, str) or not self.mime or self.mime != self.mime.strip().lower() or ";" in self.mime:
            raise ValueError(f"native format mime must be a bare lower-case type, got {self.mime!r}")
        if not _is_int(self.max_bytes) or not _is_int(self.max_pages):
            raise ValueError(f"native format caps must be integers: {self!r}")
        ceiling = native_ceiling(_kind(self.mime))
        if not 1 <= self.max_bytes <= ceiling:
            raise ValueError(
                f"native format {self.mime}: max_bytes {self.max_bytes} outside 1..{ceiling} (the native ceiling)"
            )
        if self.max_pages < 1:
            raise ValueError(f"native format {self.mime}: max_pages {self.max_pages} below 1")


def to_plain(formats: Iterable[NativeFormat]) -> List[Dict[str, Any]]:
    """The declaration as JSON-plain data (a list of dicts of str and int), the form the context key carries."""
    return [{"mime": f.mime, "max_bytes": f.max_bytes, "max_pages": f.max_pages} for f in formats]


def from_plain(value: Any) -> Tuple[NativeFormat, ...]:
    """The declaration back from its plain form. None (no key) is text only, `()`. Anything that is not a list of
    dicts with exactly the three fields, or a format above its ceiling, raises ValueError: the key is written only by
    `to_plain`, so a bad value is a bug, never a reason to guess."""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{NATIVE_FORMATS_KEY} must be a list, got {type(value).__name__}")
    formats = []
    for item in value:
        if not isinstance(item, dict) or set(item) != set(_FIELDS):
            raise ValueError(f"{NATIVE_FORMATS_KEY} item must be a dict with {_FIELDS}, got {item!r}")
        formats.append(NativeFormat(mime=item["mime"], max_bytes=item["max_bytes"], max_pages=item["max_pages"]))
    return tuple(formats)
