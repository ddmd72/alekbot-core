"""Bench 7, gate G5: `unbased_revives` counts a review's move to current that names no basis, and only that.

The history fact the owner's "ended" answer writes beside an archive is born current under the review actor
(`history of <id>`); it is no revive, so it must not trip the gate. A bare move to current still does.
"""
from datetime import datetime, timezone

from scripts.consolidation import review_bench_support as support
from src.domain.entities import FactState
from src.domain.fact_lifecycle import lifecycle_event
from src.domain.fact_review_reasons import history_reason

AT = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)
ARCHIVED_ID = "abcd1234-ab12-4cde-9f01-1a2b3c4d5e6f"


def test_the_history_fact_beside_an_archive_is_no_revive():
    entry = lifecycle_event(AT, None, FactState.CURRENT, "review", history_reason(ARCHIVED_ID))

    assert support.unbased_revives([entry]) == []


def test_a_move_to_current_without_a_basis_is_still_counted():
    bare = {"actor": "review", "from": "stale", "to": "current", "reason": "looked fine"}
    history = lifecycle_event(AT, None, FactState.CURRENT, "review", history_reason(ARCHIVED_ID))

    assert support.unbased_revives([history, bare]) == ["looked fine"]
