"""
Email card lifecycle — the fields a stored mail carries for its v2 card and the rules that produce them (email index v2
§2.1, §4, §6.1, §6.3; slice 6). Pure, mypy --strict.

The card is stored as extra fields on the mail's v1 document, written as a field update that never touches a v1 field
(`CARD_FIELDS` is the whole set a card write may name). Four outcomes are stored: a settled card (`carded` or `empty`,
through `settled_fields`), a `pending` one (`pending_fields`: the model or the parser could not serve now, or the mail
could not be read; `card_failures` counts the failures charged to this mail's input, `card_reason` says why) and a
`source_gone` one (`gone_fields`). `abandoned` is slice 7's, after `CARD_FAILURES_MAX` charged failures.

`people_digest` is the fingerprint of the confirmed-people entries a card depends on: an edit of one of those entries
(a name, a form, their order) changes it and the mail is re-carded (slice 7); an edit of any other entry does not. The
static-block digest is the same idea for the per-page prompt block (logged, not stored).

The compare-and-set contract (`CardExpect`, `CardWriteResult`) is one concept with the two fields it compares and shares
this module on purpose (one-class rule waived; precedent `fact_profiler_reply`).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from .confirmed_people import ConfirmedPeople, ConfirmedPerson
from .email_card import CardState, EmailCard
from .email_card_document import card_document
from .email_card_params import CARD_VERSION
from .email_card_people import _names_entry

CARD_FAILURES_MAX = 3          # RFC §4: three charged failures on one input -> abandoned (slice 7 applies it)
CARD_CONCURRENCY = 4           # cards in flight per page
CARD_PAGE_BUDGET_S = 240       # a page spends at most this long on cards; the rest is stored `pending` (reason budget)
CARD_MAIL_BUDGET_S = 120       # attachment reading per mail

LIFECYCLE_FIELDS: Tuple[str, ...] = (
    "card_state", "card_version", "people_digest", "card_model", "carded_at", "card_failures", "card_reason",
    "card_vector_pending",
)
CARD_FIELDS: Tuple[str, ...] = LIFECYCLE_FIELDS + (
    "about_keys", "name_keys", "people_keys", "month_keys", "template_key", "card", "card_vector",
)


@dataclass(frozen=True)
class CardExpect:
    """What the writer read before deciding: the stored `card_version` and `people_digest`; both None = no card fields."""
    card_version: Optional[int]
    people_digest: Optional[str]

    @staticmethod
    def absent() -> "CardExpect":
        return CardExpect(None, None)


class CardWriteResult(str, Enum):
    WRITTEN = "written"
    CONFLICT = "conflict"
    MISSING = "missing"


def block_digest(static_block: str) -> str:
    """sha256 hex of the static block's UTF-8 bytes."""
    return hashlib.sha256(static_block.encode("utf-8")).hexdigest()


def people_digest(card: EmailCard, people: Optional[ConfirmedPeople]) -> str:
    """The fingerprint of the entries `card` depends on: the ones it resolved and the ones a kept person's printed
    name holds (the owner's entry included); each once, sorted by name, over (name, forms). No list or no such entry:
    the digest of `[]`."""
    if people is None:
        return _digest([])
    resolved = {p.entry_name for p in card.people if p.entry_name}
    picked: Dict[str, ConfirmedPerson] = {}
    for entry in people.entries():
        if entry.name in resolved or any(_names_entry(p.printed, entry) for p in card.people):
            picked.setdefault(entry.name, entry)
    return _digest([[e.name, list(e.forms)] for e in sorted(picked.values(), key=lambda e: e.name)])


def settled_fields(card: EmailCard, *, digest: str, model: str, now: datetime,
                   vector_pending: bool) -> Dict[str, Any]:
    """The fields of a `carded` or `empty` card. Raises ValueError for any other card state."""
    if card.state not in (CardState.CARDED, CardState.EMPTY):
        raise ValueError(f"a {card.state.value} card is not settled")
    fields = card_document(card)
    fields.update(
        people_digest=digest, card_model=model, carded_at=now, card_failures=0, card_reason="",
        card_vector_pending=vector_pending)
    return fields


def pending_fields(*, reason: str, failures: int, model: str) -> Dict[str, Any]:
    """The fields of a mail whose card could not be made now (`reason`: invalid, refused, outage, attachments, budget,
    source, error). `failures` is the charged count (the writer's invalid or refused answer)."""
    return {
        "card_state": CardState.PENDING.value, "card_version": CARD_VERSION, "people_digest": "",
        "card_model": model, "card_failures": failures, "card_reason": reason, "card_vector_pending": False,
    }


def gone_fields() -> Dict[str, Any]:
    """The fields of a mail the provider no longer has."""
    return {
        "card_state": CardState.SOURCE_GONE.value, "card_version": CARD_VERSION, "people_digest": "",
        "card_reason": "gone", "card_failures": 0, "card_model": "", "card_vector_pending": False,
    }


def _digest(entries: List[List[Any]]) -> str:
    return hashlib.sha256(json.dumps(entries, ensure_ascii=False).encode("utf-8")).hexdigest()
