"""P2-1: a fact whose date passed is rewritten as history (`as_history`): the parse, the typed refusal, the writer, the
version it becomes and its trail entry."""
from dataclasses import fields
from datetime import date, datetime, timedelta, timezone

import pytest

from src.domain.entities import ContextPriority, ExpiryKind, FactDomain, FactEntity, FactState, FactType, TemporalClass
from src.domain.fact_embedding import fact_embedding_texts
from src.domain.fact_expiry import EXPIRY_NONE, Expiry
from src.domain.fact_review_basis import ReviewBasis
from src.domain.fact_review_reasons import basis_reason
from src.domain.fact_update import (
    AS_HISTORY_SAME_TEXT, AS_HISTORY_SHAPE_REFUSAL, ParsedFactUpdate, parse_fact_update, typed_update_refusal,
)
from src.domain.fact_update_lifecycle import finish_fact_update
from src.domain.fact_update_plan import content_changed, plan_fact_update
from tests.support.fact_write import ctx, horizon

UTC = timezone.utc
NOW = datetime(2026, 11, 22, 9, 0, tzinfo=UTC)
REPORTED = datetime(2026, 9, 1, 8, 30, tzinfo=UTC)
CREATED = datetime(2026, 9, 1, 8, 31, tzinfo=UTC)
PLAN = "A talk at a conference is planned for Nov 18-20, 2026."
HISTORY = "A talk at a conference took place Nov 18-20, 2026."
HISTORY_UPDATES = {"state": "CURRENT", "content": HISTORY, "expiry": {"kind": "none"}, "as_history": True}
DATE_PASSED = basis_reason("rewrite", ReviewBasis("date_passed", on=date(2026, 11, 20)), date(2026, 11, 20), as_history=True)
ON_EVIDENCE = basis_reason("rewrite", ReviewBasis("evidence", evidence_fact_id="e1234567-rest"), as_history=True)
ON_REPLY = basis_reason("rewrite", ReviewBasis("reply", question_id="q7", quote="I went"), as_history=True)


def plan_fact(state=FactState.STALE, **over) -> FactEntity:
    base = dict(
        id="f1", account_id="acc", created_by_user_id="u1", lineage_id="lin", text=PLAN, type=FactType.EVENT,
        tags=["travel"], metadata={"city": "Berlin"}, context="said while packing",
        domain=FactDomain.WORK, temporal_class=TemporalClass.DYNAMIC, context_priority=ContextPriority.HIGH,
        version=1, reported_date=REPORTED, created_at=CREATED, last_updated=REPORTED, expiry_kind=ExpiryKind.DATE,
        expires_at=datetime(2026, 11, 21, tzinfo=UTC), state=state,
        stale_since=NOW - timedelta(days=1) if state is FactState.STALE else None,
        lifecycle_events=[{"at": NOW - timedelta(days=1), "from": "current", "to": "stale", "actor": "janitor"}],
        vector=[0.1], tags_vector=[0.2], metadata_vector=[0.3],
    )
    base.update(over)
    return FactEntity(**base)


def history_finish(current, actor="review", reason=DATE_PASSED, updates=HISTORY_UPDATES):
    update = parse_fact_update(updates)
    plan = plan_fact_update(current, update, NOW)
    return finish_fact_update(plan, current, update, ctx(actor), NOW, reason=reason)


# ------------------------------------------------------------------------------------------------ the parse

def test_as_history_is_the_last_field_and_defaults_to_false():
    assert fields(ParsedFactUpdate)[-1].name == "as_history"
    assert ParsedFactUpdate().as_history is False
    assert parse_fact_update({"state": "CURRENT"}).as_history is False


@pytest.mark.parametrize("raw, expected", [(True, True), (False, False), ("true", True), ("false", False), (" TRUE ", True)])
def test_as_history_is_read_as_a_bool_or_its_text(raw, expected):
    updates = {**HISTORY_UPDATES, "as_history": raw}
    assert parse_fact_update(updates).as_history is expected


@pytest.mark.parametrize("raw", [None, 1, "yes", [], {}])
def test_anything_else_is_refused_with_the_argument_named(raw):
    with pytest.raises(ValueError, match="^as_history must be true or false$"):
        parse_fact_update({**HISTORY_UPDATES, "as_history": raw})


def test_a_history_rewrite_parses_with_its_text_and_none():
    parsed = parse_fact_update(HISTORY_UPDATES)
    assert parsed == ParsedFactUpdate(state=FactState.CURRENT, content=HISTORY, expiry=EXPIRY_NONE, as_history=True)


@pytest.mark.parametrize("updates", [
    {"content": HISTORY, "expiry": {"kind": "none"}},                                  # no state
    {"state": "ARCHIVED", "content": HISTORY},                                         # a close
    {"state": "CURRENT", "expiry": {"kind": "none"}},                                  # no content
    {"state": "CURRENT", "content": HISTORY},                                          # no expiry
    {"state": "CURRENT", "content": HISTORY, "expiry": {"kind": "horizon", "days": 30}},  # an expiry other than none
])
def test_the_shape_is_current_a_content_and_none_or_it_is_refused(updates):
    with pytest.raises(ValueError) as refused:
        parse_fact_update({**updates, "as_history": True})
    assert str(refused.value) == AS_HISTORY_SHAPE_REFUSAL
    assert AS_HISTORY_SHAPE_REFUSAL == (
        "as_history rewrites the fact as history and needs updates {state: CURRENT, content: <the history text>, "
        "expiry: {kind: none}}"
    )


def test_the_typed_path_holds_code_to_the_same_shape():
    good = ParsedFactUpdate(state=FactState.CURRENT, content=HISTORY, expiry=EXPIRY_NONE, as_history=True)
    assert typed_update_refusal(good) is None
    for bad in (
        ParsedFactUpdate(state=FactState.CURRENT, content=HISTORY, as_history=True),
        ParsedFactUpdate(state=FactState.CURRENT, expiry=EXPIRY_NONE, as_history=True),
        ParsedFactUpdate(state=FactState.ARCHIVED, content=HISTORY, expiry=EXPIRY_NONE, as_history=True),
        ParsedFactUpdate(state=FactState.CURRENT, content=HISTORY, expiry=Expiry(ExpiryKind.HORIZON, days=30), as_history=True),
    ):
        assert typed_update_refusal(bad) == AS_HISTORY_SHAPE_REFUSAL
    # Without the flag the same updates are ordinary ones.
    assert typed_update_refusal(ParsedFactUpdate(state=FactState.CURRENT, content=HISTORY)) is None


# ------------------------------------------------------------------------------------------------ the plan

def test_the_plan_clears_metadata_before_the_embedding_texts_and_context_on_the_version():
    current = plan_fact(metadata={"city": "Berlin"})
    update = parse_fact_update(HISTORY_UPDATES)
    plan = plan_fact_update(current, update, NOW)
    assert plan.kind == "version" and plan.vectors_stale
    assert plan.embedding_texts == fact_embedding_texts(HISTORY, ["travel"], {})
    assert plan.embedding_texts[2] == HISTORY  # the metadata vector comes from the history text
    assert plan.version_fact.metadata == {} and plan.version_fact.context is None


def test_clearing_the_metadata_alone_is_a_content_change():
    current = plan_fact()
    same_text = ParsedFactUpdate(state=FactState.CURRENT, content=PLAN, expiry=EXPIRY_NONE, as_history=True)
    assert content_changed(current, same_text)
    assert not content_changed(current, ParsedFactUpdate(state=FactState.CURRENT, content=PLAN, expiry=EXPIRY_NONE))


def test_an_ordinary_rewrite_keeps_metadata_and_context():
    current = plan_fact()
    update = parse_fact_update({"state": "CURRENT", "content": HISTORY, "expiry": horizon(30)})
    version = plan_fact_update(current, update, NOW).version_fact
    assert version.metadata == current.metadata and version.context == current.context


# ------------------------------------------------------------------------------------------------ the finish

@pytest.mark.parametrize("actor", ["review", "owner", "timeout"])
@pytest.mark.parametrize("state", [FactState.STALE, FactState.CURRENT])
def test_a_history_rewrite_is_an_archival_version_with_the_stored_dates_and_one_trail_entry(actor, state):
    current = plan_fact(state)
    got = history_finish(current, actor)
    version = got.plan.version_fact
    assert got.plan.kind == "version"
    assert version.id != current.id and version.lineage_id == current.lineage_id and version.version == 2
    assert version.replaces_fact_id == current.id
    assert version.text == HISTORY and version.state is FactState.CURRENT and version.stale_since is None
    assert version.context_priority is ContextPriority.ARCHIVAL
    assert version.expiry_kind is ExpiryKind.NONE and version.expires_at is None
    assert version.metadata == {} and version.context is None
    assert version.reported_date == REPORTED and version.created_at == CREATED
    assert got.plan.vectors_stale and got.plan.embedding_texts[2] == HISTORY
    assert version.lifecycle_events[:-1] == current.lifecycle_events
    entry = version.lifecycle_events[-1]
    assert (entry["from"], entry["to"], entry["actor"], entry["reason"]) == (None, "current", actor, DATE_PASSED)


@pytest.mark.parametrize("reason", [DATE_PASSED, ON_EVIDENCE, ON_REPLY])
def test_every_history_reason_is_a_rewrite_reason_the_version_accepts(reason):
    version = history_finish(plan_fact(), reason=reason).plan.version_fact
    assert version.lifecycle_events[-1]["reason"] == reason


def test_an_ordinary_rewrite_keeps_its_priority():
    current = plan_fact()
    updates = {"state": "CURRENT", "content": HISTORY, "expiry": horizon(30)}
    reason = basis_reason("rewrite", ReviewBasis("reply", question_id="q7", quote="moved"))
    version = history_finish(current, updates=updates, reason=reason).plan.version_fact
    assert version.context_priority is ContextPriority.HIGH


@pytest.mark.parametrize("actor", ["stage1", "email", "stage2a", "stage2b", "cluster", "backstop"])
def test_any_other_writer_is_refused_and_nothing_is_written(actor):
    with pytest.raises(ValueError) as refused:
        history_finish(plan_fact(FactState.CURRENT), actor)
    assert str(refused.value) == (
        f"as_history: only the review, the owner and a timeout rewrite a fact as history, not {actor}; nothing was written"
    )


def test_the_same_text_is_refused_by_the_review_version():
    current = plan_fact()
    with pytest.raises(ValueError) as refused:
        history_finish(current, updates={**HISTORY_UPDATES, "content": PLAN})
    assert str(refused.value) == AS_HISTORY_SAME_TEXT
    assert AS_HISTORY_SAME_TEXT == "as_history needs content: the history text, different from the stored text"


def test_the_same_text_on_a_fact_with_no_metadata_is_no_rewrite_and_is_refused():
    current = plan_fact(metadata={}, context=None)
    with pytest.raises(ValueError) as refused:
        history_finish(current, updates={**HISTORY_UPDATES, "content": PLAN})
    assert str(refused.value) == AS_HISTORY_SAME_TEXT


# ------------------------------------------------------------------------------------------------ the timeout writer

def test_the_timeout_writer_is_its_own_label_on_the_owners_row():
    from src.domain.pass_actor import PassActor
    assert PassActor("timeout") is PassActor.TIMEOUT and str(PassActor.TIMEOUT) == "timeout"
    for prop in ("model_dates", "floors_due_horizon", "accepts", "born_due_refusal_is_loss", "one_result_per_fact",
                 "refusals_counter", "refusal_wording", "closing_reason"):
        assert getattr(PassActor.TIMEOUT, prop) == getattr(PassActor.OWNER, prop), prop
    assert FactState.STALE in PassActor.TIMEOUT.accepts


def test_an_ordinary_metadata_update_still_merges():
    current = plan_fact()
    update = parse_fact_update({"metadata": {"seat": "12A"}, "expiry": horizon(30)})
    assert plan_fact_update(current, update, NOW).version_fact.metadata == {**current.metadata, "seat": "12A"}
