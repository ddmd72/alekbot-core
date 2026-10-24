"""new_fact_decision and decided_attributes: "required unless pinned, none when born closed" (S4 T2d, review M1/M2)."""

from datetime import datetime, timedelta, timezone

import pytest

from src.domain.entities import ExpiryKind, FactDomain, FactEntity, FactType, TemporalClass
from src.domain.exceptions import FactBornDueError, FactExpiryError
from src.domain.fact_expiry import EXPIRY_NONE as NONE_DECISION
from src.domain.fact_expiry import EXPIRY_SHAPES, NOTHING_WRITTEN
from src.domain.fact_update import parse_fact_update
from src.domain.fact_update_lifecycle import finish_fact_update
from src.domain.fact_update_plan import plan_fact_update
from src.domain.new_fact_attributes import decided_attributes, new_fact_decision, parse_new_fact
from tests.support.fact_write import EXPIRY_NONE, ctx, horizon

UTC = timezone.utc
NOW = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
REPORTED = NOW - timedelta(hours=2)
PINNED_PIN_TEXT = "directives, principles and mindset facts take {kind: none}."


def attrs(**data):
    return parse_new_fact({"text": "a fact", **data})


def test_a_fact_born_closed_carries_no_decision():
    for state in ("archived", "invalidated"):
        assert new_fact_decision(attrs(state=state), "create_fact") is None
    # even one that was sent: it is dropped at parse time and the fact still has no decision
    assert new_fact_decision(attrs(state="archived", expiry=horizon(5)), "merge_facts") is None


def test_a_pinned_fact_without_expiry_gets_none():
    assert new_fact_decision(attrs(type="principle"), "create_fact") == NONE_DECISION
    assert new_fact_decision(attrs(domain="agent_directive"), "merge_facts") == NONE_DECISION
    assert new_fact_decision(attrs(tags=["Mindset"]), "create_fact") == NONE_DECISION


@pytest.mark.parametrize("tool", ["create_fact", "merge_facts"])
def test_an_unpinned_fact_without_expiry_is_refused_with_the_tool_in_the_text(tool):
    with pytest.raises(FactExpiryError) as refused:
        new_fact_decision(attrs(), tool)
    assert str(refused.value) == (
        f"{tool} needs `expiry` inside `fact_attributes`, decided from the content: use one of {EXPIRY_SHAPES}. "
        f"{NOTHING_WRITTEN}"
    )


def test_a_pinned_fact_with_a_horizon_gets_what_check_pin_says():
    with pytest.raises(FactExpiryError) as refused:
        new_fact_decision(attrs(type="principle", expiry=horizon(30)), "create_fact")
    assert str(refused.value) == f"{PINNED_PIN_TEXT} {NOTHING_WRITTEN}"
    assert new_fact_decision(attrs(type="principle", expiry=EXPIRY_NONE), "create_fact") == NONE_DECISION


def test_an_unpinned_fact_keeps_the_decision_it_sent():
    decision = new_fact_decision(attrs(expiry=horizon(14)), "create_fact")
    assert (decision.kind, decision.days) == (ExpiryKind.HORIZON, 14)


# --- decided_attributes ---------------------------------------------------------------------------------------------


def test_a_closed_fact_gets_the_reported_date_and_no_expiry_fields():
    closed = attrs(state="archived")
    got, note = decided_attributes(closed, None, REPORTED, NOW, ctx("stage1"))
    assert got.reported_date == REPORTED and got.expiry_fields is None and note is None


def test_a_current_fact_gets_the_computed_fields():
    current = attrs(expiry=horizon(14))
    got, note = decided_attributes(current, new_fact_decision(current, "create_fact"), REPORTED, NOW, ctx("stage1"))
    assert got.reported_date == REPORTED and note is None
    assert got.expiry_fields["expires_at"] == REPORTED + timedelta(days=14)


def test_a_due_horizon_follows_the_pass_of_the_context():
    due = attrs(expiry=horizon(1))
    decision = new_fact_decision(due, "create_fact")
    with pytest.raises(FactBornDueError):
        decided_attributes(due, decision, REPORTED, NOW, ctx("stage1"))
    got, note = decided_attributes(due, decision, REPORTED, NOW, ctx("stage2a"))
    assert got.expiry_fields["expires_at"] == NOW + timedelta(days=2) and note is not None


# --- the update path keeps its own text -----------------------------------------------------------------------------


def stored_fact():
    return FactEntity(
        id="f1", account_id="acc", created_by_user_id="u1", lineage_id="lin", text="meeting with Ana", type=FactType.EVENT,
        tags=["work"], metadata={}, domain=FactDomain.NETWORK, temporal_class=TemporalClass.DYNAMIC, version=2,
        reported_date=REPORTED, last_updated=REPORTED, expiry_kind=ExpiryKind.HORIZON, ttl_days=30,
        expires_at=REPORTED + timedelta(days=30), vector=[0.1], tags_vector=[0.2], metadata_vector=[0.3],
    )


def test_the_update_path_refuses_a_version_without_expiry_with_its_own_text():
    current = stored_fact()
    update = parse_fact_update({"content": "meeting with Ana moved"})
    with pytest.raises(FactExpiryError) as refused:
        finish_fact_update(plan_fact_update(current, update, NOW), current, update, ctx("stage1"), NOW)
    assert str(refused.value) == (
        "this update sends `text`, which makes a new version of the fact; "
        f"a new version needs `expiry` inside `updates`, decided again from the new text: use one of {EXPIRY_SHAPES}. "
        f"{NOTHING_WRITTEN}"
    )
