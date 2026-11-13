"""
Email card keys — the card as a drive `KeyedRecord`, its name keys, its template key and the folding stop (email index
v2 §2.2, §5.4; card slice 1). Pure: no I/O.

**The drive keyer runs unchanged.** `keyed_record` hands `about_keys_of` / `about_sources` (`drive_keys`) the card as a
`RecordKeyInputs` of kind `email`: the graded profile, the sent day as the effective date (its `y:` / `m:` keys) and
the validity date the mail prints (`l:valid`); every photo field empty. The keyer then writes the verified families
(`e:`, `v:`, `y:`, `m:`) of what the mail prints, `ef:` / `va:` / `ya:` / `ma:` of what it does not, and the automatic
word families — the card builder puts the mail-only type's names into the label words (`email_card_labels`).

**Name keys** are `name_keys_v2` of the subject and of the sender's display name.

**Template key** (folding, §5.4) is built from code fields only: the sender's domain and the subject casefolded, a
leading reply or forward prefix removed, every month name and every run holding a digit masked (`#`). Receipts,
balance alerts and grade notices of one sender share it whatever their number, amount or date.

**Only an exact id or amount match stops a member from folding**: a sender or word match never does, or templates
would never fold. `fold_stop_keys` are the card's `v:` keys made from its printed key values (ids and amounts, never
dates).

Public interface:
    keyed_record(profile, sent_day) -> RecordKeyInputs;  card_record(card) -> RecordKeyInputs
    card_name_keys(subject, from_name) -> (key, ...)
    template_key(from_domain, subject) -> str
    fold_stop_keys(card) -> frozenset;  stops_folding(card, hit_keys) -> bool
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import FrozenSet, Iterable, Tuple

from .drive_enrichment import DriveProfile
from .drive_grade import _GRADE_MONTH_OF
from .drive_key_sources import AboutField, RecordKeyInputs
from .drive_keys import about_sources, name_keys_v2
from .drive_match import KeyFamily
from .drive_profile import valid_until_of
from .email_card import EmailCard

CARD_KIND = "email"
_REPLY_PREFIX = re.compile(r"^\s*(?:(?:re|fw|fwd|aw|wg|rv|tr|res|enc|відп|ответ)\s*(?:\[\d+\])?\s*:\s*)+",
                           re.IGNORECASE)
_DIGIT_RUN = re.compile(r"[^\W_]*\d[\w.,:/-]*")
_WORD = re.compile(r"[^\W\d_]+\.?")
_MASKS = re.compile(r"#(?:[\s.,:/-]*#)*")


def keyed_record(profile: DriveProfile, sent_day: date) -> RecordKeyInputs:
    """The card as the drive keyer's record (module docstring)."""
    return RecordKeyInputs(kind=CARD_KIND, effective_date=sent_day, profile=profile,
                           valid_until=valid_until_of(profile))


def card_record(card: EmailCard) -> RecordKeyInputs:
    """A built card as the keyer's record (render time: `about_sources` of its keys)."""
    return keyed_record(card.profile, card.mail.sent_day)


def card_name_keys(subject: str, from_name: str) -> Tuple[str, ...]:
    """`name_keys_v2` of the subject and of the sender's display name, sorted, each once."""
    return tuple(sorted({*name_keys_v2(subject), *(name_keys_v2(from_name) if from_name.strip() else ())}))


def template_key(from_domain: str, subject: str) -> str:
    """`<domain>|<masked subject>` (module docstring)."""
    text = _REPLY_PREFIX.sub("", unicodedata.normalize("NFC", subject).casefold())
    text = _DIGIT_RUN.sub("#", text)
    text = _WORD.sub(lambda m: "#" if m.group(0).rstrip(".") in _GRADE_MONTH_OF else m.group(0), text)
    text = _MASKS.sub("#", text)
    return f"{from_domain.strip().casefold()}|{' '.join(text.split())}"


def fold_stop_keys(card: EmailCard) -> FrozenSet[str]:
    """The card's `v:` keys made from its printed key values (ids and amounts)."""
    value = KeyFamily.VALUE.value
    return frozenset(key for key, sources in about_sources(card_record(card)).items()
                     if key.startswith(value) and any(s.field is AboutField.VALUE for s in sources))


def stops_folding(card: EmailCard, hit_keys: Iterable[str]) -> bool:
    """True when a search hit the card by one of its printed ids or amounts: it is shown, never folded."""
    return not fold_stop_keys(card).isdisjoint(hit_keys)
