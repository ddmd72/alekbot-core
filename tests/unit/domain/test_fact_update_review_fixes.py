"""T2 review fixes (Facts S4): the supersede reason has one builder (M2), the in-place refusal reads "an" (L1), a rewrite
takes only a rewrite reason (L2), and every cleanup-worded row floors (L4)."""

from datetime import datetime, timedelta, timezone

import pytest

from src.domain.entities import ExpiryKind, FactDomain, FactEntity, FactState, FactType, TemporalClass
from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_basis import ReviewBasis
from src.domain.fact_review_reasons import (
    answer_reason, basis_reason, gives_window_reason, is_revive_reason, is_rewrite_reason, superseded_reason,
)
from src.domain.fact_update import parse_fact_update
from src.domain.fact_update_lifecycle import finish_fact_update
from src.domain.fact_update_plan import plan_fact_update
from src.domain.pass_actor import PassActor
from src.domain.refusal_wording import RefusalWording
from tests.support.fact_write import ctx, horizon

UTC = timezone.utc
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
REPORTED = NOW - timedelta(days=90, hours=5)
REVIEW_ACTORS = ["review", "owner"]
REWRITTEN = basis_reason("rewrite", ReviewBasis("evidence", evidence_fact_id="e1234567-rest"))


def fact(state=FactState.STALE):
    return FactEntity(
        id="f1", account_id="acc", created_by_user_id="u1", lineage_id="lin", text="meeting with Ana", type=FactType.EVENT,
        tags=["work"], metadata={}, domain=FactDomain.NETWORK, temporal_class=TemporalClass.DYNAMIC, version=2,
        reported_date=REPORTED, last_updated=REPORTED, expiry_kind=ExpiryKind.HORIZON, ttl_days=30,
        expires_at=REPORTED + timedelta(days=30), state=state,
        stale_since=NOW - timedelta(days=5) if state is FactState.STALE else None,
        vector=[0.1], tags_vector=[0.2], metadata_vector=[0.3])


def finish(updates, current, actor, reason):
    update = parse_fact_update(updates)
    return finish_fact_update(plan_fact_update(current, update, NOW), current, update, ctx(actor), NOW, reason=reason)


# --- M2: one builder for the supersede reason ------------------------------------------------------------------------


def test_the_supersede_reason_keeps_the_s3_text():
    assert superseded_reason("f9") == "superseded by f9"


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
@pytest.mark.parametrize("state", [FactState.STALE, FactState.CURRENT])
def test_a_review_supersede_records_the_built_reason(actor, state):
    fields = finish({"state": "superseded", "superseded_by": "f9"}, fact(state), actor, superseded_reason("f9")).plan.fields
    assert fields["lifecycle_events"][-1]["reason"] == superseded_reason("f9")


@pytest.mark.parametrize("actor", [a for a in PassActor if a.closing_reason is not None])
def test_a_consolidation_supersede_without_a_reason_gets_the_built_reason(actor):
    fields = finish({"state": "superseded", "superseded_by": "f9"}, fact(FactState.CURRENT), str(actor), None).plan.fields
    assert fields["lifecycle_events"][-1]["reason"] == superseded_reason("f9")


# --- L1: grammar ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
@pytest.mark.parametrize("state, action", [("archived", "archive"), ("invalidated", "invalidate")])
def test_a_close_with_content_is_refused_with_an(actor, state, action):
    with pytest.raises(ValueError, match=f"this pass makes an {action} in place"):
        finish({"state": state, "content": "meeting with Ana, over"}, fact(), actor, "reason")


# --- L2: a rewrite takes a rewrite reason only -----------------------------------------------------------------------


def test_is_rewrite_reason_recognises_only_rewrites():
    revive = basis_reason("revive", ReviewBasis("evidence", evidence_fact_id="e1234567"))
    assert is_rewrite_reason(REWRITTEN)
    assert not is_rewrite_reason(revive) and not is_rewrite_reason(answer_reason(AnswerKind.STILL_TRUE, "q1", "digit"))
    assert not is_rewrite_reason(None) and not is_rewrite_reason(42)
    assert gives_window_reason(REWRITTEN) and not is_revive_reason(REWRITTEN)


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
@pytest.mark.parametrize("reason", [
    basis_reason("revive", ReviewBasis("evidence", evidence_fact_id="e1234567")),
    basis_reason("revive", ReviewBasis("reply", question_id="q7", quote="yes")),
    answer_reason(AnswerKind.STILL_TRUE, "q7", "digit"),
])
def test_a_revive_reason_on_a_rewrite_is_refused(actor, reason):
    assert gives_window_reason(reason)  # the old guard let these through
    with pytest.raises(ValueError, match="rewrite's trail reason must be a rewrite reason"):
        finish({"content": "meeting with Ana in Lisbon", "expiry": horizon(30)}, fact(), actor, reason)


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
def test_a_rewrite_reason_on_a_rewrite_is_written(actor):
    version = finish({"content": "meeting with Ana in Lisbon", "expiry": horizon(30)}, fact(), actor, REWRITTEN).plan.version_fact
    assert version.lifecycle_events[-1]["reason"] == REWRITTEN


# --- L4: the cleanup horizon refusal stays unreachable ---------------------------------------------------------------


def test_every_row_worded_cleanup_floors_a_due_horizon():
    cleanup = [a for a in PassActor if a.refusal_wording is RefusalWording.CLEANUP]
    assert cleanup and all(a.floors_due_horizon for a in cleanup)
