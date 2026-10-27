"""The table of pass actors (Facts S4 T2a, hexagonal review H2): every row, every reader of the table, and that the S3
labels behave exactly as they did when each reader kept its own list."""

from datetime import datetime, timedelta, timezone

import pytest

from src.agents.consolidation_agent import ConsolidationAgent
from src.domain.consolidation_pass_stats import OUTCOME_BORN_DUE, ConsolidationPassStats
from src.domain.entities import ExpiryKind, FactDomain, FactEntity, FactState, FactType, TemporalClass
from src.domain.exceptions import FactBornDueError
from src.domain.fact_lifecycle import (
    CABINET_ACCEPTS, CONSOLIDATION_ACCEPTS, REVIEW_ACCEPTS, horizon_from_now, resolves_too_soon,
)
from src.domain.fact_review_basis import ReviewBasis
from src.domain.fact_review_reasons import basis_reason
from src.domain.fact_update import parse_fact_update
from src.domain.fact_update_lifecycle import finish_fact_update
from src.domain.fact_update_plan import plan_fact_update
from src.domain.fact_write_context import FactWriteContext
from src.domain.pass_actor import PassActor
from src.domain.refusal_wording import RefusalWording
from tests.support.fact_store import INDEXES_FILE, FactStore
from tests.support.fact_write import horizon
from tests.support.pass_calls import create

UTC = timezone.utc
NOW = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
SPAN = (NOW - timedelta(days=1), NOW - timedelta(hours=2))
STALE_OR_CURRENT = frozenset({FactState.STALE, FactState.CURRENT})
S3_LABELS = ("stage1", "email", "stage2a", "stage2b", "cluster", "backstop")

# actor: (model_dates, floors_due_horizon, accepts, born_due_refusal_is_loss, refusal_wording)
ROWS = {
    "stage1": (True, False, CONSOLIDATION_ACCEPTS, False, RefusalWording.STAGE1),
    "email": (True, False, CONSOLIDATION_ACCEPTS, False, RefusalWording.STAGE1),
    "stage2a": (False, True, CONSOLIDATION_ACCEPTS, True, RefusalWording.CLEANUP),
    "stage2b": (False, True, CONSOLIDATION_ACCEPTS, True, RefusalWording.CLEANUP),
    "cluster": (False, True, CONSOLIDATION_ACCEPTS, True, RefusalWording.CLEANUP),
    "backstop": (False, True, CONSOLIDATION_ACCEPTS, False, RefusalWording.CLEANUP),
    "review": (False, False, STALE_OR_CURRENT, True, RefusalWording.REVIEW),
    "owner": (False, False, STALE_OR_CURRENT, False, RefusalWording.REVIEW),
    # P2 revised (C9): a question's history fallback written on silence takes the owner's row.
    "timeout": (False, False, STALE_OR_CURRENT, False, RefusalWording.REVIEW),
}


# --- the table ----------------------------------------------------------------------------------------------------


def test_the_actors_are_exactly_the_eight_rows():
    assert {a.value for a in PassActor} == set(ROWS)
    assert all(isinstance(a.value, str) for a in PassActor)


@pytest.mark.parametrize("label", ROWS)
def test_every_property_of_every_row(label):
    actor = PassActor(label)
    model_dates, floors, accepts, loss, wording = ROWS[label]
    assert actor.model_dates is model_dates
    assert actor.floors_due_horizon is floors
    assert actor.accepts == accepts
    assert actor.born_due_refusal_is_loss is loss
    assert actor.refusal_wording == wording


def test_the_state_sets_sit_beside_each_other_in_fact_lifecycle():
    assert CONSOLIDATION_ACCEPTS == frozenset({FactState.CURRENT})
    assert REVIEW_ACCEPTS == STALE_OR_CURRENT
    assert REVIEW_ACCEPTS < CABINET_ACCEPTS


def test_the_three_wordings_differ_and_the_review_pair_shares_one():
    assert len(set(RefusalWording)) == 3 == len({w.value for w in RefusalWording})
    assert PassActor.REVIEW.refusal_wording == PassActor.OWNER.refusal_wording


@pytest.mark.parametrize("label", ["", "x", "consolidation", "Stage1", "STAGE1", "seed", "stage 2a"])
def test_a_label_that_is_no_pass_is_a_value_error(label):
    with pytest.raises(ValueError):
        PassActor(label)


# --- for_pass equals what each reader kept before -----------------------------------------------------------------


def assert_equals_the_old_write_context(got, label, timezone_name, span):
    """`ConsolidationAgent._write_context` before the table: the label, the zone, the span and `model_dates` for the
    conversation passes only."""
    assert (got.actor, got.timezone, got.span) == (label, timezone_name, span)
    assert got.model_dates is (label in ("stage1", "email"))


@pytest.mark.parametrize("label", S3_LABELS)
@pytest.mark.parametrize("zone,span", [("UTC", None), ("Europe/Madrid", SPAN)], ids=["bare", "with_span"])
def test_for_pass_equals_the_old_write_context_for_every_s3_label(label, zone, span):
    got = FactWriteContext.for_pass(PassActor(label), zone, span)
    assert_equals_the_old_write_context(got, label, zone, span)
    assert got.actor is PassActor(label) and str(got.actor) == label and f"{got.actor}" == label


@pytest.mark.parametrize("label", S3_LABELS)
def test_the_agents_write_context_is_for_pass(label):
    assert ConsolidationAgent._write_context(label, "Asia/Tokyo", SPAN) == FactWriteContext.for_pass(
        PassActor(label), "Asia/Tokyo", SPAN)
    assert_equals_the_old_write_context(ConsolidationAgent._write_context(label), label, "UTC", None)


def test_the_agents_write_context_refuses_a_label_that_is_no_pass():
    with pytest.raises(ValueError):
        ConsolidationAgent._write_context("x")


@pytest.mark.parametrize("label", ROWS)
def test_for_pass_takes_every_field_from_the_row(label):
    got = FactWriteContext.for_pass(PassActor(label), "Europe/Lisbon", SPAN)
    assert (got.actor, got.timezone, got.span, got.model_dates) == (label, "Europe/Lisbon", SPAN, ROWS[label][0])
    assert got == FactWriteContext(actor=label, timezone="Europe/Lisbon", span=SPAN)     # a plain label of a pass converts


def test_for_pass_span_defaults_to_none():
    assert FactWriteContext.for_pass(PassActor.STAGE1, "UTC").span is None


@pytest.mark.parametrize("label", S3_LABELS)
def test_the_derived_properties_equal_the_old_rules_for_every_s3_label(label):
    got = FactWriteContext.for_pass(PassActor(label), "UTC", None)
    assert got.floors_due_horizon is (not got.model_dates)          # the old `cleanup = not model_dates`
    assert got.accepts == CONSOLIDATION_ACCEPTS                    # the old, only, resolve


@pytest.mark.parametrize("label", ROWS)
def test_the_context_properties_read_the_row(label):
    got = FactWriteContext.for_pass(PassActor(label), "UTC", None)
    assert got.accepts == ROWS[label][2] and got.floors_due_horizon is ROWS[label][1]


@pytest.mark.parametrize("label", ["", "x", "seed", "Stage1", "consolidation"])
def test_a_context_with_an_actor_that_is_no_pass_is_refused_where_it_is_built(label):
    with pytest.raises(ValueError):
        FactWriteContext(actor=label, timezone="UTC", span=None)


def test_a_context_has_no_model_dates_to_pass_in():
    with pytest.raises(TypeError):
        FactWriteContext(actor="stage1", timezone="UTC", span=None, model_dates=False)     # type: ignore[call-arg]


# --- the readers of the table -------------------------------------------------------------------------------------


def stats(label):
    # Reviewer edit (S4 T6b): the stats take the actor and the turn cap only (model, effort and user id are the
    # renderer's), and typed `PassCall`s (`tests/support/pass_calls.py`).
    return ConsolidationPassStats(PassActor(label), 15)


@pytest.mark.parametrize("label", ROWS)
def test_an_unanswered_born_due_refusal_is_abandoned_exactly_when_the_row_says_it_is_a_loss(label):
    s = stats(label)
    calls = [create("the trip on the 3rd")]
    s.record_turn(calls)
    s.record_outcomes(calls, [OUTCOME_BORN_DUE])
    assert (s.born_due, s.abandoned) == (1, int(ROWS[label][3]))


@pytest.mark.parametrize("label", ["consolidation", "p", ""])
def test_the_stats_of_a_label_that_is_no_pass_are_refused(label):
    with pytest.raises(ValueError):
        ConsolidationPassStats(label, 15)      # type: ignore[arg-type]  # reviewer edit (S4 T6b): new signature


@pytest.mark.parametrize("label", ROWS)
def test_the_stats_name_the_pass_by_its_actor(label):
    # Reviewer edit (S4 T6b, plan T6b "there is no pass_label"): the label is the actor's value, read from the actor.
    s = stats(label)
    assert (s.actor, s.actor.value) == (PassActor(label), label)
    assert not hasattr(s, "pass_label")


@pytest.mark.parametrize("label", ROWS)
def test_a_missing_expiry_refusal_is_abandoned_on_every_label_the_row_does_not_change(label):
    s = stats(label)
    calls = [create("x")]
    s.record_turn(calls)
    s.record_outcomes(calls, ["expiry"])
    assert s.abandoned == 1


def old_fact():
    reported = NOW - timedelta(days=90)
    return FactEntity(
        id="f1", account_id="acc", created_by_user_id="u1", lineage_id="lin", text="meeting with Ana", type=FactType.EVENT,
        tags=["work"], metadata={}, domain=FactDomain.NETWORK, temporal_class=TemporalClass.DYNAMIC, version=2,
        reported_date=reported, last_updated=reported, expiry_kind=ExpiryKind.NONE, ttl_days=None, expires_at=None,
        vector=[0.1], tags_vector=[0.2], metadata_vector=[0.3])


def rewrite_with_a_due_horizon(label, days=14, reason=None):
    current = old_fact()
    update = parse_fact_update({"content": "meeting with Ana, rewritten", "expiry": horizon(days)})
    context = FactWriteContext.for_pass(PassActor(label), "UTC", SPAN)
    return finish_fact_update(plan_fact_update(current, update, NOW), current, update, context, NOW, reason=reason)


@pytest.mark.parametrize("label", ["stage2a", "stage2b", "cluster", "backstop"])
def test_the_update_path_floors_a_due_horizon_for_every_actor_whose_row_floors(label):
    version = rewrite_with_a_due_horizon(label).plan.version_fact
    assert version.expires_at == NOW + timedelta(days=2)


@pytest.mark.parametrize("label", ["review", "owner"])
def test_the_update_path_refuses_a_due_horizon_for_an_actor_whose_row_does_not_floor(label):
    # S4-D5: the review and the owner count a horizon from now, so 14 days is no longer due for them. One day from a
    # reported_date a whole number of days back ends exactly at now + 1 day: due counted from now, and refused (a
    # flooring row would have stored now + 2 days). The reason is the code-built one these rows must always pass.
    assert resolves_too_soon(horizon_from_now(1, old_fact().reported_date, NOW)["expires_at"], NOW)
    reason = basis_reason("rewrite", ReviewBasis("evidence", evidence_fact_id="e1234567-rest"))
    with pytest.raises(FactBornDueError, match="this pass reviews stored memory"):
        rewrite_with_a_due_horizon(label, days=1, reason=reason)


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


async def create_with_a_due_horizon(store, context):
    from tests.support.fact_kit import ACC, USER, put_raw

    put_raw(store, "old-source", "seeded", entity={
        "domain": "biographical", "reported_date": datetime.now(timezone.utc) - timedelta(days=100)})
    meta = {
        "account_id": ACC, "user_id": USER, "domain": "biographical", "temporal_class": "stable",
        "context_priority": "medium", "tags": ["t"], "metadata": {}, "type": "event",
        "expiry": horizon(30), "derived_from": "old-source",
    }
    return await store.adapter.create_fact("mother is Maria", meta, context=context)


@pytest.mark.parametrize("label", ["stage2a", "stage2b", "cluster", "backstop"])
async def test_the_create_path_floors_a_due_horizon_for_every_actor_whose_row_floors(store, label):
    res = await create_with_a_due_horizon(store, FactWriteContext.for_pass(PassActor(label), "UTC"))
    assert res["status"] == "created" and "had already passed" in res["note"]


@pytest.mark.parametrize("label", ["review", "owner"])
async def test_the_create_path_refuses_a_due_horizon_for_an_actor_whose_row_does_not_floor(store, label):
    res = await create_with_a_due_horizon(store, FactWriteContext.for_pass(PassActor(label), "UTC"))
    assert (res["status"], res["refused"]) == ("failed", "expiry")
