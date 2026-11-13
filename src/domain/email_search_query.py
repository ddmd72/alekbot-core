"""
Email search query — what a mail request is made of (email index v2 §5.1-§5.3; search slice 4). Pure: no I/O.

This module holds several public classes on purpose: they are one value shape (the request, the validated slots a model
may add to it and the concepts read from its text), read together by every email search module, as `email_card` holds
the card's.

A request's **words** are the drive's query concepts (`drive_keys.query_concepts`), after the mail words that no card
holds as a topic are removed (`MAIL_GENERIC_WORDS`: the mail nouns and the arrival verbs). Concepts that name a time
(a month with a year, a date, a year, a month name) are split off: they choose the search window (`email_search_time`),
they are never required words, so "mail from September" does not block on a word no card holds.

**Slots** are optional and already validated: the model's query reading is slice 8's, this module only turns what it
returned into a closed shape (`parse_query_slots`, never raises: a value of the wrong type empties that slot alone). The
slots add to the meaning list and re-rank, they never filter (`email_search_merge`); only the time slot filters, and
only when the request's own text names a time.

Public interface:
    TimeSlot, MailQuerySlots, MailSearchRequest, MailConcepts
    parse_query_slots(raw, request_text) -> MailQuerySlots
    mail_concepts(text) -> MailConcepts
    people_query_keys(values, people) -> (key, ...)     printed-name keys of the entries a slot value names
    label_query_keys(slots) -> (key, ...)               `l:type:<type>` and `l:domain:<area>`
    exact_query_keys(slots) -> (key, ...)               the `v:` keys of the ids and amounts a slot key holds
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .confirmed_people import ConfirmedPeople
from .drive_grade import _GRADE_MONTH_OF
from .drive_keys import phrase_keys, query_concepts
from .drive_labels import LIFE_DOMAINS
from .drive_match import Concept, KeyFamily
from .email_card_labels import CARD_DOC_TYPES
from .email_card_people import _names_entry, _relation_entries
from .email_search_params import EXACT_KEYS_MAX, MAIL_GENERIC_WORDS, PEOPLE_SLOTS_MAX, SUBJECT_CHARS

TIME_INTENTS = ("period", "past", "upcoming", "latest")
SCOPES = ("top", "all")
YEAR_MIN, YEAR_MAX = 1990, 2100
_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_FULL_DATE_KEY = re.compile(r"v:\d{4}-\d{2}-\d{2}")
_MAIL_TOKEN = re.compile(r"[^\W_]+(?:[-'’][^\W_]+)*")
_LABEL_TYPE = "l:type:"
_LABEL_DOMAIN = "l:domain:"


@dataclass(frozen=True)
class TimeSlot:
    """The model's reading of the request's time: `intent` is one of TIME_INTENTS; `start` / `end` ISO days or None."""
    intent: str
    start: Optional[date]
    end: Optional[date]


@dataclass(frozen=True)
class MailQuerySlots:
    """The validated slots (`parse_query_slots`). `scope` is "top" or "all"."""
    subject: str
    doc_types: Tuple[str, ...]
    domains: Tuple[str, ...]
    people: Tuple[str, ...]
    time: Optional[TimeSlot]
    exact_keys: Tuple[str, ...]
    scope: str


@dataclass(frozen=True)
class MailSearchRequest:
    """One mail request: its text, the owner's Today and the optional validated slots."""
    text: str
    today: date
    slots: Optional[MailQuerySlots] = None


@dataclass(frozen=True)
class MailConcepts:
    """The request's concepts: the topic ones (strict-AND words and ids), the time ones (they pick the window), the
    words removed (mail words and the drive's function, generic and time words) and the concepts cut past the cap."""
    topic: Tuple[Concept, ...]
    time: Tuple[Concept, ...]
    ignored: Tuple[str, ...]
    cut: Tuple[str, ...]


def empty_slots() -> MailQuerySlots:
    """Slots that say nothing."""
    return MailQuerySlots(subject="", doc_types=(), domains=(), people=(), time=None, exact_keys=(), scope="top")


def parse_query_slots(raw: Mapping[str, Any], request_text: str) -> MailQuerySlots:
    """The slots from the model's reading of the request (module docstring); never raises."""
    if not isinstance(raw, Mapping):
        return empty_slots()
    subject = raw.get("subject")
    scope = raw.get("scope")
    return MailQuerySlots(
        subject=" ".join(subject.split())[:SUBJECT_CHARS] if isinstance(subject, str) else "",
        doc_types=_closed(raw.get("doc_types"), tuple(t for t in CARD_DOC_TYPES if t != "other")),
        domains=_closed(raw.get("domains"), tuple(d for d in LIFE_DOMAINS if d != "other")),
        people=_strings(raw.get("people"), PEOPLE_SLOTS_MAX),
        time=_time_slot(raw.get("time")),
        exact_keys=_exact_keys(raw.get("exact_keys"), request_text),
        scope="all" if scope == "all" else "top")


def mail_concepts(text: str) -> MailConcepts:
    """The request's concepts with the mail words removed first (module docstring)."""
    removed: Dict[str, None] = {}

    def drop(match: "re.Match[str]") -> str:
        word = unicodedata.normalize("NFC", match.group(0)).casefold()
        if word in MAIL_GENERIC_WORDS:
            removed.setdefault(word, None)
            return " "
        return match.group(0)

    kept = _MAIL_TOKEN.sub(drop, text)
    found = query_concepts(kept)
    topic = tuple(c for c in found.concepts if not is_time_concept(c))
    time = tuple(c for c in found.concepts if is_time_concept(c))
    ignored = tuple(dict.fromkeys((*removed, *found.ignored)))
    return MailConcepts(topic=topic, time=time, ignored=ignored, cut=found.cut)


def is_time_concept(concept: Concept) -> bool:
    """A month with a year, a full date, a year or a month name (module docstring)."""
    if concept.is_number:
        if any(k.startswith(KeyFamily.MONTH.value) for k in concept.about_keys):
            return True
        if any(_FULL_DATE_KEY.fullmatch(k) for k in concept.about_keys):
            return True
        return concept.word.isdigit() and len(concept.word) == 4 and YEAR_MIN <= int(concept.word) <= YEAR_MAX
    return unicodedata.normalize("NFC", concept.word).casefold() in _GRADE_MONTH_OF


def people_query_keys(values: Sequence[str], people: Optional[ConfirmedPeople]) -> Tuple[str, ...]:
    """For each slot value, the printed-name keys of the confirmed entries (the owner excluded) whose name or form it
    names, or whose relation words it holds; a value no entry fits is a name the owner typed and gives its own keys,
    unless it names the owner alone ("me" is not a person). Each key once, in order."""
    entries = list(people.people) if people is not None else []
    keys: Dict[str, None] = {}
    for value in values:
        fit = [e for e in entries if _names_entry(value, e)]
        fit += [e for e in _relation_entries(value, entries) if e not in fit]
        if fit:
            for entry in sorted(fit, key=entries.index):
                for name in (entry.name, *entry.forms):
                    keys.update(dict.fromkeys(phrase_keys(name)))
        elif people is not None and _names_entry(value, people.user):
            continue
        else:
            keys.update(dict.fromkeys(phrase_keys(value)))
    return tuple(keys)


def label_query_keys(slots: Optional[MailQuerySlots]) -> Tuple[str, ...]:
    """`l:type:<doc type>` per slot type and `l:domain:<area>` per slot area (the keyer writes them)."""
    if slots is None:
        return ()
    return (*(f"{_LABEL_TYPE}{t}" for t in slots.doc_types), *(f"{_LABEL_DOMAIN}{d}" for d in slots.domains))


def exact_query_keys(slots: Optional[MailQuerySlots]) -> Tuple[str, ...]:
    """The `v:` about keys of the number concepts of each slot key ("FA-0137" -> `v:fa0137`)."""
    if slots is None:
        return ()
    keys: Dict[str, None] = {}
    for key in slots.exact_keys:
        for concept in query_concepts(key).concepts:
            if concept.is_number:
                keys.update(dict.fromkeys(k for k in concept.about_keys if k.startswith(KeyFamily.VALUE.value)))
    return tuple(keys)


# -- implementation -------------------------------------------------------------------------------------------------

def _strings(raw: Any, limit: int) -> Tuple[str, ...]:
    """The non-empty strings of a list, whitespace collapsed, each once, at most `limit`; () for anything else."""
    if not isinstance(raw, (list, tuple)):
        return ()
    out = dict.fromkeys(" ".join(v.split()) for v in raw if isinstance(v, str) and v.strip())
    return tuple(out)[:limit]


def _closed(raw: Any, allowed: Sequence[str]) -> Tuple[str, ...]:
    """The values of a list that are in a closed set, each once, in the list's order."""
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(dict.fromkeys(v for v in raw if isinstance(v, str) and v in allowed))


def _day(raw: Any) -> Optional[date]:
    if not isinstance(raw, str) or not _ISO_DAY.fullmatch(raw):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _time_slot(raw: Any) -> Optional[TimeSlot]:
    if not isinstance(raw, Mapping) or raw.get("intent") not in TIME_INTENTS:
        return None
    start, end = _day(raw.get("from")), _day(raw.get("to"))
    if start is not None and end is not None and start > end:
        return None
    return TimeSlot(intent=str(raw["intent"]), start=start, end=end)


def _flat(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).casefold().split())


def _exact_keys(raw: Any, request_text: str) -> Tuple[str, ...]:
    """Keys the request text holds (whitespace collapsed, casefolded, both sides), at most EXACT_KEYS_MAX."""
    haystack = _flat(request_text)
    kept: List[str] = [k for k in _strings(raw, EXACT_KEYS_MAX * 4) if _flat(k) in haystack]
    return tuple(kept[:EXACT_KEYS_MAX])
