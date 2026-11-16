"""
Email memory mail — one stored mail as Stage 3 of consolidation reads it (email index v2 §6.7, slice 9). Pure data.

The v1 record (`email`) and the card fields stored beside it: `card_state` ("" when the document has no card fields) and
`card_doc`, the stored card fields as `card_from_document` reads them (None when there are none). Whether the mail
enters memory as its card or as the v1 one-line candidate is decided by `email_memory_candidate`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from .email import IndexedEmail


@dataclass(frozen=True)
class MemoryMail:
    email: IndexedEmail
    card_state: str
    card_doc: Optional[Mapping[str, Any]]
