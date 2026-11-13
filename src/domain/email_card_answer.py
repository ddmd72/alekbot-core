"""
Email card answer — the parse of the model's lean card answer (email index v2, card slice 1; the answer shape is the
lean card schema). Pure: no I/O.

`parse_mail_answer(raw)` is what the extraction agent's `_parse_response` calls (slice 3): `json.loads` on the raw
text, never a regex rescue. A malformed answer is a `ValueError` — the agent's parse retry: not a JSON object, a
required field missing, a field of the wrong type. Interpretation never costs the card: a value outside its closed set
becomes `other` (doc type, date label, entity type), `other_id` (value label), `unknown` (about) or `some`
(substance: lean to keep), or is dropped (a life area); a date that is not `YYYY-MM-DD` is left out (an invalid
`date_end`, or one not after `date`, leaves a single day); lists are cut at the mail caps (`email_card_params`); text is whitespace-collapsed.

Public interface:
    REQUIRED_FIELDS
    parse_mail_answer(raw) -> MailAnswer          ValueError on a malformed answer
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any, List, Mapping, Optional, Tuple

from .drive_enrichment import KeyValue, ProfileEntity
from .drive_labels import DATE_LABELS, ENTITY_TYPES, LIFE_DOMAINS, VALUE_LABELS
from .email_card import About, AnswerDate, AnswerPerson, MailAnswer, Substance
from .email_card_labels import CARD_DOC_TYPES
from .email_card_params import (
    MAIL_DATES_MAX, MAIL_ENTITIES_MAX, MAIL_ENTITY_FORMS_MAX, MAIL_KEY_VALUES_MAX, MAIL_LIFE_DOMAINS_MAX,
    MAIL_PEOPLE_MAX, MAIL_TITLE_CHARS, MAIL_TOPICS_MAX, MAIL_WHAT_IT_IS_CHARS, MAIL_WORD_LIST_MAX,
)

REQUIRED_FIELDS: Tuple[str, ...] = (
    "language", "doc_type", "life_domains", "title", "what_it_is", "people", "about", "dates", "entities",
    "key_values", "topics", "topics_native", "topic_words", "substance",
)
_ABOUT = {a.value: a for a in About}
_SUBSTANCE = {s.value: s for s in Substance}


def parse_mail_answer(raw: str) -> MailAnswer:
    """The model's lean answer, parsed and inside its sets and caps (module docstring); ValueError when malformed."""
    data = json.loads(raw)   # JSONDecodeError is a ValueError
    if not isinstance(data, dict):
        raise ValueError("the card must be a JSON object")
    missing = [f for f in REQUIRED_FIELDS if f not in data]
    if missing:
        raise ValueError(f"missing fields: {', '.join(missing)}")
    doc_type = _text(data, "doc_type")
    domains = tuple(dict.fromkeys(d for d in _strings(data, "life_domains") if d in LIFE_DOMAINS))
    people = [AnswerPerson(printed=_text(p, "printed"), handle=_text(p, "entry_id").upper())
              for p in _objects(data, "people")]
    dates: List[AnswerDate] = []
    for d in _objects(data, "dates"):
        start = _iso(_text(d, "date"))
        if start is None:
            continue
        end = _iso(_text(d, "date_end"))
        label = _text(d, "label")
        dates.append(AnswerDate(label=label if label in DATE_LABELS else "other", start=start,
                                end=end if end is not None and end > start else None, printed=_text(d, "printed")))
    entities = []
    for e in _objects(data, "entities"):
        etype = _text(e, "type")
        entities.append(ProfileEntity(name=_text(e, "name"), type=etype if etype in ENTITY_TYPES else "other",
                                      printed=_text(e, "printed"),
                                      forms=_strings(e, "forms")[:MAIL_ENTITY_FORMS_MAX]))
    values = []
    for v in _objects(data, "key_values"):
        label = _text(v, "label")
        values.append(KeyValue(label=label if label in VALUE_LABELS else "other_id", printed=_text(v, "printed"),
                               canonical=_text(v, "canonical")))
    return MailAnswer(
        language=_text(data, "language")[:16],
        doc_type=doc_type if doc_type in CARD_DOC_TYPES else "other",
        life_domains=domains[:MAIL_LIFE_DOMAINS_MAX],
        title=_text(data, "title")[:MAIL_TITLE_CHARS],
        what_it_is=_text(data, "what_it_is")[:MAIL_WHAT_IT_IS_CHARS],
        people=tuple(people[:MAIL_PEOPLE_MAX]),
        about=_ABOUT.get(_text(data, "about"), About.UNKNOWN),
        dates=tuple(dates[:MAIL_DATES_MAX]),
        entities=tuple(entities[:MAIL_ENTITIES_MAX]),
        key_values=tuple(values[:MAIL_KEY_VALUES_MAX]),
        topics=_strings(data, "topics")[:MAIL_TOPICS_MAX],
        topics_native=_strings(data, "topics_native")[:MAIL_TOPICS_MAX],
        topic_words=_strings(data, "topic_words")[:MAIL_WORD_LIST_MAX],
        substance=_SUBSTANCE.get(_text(data, "substance"), Substance.SOME),
    )


def _text(obj: Mapping[str, Any], key: str) -> str:
    value = obj.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"`{key}` must be a string")
    return " ".join(value.split())


def _strings(obj: Mapping[str, Any], key: str) -> Tuple[str, ...]:
    value = obj.get(key, [])
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"`{key}` must be a list of strings")
    return tuple(dict.fromkeys(c for c in (" ".join(v.split()) for v in value) if c))


def _objects(obj: Mapping[str, Any], key: str) -> List[Mapping[str, Any]]:
    value = obj.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise ValueError(f"`{key}` must be a list of objects")
    return value


def _iso(text: str) -> Optional[date]:
    if len(text) != 10:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None
