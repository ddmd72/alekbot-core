"""The validated attributes of a fact about to be created."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional

from pydantic import TypeAdapter, ValidationError

from .entities import ContextPriority, FactDomain, FactState, FactType, TemporalClass
from .fact_argument_parsing import parse_enum, parse_reported_date

# Unknown type names fall back to EVENT, as the write service always did.
_TYPE_BY_NAME = {member.value: member for member in FactType}
_OPTIONAL_INT = TypeAdapter(Optional[int])
_OPTIONAL_STR = TypeAdapter(Optional[str])


@dataclass(frozen=True)
class NewFactAttributes:
    """`reported_date` None means "now", decided where the fact is built. `tags` is the caller's own list."""

    content: str
    tags: list[str]
    type: FactType
    metadata: dict[str, Any]
    domain: Optional[FactDomain]
    temporal_class: Optional[TemporalClass]
    state: FactState
    context_priority: ContextPriority
    ttl_days: Optional[int]
    context: Optional[str]
    reported_date: Optional[datetime]


def parse_new_fact(fact_data: Mapping[str, Any]) -> NewFactAttributes:
    """Validate one fact dict before any embedding or write. Raises ValueError with a message the model can act on.

    `content` wins over `text`. State SUPERSEDED is refused: a document must not be born closed with no successor.
    A non-dict `metadata` is ignored.
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
        ttl_days=_validated("ttl_days", _OPTIONAL_INT, fact_data.get("ttl_days")),
        context=_validated("context", _OPTIONAL_STR, fact_data.get("context")),
        reported_date=parse_reported_date(fact_data.get("reported_date")),
    )


def _validated(name: str, adapter: TypeAdapter[Any], value: Any) -> Any:
    try:
        return adapter.validate_python(value)
    except ValidationError:
        raise ValueError(f"Invalid {name} {value!r}") from None
