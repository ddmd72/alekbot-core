"""
Email card prompt — every model-facing piece of code text of the card call (email index v2, slice 3). Pure, mypy --strict.

The card writer's system prompt is the two Firestore tokens (`COGNITIVE_PROCESS_EMAIL_CARD`, `OUTPUT_FORMAT_EMAIL_CARD`)
plus one per-account static block. The tokens keep seven `{NAME}` placeholders for the closed lists; code fills them from
its own enums, so no list lives twice (the cognitive token has none). The static block names the owner and the people the
owner confirmed, under per-render handles `P<n>` that `email_card_people.resolve_people` maps back. The user message is the
card core's input followed by the task line, which states the numeric caps in digits.

Public interface:
    STATIC_BLOCK_TITLE
    render_card_static_block(people, mailbox) -> str
    card_placeholder_values() -> {NAME: "a, b, c"}               the seven placeholders of the output-format token
    render_card_prompt(assembled) -> str                         placeholders filled; ValueError naming any left over
    card_response_schema() -> dict                               the structured-output schema (Gemini, as measured)
    card_task_line() -> str
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional

from .confirmed_people import ConfirmedPeople
from .drive_labels import DATE_LABELS, ENTITY_TYPES, LIFE_DOMAINS, VALUE_LABELS
from .email_card import About, Substance
from .email_card_answer import REQUIRED_FIELDS
from .email_card_labels import CARD_DOC_TYPES
from .email_card_params import (
    MAIL_DATES_MAX, MAIL_ENTITIES_MAX, MAIL_ENTITY_FORMS_MAX, MAIL_KEY_VALUES_MAX, MAIL_PEOPLE_MAX, MAIL_TOPICS_MAX,
    MAIL_WORD_LIST_MAX,
)
from .email_card_people import person_handles

STATIC_BLOCK_TITLE = "OWNER AND CONFIRMED PEOPLE (from the owner's own settings, not from the mail)"

_PLACEHOLDER = re.compile(r"\{([A-Z][A-Z_]*)\}")


def _neutral(text: str) -> str:
    """A name from the owner's settings cannot look like a placeholder: braces become parentheses."""
    return text.replace("{", "(").replace("}", ")")


def render_card_static_block(people: Optional[ConfirmedPeople], mailbox: str) -> str:
    """The per-account system part: the owner, the mailbox and the confirmed people with their `P<n>` handles."""
    if people is None or not people.user.name:
        owner = "name not given"
    else:
        user = people.user
        owner = f"{user.name}; also written {', '.join(user.forms)}" if user.forms else user.name
    lines = [f"{handle} | {entry.name} | {', '.join(entry.forms) if entry.forms else 'none'} | {entry.relation}"
             for handle, entry in person_handles(people).items()]
    return _neutral("\n".join([
        STATIC_BLOCK_TITLE,
        f"The owner: {owner}",
        f"The owner's mailbox: {mailbox.strip() or 'not known'}",
        "People the owner has confirmed, one per line as entry id | name | other forms | relation to the owner:",
        "\n".join(lines) or "none",
    ]))


def card_placeholder_values() -> Mapping[str, str]:
    """The seven placeholders of `OUTPUT_FORMAT_EMAIL_CARD` and the lists they stand for, joined by ", "."""
    return {
        "DOC_TYPES": ", ".join(CARD_DOC_TYPES),
        "LIFE_DOMAINS": ", ".join(LIFE_DOMAINS),
        "ABOUT_VALUES": ", ".join(a.value for a in About),
        "DATE_LABELS": ", ".join(DATE_LABELS),
        "ENTITY_TYPES": ", ".join(ENTITY_TYPES),
        "VALUE_LABELS": ", ".join(VALUE_LABELS),
        "SUBSTANCE_VALUES": ", ".join(s.value for s in Substance),
    }


def render_card_prompt(assembled: str) -> str:
    """The assembled prompt with each `{NAME}` of `card_placeholder_values()` replaced once (a single pass, so a value
    is never scanned again). Any other ALL-CAPS placeholder is a token from a wrong upload: ValueError naming each."""
    values = card_placeholder_values()
    left: List[str] = []

    def fill(match: "re.Match[str]") -> str:
        name = match.group(1)
        if name in values:
            return values[name]
        left.append(name)
        return match.group(0)

    out = _PLACEHOLDER.sub(fill, assembled)
    if left:
        raise ValueError(f"placeholders left unrendered: {', '.join(dict.fromkeys(left))}")
    return out


def card_task_line() -> str:
    """The last line of the user message: the caps in digits."""
    return (f"Write the card of this mail as the output format says. Limits: at most {MAIL_ENTITIES_MAX} entities with "
            f"at most {MAIL_ENTITY_FORMS_MAX} forms each, {MAIL_KEY_VALUES_MAX} key values, {MAIL_DATES_MAX} dates, "
            f"{MAIL_PEOPLE_MAX} people, {MAIL_TOPICS_MAX} topics and {MAIL_TOPICS_MAX} native topics, and "
            f"{MAIL_WORD_LIST_MAX} words in each word list.")


def _enum(values: List[str]) -> Dict[str, Any]:
    return {"type": "string", "enum": values}


def card_response_schema() -> Dict[str, Any]:
    """The structured-output schema of the lean card, field for field as the probe measured it (Gemini only, so
    `propertyOrdering` is kept); the closed lists come from the code sets."""
    obj_people = {
        "type": "object",
        "properties": {
            "printed": {"type": "string", "description": "The name as printed, or the relation words when no name is."},
            "entry_id": {"type": "string", "description": "The one list entry that fits, or empty."},
        },
        "required": ["printed", "entry_id"],
        "propertyOrdering": ["printed", "entry_id"],
    }
    obj_dates = {
        "type": "object",
        "properties": {
            "label": _enum(list(DATE_LABELS)),
            "date": {"type": "string", "description": "YYYY-MM-DD; a span's first day."},
            "date_end": {"type": "string", "description": "YYYY-MM-DD, a span's last day; empty for a single day."},
            "printed": {"type": "string", "description": "The date or span exactly as printed."},
        },
        "required": ["label", "date", "date_end", "printed"],
        "propertyOrdering": ["label", "date", "date_end", "printed"],
    }
    obj_entities = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Its usual name."},
            "type": _enum(list(ENTITY_TYPES)),
            "printed": {"type": "string", "description": "Exactly as printed in the mail."},
            "forms": {"type": "array", "items": {"type": "string"},
                      "description": "At most three spellings a person would type: another script or language, "
                                     "never a trivial variant."},
        },
        "required": ["name", "type", "printed", "forms"],
        "propertyOrdering": ["name", "type", "printed", "forms"],
    }
    obj_values = {
        "type": "object",
        "properties": {
            "label": _enum(list(VALUE_LABELS)),
            "printed": {"type": "string", "description": "Exactly as printed."},
            "canonical": {"type": "string",
                          "description": "Digits and letters only; an amount with a dot and two decimals."},
        },
        "required": ["label", "printed", "canonical"],
        "propertyOrdering": ["label", "printed", "canonical"],
    }
    properties: Dict[str, Any] = {
        "language": {"type": "string", "description": "The mail's main language, ISO 639-1."},
        "doc_type": {**_enum(list(CARD_DOC_TYPES)), "description": "The closest type of what the mail is."},
        "life_domains": {"type": "array", "items": _enum(list(LIFE_DOMAINS)),
                         "description": "One to three life areas, the most relevant first."},
        "title": {"type": "string", "description": "A short name of what the mail is, in the mail's language."},
        "what_it_is": {"type": "string", "description": "One English sentence: what this mail is, as of its sent date."},
        "people": {"type": "array", "description": "Each person other than the owner whose matter the mail is.",
                   "items": obj_people},
        "about": {**_enum([a.value for a in About]), "description": "Whose matter the mail is."},
        "dates": {"type": "array", "description": "Dates the mail prints, each with its role.", "items": obj_dates},
        "entities": {"type": "array", "description": "Named places, organisations, people, events, products, works.",
                     "items": obj_entities},
        "key_values": {"type": "array", "description": "Identifiers and amounts printed in the mail.",
                       "items": obj_values},
        "topics": {"type": "array", "items": {"type": "string"}, "description": "Short subjects, in English."},
        "topics_native": {"type": "array", "items": {"type": "string"},
                          "description": "The same subjects in the mail's language."},
        "topic_words": {"type": "array", "items": {"type": "string"},
                        "description": "Each topic's everyday words in English and the mail's own language, at most "
                                       "twelve."},
        "substance": {**_enum([s.value for s in Substance]),
                      "description": "none only when nothing in the mail could be asked about later."},
    }
    order = list(REQUIRED_FIELDS)
    return {"type": "object", "properties": {k: properties[k] for k in order}, "required": order,
            "propertyOrdering": order}
