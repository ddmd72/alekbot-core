"""
Email query slots — every model-facing piece of code text of the slots call of email search v2 (email index v2 §5.2;
search slice 8). Pure, mypy --strict.

The slots call reads one mail request into the closed shape `email_search_query.parse_query_slots` validates. Its system
prompt is one Firestore token (`OUTPUT_FORMAT_EMAIL_QUERY_SLOTS`) that keeps four `{NAME}` placeholders for the closed
lists and caps; code fills them from the same sets the parser checks, so no list lives twice. The user message carries
the request and the owner's Today in his zone, so the system prompt stays the same for every request (cacheable).
`none` is the schema's way of saying "no time"; the parser drops it like any other unknown intent.

Public interface:
    SLOTS_PROFILE, NO_TIME
    slot_doc_types(), slot_domains() -> [value, ...]      the closed sets of the two label slots
    slots_placeholder_values() -> {NAME: "a, b, c"}     the four placeholders of the token
    render_slots_prompt(assembled) -> str                placeholders filled; ValueError naming any left over
    slots_response_schema() -> dict                      the structured-output schema (Gemini)
    slots_user_message(request, local_now, zone) -> str
    slots_retry_message(reason) -> str
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Mapping

from .drive_labels import LIFE_DOMAINS
from .email_card_labels import CARD_DOC_TYPES
from .email_search_params import EXACT_KEYS_MAX, PEOPLE_SLOTS_MAX
from .email_search_query import SCOPES, TIME_INTENTS

SLOTS_PROFILE = "email_query_slots"
NO_TIME = "none"

_OTHER = "other"
_PLACEHOLDER = re.compile(r"\{([A-Z][A-Z_]*)\}")
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def slot_doc_types() -> List[str]:
    """The document types a slot may name: the card's types without `other` (the parser's set)."""
    return [t for t in CARD_DOC_TYPES if t != _OTHER]


def slot_domains() -> List[str]:
    """The life areas a slot may name: the drive's areas without `other` (the parser's set)."""
    return [d for d in LIFE_DOMAINS if d != _OTHER]


def slots_placeholder_values() -> Mapping[str, str]:
    """The four placeholders of `OUTPUT_FORMAT_EMAIL_QUERY_SLOTS` and what they stand for."""
    return {
        "DOC_TYPES": ", ".join(slot_doc_types()),
        "LIFE_DOMAINS": ", ".join(slot_domains()),
        "MAX_EXACT_KEYS": str(EXACT_KEYS_MAX),
        "MAX_PEOPLE": str(PEOPLE_SLOTS_MAX),
    }


def render_slots_prompt(assembled: str) -> str:
    """The assembled prompt with each `{NAME}` of `slots_placeholder_values()` replaced once (a single pass). Any other
    ALL-CAPS placeholder is a token from a wrong upload: ValueError naming each."""
    values = slots_placeholder_values()
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


def _enum(values: List[str]) -> Dict[str, Any]:
    return {"type": "string", "enum": values}


def slots_response_schema() -> Dict[str, Any]:
    """The structured-output schema of the slots (Gemini only, so `propertyOrdering` is kept); every field is required
    and the closed lists come from the code sets."""
    time = {
        "type": "object",
        "properties": {
            "intent": _enum([*TIME_INTENTS, NO_TIME]),
            "from": {"type": "string", "description": "YYYY-MM-DD, or empty."},
            "to": {"type": "string", "description": "YYYY-MM-DD, or empty."},
        },
        "required": ["intent", "from", "to"],
        "propertyOrdering": ["intent", "from", "to"],
    }
    properties: Dict[str, Any] = {
        "subject": {"type": "string", "description": "What the wanted mail's card would say, in English."},
        "doc_types": {"type": "array", "items": _enum(slot_doc_types())},
        "domains": {"type": "array", "items": _enum(slot_domains())},
        "people": {"type": "array", "items": {"type": "string"}},
        "time": time,
        "exact_keys": {"type": "array", "items": {"type": "string"}},
        "scope": _enum(list(SCOPES)),
    }
    order = list(properties)
    return {"type": "object", "properties": properties, "required": order, "propertyOrdering": order}


def slots_user_message(request: str, local_now: datetime, zone: str) -> str:
    """The request, then the owner's Today in his zone (the date every relative period is resolved against)."""
    return (f'EMAIL_SEARCH_REQUEST "{request}"\n'
            f"Today: {local_now.date().isoformat()} ({_WEEKDAYS[local_now.weekday()]}), timezone {zone}.")


def slots_retry_message(reason: str) -> str:
    """The correction sent after an output that is not the slots' JSON object."""
    return (f"Your previous output was not valid query slots ({reason}). Output only the JSON object described by the "
            "output format: no markdown, no explanation.")
