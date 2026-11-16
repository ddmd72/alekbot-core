"""
Email memory candidate — what memory reads of a carded mail (email index v2 §6.7, slice 9). Pure, mypy --strict.

Stage 3 of consolidation turns mail into facts. A mail whose card is settled and readable enters as its card, filtered
here by code; every other mail enters as the v1 one-line candidate (the agent builds that one). A card is a model's
reading of mail other people sent: the filter sends what the mail prints (verified time points, printed names and
values, the people the mail names), the card's own reading (type, life areas, title, summary, `about`) and the mail's
code fields, and never the index's keys, the link to a confirmed entry, the word lists or the lifecycle fields.

**The waiting rule.** A mail waits for a settled card (`SETTLED_FOR_MEMORY`); one still unsettled once `indexed_at` is
older than `MEMORY_CARD_WAIT` enters in the line form, so a mail whose card never settles is not starved.

**Withholding.** Title and summary are free text the card writer wrote. When either names a confirmed person (the
owner left out) whom the card does not link and the mail's printed parts do not name, both are withheld: a name from
the owner's list may have been carried in by the writer, not printed by the mail. An unreadable list withholds both on
every card.

Public interface:
    SETTLED_FOR_MEMORY      the card states a mail waits for
    MEMORY_CARD_WAIT        how long an unsettled mail waits before it enters in the line form
    EMAIL_CANDIDATES_HEADER the first line of the Stage 3 message (code text, never the system's voice)
    CARD_CANDIDATE_KEYS     the keys a card candidate may carry, in order
    CardCandidate           the candidate's fields and whether title and summary were withheld
    memory_card(mail) -> Optional[EmailCard]       the readable card of a `carded` mail, else None
    card_candidate(card, zone, people, *, list_unreadable) -> CardCandidate
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from .confirmed_people import ConfirmedPeople, people_named_in_texts
from .drive_enrichment import GradedField, is_unconfirmed
from .email_card import CardState, EmailCard
from .email_card_document import card_from_document
from .email_memory_mail import MemoryMail
from .local_time import local_moment

SETTLED_FOR_MEMORY: Tuple[str, ...] = (
    CardState.CARDED.value, CardState.EMPTY.value, CardState.ABANDONED.value, CardState.SOURCE_GONE.value,
)
# No online retry of `pending` exists: without this cap a pending mail would never reach memory.
MEMORY_CARD_WAIT = timedelta(hours=6)

EMAIL_CANDIDATES_HEADER = "Email candidates from the owner's mailbox, one JSON object per mail:"

CARD_CANDIDATE_KEYS: Tuple[str, ...] = (
    "email_id", "date", "from", "from_name", "subject", "doc_type", "life_areas", "title", "summary", "times",
    "printed_names", "printed_values", "attachments", "about", "people",
)


@dataclass(frozen=True)
class CardCandidate:
    fields: Dict[str, Any]
    withheld: bool


def memory_card(mail: MemoryMail) -> Optional[EmailCard]:
    """The card of a `carded` mail when its stored fields read back; None for any other state or an unreadable card."""
    if mail.card_state != CardState.CARDED.value or mail.card_doc is None:
        return None
    try:
        return card_from_document(mail.card_doc)
    except ValueError:
        return None


def card_candidate(card: EmailCard, zone: str, people: Optional[ConfirmedPeople], *,
                   list_unreadable: bool) -> CardCandidate:
    """The fields memory reads of `card` (module docstring), in `CARD_CANDIDATE_KEYS` order. `date` is the sent moment
    in `zone` with its offset. A key with nothing to show is left out, except the mail's own identity fields."""
    mail, profile = card.mail, card.profile
    names = [{"name": e.printed or e.name, "type": e.type} for e in profile.entities
             if not is_unconfirmed(profile, GradedField.ENTITY, e.printed.strip() or e.name)]
    values = [{"label": v.label, "value": v.printed.strip() or v.canonical} for v in profile.key_values
              if not is_unconfirmed(profile, GradedField.VALUE, v.printed.strip() or v.canonical)]
    times: List[Dict[str, str]] = []
    for point in card.times:
        if point.verified:
            span = {"label": point.label, "from": point.start.isoformat()}
            if point.end != point.start:
                span["to"] = point.end.isoformat()
            times.append(span)
    withheld = list_unreadable or _names_unprinted_person(card, people, [n["name"] for n in names])
    fields: Dict[str, Any] = {
        "email_id": mail.email_id,
        "date": local_moment(mail.sent_at, zone).isoformat(timespec="seconds"),
        "from": mail.from_address,
        "from_name": mail.from_name,
        "subject": mail.subject,
        "doc_type": profile.doc_type,
        "life_areas": list(profile.life_domains),
        "title": "" if withheld else profile.title,
        "summary": "" if withheld else profile.what_it_is,
        "times": times,
        "printed_names": names,
        "printed_values": values,
        "attachments": [{"name": a.name, "read": a.read_ok} for a in card.attachments],
        "about": card.about.value,
        "people": [p.printed for p in card.people],
    }
    always = ("email_id", "date", "from", "subject", "doc_type", "about")
    return CardCandidate({k: v for k, v in fields.items() if k in always or v}, withheld)


def _names_unprinted_person(card: EmailCard, people: Optional[ConfirmedPeople], printed_names: List[str]) -> bool:
    """True when the title or summary names a list entry (not the owner) that the card neither links nor prints."""
    named = [e for e in people_named_in_texts(people, [card.profile.title, card.profile.what_it_is]) if not e.is_user]
    if not named:
        return False
    linked = {p.entry_name for p in card.people if p.entry_name}
    printed_texts = [card.mail.subject, card.mail.from_name, *(p.printed for p in card.people), *printed_names,
                     *(a.name for a in card.attachments)]
    printed = {e.name for e in people_named_in_texts(people, printed_texts)}
    return any(e.name not in linked and e.name not in printed for e in named)
