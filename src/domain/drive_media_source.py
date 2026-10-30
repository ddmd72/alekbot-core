"""Where a tool may stream a file's bytes from (MULTI_DRIVE_RFC §6, E24).

`MediaSource(url, headers, expires_at)` replaces the bare download URL. `url` and `headers` are secrets: they are left out
of `repr`, and are never logged, stored or put in an error. The network-capable parser sandbox receives a source only
when it is `header_free` (a pre-signed URL); otherwise the caller streams the bytes into the sandbox's work directory,
so no provider token ever enters the sandbox. `expires_at` is None when the provider does not say.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Optional


@dataclass(frozen=True)
class MediaSource:
    url: str = field(repr=False)
    headers: Mapping[str, str] = field(default_factory=dict, repr=False, hash=False)
    expires_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if not self.url:
            raise ValueError("a media source needs a url")

    @property
    def header_free(self) -> bool:
        """True when the url alone fetches the bytes: the only kind the sandbox may receive."""
        return not self.headers
