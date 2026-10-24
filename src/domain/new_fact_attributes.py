"""The validated attributes of a fact about to be created."""

import dataclasses
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Mapping, Optional, Tuple

from pydantic import TypeAdapter, ValidationError

from .entities import ContextPriority, FactDomain, FactState, FactType, TemporalClass
from .fact_argument_parsing import parse_enum, parse_reported_date
from .exceptions import FactExpiryError
from .fact_expiry import EXPIRY_IGNORED_ON_CLOSE, EXPIRY_SHAPES, NOTHING_WRITTEN, Expiry, parse_expiry
from .fact_lifecycle import CLOSING_STATES, expiry_pinned, required_decision
from .fact_write_context import FactWriteContext, decide_expiry

# Unknown type names fall back to EVENT, as the write service always did.
_TYPE_BY_NAME = {member.value: member for member in FactType}
_OPTIONAL_STR = TypeAdapter(Optional[str])


@dataclass(frozen=True)
class NewFactAttributes:
    """`reported_date` None means "now", decided where the fact is built. `tags` is the caller's own list.

    `expiry`: the parsed decision, None when not sent or dropped because the fact is born closed (see `notes`). The model's
    `ttl_days` is not read. `derived_from`: ids of the facts this one is made from; a code argument, never stored.
    `expiry_fields`: the computed `expiry_kind`/`ttl_days`/`expires_at`, set by the caller once it knows the date (with
    `dataclasses.replace`); None writes no decision.
    """

    content: str
    tags: list[str]
    type: FactType
    metadata: dict[str, Any]
    domain: Optional[FactDomain]
    temporal_class: Optional[TemporalClass]
    state: FactState
    context_priority: ContextPriority
    context: Optional[str]
    reported_date: Optional[datetime]
    expiry: Optional[Expiry] = None
    derived_from: Tuple[str, ...] = ()
    expiry_fields: Optional[Dict[str, Any]] = None
    notes: Tuple[str, ...] = ()


def parse_new_fact(fact_data: Mapping[str, Any], timezone_name: Optional[str] = None) -> NewFactAttributes:
    """Validate one fact dict before any embedding or write. Raises ValueError with a message the model can act on.

    `content` wins over `text`. State SUPERSEDED is refused: a document must not be born closed with no successor.
    State STALE is refused: only the system marks a fact stale. An `expiry` is optional here (the caller decides when
    it is required); one sent with a state that closes the fact is dropped, with a note. A non-dict `metadata` is ignored.
    `timezone_name` (the owner's zone) lets `reported_date` be the consolidator's own conversation stamp.
    """
    content = fact_data.get("content") or fact_data.get("text")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("A fact needs non-empty content")

    tags = fact_data.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise ValueError("tags must be a list of strings")

    type_name = fact_data.get("type") or "event"
    if not isinstance(type_name, str):
        raise ValueError("type must be a string")
    metadata = fact_data.get("metadata")

    state = parse_enum(FactState, "state", fact_data["state"]) if fact_data.get("state") else FactState.CURRENT
    if state is FactState.SUPERSEDED:
        raise ValueError("A new fact cannot have state SUPERSEDED; supersede an existing fact through an update or a merge")
    if state is FactState.STALE:
        raise ValueError("A new fact cannot have state STALE: only the system marks a fact stale")

    expiry: Optional[Expiry] = None
    notes: Tuple[str, ...] = ()
    if "expiry" in fact_data:
        if state in CLOSING_STATES:
            notes = (EXPIRY_IGNORED_ON_CLOSE,) if fact_data["expiry"] is not None else ()
        else:
            expiry = parse_expiry(fact_data["expiry"])

    return NewFactAttributes(
        content=content,
        tags=tags,
        type=_TYPE_BY_NAME.get(type_name.lower(), FactType.EVENT),
        metadata=metadata if isinstance(metadata, dict) else {},
        domain=parse_enum(FactDomain, "domain", fact_data["domain"]) if fact_data.get("domain") else None,
        temporal_class=(
            parse_enum(TemporalClass, "temporal_class", fact_data["temporal_class"])
            if fact_data.get("temporal_class")
            else None
        ),
        state=state,
        context_priority=(
            parse_enum(ContextPriority, "context_priority", fact_data["context_priority"])
            if fact_data.get("context_priority")
            else ContextPriority.MEDIUM
        ),
        context=_validated("context", _OPTIONAL_STR, fact_data.get("context")),
        reported_date=parse_reported_date(fact_data.get("reported_date"), timezone_name),
        expiry=expiry,
        derived_from=_parse_derived_from(fact_data.get("derived_from")),
        notes=notes,
    )


def new_fact_decision(attrs: NewFactAttributes, tool: str) -> Optional[Expiry]:
    """The expiry decision a new fact is written with: None for a fact born closed (it carries none). A current fact
    needs one inside `fact_attributes` unless it is pinned (a pinned one gets `none`)."""
    if attrs.state in CLOSING_STATES:
        return None
    return required_decision(
        attrs.expiry,
        expiry_pinned(attrs.domain, attrs.type, attrs.tags),
        lambda: FactExpiryError(
            f"{tool} needs `expiry` inside `fact_attributes`, decided from the content: use one of {EXPIRY_SHAPES}. "
            f"{NOTHING_WRITTEN}"
        ),
    )


def decided_attributes(
    attrs: NewFactAttributes, decision: Optional[Expiry], reported: datetime, now: datetime, context: FactWriteContext
) -> Tuple[NewFactAttributes, Optional[str]]:
    """`attrs` with the resolved `reported_date` and, for a current fact, the computed expiry fields (never born due)."""
    if decision is None:
        return dataclasses.replace(attrs, reported_date=reported), None
    fields, note = decide_expiry(decision, reported, now, context)
    return dataclasses.replace(attrs, reported_date=reported, expiry_fields=fields), note


def _parse_derived_from(raw: Any) -> Tuple[str, ...]:
    if raw is None:
        return ()
    items = [raw] if isinstance(raw, str) else raw
    if not isinstance(items, (list, tuple)) or not all(isinstance(i, str) and i.strip() for i in items):
        raise ValueError("derived_from must be a fact id or a list of fact ids")
    return tuple(dict.fromkeys(i.strip() for i in items))


def _validated(name: str, adapter: TypeAdapter[Any], value: Any) -> Any:
    try:
        return adapter.validate_python(value)
    except ValidationError:
        raise ValueError(f"Invalid {name} {value!r}") from None
