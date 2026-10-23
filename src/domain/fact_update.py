"""The validated arguments of an `update_fact` call."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional, Tuple

from .entities import FactState, TemporalClass
from .fact_argument_parsing import parse_enum, parse_reported_date
from .fact_expiry import EXPIRY_IGNORED_ON_CLOSE, Expiry, parse_expiry
from .fact_lifecycle import CLOSING_STATES


@dataclass(frozen=True)
class ParsedFactUpdate:
    """Only the keys the call carried are set; None means "leave as it is".

    `expiry` is the parsed decision (None = not sent, or dropped because the update closes the fact); `notes` are lines
    for the tool result about what was not used.
    """

    content: Optional[str] = None
    tags: Optional[list[str]] = None
    metadata: Optional[dict[str, Any]] = None
    state: Optional[FactState] = None
    temporal_class: Optional[TemporalClass] = None
    reported_date: Optional[datetime] = None
    superseded_by: Optional[str] = None
    expiry: Optional[Expiry] = None
    notes: Tuple[str, ...] = ()


def parse_fact_update(updates: Mapping[str, Any]) -> ParsedFactUpdate:
    """Validate `updates` before any embedding or write. Raises ValueError with a message the model can act on.

    Unknown keys are ignored. A non-dict `metadata` is ignored. Empty `content`, `tags: null` and `tags: []` are
    refused: an empty field never destroys data. State STALE is refused (only the system marks a fact stale). An
    `expiry` sent with a closing state is dropped, with a note; any other `expiry` must parse (FactExpiryError).
    """
    content = _parse_content(updates)
    tags = _parse_tags(updates)
    metadata = updates.get("metadata")
    if not isinstance(metadata, dict):
        metadata = None
    state = parse_enum(FactState, "state", updates["state"]) if updates.get("state") else None
    temporal_class = (
        parse_enum(TemporalClass, "temporal_class", updates["temporal_class"]) if updates.get("temporal_class") else None
    )
    if state is FactState.STALE:
        raise ValueError(
            "only the system marks a fact stale; to retire a fact use ARCHIVED, INVALIDATED or SUPERSEDED"
        )
    reported_date = parse_reported_date(updates.get("reported_date"))
    superseded_by = _parse_superseded_by(updates)
    expiry, notes = _parse_update_expiry(updates, state)

    if state is FactState.SUPERSEDED:
        if superseded_by is None:
            raise ValueError("state SUPERSEDED needs superseded_by: the id of the fact that replaces this one")
        if any(value is not None for value in (content, tags, metadata, temporal_class)):
            raise ValueError(
                "state SUPERSEDED closes a fact in favour of superseded_by and cannot be combined with "
                "content, tags, metadata or temporal_class; send those as a separate update"
            )
    elif superseded_by is not None:
        raise ValueError("superseded_by is accepted only together with state SUPERSEDED")

    return ParsedFactUpdate(
        content, tags, metadata, state, temporal_class, reported_date, superseded_by, expiry, notes
    )


def _parse_update_expiry(updates: Mapping[str, Any], state: Optional[FactState]) -> Tuple[Optional[Expiry], Tuple[str, ...]]:
    if "expiry" not in updates:
        return None, ()
    if state in CLOSING_STATES:
        return None, (EXPIRY_IGNORED_ON_CLOSE,) if updates["expiry"] is not None else ()
    return parse_expiry(updates["expiry"]), ()


def _parse_content(updates: Mapping[str, Any]) -> Optional[str]:
    if "content" not in updates:
        return None
    content = updates["content"]
    if not isinstance(content, str) or not content.strip():
        raise ValueError("content must be a non-empty string; leave the key out to keep the current text")
    return content


def _parse_tags(updates: Mapping[str, Any]) -> Optional[list[str]]:
    if "tags" not in updates:
        return None
    tags = updates["tags"]
    if not isinstance(tags, list) or not tags or not all(isinstance(tag, str) for tag in tags):
        raise ValueError("tags must be a non-empty list of strings; leave the key out to keep the current tags")
    return list(tags)


def _parse_superseded_by(updates: Mapping[str, Any]) -> Optional[str]:
    value = updates.get("superseded_by")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("superseded_by must be the id of the replacing fact, a non-empty string")
    return value
