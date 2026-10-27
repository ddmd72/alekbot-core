"""A decided update of one fact: what the model's `update_fact` arguments parse to, and what code builds itself."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Mapping, Optional, Tuple

from .entities import ExpiryKind, FactState, TemporalClass
from .fact_argument_parsing import parse_enum, parse_reported_date
from .fact_expiry import EXPIRY_IGNORED_ON_CLOSE, Expiry, expiry_refusal, parse_expiry
from .fact_lifecycle import CLOSING_STATES


@dataclass(frozen=True)
class ParsedFactUpdate:
    """The domain's decided update of one fact: only the parts it changes are set; None means "leave as it is".

    `parse_fact_update` builds one from the model's arguments; code builds one directly and checks it with
    `typed_update_refusal`, which holds it to the same rules. `expiry` is the decision (None = none sent, or dropped
    because the update closes the fact); `notes` are lines for the result about what was not used. `as_history`: the
    rewrite states what became of the fact (P2-1): a new version with the history text, `none`, priority archival, no
    metadata and no context; its shape is state CURRENT, a `content` and expiry `none` (`AS_HISTORY_SHAPE_REFUSAL`).
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
    as_history: bool = False


# The shape of a history rewrite, and its content-change rule (checked where the stored text is known).
AS_HISTORY_SHAPE_REFUSAL: Final[str] = (
    "as_history rewrites the fact as history and needs updates {state: CURRENT, content: <the history text>, "
    "expiry: {kind: none}}"
)
AS_HISTORY_SAME_TEXT: Final[str] = "as_history needs content: the history text, different from the stored text"


def parse_fact_update(updates: Mapping[str, Any], timezone_name: Optional[str] = None) -> ParsedFactUpdate:
    """Validate `updates` before any embedding or write. Raises ValueError with a message the model can act on.

    Unknown keys are ignored. A non-dict `metadata` is ignored. Empty `content`, `tags: null` and `tags: []` are
    refused: an empty field never destroys data. State STALE is refused (only the system marks a fact stale). An
    `expiry` sent with a closing state is dropped, with a note; any other `expiry` must parse (FactExpiryError).
    `timezone_name` (the owner's zone) lets `reported_date` be the consolidator's own conversation stamp. `as_history`
    is a bool, `"true"` or `"false"`, or absent (False), and holds to its shape (`AS_HISTORY_SHAPE_REFUSAL`).
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
        raise ValueError(_STALE_REFUSAL)
    reported_date = parse_reported_date(updates.get("reported_date"), timezone_name)
    superseded_by = _parse_superseded_by(updates)
    expiry, notes = _parse_update_expiry(updates, state)
    parsed = ParsedFactUpdate(
        content=content, tags=tags, metadata=metadata, state=state, temporal_class=temporal_class,
        reported_date=reported_date, superseded_by=superseded_by, expiry=expiry, notes=notes,
        as_history=_parse_as_history(updates),
    )
    refusal = _supersede_refusal(parsed) or _history_shape_refusal(parsed)
    if refusal is not None:
        raise ValueError(refusal)
    return parsed


def typed_update_refusal(update: ParsedFactUpdate) -> Optional[str]:
    """Why a decided `update` may not be written, or None: the rules `parse_fact_update` holds the model's arguments to,
    for an update code built. Empty `content` or `tags`, state STALE, an empty `superseded_by`, an `expiry` out of the
    parser's bounds (`expiry_refusal`), a SUPERSEDED state without its successor or with a content change, and a
    successor without SUPERSEDED are refused, each with the parser's own words; so is an `as_history` update outside its
    shape."""
    if update.content is not None and not _content_ok(update.content):
        return _CONTENT_REFUSAL
    if update.tags is not None and not _tags_ok(update.tags):
        return _TAGS_REFUSAL
    if update.state is FactState.STALE:
        return _STALE_REFUSAL
    if update.superseded_by is not None and not _successor_ok(update.superseded_by):
        return _SUCCESSOR_REFUSAL
    if update.expiry is not None:
        refusal = expiry_refusal(update.expiry)
        if refusal is not None:
            return refusal
    return _supersede_refusal(update) or _history_shape_refusal(update)


_STALE_REFUSAL = "only the system marks a fact stale; to retire a fact use ARCHIVED, INVALIDATED or SUPERSEDED"
_CONTENT_REFUSAL = "content must be a non-empty string; leave the key out to keep the current text"
_TAGS_REFUSAL = "tags must be a non-empty list of strings; leave the key out to keep the current tags"
_SUCCESSOR_REFUSAL = "superseded_by must be the id of the replacing fact, a non-empty string"


def _supersede_refusal(update: ParsedFactUpdate) -> Optional[str]:
    """State SUPERSEDED needs its successor and nothing that changes what the fact says; a successor needs SUPERSEDED."""
    if update.state is FactState.SUPERSEDED:
        if update.superseded_by is None:
            return "state SUPERSEDED needs superseded_by: the id of the fact that replaces this one"
        if any(value is not None for value in (update.content, update.tags, update.metadata, update.temporal_class)):
            return (
                "state SUPERSEDED closes a fact in favour of superseded_by and cannot be combined with "
                "content, tags, metadata or temporal_class; send those as a separate update"
            )
    elif update.superseded_by is not None:
        return "superseded_by is accepted only together with state SUPERSEDED"
    return None


def _history_shape_refusal(update: ParsedFactUpdate) -> Optional[str]:
    """A history rewrite is state CURRENT, a `content` and expiry `none`; anything else is refused."""
    if not update.as_history:
        return None
    if (
        update.state is FactState.CURRENT
        and update.content is not None
        and update.expiry is not None
        and update.expiry.kind is ExpiryKind.NONE
    ):
        return None
    return AS_HISTORY_SHAPE_REFUSAL


def _parse_as_history(updates: Mapping[str, Any]) -> bool:
    if "as_history" not in updates:
        return False
    value = updates["as_history"]
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise ValueError("as_history must be true or false")


def _content_ok(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _tags_ok(value: object) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(tag, str) for tag in value)


def _successor_ok(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


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
    if not _content_ok(content):
        raise ValueError(_CONTENT_REFUSAL)
    text: str = content
    return text


def _parse_tags(updates: Mapping[str, Any]) -> Optional[list[str]]:
    if "tags" not in updates:
        return None
    tags = updates["tags"]
    if not _tags_ok(tags):
        raise ValueError(_TAGS_REFUSAL)
    return list(tags)


def _parse_superseded_by(updates: Mapping[str, Any]) -> Optional[str]:
    value = updates.get("superseded_by")
    if value is None:
        return None
    if not _successor_ok(value):
        raise ValueError(_SUCCESSOR_REFUSAL)
    successor: str = value
    return successor
