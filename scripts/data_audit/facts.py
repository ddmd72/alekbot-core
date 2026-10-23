"""Fact invariants over raw stored documents. Pure: no I/O, reads only the documents it is given.

Source of truth: the `FactRepository` docstring (src/ports/repository.py) and `_LIFECYCLE` (src/domain/fact_lifecycle.py).
A class name is a stable string; vector and text values are never returned, only field names and document ids.
"""
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from google.cloud.firestore_v1.vector import Vector
from pydantic import ValidationError

from src.domain.entities import FactEntity, FactState
from src.domain.fact_lifecycle import _LIFECYCLE

VECTOR_FIELDS = ("vector", "tags_vector", "metadata_vector")
# The fields whose own class already reports their error, so invalid_entity skips them.
_OWN_CLASS = {"account_id", "state"}
# Classes reported beside the violations but not violations: documents closed before S2, when no `superseded_by` existed.
COUNTED_CLASSES = frozenset({"superseded_without_successor_pre_s2"})


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
    # No "no current document" class: merge_facts writes the merged fact under a NEW lineage and closes its sources in
    # their own, so a lineage of only superseded documents is legitimate.
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


def check_version_chain(
    docs: Iterable[tuple[str, dict[str, Any]]], s2_cutoff: Optional[datetime] = None
) -> dict[str, list[str]]:
    """Class -> ids of the documents whose version pointers are wrong (`superseded_by`, `replaces_fact_id`, `version`).

    A target is looked up among the given documents, so a partial read (`--limit`) reports false dangling pointers.
    A document closed without `superseded_by` is a violation only when it was last written at or after `s2_cutoff`;
    earlier ones (and all of them without a cutoff) go to a counted class in `COUNTED_CLASSES`.
    """
    members = list(docs)
    by_id = dict(members)
    found: dict[str, list[str]] = {}

    def flag(name: str, doc_id: str) -> None:
        found.setdefault(name, []).append(doc_id)

    for doc_id, doc in members:
        successor_id = doc.get("superseded_by")
        if successor_id:
            successor = by_id.get(successor_id)
            if successor is None or successor.get("account_id") != doc.get("account_id"):
                flag("superseded_by_dangling", doc_id)
        predecessor_id = doc.get("replaces_fact_id")
        if predecessor_id:
            predecessor = by_id.get(predecessor_id)
            if predecessor is None:
                flag("replaces_target_missing", doc_id)
            else:
                if predecessor.get("lineage_id") != doc.get("lineage_id"):
                    flag("replaces_lineage_mismatch", doc_id)
                if not _is_next_version(predecessor.get("version"), doc.get("version")):
                    flag("replaces_version_mismatch", doc_id)
                if predecessor.get("superseded_by") != doc_id:
                    flag("replaces_not_pointing_back", doc_id)
        if _state(doc) is FactState.SUPERSEDED and not successor_id:
            late = s2_cutoff is not None and _written_at_or_after(doc.get("last_updated"), s2_cutoff)
            flag("superseded_without_successor" if late else "superseded_without_successor_pre_s2", doc_id)
    return {**found, **_duplicate_versions(members)}


def check_across(
    docs: Iterable[tuple[str, dict[str, Any]]], s2_cutoff: Optional[datetime] = None
) -> dict[str, list[str]]:
    """Every cross-document class: `check_lineages` and `check_version_chain`."""
    members = list(docs)
    return {**check_lineages(members), **check_version_chain(members, s2_cutoff)}


def _is_next_version(older: Any, newer: Any) -> bool:
    return isinstance(older, int) and isinstance(newer, int) and newer == older + 1


def _written_at_or_after(moment: Any, cutoff: datetime) -> bool:
    """False for an absent or non-timestamp value (`invalid_entity` reports that); a naive timestamp reads as UTC."""
    if not isinstance(moment, datetime):
        return False
    return _aware(moment) >= _aware(cutoff)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _duplicate_versions(members: list[tuple[str, dict[str, Any]]]) -> dict[str, list[str]]:
    """`version_not_unique`: every document that shares a (lineage, version) pair with another one."""
    seen: dict[tuple[Any, Any], list[str]] = defaultdict(list)
    for doc_id, doc in members:
        if doc.get("lineage_id") is not None and isinstance(doc.get("version"), int):
            seen[(doc["lineage_id"], doc["version"])].append(doc_id)
    ids = [doc_id for shared in seen.values() if len(shared) > 1 for doc_id in shared]
    return {"version_not_unique": ids} if ids else {}


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
