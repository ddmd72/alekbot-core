"""
Email card page — what one indexing page hands to the card step and what the step reports (email index v2 §6.5;
slice 6). Pure value objects; they live in the domain so the indexing service and the card service can share them
without importing each other (REQ-ARCH-22). The two classes are one hand-off and share this module on purpose
(one-class rule waived).

`MailForCard`: one saved mail — its listing metadata and the full content fetched for it (None when that fetch failed:
the mail is then stored `pending`, reason `source`).
`CardPageReport`: counts of one page; `usd` is the writer's cost, `ms` the page's wall time.
"""
from dataclasses import dataclass
from typing import Optional

from .email import EmailFullContent, EmailMetadata


@dataclass(frozen=True)
class MailForCard:
    meta: EmailMetadata
    content: Optional[EmailFullContent]


@dataclass(frozen=True)
class CardPageReport:
    carded: int = 0
    empty: int = 0
    pending: int = 0
    gone: int = 0
    conflicts: int = 0
    missing: int = 0
    usd: float = 0.0
    ms: int = 0
