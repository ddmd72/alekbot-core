"""finish_fact_update in a review or owner context (Facts S4 plan §3.6, T2): revive, rewrite and close, from stale and
from current; the reason the caller passes; the one `now` of a horizon and its trail entry."""

from datetime import date, datetime, timedelta, timezone

import pytest

from src.domain.entities import ExpiryKind, FactDomain, FactEntity, FactState, FactType, TemporalClass
from src.domain.exceptions import FactBornDueError, FactExpiryError
from src.domain.fact_expiry import EXPIRY_IGNORED_ON_CLOSE, Expiry
from src.domain.fact_lifecycle import horizon_from_now
from src.domain.fact_review_basis import ReviewBasis
from src.domain.fact_review_reasons import answer_reason, basis_reason, gives_window_reason
from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_rules import STAYS_STALE_REFUSAL, confirmed_expiry, fallback_window_days
from src.domain.fact_update import ParsedFactUpdate, parse_fact_update
from src.domain.fact_update_lifecycle import FactFinish, finish_fact_update
from src.domain.fact_update_plan import plan_fact_update
from src.domain.fact_write_context import REVIEW_REPORTED_DATE_NOTE
from src.domain.pass_actor import PassActor
from tests.support.fact_write import EXPIRY_NONE, ctx, horizon, on

UTC = timezone.utc
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
REPORTED = NOW - timedelta(days=90, hours=5, minutes=17)  # not a whole number of days before NOW
STALE_SINCE = NOW - timedelta(days=12)
REVIEW_ACTORS = ["review", "owner"]
FROM_STATES = [FactState.STALE, FactState.CURRENT]

REVIVED = basis_reason("revive", ReviewBasis("evidence", evidence_fact_id="e1234567-rest"))
REWRITTEN = basis_reason("rewrite", ReviewBasis("reply", question_id="q7", quote="it moved to Lisbon"))
ARCHIVED = basis_reason("archive", ReviewBasis("date_passed", on=date(2026, 8, 1)), date(2026, 8, 1))
INVALIDATED = basis_reason("invalidate", ReviewBasis("reply", question_id="q7", quote="never true"))
SUPERSEDED = "superseded by f9"


def fact(state=FactState.STALE, **overrides):
    base = dict(
        id="f1", account_id="acc", created_by_user_id="u1", lineage_id="lin", text="meeting with Ana", type=FactType.EVENT,
        tags=["work"], metadata={"who": "Ana"}, domain=FactDomain.NETWORK, temporal_class=TemporalClass.DYNAMIC,
        version=2, reported_date=REPORTED, last_updated=REPORTED, expiry_kind=ExpiryKind.HORIZON, ttl_days=30,
        expires_at=REPORTED + timedelta(days=30), state=state,
        stale_since=STALE_SINCE if state is FactState.STALE else None,
        vector=[0.1], tags_vector=[0.2], metadata_vector=[0.3])
    base.update(overrides)
    return FactEntity(**base)


def pinned_fact(state=FactState.STALE):
    return fact(state, domain=FactDomain.AGENT_DIRECTIVE, expiry_kind=ExpiryKind.NONE, ttl_days=None, expires_at=None)


def finish(updates, current, actor="review", now=NOW, evidence_at=None, reason=REVIVED) -> FactFinish:
    update = parse_fact_update(updates)
    plan = plan_fact_update(current, update, now)
    return finish_fact_update(plan, current, update, ctx(actor), now, evidence_at=evidence_at, reason=reason)


def revive_updates(state, expiry):
    """A stale fact comes back with `state: current`; a current one waiting for an answer may send the expiry alone."""
    return {"state": "current", "expiry": expiry} if state is FactState.STALE else {"expiry": expiry}


def applied(current, fields):
    return current.model_copy(update=fields)


# --- revive ----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
@pytest.mark.parametrize("state", FROM_STATES)
def test_a_revive_is_in_place_current_with_a_horizon_counted_from_now(actor, state):
    current = fact(state)
    got = finish(revive_updates(state, horizon(30)), current, actor)
    fields = got.plan.fields
    assert got.plan.kind == "in_place" and got.notes == ()
    assert (fields["state"], fields["is_current"], fields["valid_to"], fields["stale_since"]) == (
        FactState.CURRENT, True, None, None)
    expected = horizon_from_now(30, REPORTED, NOW)
    assert {k: fields[k] for k in ("expiry_kind", "ttl_days", "expires_at")} == expected
    assert fields["expires_at"] == REPORTED + timedelta(days=fields["ttl_days"])
    assert fields["expires_at"] >= NOW + timedelta(days=30)
    assert "reported_date" not in fields


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
@pytest.mark.parametrize("state", FROM_STATES)
def test_a_revive_writes_one_trail_entry_with_the_given_reason(actor, state):
    current = fact(state, lifecycle_events=[{"at": REPORTED, "from": None, "to": "current", "actor": "stage1", "reason": "x"}])
    events = finish(revive_updates(state, horizon(30)), current, actor).plan.fields["lifecycle_events"]
    assert len(events) == 2
    assert events[-1] == {"at": NOW, "from": state.value, "to": "current", "actor": actor, "reason": REVIVED}


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
@pytest.mark.parametrize("updates", [{"expiry": horizon(30)}, {"state": "current", "expiry": horizon(30)}])
def test_a_revive_of_a_current_fact_still_writes_its_entry(actor, updates):
    fields = finish(updates, fact(FactState.CURRENT), actor).plan.fields
    event = fields["lifecycle_events"][-1]
    assert (event["from"], event["to"], event["reason"]) == ("current", "current", REVIVED)


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
@pytest.mark.parametrize("state", FROM_STATES)
def test_a_revive_without_an_expiry_is_refused(actor, state):
    with pytest.raises(FactExpiryError, match="needs `expiry`"):
        finish({"state": "current"}, fact(state), actor)


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
@pytest.mark.parametrize("updates", [{"state": "current"}, {"state": "current", "expiry": EXPIRY_NONE}])
def test_a_pinned_revive_gets_none(actor, updates):
    fields = finish(updates, pinned_fact(), actor).plan.fields
    assert (fields["expiry_kind"], fields["ttl_days"], fields["expires_at"]) == (ExpiryKind.NONE, None, None)


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
def test_a_pinned_still_true_arrives_as_none_and_stays_none(actor):
    current = pinned_fact(FactState.CURRENT)
    sent = confirmed_expiry(current, NOW, "Europe/Lisbon")
    # Reviewer edit (facts S4, T3c): the decision is an Expiry, sent as the typed update, so no parse in front.
    assert sent == Expiry(ExpiryKind.NONE)
    update = ParsedFactUpdate(expiry=sent)
    plan = plan_fact_update(current, update, NOW)
    fields = finish_fact_update(plan, current, update, ctx(actor), NOW, reason=REVIVED).plan.fields
    assert fields["expiry_kind"] is ExpiryKind.NONE and fields["expires_at"] is None


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
def test_a_pinned_revive_with_a_horizon_is_refused(actor):
    with pytest.raises(FactExpiryError, match="take {kind: none}"):
        finish({"state": "current", "expiry": horizon(30)}, pinned_fact(), actor)


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
@pytest.mark.parametrize("expiry", [on("2026-10-20"), on("2026-10-24"), horizon(1)])
def test_a_revive_is_strictly_not_born_due_with_review_wording(actor, expiry):
    # REPORTED a whole number of days back makes a one-day horizon end exactly at now + 1 day: due.
    current = fact(reported_date=NOW - timedelta(days=90))
    with pytest.raises(FactBornDueError) as refused:
        finish({"state": "current", "expiry": expiry}, current, actor)
    text = str(refused.value)
    assert "this pass reviews stored memory: give a horizon (counted from now)" in text
    assert "DISCARD" not in text and "candidate" not in text and "history" not in text


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
def test_a_revive_with_a_future_date_stores_the_date(actor):
    fields = finish({"state": "current", "expiry": on("2027-06-30")}, fact(), actor).plan.fields
    assert fields["expiry_kind"] is ExpiryKind.DATE
    assert fields["expires_at"] == datetime(2027, 6, 30, 23, 0, tzinfo=UTC)  # end of the day in Europe/Lisbon (WEST)


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
def test_a_revive_on_evidence_moves_reported_date_to_the_later_and_counts_from_it(actor):
    evidence = NOW - timedelta(days=3)
    fields = finish({"state": "current", "expiry": horizon(30)}, fact(), actor, evidence_at=evidence).plan.fields
    assert fields["reported_date"] == evidence
    assert {k: fields[k] for k in ("expiry_kind", "ttl_days", "expires_at")} == horizon_from_now(30, evidence, NOW)


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
def test_an_earlier_evidence_date_leaves_reported_date_alone(actor):
    fields = finish({"state": "current", "expiry": horizon(30)}, fact(), actor, evidence_at=REPORTED - timedelta(days=5)).plan.fields
    assert "reported_date" not in fields


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
def test_a_model_reported_date_is_not_used_and_gets_the_review_note(actor):
    got = finish({"state": "current", "expiry": horizon(30), "reported_date": (NOW - timedelta(days=1)).isoformat()},
                 fact(), actor)
    assert "reported_date" not in got.plan.fields
    assert got.notes == (REVIEW_REPORTED_DATE_NOTE,)


# --- one now ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
@pytest.mark.parametrize("state", FROM_STATES)
@pytest.mark.parametrize("now", [NOW, NOW + timedelta(microseconds=1), NOW + timedelta(hours=7, seconds=13)])
def test_the_entry_is_stamped_with_the_now_the_horizon_counted_from(actor, state, now):
    current = fact(state)
    fields = finish(revive_updates(state, horizon(30)), current, actor, now=now).plan.fields
    assert fields["lifecycle_events"][-1]["at"] == now
    assert fields["expires_at"] == horizon_from_now(30, REPORTED, now)["expires_at"]
    assert fallback_window_days(applied(current, fields)) == 60


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
@pytest.mark.parametrize("state", FROM_STATES)
def test_a_rewrite_gives_a_window_the_next_silence_doubles_exactly(actor, state):
    version = finish({"content": "meeting with Ana in Lisbon", "expiry": horizon(30)}, fact(state), actor,
                     reason=REWRITTEN).plan.version_fact
    assert version.lifecycle_events[-1]["at"] == NOW
    assert fallback_window_days(version) == 60


# --- rewrite ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
@pytest.mark.parametrize("state", FROM_STATES)
def test_a_rewrite_is_a_current_version_with_the_stored_date_and_the_review_horizon(actor, state):
    current = fact(state)
    got = finish({"content": "meeting with Ana in Lisbon", "expiry": horizon(30)}, current, actor, reason=REWRITTEN)
    version = got.plan.version_fact
    assert got.plan.kind == "version" and got.notes == ()
    assert (version.state, version.is_current, version.valid_to, version.stale_since) == (FactState.CURRENT, True, None, None)
    assert version.reported_date == REPORTED
    assert {k: getattr(version, k) for k in ("expiry_kind", "ttl_days", "expires_at")} == horizon_from_now(30, REPORTED, NOW)


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
@pytest.mark.parametrize("state", FROM_STATES)
def test_a_rewrite_entry_leads_to_current_with_the_rewrite_reason(actor, state):
    version = finish({"content": "meeting with Ana in Lisbon", "expiry": horizon(30)}, fact(state), actor,
                     reason=REWRITTEN).plan.version_fact
    entry = version.lifecycle_events[-1]
    assert (entry["from"], entry["to"], entry["actor"], entry["reason"]) == (None, "current", actor, REWRITTEN)
    assert gives_window_reason(entry["reason"])


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
def test_a_rewrite_ignores_evidence_at_and_the_model_date(actor):
    got = finish({"content": "meeting with Ana in Lisbon", "expiry": horizon(30), "reported_date": NOW.isoformat()},
                 fact(), actor, evidence_at=NOW - timedelta(days=1), reason=REWRITTEN)
    assert got.plan.version_fact.reported_date == REPORTED
    assert got.notes == (REVIEW_REPORTED_DATE_NOTE,)


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
def test_a_rewrite_is_strictly_not_born_due(actor):
    with pytest.raises(FactBornDueError, match="this pass reviews stored memory"):
        finish({"content": "meeting with Ana in Lisbon", "expiry": on("2026-10-01")}, fact(), actor, reason=REWRITTEN)


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
def test_a_rewrite_needs_an_expiry(actor):
    with pytest.raises(FactExpiryError, match="new version"):
        finish({"content": "meeting with Ana in Lisbon"}, fact(), actor, reason=REWRITTEN)


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
def test_a_rewrite_whose_reason_gives_no_window_is_refused(actor):
    with pytest.raises(ValueError, match="rewrite's trail reason"):
        finish({"content": "meeting with Ana in Lisbon", "expiry": horizon(30)}, fact(), actor, reason=ARCHIVED)


# --- archive, invalidate, supersede ----------------------------------------------------------------------------------

CLOSES = [
    ({"state": "archived"}, FactState.ARCHIVED, ARCHIVED),
    ({"state": "invalidated"}, FactState.INVALIDATED, INVALIDATED),
    ({"state": "superseded", "superseded_by": "f9"}, FactState.SUPERSEDED, SUPERSEDED),
]


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
@pytest.mark.parametrize("state", FROM_STATES)
@pytest.mark.parametrize("updates, to_state, reason", CLOSES)
def test_a_close_is_in_place_keeps_stale_since_and_the_expiry_and_records_the_reason(actor, state, updates, to_state, reason):
    current = fact(state)
    got = finish(updates, current, actor, reason=reason)
    fields = got.plan.fields
    assert got.plan.kind == "in_place"
    assert fields["state"] is to_state and fields["valid_to"] == NOW
    assert not {"stale_since", "expiry_kind", "ttl_days", "expires_at", "reported_date"} & fields.keys()
    assert applied(current, fields).stale_since == current.stale_since
    assert fields["lifecycle_events"][-1] == {"at": NOW, "from": state.value, "to": to_state.value, "actor": actor, "reason": reason}


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
def test_a_supersede_names_its_successor(actor):
    assert finish({"state": "superseded", "superseded_by": "f9"}, fact(), actor, reason=SUPERSEDED).plan.fields[
        "superseded_by"] == "f9"


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
@pytest.mark.parametrize("updates, to_state, reason", CLOSES[:2])
def test_an_expiry_sent_with_a_close_is_ignored_with_the_note(actor, updates, to_state, reason):
    got = finish({**updates, "expiry": horizon(5)}, fact(), actor, reason=reason)
    assert got.notes == (EXPIRY_IGNORED_ON_CLOSE,)
    assert "expires_at" not in got.plan.fields


@pytest.mark.parametrize("updates, reason", [({"state": "archived"}, ARCHIVED), ({"state": "invalidated"}, INVALIDATED)])
def test_an_owner_close_with_evidence_at_keeps_the_closed_reported_date(updates, reason):
    current = fact()
    got = finish(updates, current, "owner", evidence_at=NOW - timedelta(hours=1), reason=reason)
    assert "reported_date" not in got.plan.fields
    assert applied(current, got.plan.fields).reported_date == REPORTED


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
def test_a_close_that_also_changes_the_text_is_refused(actor):
    with pytest.raises(ValueError, match="in place"):
        finish({"state": "archived", "content": "meeting with Ana, over"}, fact(), actor, reason=ARCHIVED)


# --- the reason ------------------------------------------------------------------------------------------------------

OUTCOMES = [
    ({"state": "current", "expiry": horizon(30)}),
    ({"content": "meeting with Ana in Lisbon", "expiry": horizon(30)}),
    ({"state": "archived"}),
    ({"state": "invalidated"}),
    ({"state": "superseded", "superseded_by": "f9"}),
]


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
@pytest.mark.parametrize("updates", OUTCOMES)
def test_a_missing_reason_is_refused_for_every_outcome(actor, updates):
    with pytest.raises(ValueError, match="trail reason"):
        finish(updates, fact(), actor, reason=None)


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
def test_the_owners_answer_reason_reaches_the_trail(actor):
    reason = answer_reason(AnswerKind.STILL_TRUE, "q7", "digit")
    event = finish({"state": "current", "expiry": horizon(30)}, fact(), actor, reason=reason).plan.fields[
        "lifecycle_events"][-1]
    assert event["reason"] == reason == "confirmed q=q7 (digit)"


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
@pytest.mark.parametrize("updates", [{}, {"reported_date": NOW.isoformat()}, {"expiry": horizon(30)}])
def test_a_review_write_that_leaves_a_stale_fact_stale_is_refused(actor, updates):
    with pytest.raises(ValueError, match=STAYS_STALE_REFUSAL):
        finish(updates, fact(FactState.STALE), actor)


@pytest.mark.parametrize("actor", list(PassActor))
def test_an_in_place_close_takes_the_rows_closing_reason_unless_one_is_given(actor):
    """The row's `closing_reason` column: `model` for the model passes, `directive cap` for the backstop, none for the
    review and the owner, which must give theirs."""
    current = fact(FactState.CURRENT)
    if actor.closing_reason is None:
        with pytest.raises(ValueError, match="trail reason"):
            finish({"state": "invalidated"}, current, str(actor), reason=None)
    else:
        assert finish({"state": "invalidated"}, current, str(actor), reason=None).plan.fields[
            "lifecycle_events"][-1]["reason"] == actor.closing_reason
    assert finish({"state": "invalidated"}, current, str(actor), reason="given").plan.fields[
        "lifecycle_events"][-1]["reason"] == "given"


def test_the_closing_reason_column_is_the_plans():
    assert {a: a.closing_reason for a in PassActor} == {
        PassActor.STAGE1: "model", PassActor.EMAIL: "model", PassActor.STAGE2A: "model", PassActor.STAGE2B: "model",
        PassActor.CLUSTER: "model", PassActor.BACKSTOP: "directive cap", PassActor.REVIEW: None, PassActor.OWNER: None,
        PassActor.TIMEOUT: None,  # P2 revised: the owner's row, whose writes always carry their reason
    }


@pytest.mark.parametrize("actor", [a for a in PassActor if a.closing_reason is not None])
def test_a_consolidation_pass_ignores_evidence_at(actor):
    current = fact(FactState.CURRENT)
    updates = {"expiry": horizon(200)}
    assert finish(updates, current, str(actor), evidence_at=NOW, reason=None) == finish(
        updates, current, str(actor), reason=None)
