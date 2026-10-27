"""What an update does to a stored fact: a field-masked in-place write or a new version document."""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Optional
from uuid import uuid4

from .entities import FACT_SYSTEM_TAGS, FactEntity, FactState
from .fact_embedding import fact_embedding_texts
from .fact_lifecycle import closure_fields, lifecycle_fields
from .fact_update import ParsedFactUpdate


@dataclass(frozen=True)
class FactUpdatePlan:
    """`in_place`: write `fields` onto the current document. `version`: write `version_fact`, which closes the current one.

    `fields` is empty for a version (the repository closes the old document). When `vectors_stale`, `version_fact`
    carries no vectors and `embedding_texts` are the three texts to embed; otherwise the old vectors are copied.
    """

    kind: Literal["in_place", "version"]
    fields: dict[str, Any]
    version_fact: Optional[FactEntity]
    vectors_stale: bool
    embedding_texts: Optional[list[str]]


def plan_fact_update(current: FactEntity, update: ParsedFactUpdate, now: datetime) -> FactUpdatePlan:
    """Decide the write for `update` on `current`. Pure; raises ValueError for a superseded `current` or a SUPERSEDED update that is not closable."""
    if current.state is FactState.SUPERSEDED:
        raise ValueError("A superseded fact cannot be updated; resolve to its successor first")
    if update.state is FactState.SUPERSEDED:
        if update.superseded_by is None:
            raise ValueError("state SUPERSEDED needs superseded_by")
        if _carries_content(update):
            raise ValueError("state SUPERSEDED cannot be combined with content, tags, metadata or temporal_class")

    new_text, new_tags, new_metadata = _resulting_content(current, update)
    if not content_changed(current, update):
        return FactUpdatePlan("in_place", _in_place_fields(current, update, now), None, False, None)

    old_texts = fact_embedding_texts(current.text, current.tags, current.metadata)
    new_texts = fact_embedding_texts(new_text, new_tags, new_metadata)
    stale = new_texts != old_texts
    changes: dict[str, Any] = {
        "id": str(uuid4()),
        "version": (current.version or 1) + 1,
        "replaces_fact_id": current.id,
        "valid_from": now,
        "last_updated": now,
        "superseded_by": None,
        "text": new_text,
        # An order-only tag difference is not a change, so the tags of the new version follow the update's order.
        "tags": new_tags,
        "metadata": dict(new_metadata),
        "reported_date": update.reported_date if update.reported_date is not None else current.reported_date,
    }
    if update.temporal_class is not None:
        changes["temporal_class"] = update.temporal_class
    if update.as_history:
        # Metadata and context described the fact while it held (as a history fact drops them, `history_fact`).
        changes["context"] = None
    changes.update(lifecycle_fields(update.state or current.state, now))
    if stale:
        changes.update({"vector": None, "tags_vector": None, "metadata_vector": None})
    return FactUpdatePlan("version", {}, current.model_copy(update=changes, deep=True), stale, new_texts if stale else None)


def concurrent_update_refusal(
    fact_id: str, first_resolved_id: str, resolved_id: str, update: ParsedFactUpdate
) -> Optional[str]:
    """The refusal text when an update's `content` or `tags` would land on a version other than the first one resolved, else None.

    They replace whole: written over a version the model never saw, they would drop its change.
    """
    if resolved_id != first_resolved_id and (update.content is not None or update.tags is not None):
        return f"fact {fact_id} changed concurrently; search again"
    return None


def merge_source_moved_refusal(first_ids: list[str], ids: list[str]) -> Optional[str]:
    """The refusal text when the merge sources resolved to other documents than on the first attempt, else None."""
    return "source changed; search again" if ids != first_ids else None


def successor_refusal(successor_id: str, closing_id: str) -> Optional[str]:
    """The refusal text when the successor is the fact being closed (a fact cannot succeed itself), else None."""
    if successor_id == closing_id:
        return "superseded_by must name a different fact than the one being closed"
    return None


def _resulting_content(current: FactEntity, update: ParsedFactUpdate) -> tuple[str, list[str], dict[str, Any]]:
    """The text, tags and metadata `update` leaves; a history rewrite (`as_history`) leaves no metadata, so the metadata
    vector comes from the history text."""
    new_tags = _resulting_tags(current.tags, update.tags)
    new_metadata: dict[str, Any]
    if update.as_history:
        new_metadata = {}
    elif update.metadata is not None:
        new_metadata = {**current.metadata, **update.metadata}
    else:
        new_metadata = current.metadata
    new_text = update.content if update.content is not None else current.text
    return new_text, new_tags, new_metadata


def content_changed(current: FactEntity, update: ParsedFactUpdate) -> bool:
    """Whether `update` changes what the fact says (text, tags, metadata or temporal class): the one test that makes a
    version document instead of an in-place write. An order-only tag difference is not a change."""
    new_text, new_tags, new_metadata = _resulting_content(current, update)
    return (
        new_text != current.text
        or Counter(new_tags) != Counter(current.tags)
        or new_metadata != current.metadata
        or (update.temporal_class is not None and update.temporal_class != current.temporal_class)
    )


def _carries_content(update: ParsedFactUpdate) -> bool:
    return any(v is not None for v in (update.content, update.tags, update.metadata, update.temporal_class))


def _resulting_tags(stored: list[str], given: Optional[list[str]]) -> list[str]:
    """The update's tags, then the system tags the current version carries and the update omits, in stored order."""
    if given is None:
        return list(stored)
    kept = [tag for tag in stored if tag in FACT_SYSTEM_TAGS and tag not in given]
    return [*given, *kept]


def _in_place_fields(current: FactEntity, update: ParsedFactUpdate, now: datetime) -> dict[str, Any]:
    fields: dict[str, Any] = {"last_updated": now}
    # Re-applying the stored state must not move valid_to.
    if update.state is not None and update.state != current.state:
        if update.state is FactState.SUPERSEDED and update.superseded_by is not None:
            fields.update(closure_fields(update.superseded_by, now))
        else:
            fields.update(lifecycle_fields(update.state, now))
    if update.reported_date is not None:
        fields["reported_date"] = update.reported_date
    return fields
