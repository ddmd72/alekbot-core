"""
ShortLink — value object for a short-code → target-URL mapping.

Used by ShortLinkService to wrap long capability links (`/f/<token>`) behind a
short, copy-paste-friendly `/s/<code>` alias. Pure data: no behavior, no I/O.
"""
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ShortLink:
    """A minted short code and what it points to."""

    code: str
    target_url: str
    expires_at: datetime
