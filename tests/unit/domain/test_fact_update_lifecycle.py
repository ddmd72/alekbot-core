"""finish_fact_update: the expiry, reported_date and trail rules applied to a planned update (Facts S3, plan §3.4-§3.8)."""

from datetime import datetime, timedelta, timezone

import pytest

from src.domain.entities import (
    ExpiryKind, FactDomain, FactEntity, FactState, FactType, TemporalClass)
from src.domain.exceptions import FactExpiryError
from src.domain.fact_expiry import EXPIRY_IGNORED_ON_CLOSE, NOTHING_WRITTEN
from src.domain.fact_lifecycle import LIFECYCLE_EVENTS_CAP, lifecycle_event
from src.domain.fact_update import parse_fact_update
from src.domain.fact_update_lifecycle import FactFinish, finish_fact_update
from src.domain.fact_update_plan import plan_fact_update
from tests.support.fact_write import EXPIRY_NONE, ctx, horizon, on

UTC = timezone.utc
NOW = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)  # 11:00 in Europe/Paris
STORED_REPORTED = NOW - timedelta(days=3)
SPAN = (NOW - timedelta(days=1), NOW - timedelta(hours=2))
STAGE1 = ctx("stage1", span=SPAN)
STAGE2A = ctx("stage2a", span=SPAN)
GENERIC_PIN_TEXT = "expiry is required for this fact"


def fact(**overrides):
    base = dict(
        id="f1", account_id="acc", created_by_user_id="u1", lineage_id="lin", text="meeting with Ana", type=FactType.EVENT,
        tags=["work", "consolidated"], metadata={"who": "Ana"}, domain=FactDomain.NETWORK,
        temporal_class=TemporalClass.DYNAMIC, version=2, reported_date=STORED_REPORTED, last_updated=STORED_REPORTED,
        expiry_kind=ExpiryKind.HORIZON, ttl_days=30, expires_at=STORED_REPORTED + timedelta(days=30),
        vector=[0.1], tags_vector=[0.2], metadata_vector=[0.3])
    base.update(overrides)
    return FactEntity(**base)


def finish(updates, current=None, context=STAGE1, now=NOW) -> FactFinish:
    current = current or fact()
    update = parse_fact_update(updates)
    return finish_fact_update(plan_fact_update(current, update, now), current, update, context, now)


# --- a version needs the decision again -----------------------------------------------------------------------------


def test_a_version_stores_the_decision_computed_from_the_resolved_reported_date():
    got = finish({"content": "meeting with Ana moved", "expiry": horizon(14), "reported_date": SPAN[0].isoformat()})
    version = got.plan.version_fact
    assert got.plan.kind == "version" and got.notes == ()
    assert version.reported_date == SPAN[0]
    assert (version.expiry_kind, version.ttl_days) == (ExpiryKind.HORIZON, 14)
    assert version.expires_at == version.reported_date + timedelta(days=14)


def test_a_version_without_a_model_date_counts_the_horizon_from_the_span_end():
    version = finish({"content": "meeting with Ana moved", "expiry": horizon(20)}).plan.version_fact
    assert version.reported_date == SPAN[1]
    assert version.expires_at == SPAN[1] + timedelta(days=20)


def test_a_version_with_a_date_decision_ends_with_that_day_in_the_owners_calendar():
    version = finish({"content": "meeting with Ana is on Friday", "expiry": on("2026-10-30")}).plan.version_fact
    assert version.expiry_kind is ExpiryKind.DATE and version.ttl_days is None
    assert version.expires_at == datetime(2026, 10, 30, 23, 0, tzinfo=UTC)  # midnight on the 31st in Europe/Paris (CET)


def test_a_version_with_expiry_none_clears_the_old_decision():
    version = finish({"tags": ["work", "calendar"], "expiry": EXPIRY_NONE}).plan.version_fact
    assert (version.expiry_kind, version.ttl_days, version.expires_at) == (ExpiryKind.NONE, None, None)


@pytest.mark.parametrize(
    "updates, sent",
    [
        ({"content": "x"}, "`text`"),
        ({"tags": ["a"]}, "`tags`"),
        ({"metadata": {"a": 1}}, "`metadata`"),
        ({"temporal_class": "STABLE"}, "`temporal_class`"),
        ({"content": "x", "tags": ["a"], "metadata": {"a": 1}}, "`text`, `tags`, `metadata`"),
    ],
)
def test_a_version_without_expiry_gets_the_call_specific_text_never_the_generic_one(updates, sent):
    with pytest.raises(FactExpiryError) as refused:
        finish(updates)
    text = str(refused.value)
    assert f"this update sends {sent}, which makes a new version of the fact" in text
    assert "needs `expiry` inside `updates`" in text and "decided again from the new text" in text
    assert "{kind: none}" in text and text.endswith(NOTHING_WRITTEN)
    assert GENERIC_PIN_TEXT not in text


@pytest.mark.parametrize(
    "overrides",
    [
        {"domain": FactDomain.AGENT_DIRECTIVE},
        {"type": FactType.PRINCIPLE},
        {"tags": ["mindset", "consolidated"]},
    ],
)
def test_a_pinned_fact_version_without_expiry_gets_none(overrides):
    version = finish({"content": "never judge by surface parsing"}, current=fact(**overrides)).plan.version_fact
    assert (version.expiry_kind, version.expires_at) == (ExpiryKind.NONE, None)


def test_a_pinned_fact_version_refuses_a_date_or_horizon():
    with pytest.raises(FactExpiryError, match="directives, principles and mindset facts take"):
        finish({"content": "x", "expiry": horizon(9)}, current=fact(type=FactType.PRINCIPLE))
    assert finish({"content": "x", "expiry": EXPIRY_NONE}, current=fact(type=FactType.PRINCIPLE)).plan.kind == "version"


def test_the_pin_is_judged_on_the_resulting_tags():
    with pytest.raises(FactExpiryError, match="mindset"):
        finish({"tags": ["mindset"], "expiry": horizon(9)})
    got = finish({"tags": ["work"], "expiry": horizon(9)}, current=fact(tags=["mindset", "consolidated"]))
    assert got.plan.version_fact.expiry_kind is ExpiryKind.HORIZON


def test_a_malformed_expiry_never_reaches_finish_it_fails_at_parse():
    with pytest.raises(FactExpiryError, match="Invalid expiry"):
        finish({"content": "x", "expiry": {"kind": "soon"}})


# --- born-due on a version -----------------------------------------------------------------------------------------


def test_stage1_refuses_a_horizon_that_is_over_already():
    with pytest.raises(FactExpiryError, match="counted from when the owner said it"):
        finish({"content": "x", "expiry": horizon(1), "reported_date": SPAN[0].isoformat()}, context=STAGE1)


def test_a_cleanup_pass_floors_a_due_horizon_to_two_days_with_a_note():
    old = fact(reported_date=NOW - timedelta(days=90), expires_at=None)
    got = finish({"content": "meeting with Ana, rewritten", "expiry": horizon(14)}, current=old, context=STAGE2A)
    version = got.plan.version_fact
    assert version.reported_date == NOW - timedelta(days=90)
    assert (version.expiry_kind, version.ttl_days, version.expires_at) == (ExpiryKind.HORIZON, 14, NOW + timedelta(days=2))
    assert len(got.notes) == 1 and "had already passed; the fact goes to review on 2026-10-25" in got.notes[0]


def test_a_past_date_is_refused_on_both_pass_kinds():
    for context in (STAGE1, STAGE2A):
        with pytest.raises(FactExpiryError, match="is past"):
            finish({"content": "x", "expiry": on("2026-10-01")}, context=context)


def test_a_cleanup_version_keeps_the_stored_reported_date_and_names_the_ignored_one():
    got = finish(
        {"content": "meeting with Ana, rewritten", "expiry": horizon(60), "reported_date": NOW.isoformat()},
        context=STAGE2A,
    )
    assert got.plan.version_fact.reported_date == STORED_REPORTED
    assert any("reported_date is kept by the system in this pass (2026-10-20)" in n for n in got.notes)


def test_a_conversation_pass_never_moves_reported_date_back_on_a_version():
    current = fact(reported_date=SPAN[1] + timedelta(hours=1))
    version = finish({"content": "x", "expiry": horizon(30), "reported_date": SPAN[0].isoformat()}, current=current).plan.version_fact
    assert version.reported_date == SPAN[1] + timedelta(hours=1)


# --- a version that closes the fact --------------------------------------------------------------------------------


def test_a_version_that_archives_needs_no_expiry_and_keeps_the_copied_decision():
    current = fact()
    got = finish({"content": "meeting with Ana, cancelled", "state": "ARCHIVED"}, current=current)
    version = got.plan.version_fact
    assert version.state is FactState.ARCHIVED
    assert (version.expiry_kind, version.ttl_days, version.expires_at) == (current.expiry_kind, 30, current.expires_at)


def test_an_expiry_sent_with_a_closing_state_is_ignored_and_the_parse_note_is_passed_on():
    got = finish({"content": "x", "state": "INVALIDATED", "expiry": on("2026-10-01")})
    assert got.plan.version_fact.expires_at == fact().expires_at
    assert got.notes == (EXPIRY_IGNORED_ON_CLOSE,)


# --- the trail ------------------------------------------------------------------------------------------------------


def test_a_version_starts_with_its_predecessors_events_plus_its_own():
    old = lifecycle_event(NOW - timedelta(days=9), FactState.STALE, FactState.CURRENT, "review", "revived")
    got = finish({"content": "x", "expiry": horizon(30)}, current=fact(lifecycle_events=[old]), context=STAGE2A)
    first, own = got.plan.version_fact.lifecycle_events
    assert first == old
    assert own == {
        "at": NOW, "from": None, "to": "current", "actor": "stage2a", "reason": "version 3 of f1",
    }


def test_the_trail_keeps_the_newest_twenty():
    events = [lifecycle_event(NOW - timedelta(days=i), None, FactState.CURRENT, "x", f"r{i}") for i in range(30, 10, -1)]
    assert len(events) == LIFECYCLE_EVENTS_CAP
    got = finish({"content": "x", "expiry": horizon(30)}, current=fact(lifecycle_events=events))
    kept = got.plan.version_fact.lifecycle_events
    assert len(kept) == LIFECYCLE_EVENTS_CAP and kept[0] == events[1] and kept[-1]["reason"] == "version 3 of f1"
    assert len(fact(lifecycle_events=events).lifecycle_events) == LIFECYCLE_EVENTS_CAP  # the stored list is not mutated


def test_the_plans_other_decisions_are_left_alone():
    current = fact()
    update = parse_fact_update({"content": "meeting with Ana moved", "expiry": horizon(14)})
    plan = plan_fact_update(current, update, NOW)
    got = finish_fact_update(plan, current, update, STAGE1, NOW).plan
    assert (got.kind, got.vectors_stale, got.embedding_texts, got.fields) == (
        plan.kind, plan.vectors_stale, plan.embedding_texts, plan.fields)
    assert got.version_fact.id == plan.version_fact.id and got.version_fact.text == plan.version_fact.text


# --- in place -------------------------------------------------------------------------------------------------------


def test_an_expiry_only_update_recomputes_the_decision_in_place():
    got = finish({"expiry": horizon(45)})
    assert got.plan.kind == "in_place"
    assert got.plan.fields["expiry_kind"] is ExpiryKind.HORIZON and got.plan.fields["ttl_days"] == 45
    assert got.plan.fields["expires_at"] == STORED_REPORTED + timedelta(days=45)
    assert "reported_date" not in got.plan.fields and "lifecycle_events" not in got.plan.fields


def test_an_expiry_only_update_counts_from_a_re_mentioned_date():
    mentioned = SPAN[1] - timedelta(hours=1)
    got = finish({"expiry": horizon(45), "reported_date": mentioned.isoformat()})
    assert got.plan.fields["reported_date"] == mentioned
    assert got.plan.fields["expires_at"] == mentioned + timedelta(days=45)


def test_an_expiry_only_update_is_born_due_checked():
    with pytest.raises(FactExpiryError, match="is past"):
        finish({"expiry": on("2026-10-22")})
    floored = finish({"expiry": horizon(5)}, current=fact(reported_date=NOW - timedelta(days=40)), context=STAGE2A)
    assert floored.plan.fields["expires_at"] == NOW + timedelta(days=2) and len(floored.notes) == 1


def test_a_pinned_fact_in_place_takes_none_and_refuses_the_rest():
    pinned = fact(domain=FactDomain.AGENT_DIRECTIVE)
    assert finish({"expiry": EXPIRY_NONE}, current=pinned).plan.fields["expiry_kind"] is ExpiryKind.NONE
    with pytest.raises(FactExpiryError, match="take {kind: none}"):
        finish({"expiry": on("2026-12-01")}, current=pinned)


def test_the_re_applied_current_state_with_an_expiry_still_recomputes_without_an_event():
    got = finish({"state": "CURRENT", "expiry": horizon(45)})
    assert got.plan.fields["ttl_days"] == 45 and "lifecycle_events" not in got.plan.fields


def test_a_re_mention_moves_a_horizon_to_the_new_date_plus_its_days():
    mentioned = SPAN[1] - timedelta(hours=1)
    got = finish({"reported_date": mentioned.isoformat()})
    assert got.plan.fields["reported_date"] == mentioned
    assert got.plan.fields["expires_at"] == mentioned + timedelta(days=30)
    assert "expiry_kind" not in got.plan.fields and "ttl_days" not in got.plan.fields


def test_a_re_mention_from_an_old_batch_never_moves_a_floored_fact_earlier():
    floored = fact(reported_date=NOW - timedelta(days=90), ttl_days=14, expires_at=NOW + timedelta(days=2))
    old_span = (NOW - timedelta(days=88), NOW - timedelta(days=87))
    old_batch = ctx("stage1", span=old_span)
    got = finish({"reported_date": (NOW - timedelta(days=87, hours=1)).isoformat()}, current=floored, context=old_batch)
    assert got.plan.fields["reported_date"] == NOW - timedelta(days=87, hours=1)
    assert "expires_at" not in got.plan.fields


def test_a_re_mention_of_a_date_or_none_fact_leaves_expires_at_alone():
    for current in (
        fact(expiry_kind=ExpiryKind.DATE, ttl_days=None, expires_at=NOW + timedelta(days=5)),
        fact(expiry_kind=ExpiryKind.NONE, ttl_days=None, expires_at=None),
        fact(expiry_kind=None, ttl_days=113, expires_at=None),
    ):
        got = finish({"reported_date": SPAN[1].isoformat()}, current=current)
        assert got.plan.fields["reported_date"] == SPAN[1] and "expires_at" not in got.plan.fields


def test_a_horizon_fact_without_an_expires_at_gets_one_on_a_re_mention():
    current = fact(expires_at=None)
    got = finish({"reported_date": SPAN[1].isoformat()}, current=current)
    assert got.plan.fields["expires_at"] == SPAN[1] + timedelta(days=30)


def test_an_older_model_date_is_dropped_from_the_in_place_write():
    current = fact(reported_date=SPAN[1] + timedelta(hours=1))
    got = finish({"reported_date": SPAN[0].isoformat()}, current=current)
    assert "reported_date" not in got.plan.fields and "expires_at" not in got.plan.fields


def test_a_cleanup_pass_never_writes_reported_date_in_place_and_says_so():
    got = finish({"reported_date": NOW.isoformat()}, context=STAGE2A)
    assert "reported_date" not in got.plan.fields and "expires_at" not in got.plan.fields
    assert len(got.notes) == 1 and "kept by the system" in got.notes[0]


def test_a_state_change_in_place_keeps_the_decision_and_adds_an_event():
    got = finish({"state": "ARCHIVED"}, context=STAGE2A)
    fields = got.plan.fields
    assert fields["state"] is FactState.ARCHIVED
    assert not {"expiry_kind", "ttl_days", "expires_at"} & set(fields)
    assert fields["lifecycle_events"] == [
        {"at": NOW, "from": "current", "to": "archived", "actor": "stage2a", "reason": "model"}]


def test_an_in_place_supersede_names_the_successor_in_the_event():
    got = finish({"state": "SUPERSEDED", "superseded_by": "f9"})
    assert got.plan.fields["superseded_by"] == "f9"
    assert got.plan.fields["lifecycle_events"][-1]["reason"] == "superseded by f9"
    assert got.plan.fields["lifecycle_events"][-1]["to"] == "superseded"


def test_an_expiry_sent_with_an_in_place_closing_state_is_ignored_with_the_note():
    got = finish({"state": "ARCHIVED", "expiry": horizon(5)})
    assert not {"expiry_kind", "ttl_days", "expires_at"} & set(got.plan.fields)
    assert got.notes == (EXPIRY_IGNORED_ON_CLOSE,)


def test_an_empty_update_changes_nothing_but_last_updated():
    got = finish({})
    assert got.plan.fields == {"last_updated": NOW} and got.notes == ()


def test_a_naive_now_is_read_as_utc():
    current, update = fact(), parse_fact_update({"expiry": horizon(45)})
    plan = plan_fact_update(current, update, NOW)
    naive = finish_fact_update(plan, current, update, STAGE1, NOW.replace(tzinfo=None))
    assert naive == finish_fact_update(plan, current, update, STAGE1, NOW)
