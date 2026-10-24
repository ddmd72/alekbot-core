"""The review's fallbacks, revive alert, interval, janitor trigger, pool and queue alert (Facts S4 · T1b, plan §3.8, §3.11, §3.12)."""
from datetime import datetime, timedelta, timezone

import pytest

from src.domain import fact_review_rules as rules
from src.domain.entities import ExpiryKind, FactState
from src.domain.fact_lifecycle import IN_PLACE_FIELDS, LIFECYCLE_EVENTS_CAP
from src.domain.fact_review_question import QuestionStatus
from src.domain.fact_review_rules import (
    digest_due,
    fallback_fields,
    fallback_window_days,
    interval_elapsed,
    pool,
    queue_alert,
    review_due,
    review_interval,
    revive_alert_due,
    revives_in_30_days,
)
from src.domain.settings import DEFAULT_FACT_REVIEW_INTERVAL_DAYS
from src.domain.user import UserBotConfig
from tests.support.review_rule_builders import NOW, fact, question, state

Q = QuestionStatus
DAY = timedelta(days=1)


# ------------------------------------------------------------------------------------------------ fallback_window_days

@pytest.mark.parametrize(
    "window_days,expected",
    [(0, 14), (3, 14), (7, 14), (8, 16), (14, 28), (100, 200), (182, 364), (183, 365), (400, 365), (-5, 14)],
)
def test_the_window_doubles_between_14_and_365_days(window_days, expected):
    reported = NOW - timedelta(days=500)
    f = fact(reported_date=reported, expires_at=reported + timedelta(days=window_days))
    assert fallback_window_days(f) == expected


def test_a_fact_with_no_expiry_comes_back_for_14_days():
    assert fallback_window_days(fact(expiry_kind=ExpiryKind.NONE, ttl_days=None, expires_at=None)) == 14


def test_the_window_counts_whole_days_of_expires_at_minus_reported_date():
    reported = NOW - timedelta(days=500)
    f = fact(reported_date=reported, expires_at=reported + timedelta(days=20, hours=23))
    assert fallback_window_days(f) == 40


# ------------------------------------------------------------------------------------------------ fallback_fields: revive

def test_a_revive_fallback_brings_the_fact_back_for_twice_its_window_counted_from_now():
    f = fact()  # reported 60 days ago, a 14-day window: back for 28 days from now
    fields = fallback_fields(f, question(), NOW, "timeout")
    assert fields["state"] is FactState.CURRENT and fields["is_current"] is True and fields["valid_to"] is None
    assert fields["stale_since"] is None
    assert fields["expiry_kind"] is ExpiryKind.HORIZON
    assert fields["ttl_days"] == 88  # counted from reported_date (60 days ago) + 28
    assert fields["expires_at"] == f.reported_date + timedelta(days=88) == NOW + timedelta(days=28)
    assert fields["last_updated"] == NOW


def test_the_stored_horizon_keeps_expires_at_equal_to_reported_date_plus_ttl_days():
    f = fact(reported_date=NOW - timedelta(days=60, hours=5, minutes=7))
    fields = fallback_fields(f, question(), NOW, "timeout")
    assert fields["expires_at"] == f.reported_date + timedelta(days=fields["ttl_days"])
    assert NOW + timedelta(days=28) <= fields["expires_at"] < NOW + timedelta(days=29)


def test_a_revive_leaves_reported_date_and_the_content_alone():
    fields = fallback_fields(fact(), question(), NOW, "timeout")
    assert "reported_date" not in fields and "text" not in fields and "version" not in fields


def test_every_fallback_field_is_one_an_in_place_write_may_carry():
    for fb in ("revive", "archive"):
        for actor in ("timeout", "owner"):
            assert set(fallback_fields(fact(), question(fallback=fb), NOW, actor)) <= IN_PLACE_FIELDS


def test_a_revive_of_a_queued_current_fact_is_the_same_write_and_records_current_to_current():
    f = fact(state=FactState.CURRENT, stale_since=None)
    fields = fallback_fields(f, question(), NOW, "timeout")
    assert fields["state"] is FactState.CURRENT and fields["ttl_days"] == 88
    assert fields["lifecycle_events"][-1]["from"] == "current"


def test_every_silence_doubles_what_the_last_one_gave_up_to_365():
    f, q, now, given = fact(), question(), NOW, []
    for _ in range(7):
        fields = fallback_fields(f, q, now, "timeout")
        given.append((fields["expires_at"] - now).days)
        f = f.model_copy(update={**fields, "state": FactState.STALE})
        now = fields["expires_at"]  # falls due again at once
    assert given == [28, 56, 112, 224, 365, 365, 365]


def test_the_window_runs_from_the_last_revive_not_from_reported_date():
    old = fact(reported_date=NOW - timedelta(days=300), expires_at=NOW + timedelta(days=28), ttl_days=328)
    assert fallback_window_days(old) == 365  # no revive known: 2 * 328
    revived = old.model_copy(update={"lifecycle_events": [_event("no answer to q=q1: revive", at=NOW)]})
    assert fallback_window_days(revived) == 56  # 2 * (28 days given at the revive)


def test_the_latest_revive_counts_and_an_earlier_one_does_not():
    events = [_event("revived on a", at=NOW - 80 * DAY), _event("confirmed q=q9 (digit)", at=NOW - 20 * DAY)]
    f = fact(reported_date=NOW - 400 * DAY, expires_at=NOW + 20 * DAY, lifecycle_events=events)
    assert fallback_window_days(f) == 80  # 2 * 40


def test_entries_that_are_not_revives_or_cannot_be_read_do_not_move_the_start():
    events = [
        _event("queued for the owner q=q1", at=NOW - 2 * DAY),
        _event("expired 2026-10-20", at=NOW - DAY, to="stale", frm="current"),
        {"to": "current", "reason": "revived on a"},
        _event("revived on b", at="garbage"),
    ]
    f = fact(reported_date=NOW - 100 * DAY, expires_at=NOW + 20 * DAY, lifecycle_events=events)
    assert fallback_window_days(f) == 240  # 2 * 120 from reported_date


def test_a_revive_older_than_reported_date_is_ignored():
    events = [_event("revived on a", at=NOW - 500 * DAY)]
    f = fact(reported_date=NOW - 100 * DAY, expires_at=NOW + 20 * DAY, lifecycle_events=events)
    assert fallback_window_days(f) == 240


def test_a_horizon_shorter_than_the_ttl_gap_still_never_goes_below_fourteen_days_from_now():
    f = fact(reported_date=NOW - timedelta(days=2), expires_at=NOW - timedelta(days=1))
    fields = fallback_fields(f, question(), NOW, "timeout")
    assert fields["expires_at"] >= NOW + timedelta(days=14)


def test_the_trail_entry_of_a_timeout_revive_names_the_question_and_the_actor():
    f = fact(lifecycle_events=[{"at": NOW - 10 * DAY, "from": "current", "to": "stale", "actor": "janitor", "reason": "expired"}])
    events = fallback_fields(f, question(question_id="qx"), NOW, "timeout")["lifecycle_events"]
    assert len(events) == 2 and events[0]["actor"] == "janitor"
    assert events[1] == {"at": NOW, "from": "stale", "to": "current", "actor": "timeout", "reason": "no answer to q=qx: revive"}


def test_the_trail_entry_of_an_owner_who_does_not_remember_has_its_own_reason():
    events = fallback_fields(fact(), question(question_id="qx"), NOW, "owner")["lifecycle_events"]
    assert events[-1]["actor"] == "owner" and events[-1]["reason"] == "did not remember q=qx"
    assert events[-1]["to"] == "current"


def test_the_trail_keeps_its_newest_twenty_entries():
    old = [{"at": NOW - (30 - n) * DAY, "from": "a", "to": "b", "actor": "x", "reason": f"r{n}"} for n in range(LIFECYCLE_EVENTS_CAP)]
    events = fallback_fields(fact(lifecycle_events=old), question(), NOW, "timeout")["lifecycle_events"]
    assert len(events) == LIFECYCLE_EVENTS_CAP and events[0]["reason"] == "r1" and events[-1]["actor"] == "timeout"


# ------------------------------------------------------------------------------------------------ fallback_fields: archive

def test_an_archive_fallback_archives_the_fact_and_keeps_its_stale_since_and_expiry():
    f = fact()
    fields = fallback_fields(f, question(fallback="archive"), NOW, "timeout")
    assert fields["state"] is FactState.ARCHIVED and fields["is_current"] is True and fields["valid_to"] == NOW
    assert fields["last_updated"] == NOW
    for kept in ("stale_since", "expiry_kind", "ttl_days", "expires_at", "reported_date"):
        assert kept not in fields


def test_the_archive_trail_entry_says_so():
    events = fallback_fields(fact(), question(fallback="archive", question_id="qa"), NOW, "timeout")["lifecycle_events"]
    assert events[-1] == {"at": NOW, "from": "stale", "to": "archived", "actor": "timeout", "reason": "no answer to q=qa: archive"}


def test_an_owner_who_does_not_remember_gets_the_archive_fallback_too():
    fields = fallback_fields(fact(), question(fallback="archive", fallback_date=None, noise=True), NOW, "owner")
    assert fields["state"] is FactState.ARCHIVED and fields["lifecycle_events"][-1]["reason"].startswith("did not remember")


@pytest.mark.parametrize("bad", [FactState.ARCHIVED, FactState.INVALIDATED, FactState.SUPERSEDED])
def test_a_fallback_refuses_a_fact_that_is_already_closed(bad):
    with pytest.raises(ValueError):
        fallback_fields(fact(state=bad), question(), NOW, "timeout")


# ------------------------------------------------------------------------------------------------ revives_in_30_days

def _event(reason, at=None, to="current", frm="stale"):
    return {"at": at if at is not None else NOW - 5 * DAY, "from": frm, "to": to, "actor": "review", "reason": reason}


@pytest.mark.parametrize(
    "reason",
    ["revived on abcd1234", "revived on reply q=q1", "confirmed q=q1 (digit)", "no answer to q=q1: revive", "did not remember q=q1"],
)
def test_each_kind_of_revive_counts(reason):
    assert revives_in_30_days([_event(reason)], NOW) == 1


@pytest.mark.parametrize(
    "event",
    [
        _event("queued for the owner q=q1"),
        _event("rewritten on abcd1234"),
        _event("version 2 of f1", frm=None),
        _event("expired 2026-10-01T00:00:00+00:00", to="stale", frm="current"),
        _event("no answer to q=q1: archive", to="archived"),
        _event("archived on reply q=q1", to="archived"),
        _event("confirmed q=q1 (digit)", to="invalidated"),
    ],
)
def test_other_trail_entries_do_not_count(event):
    assert revives_in_30_days([event], NOW) == 0


def test_the_window_is_thirty_days_inclusive():
    on_the_edge = _event("revived on a", at=NOW - timedelta(days=30))
    just_outside = _event("revived on b", at=NOW - timedelta(days=30, seconds=1))
    assert revives_in_30_days([on_the_edge, just_outside], NOW) == 1


def test_an_entry_in_the_future_counts():
    assert revives_in_30_days([_event("revived on a", at=NOW + DAY)], NOW) == 1


def test_a_naive_or_iso_string_time_is_read_as_utc():
    naive = _event("revived on a", at=datetime(2026, 10, 20, 12, 0))
    text = _event("revived on b", at="2026-10-20T12:00:00Z")
    assert revives_in_30_days([naive, text], NOW) == 2


@pytest.mark.parametrize(
    "broken",
    [{}, {"to": "current"}, {"to": "current", "reason": "revived on a"}, _event("revived on a", at="not a time"),
     _event("revived on a", at=None) | {"at": object()}, {"at": NOW, "to": "current", "reason": 7}],
)
def test_an_entry_that_cannot_be_read_counts_for_nothing(broken):
    assert revives_in_30_days([broken], NOW) == 0


def test_the_alert_is_due_at_three_revives_in_the_window_not_two():
    two = [_event("revived on a"), _event("confirmed q=1 (digit)")]
    three = [*two, _event("no answer to q=2: revive")]
    old = _event("revived on z", at=NOW - 45 * DAY)
    assert not revive_alert_due(two, NOW) and not revive_alert_due([*two, old], NOW)
    assert revive_alert_due(three, NOW) and revive_alert_due([*three, _event("revived on d")], NOW)


# ------------------------------------------------------------------------------------------------ review_interval

def test_the_system_default_is_one_day():
    assert DEFAULT_FACT_REVIEW_INTERVAL_DAYS == 1 and UserBotConfig().fact_review_interval_days is None


def test_the_users_interval_wins_then_the_accounts_then_the_systems():
    user, account = UserBotConfig(fact_review_interval_days=3), UserBotConfig(fact_review_interval_days=7)
    assert review_interval(user, account) == 3
    assert review_interval(UserBotConfig(), account) == 7
    assert review_interval(UserBotConfig(), UserBotConfig()) == DEFAULT_FACT_REVIEW_INTERVAL_DAYS
    assert review_interval(None, None) == 1
    assert review_interval(user, None) == 3
    assert review_interval(None, account) == 7


@pytest.mark.parametrize("bad", [0, -1, -30])
def test_an_interval_below_one_is_no_setting(bad):
    assert review_interval(UserBotConfig(fact_review_interval_days=bad), UserBotConfig(fact_review_interval_days=5)) == 5
    assert review_interval(UserBotConfig(fact_review_interval_days=bad), None) == 1


def test_an_interval_of_exactly_one_is_a_setting():
    assert review_interval(UserBotConfig(fact_review_interval_days=1), UserBotConfig(fact_review_interval_days=5)) == 1


# ------------------------------------------------------------------------------------------------ interval_elapsed

def _at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc)


def test_no_review_yet_means_elapsed():
    assert interval_elapsed(None, NOW, "UTC", 30)


def test_a_review_the_same_local_day_has_not_elapsed_at_interval_one():
    assert not interval_elapsed(_at(24, 0, 5), _at(24, 23, 59), "UTC", 1)


def test_a_daily_review_is_never_skipped_for_a_few_minutes_of_jitter():
    # yesterday's ran late at 09:20; today's tick comes at 09:05, less than 24 h later but on the next local date
    assert interval_elapsed(_at(23, 9, 20), _at(24, 9, 5), "UTC", 1)


def test_two_minutes_across_local_midnight_are_a_new_day():
    assert interval_elapsed(_at(23, 23, 59), _at(24, 0, 1), "UTC", 1)


def test_the_midnight_is_the_owners_not_utc():
    last, now = _at(23, 22, 30), _at(24, 1, 0)
    assert interval_elapsed(last, now, "UTC", 1)  # 23 Oct and 24 Oct
    assert not interval_elapsed(last, now, "America/New_York", 1)  # both on 23 Oct there
    assert not interval_elapsed(last, now, "Europe/Madrid", 1)  # both on 24 Oct in Madrid
    assert interval_elapsed(_at(23, 21, 30), _at(23, 22, 30), "Europe/Madrid", 1)  # 23 Oct 23:30 and 24 Oct 00:30


def test_an_interval_of_three_needs_three_local_dates():
    assert not interval_elapsed(_at(21, 23, 0), _at(23, 23, 0), "UTC", 3)
    assert interval_elapsed(_at(21, 23, 0), _at(24, 0, 1), "UTC", 3)
    assert interval_elapsed(_at(20, 9, 0), _at(24, 9, 0), "UTC", 3)


# ------------------------------------------------------------------------------------------------ digest_due

def test_nothing_set_aside_means_no_digest():
    assert not digest_due(state(), NOW)
    assert not digest_due(state(last_digest_at=NOW - 30 * DAY), NOW)


def test_a_digest_is_due_when_something_was_set_aside_and_none_was_ever_sent():
    assert digest_due(state(undigested_since=NOW - DAY), NOW)


def test_a_digest_waits_seven_days_from_the_last_one():
    st = lambda last: state(undigested_since=NOW - DAY, last_digest_at=last)  # noqa: E731
    assert digest_due(st(NOW - timedelta(days=7)), NOW)
    assert not digest_due(st(NOW - timedelta(days=7) + timedelta(seconds=1)), NOW)
    assert digest_due(st(NOW - timedelta(days=30)), NOW)


# ------------------------------------------------------------------------------------------------ review_due

LONG_AGO = NOW - 3 * DAY


def due(stale=0, st=None, questions=(), interval=1, now=NOW, tz="UTC"):
    return review_due(stale, st if st is not None else state(last_review_at=NOW - 2 * DAY), list(questions), interval, now, tz)


def test_nothing_to_do_is_not_due():
    assert due() is None


def test_stale_facts_and_an_elapsed_interval_are_due():
    assert due(stale=1) == "stale"


def test_stale_facts_inside_the_interval_are_not_due():
    assert due(stale=4, st=state(last_review_at=_at(24, 0, 5)), now=_at(24, 8, 0)) is None
    assert due(stale=4, st=state(last_review_at=NOW - 2 * DAY), interval=3) is None


def test_no_stale_facts_are_not_a_reason_even_after_a_long_gap():
    assert due(stale=0, st=state(last_review_at=NOW - 90 * DAY)) is None


def test_stale_facts_with_no_review_yet_are_due():
    assert due(stale=1, st=state()) == "stale"


def test_a_recorded_reply_nobody_read_is_due():
    replied = question(Q.ASKED, asked_at=NOW - 2 * DAY, reply_text="not sure", reply_at=NOW - DAY)
    assert due(questions=[replied]) == "reply"
    assert due(questions=[question(Q.UNANSWERED, asked_at=NOW - 2 * DAY, reply_text="x", reply_at=NOW - DAY)]) == "reply"


def test_a_reply_the_review_already_read_is_not_a_reason():
    read = question(
        Q.ASKED, asked_at=NOW - 2 * DAY, reply_text="not sure", reply_at=NOW - DAY, offered_at=NOW - timedelta(hours=12)
    )
    assert due(questions=[read]) is None


def test_a_question_past_its_timeout_is_due():
    assert due(questions=[question(Q.ASKED, asked_at=NOW - timedelta(days=14))]) == "timeout"
    assert due(questions=[question(Q.UNANSWERED, asked_at=NOW - timedelta(days=20))]) == "timeout"
    assert due(questions=[question(Q.ASKED, asked_at=NOW - timedelta(days=14) + timedelta(seconds=1))],
               st=state(current_question_id="q1", last_review_at=NOW - 2 * DAY)) is None


def test_a_digest_that_is_due_is_a_reason():
    assert due(st=state(undigested_since=NOW - DAY, last_review_at=NOW - DAY)) == "digest"


def test_a_queued_question_with_nothing_out_is_due():
    assert due(questions=[question(Q.QUEUED)]) == "queue"


def test_a_queued_question_waits_while_one_sent_today_is_still_out():
    out = question(Q.ASKED, question_id="out", fact_id="f9", asked_at=NOW - timedelta(hours=3))
    assert due(questions=[question(Q.QUEUED), out], st=state(current_question_id="out", last_review_at=NOW - 2 * DAY)) is None


def test_a_queued_question_is_due_when_the_current_one_was_sent_on_an_earlier_day():
    out = question(Q.ASKED, question_id="out", fact_id="f9", asked_at=NOW - timedelta(days=1))
    assert due(questions=[question(Q.QUEUED), out], st=state(current_question_id="out", last_review_at=NOW - 2 * DAY)) == "queue"


def test_a_pointer_to_a_question_that_is_gone_or_closed_frees_the_slot():
    st = state(current_question_id="out", last_review_at=NOW - 2 * DAY)
    assert due(questions=[question(Q.QUEUED)], st=st) == "queue"
    closed = question(Q.CLOSED, question_id="out", fact_id="f9")
    assert due(questions=[question(Q.QUEUED), closed], st=st) == "queue"


def test_an_open_question_that_is_not_the_current_one_does_not_hold_the_slot():
    other = question(Q.UNANSWERED, question_id="other", fact_id="f9", asked_at=NOW - timedelta(days=2))
    assert due(questions=[question(Q.QUEUED), other]) == "queue"


def test_no_queued_question_is_no_queue_reason():
    assert due(questions=[question(Q.ASKED, asked_at=NOW - timedelta(hours=2))]) is None


def test_the_reasons_are_tried_in_order():
    replied = question(Q.ASKED, question_id="r", fact_id="f1", asked_at=NOW - timedelta(days=15), reply_text="w", reply_at=NOW - DAY)
    queued = question(Q.QUEUED, question_id="k", fact_id="f2")
    st = state(undigested_since=NOW - DAY, last_review_at=NOW - 2 * DAY)
    assert due(stale=2, st=st, questions=[replied, queued]) == "stale"
    assert due(stale=0, st=st, questions=[replied, queued]) == "reply"
    timed_out = question(Q.ASKED, question_id="t", fact_id="f1", asked_at=NOW - timedelta(days=15))
    assert due(stale=0, st=st, questions=[timed_out, queued]) == "timeout"
    assert due(stale=0, st=st, questions=[queued]) == "digest"
    assert due(stale=0, st=state(last_review_at=NOW - 2 * DAY), questions=[queued]) == "queue"


def test_the_stale_reason_counts_owner_local_dates():
    st = state(last_review_at=_at(23, 22, 30))
    assert due(stale=1, st=st, now=_at(24, 1, 0), tz="UTC") == "stale"
    assert due(stale=1, st=st, now=_at(24, 1, 0), tz="America/New_York") is None


def test_the_queue_reason_uses_the_owners_day_for_the_current_question():
    asked = datetime(2026, 10, 23, 22, 30, tzinfo=timezone.utc)
    out = question(Q.ASKED, question_id="out", fact_id="f9", asked_at=asked)
    st = state(current_question_id="out", last_review_at=NOW - 2 * DAY)
    assert due(questions=[question(Q.QUEUED), out], st=st, now=_at(24, 9, 0), tz="UTC") == "queue"
    assert due(questions=[question(Q.QUEUED), out], st=st, now=_at(24, 9, 0), tz="Europe/Madrid") is None


# ------------------------------------------------------------------------------------------------ pool

@pytest.mark.parametrize("status", [Q.QUEUED, Q.PENDING, Q.ASKED, Q.UNANSWERED])
def test_the_pool_counts_the_fact_of_each_open_question(status):
    assert pool(10, [question(status)]) == 11


def test_a_closed_question_adds_nothing_to_the_pool():
    assert pool(10, [question(Q.CLOSED)]) == 10


def test_the_pool_is_stale_plus_the_facts_of_open_questions_each_counted_once():
    qs = [question(Q.QUEUED, question_id="a", fact_id="f1"), question(Q.ASKED, question_id="b", fact_id="f1"),
          question(Q.QUEUED, question_id="c", fact_id="f2")]
    assert pool(7, qs) == 9
    assert pool(0, []) == 0


def test_forty_five_queued_and_five_stale_fill_the_pool_cap():
    qs = [question(Q.QUEUED, question_id=f"q{n}", fact_id=f"f{n}") for n in range(45)]
    assert pool(5, qs) == rules.POOL_CAP


# ------------------------------------------------------------------------------------------------ queue_alert

def test_the_queue_alert_fires_once_above_thirty_five_and_not_at_thirty_five():
    assert queue_alert(state(), 35) is None
    assert queue_alert(state(), 36) == "raise"
    assert queue_alert(state(queue_alert_at=NOW), 36) is None
    assert queue_alert(state(queue_alert_at=NOW), 37) is None
    assert queue_alert(state(queue_alert_at=NOW), 100) is None


def test_the_queue_alert_rearms_when_the_queue_falls_to_thirty_five_or_fewer():
    assert queue_alert(state(queue_alert_at=NOW), 35) == "rearm"
    assert queue_alert(state(queue_alert_at=NOW), 0) == "rearm"
    assert queue_alert(state(), 0) is None


def test_the_queue_alert_crosses_rearms_and_crosses_again():
    marked = None
    fired = []
    for count in (30, 36, 37, 40, 35, 36, 36):
        action = queue_alert(state(queue_alert_at=marked), count)
        if action == "raise":
            marked = NOW
            fired.append(count)
        elif action == "rearm":
            marked = None
    assert fired == [36, 36]
