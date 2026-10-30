"""The identity of a file's bytes (MULTI_DRIVE_RFC §7.2, §10, D12).

`ContentIdentity(family, digest, size)`: `family` names the hash the adapter computed or read (a lowercase token such as
the provider's hash name), `digest` its value as the provider prints it, `size` the byte count. Rendered
`{family}:{digest}:{size}`, which is the stored transcript's content key: two copies of one recording share it, on any
connection. No family is implied by absence: an item without a provider hash has no content identity (None), and its
transcript falls back to the item identity and content version.

Stored as the map `to_dict()` (`family`, `digest`, `size`); `from_stored(value)` reads it back, None for a missing or
empty value (a record stored before the field existed).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

_FAMILY_RE = re.compile(r"[a-z0-9_]+")


@dataclass(frozen=True)
class ContentIdentity:
    family: str
    digest: str
    size: int

    def __post_init__(self) -> None:
        if not _FAMILY_RE.fullmatch(self.family or ""):
            raise ValueError(f"a content family is a lowercase token: {self.family!r}")
        if not self.digest:
            raise ValueError("a content identity needs a digest")
        if self.size < 0:
            raise ValueError(f"a content size is not negative: {self.size}")

    def render(self) -> str:
        """`{family}:{digest}:{size}`."""
        return f"{self.family}:{self.digest}:{self.size}"

    def to_dict(self) -> Dict[str, Any]:
        return {"family": self.family, "digest": self.digest, "size": self.size}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ContentIdentity:
        """The identity a `to_dict()` map holds; ValueError or KeyError for a malformed one."""
        return cls(family=str(data["family"]), digest=str(data["digest"]), size=int(data["size"]))

    @classmethod
    def from_stored(cls, value: Optional[Mapping[str, Any]]) -> Optional[ContentIdentity]:
        """`from_dict`, with None for a missing or empty stored value."""
        return cls.from_dict(value) if value else None

