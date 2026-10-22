"""Fact invariants over raw stored documents. Pure: no I/O, reads only the documents it is given.

Source of truth: the `FactRepository` docstring (src/ports/repository.py) and `_LIFECYCLE` (src/domain/fact_lifecycle.py).
A class name is a stable string; vector and text values are never returned, only field names and document ids.
"""
from collections import defaultdict
from datetime import datetime
from typing import Any, Iterable

from google.cloud.firestore_v1.vector import Vector
from pydantic import ValidationError

from src.domain.entities import FactEntity, FactState
from src.domain.fact_lifecycle import _LIFECYCLE

VECTOR_FIELDS = ("vector", "tags_vector", "metadata_vector")
# The fields whose own class already reports their error, so invalid_entity skips them.
_OWN_CLASS = {"account_id", "state"}


def check_fact(doc_id: str, doc: dict[str, Any]) -> list[str]:
    """Class names of the per-document invariants this stored document violates (empty = clean)."""
    found: list[str] = []
    # Identity: every writer sets the stored `id` equal to the document id.
    if doc.get("id") != doc_id:
        found.append("id_mismatch")
    # Legacy: an owner_id-only document is absent from every account-scoped read.
    if not doc.get("account_id"):
        found.append("missing_account_id")
    # A document without `state` matches no state-filtered read (B9).
    state = _state(doc)
    if doc.get("state") is None:
        found.append("missing_state")
    elif state is None:
        found.append("invalid_state")
    # `is_current` and `valid_to` follow `state` (_LIFECYCLE).
    if state is not None:
        is_current, closes = _LIFECYCLE[state]
        if doc.get("is_current") is not is_current:
            found.append("is_current_mismatch")
        if (doc.get("valid_to") is not None) != closes:
            found.append("valid_to_mismatch")
    # Reads order by `created_at`; a document without it is absent from them.
    if doc.get("created_at") is None:
        found.append("missing_created_at")
    reported = doc.get("reported_date")
    if reported is not None and not isinstance(reported, datetime):
        found.append("reported_date_not_timestamp")
    # A vector missing, empty or stored as a plain array (the repo writers wrap it in Vector) is invisible to find_nearest.
    for name in VECTOR_FIELDS:
        if not _is_vector(doc.get(name)):
            found.append(f"missing_{name}")
    found.extend(_entity_errors(doc))
    return found


def check_lineages(docs: Iterable[tuple[str, dict[str, Any]]]) -> dict[str, list[str]]:
    """Lineages with more than one `is_current=True` document: class -> ids of every document of those lineages."""
    # No "no current document" class: merge_facts gives the merged fact a NEW lineage (until FACT_LIFECYCLE S2) and
    # supersedes its sources in their own, so a lineage of only superseded documents is legitimate.
    lineages: dict[Any, list[tuple[str, bool]]] = defaultdict(list)
    for doc_id, doc in docs:
        lineage_id = doc.get("lineage_id")
        if lineage_id is not None:
            lineages[lineage_id].append((doc_id, doc.get("is_current") is True))
    found: dict[str, list[str]] = {}
    for members in lineages.values():
        if sum(1 for _, flag in members if flag) > 1:
            found.setdefault("lineage_multiple_current", []).extend(doc_id for doc_id, _ in members)
    return found


def _state(doc: dict[str, Any]) -> FactState | None:
    try:
        return FactState(doc.get("state"))
    except (ValueError, TypeError):
        return None


def _is_vector(value: Any) -> bool:
    return isinstance(value, Vector) and len(value) > 0


def _entity_errors(doc: dict[str, Any]) -> list[str]:
    """`invalid_entity:<field>` per failing field: the call that reads this document would fail."""
    view = dict(doc)
    # The repo's read-time migration (FirestoreFactRepository._migrate_ownership_fields), only on its own condition.
    if not ("account_id" in view and "created_by_user_id" in view):
        if "owner_id" in view:
            view["account_id"] = view["created_by_user_id"] = view.pop("owner_id")
        if "visibility" not in view:
            view["visibility"] = "account_shared"
        elif view["visibility"] == "private":
            view["visibility"] = "user_private"
    try:
        FactEntity.model_validate(view)
    except ValidationError as exc:
        fields = {str(err["loc"][0]) for err in exc.errors() if err["loc"]}
        return [f"invalid_entity:{name}" for name in sorted(fields - _OWN_CLASS)]
    return []
