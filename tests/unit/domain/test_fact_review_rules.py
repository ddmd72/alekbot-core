"""The review's selection, queue, sending and timeout rules (Facts S4 · T1b, plan §3.2 steps 5-8, §3.7, §3.8)."""
from datetime import date, datetime, timedelta, timezone

import pytest

from src.domain import fact_lifecycle_sweep
from src.domain import fact_review_rules as rules
from src.domain.fact_review_question import QUESTION_TIMEOUT, QuestionStatus
from src.domain.fact_review_rules import (
    EARLIEST_QUEUE_KEY,
    becomes_unanswered,
    due_timeouts,
    kept_twice,
    may_send,
    next_in_queue,
    queue_key,
    queue_order,
    select_batch,
    sent_today,
)
from tests.support.review_rule_builders import NOW, fact, question, stale_since, state

Q = QuestionStatus
OPEN_STATUSES = [Q.QUEUED, Q.PENDING, Q.ASKED, Q.UNANSWERED]


def ids(facts):
    return [f.id for f in facts]


# ------------------------------------------------------------------------------------------------ the constants

def test_the_constants_are_the_plans_numbers():
    assert (rules.REVIEW_BATCH, rules.MAX_QUESTIONS_PER_DAY, rules.MAX_OFFERS) == (20, 5, 2)
    assert (rules.QUEUE_ALERT, rules.POOL_CAP) == (35, 50)
    assert (rules.MIN_FALLBACK_DAYS, rules.MAX_FALLBACK_DAYS) == (14, 365)
    assert (rules.REVIVE_ALERT, rules.REVIVE_WINDOW) == (3, timedelta(days=30))
    assert rules.DIGEST_INTERVAL == timedelta(days=7)
    assert rules.KEPT_TWICE_FALLBACK == "revive"


def test_the_pool_cap_is_the_janitors_backlog_cap_not_a_second_copy():
    assert fact_lifecycle_sweep.BREAKER_BACKLOG == rules.POOL_CAP == 50


def test_the_question_timeout_is_the_records_own_14_days():
    assert QUESTION_TIMEOUT == timedelta(days=14)
    assert not hasattr(rules, "QUESTION_TIMEOUT")


# ------------------------------------------------------------------------------------------------ queue_key

def test_queue_key_is_the_facts_stale_since_as_utc():
    assert queue_key(fact(stale_since=NOW - timedelta(days=3))) == NOW - timedelta(days=3)


def test_queue_key_of_a_naive_date_reads_it_as_utc():
    key = queue_key(fact(stale_since=datetime(2026, 10, 1, 12, 0)))
    assert key == datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc) and key.tzinfo is not None


def test_queue_key_of_a_fact_with_no_stale_since_is_before_every_date():
    key = queue_key(fact(stale_since=None))
    assert key == EARLIEST_QUEUE_KEY == datetime(1970, 1, 1, tzinfo=timezone.utc)
    assert key < queue_key(fact(stale_since=datetime(1971, 1, 1, tzinfo=timezone.utc)))


# ------------------------------------------------------------------------------------------------ select_batch

def test_stale_facts_are_selected_by_offers_then_longest_stale_then_id():
    stale = [
        fact("new0", stale_since=stale_since(1)),
        fact("old1", stale_since=stale_since(50)),
        fact("old0", stale_since=stale_since(20)),
        fact("tie_b", stale_since=stale_since(5)),
        fact("tie_a", stale_since=stale_since(5)),
    ]
    offers = {"old1": 1}
    assert ids(select_batch(stale, [], [], [], offers)) == ["old0", "tie_a", "tie_b", "new0", "old1"]


def test_a_fact_with_no_stale_since_comes_first_in_its_group_only():
    stale = [
        fact("dated0", stale_since=stale_since(40)),
        fact("none0", stale_since=None),
        fact("none1", stale_since=None),
        fact("dated1", stale_since=stale_since(40)),
    ]
    assert ids(select_batch(stale, [], [], [], {"none1": 1, "dated1": 1})) == ["none0", "dated0", "none1", "dated1"]


def test_a_stale_fact_and_a_naive_one_compare_without_error():
    stale = [fact("a", stale_since=datetime(2026, 10, 1, 0, 0)), fact("b", stale_since=stale_since(100))]
    assert ids(select_batch(stale, [], [], [], {})) == ["b", "a"]


@pytest.mark.parametrize("status", OPEN_STATUSES)
def test_a_stale_fact_with_an_open_question_is_not_selected(status):
    stale = [fact("f1"), fact("f2")]
    assert ids(select_batch(stale, [question(status, fact_id="f1")], [], [], {})) == ["f2"]


def test_a_stale_fact_whose_question_is_closed_is_selected():
    stale = [fact("f1")]
    assert ids(select_batch(stale, [question(Q.CLOSED, fact_id="f1")], [], [], {})) == ["f1"]


def test_a_fact_offered_twice_is_asked_not_offered_a_third_time():
    stale = [fact("once", stale_since=stale_since(1)), fact("twice", stale_since=stale_since(90)), fact("thrice")]
    assert ids(select_batch(stale, [], [], [], {"once": 1, "twice": 2, "thrice": 3})) == ["once"]


def test_reply_facts_then_timed_out_candidates_then_stale_all_ahead_of_the_age_order():
    stale = [fact("s_old", stale_since=stale_since(90)), fact("s_new", stale_since=stale_since(1))]
    replied = [fact("r2", state="current"), fact("r1", state="current")]
    cands = [fact("c1", state="current")]
    assert ids(select_batch(stale, [], replied, cands, {})) == ["r2", "r1", "c1", "s_old", "s_new"]


def test_priority_facts_are_not_filtered_by_their_own_open_question_or_by_offers():
    held = fact("held", state="current")
    batch = select_batch([], [question(Q.ASKED, fact_id="held")], [held], [fact("c", state="current")], {"held": 5, "c": 5})
    assert ids(batch) == ["held", "c"]


def test_a_fact_in_two_lists_is_selected_once_at_its_earliest_place():
    same = fact("same")
    batch = select_batch([same, fact("other")], [], [same], [same], {})
    assert ids(batch) == ["same", "other"]


def test_the_batch_is_at_most_twenty_stale_facts_oldest_first():
    stale = [fact(f"f{n:02d}", stale_since=stale_since(100 - n)) for n in range(25)]
    batch = select_batch(stale, [], [], [], {})
    assert len(batch) == rules.REVIEW_BATCH == 20
    assert ids(batch) == [f"f{n:02d}" for n in range(20)]


def test_priority_facts_count_inside_the_twenty():
    stale = [fact(f"s{n:02d}", stale_since=stale_since(100 - n)) for n in range(25)]
    batch = select_batch(stale, [], [fact("r", state="current")], [fact("c", state="current")], {})
    assert len(batch) == 20 and ids(batch)[:2] == ["r", "c"] and ids(batch)[-1] == "s17"


def test_more_than_twenty_priority_facts_fill_the_batch_alone():
    replied = [fact(f"r{n:02d}", state="current") for n in range(22)]
    assert ids(select_batch([fact("s")], [], replied, [], {})) == [f"r{n:02d}" for n in range(20)]


def test_nothing_to_select_is_an_empty_batch():
    assert select_batch([], [], [], [], {}) == []


# ------------------------------------------------------------------------------------------------ kept_twice

def test_kept_twice_takes_the_facts_offered_the_limit_or_more_times():
    stale = [fact("a"), fact("b"), fact("c"), fact("d")]
    assert ids(kept_twice(stale, {"a": 1, "b": 2, "c": 3}, [])) == ["b", "c"]


def test_kept_twice_skips_a_fact_with_an_open_question_not_a_closed_one():
    stale = [fact("open"), fact("closed")]
    qs = [question(Q.QUEUED, question_id="q1", fact_id="open"), question(Q.CLOSED, question_id="q2", fact_id="closed")]
    assert ids(kept_twice(stale, {"open": 2, "closed": 2}, qs)) == ["closed"]


def test_kept_twice_goes_longest_stale_first_a_missing_date_first_then_id():
    stale = [
        fact("c", stale_since=stale_since(5)), fact("b", stale_since=stale_since(50)),
        fact("a", stale_since=stale_since(50)), fact("z", stale_since=None),
    ]
    assert ids(kept_twice(stale, {f.id: 2 for f in stale}, [])) == ["z", "a", "b", "c"]


def test_a_fact_kept_twice_is_never_in_the_batch_too():
    stale = [fact("f1"), fact("f2")]
    offers = {"f1": 2}
    assert ids(kept_twice(stale, offers, [])) == ["f1"] and ids(select_batch(stale, [], [], [], offers)) == ["f2"]


# ------------------------------------------------------------------------------------------------ queue_order

def _queued(qid, key_days, queued_days=4):
    return question(question_id=qid, queue_key=NOW - timedelta(days=key_days), queued_at=NOW - timedelta(days=queued_days))


def test_the_queue_goes_by_queue_key_then_queued_at_then_id():
    qs = [_queued("c", 10), _queued("b", 30, queued_days=1), _queued("a", 30, queued_days=9), _queued("a2", 30, queued_days=1)]
    assert [q.question_id for q in queue_order(qs)] == ["a", "a2", "b", "c"]


def test_queue_order_does_not_change_its_input():
    qs = [_queued("b", 1), _queued("a", 2)]
    queue_order(qs)
    assert [q.question_id for q in qs] == ["b", "a"]


def test_next_in_queue_is_the_first_queued_question_in_order():
    qs = [_queued("later", 10), _queued("first", 40)]
    assert next_in_queue(qs).question_id == "first"


def test_next_in_queue_skips_questions_that_are_not_queued_however_old():
    sent = question(Q.ASKED, question_id="sent", queue_key=NOW - timedelta(days=99))
    pending = question(Q.PENDING, question_id="pend", queue_key=NOW - timedelta(days=98))
    closed = question(Q.CLOSED, question_id="done", queue_key=NOW - timedelta(days=97))
    assert next_in_queue([sent, pending, closed, _queued("q", 1)]).question_id == "q"


def test_next_in_queue_of_nothing_queued_is_none():
    assert next_in_queue([]) is None
    assert next_in_queue([question(Q.ASKED)]) is None


# ------------------------------------------------------------------------------------------------ sending

TODAY = date(2026, 10, 24)
YESTERDAY = date(2026, 10, 23)


def test_may_send_when_nothing_is_out_and_nothing_sent_today():
    assert may_send(state(), TODAY, open_current=False)


def test_may_send_refuses_while_the_current_question_is_open():
    assert not may_send(state(current_question_id="q1"), TODAY, open_current=True)


def test_a_pointer_to_a_question_that_is_no_longer_open_holds_nothing():
    assert may_send(state(current_question_id="q1"), TODAY, open_current=False)


def test_open_current_without_a_pointer_holds_nothing():
    assert may_send(state(), TODAY, open_current=True)


@pytest.mark.parametrize("sent,allowed", [(0, True), (4, True), (5, False), (6, False)])
def test_the_fifth_question_of_a_day_is_the_last(sent, allowed):
    assert may_send(state(sent_on=TODAY, sent=sent), TODAY, open_current=False) is allowed


def test_the_count_of_an_earlier_day_is_not_todays():
    st = state(sent_on=YESTERDAY, sent=5)
    assert sent_today(st, TODAY) == 0 and may_send(st, TODAY, open_current=False)


def test_sent_today_is_the_count_of_the_same_local_day():
    assert sent_today(state(sent_on=TODAY, sent=3), TODAY) == 3
    assert sent_today(state(), TODAY) == 0


def test_may_send_refuses_after_a_failed_delivery_today_and_allows_after_midnight():
    st = state(delivery_failed_on=TODAY)
    assert not may_send(st, TODAY, open_current=False)
    assert may_send(st, TODAY + timedelta(days=1), open_current=False)


def test_a_failed_delivery_of_an_earlier_day_does_not_block():
    assert may_send(state(delivery_failed_on=YESTERDAY), TODAY, open_current=False)


# ------------------------------------------------------------------------------------------------ becomes_unanswered

def test_a_question_asked_on_an_earlier_local_day_becomes_unanswered():
    assert becomes_unanswered(question(Q.ASKED, asked_at=NOW - timedelta(days=1)), TODAY, "UTC")


def test_a_question_asked_today_stays_current():
    assert not becomes_unanswered(question(Q.ASKED, asked_at=NOW - timedelta(hours=8)), TODAY, "UTC")


def test_the_day_is_the_owners_not_utc():
    asked = question(Q.ASKED, asked_at=datetime(2026, 10, 23, 22, 30, tzinfo=timezone.utc))
    assert becomes_unanswered(asked, TODAY, "UTC")  # 23 Oct in UTC
    assert not becomes_unanswered(asked, TODAY, "Europe/Madrid")  # 00:30 on 24 Oct in Madrid
    assert becomes_unanswered(asked, TODAY, "America/New_York")  # 23 Oct there too


def test_a_recorded_reply_in_words_does_not_keep_a_question_current():
    replied = question(Q.ASKED, asked_at=NOW - timedelta(days=1), reply_text="not sure", reply_at=NOW - timedelta(hours=20))
    assert becomes_unanswered(replied, TODAY, "UTC")


@pytest.mark.parametrize("status", [Q.QUEUED, Q.PENDING, Q.UNANSWERED, Q.CLOSED])
def test_only_an_asked_question_becomes_unanswered(status):
    assert not becomes_unanswered(question(status, asked_at=NOW - timedelta(days=5)), TODAY, "UTC")


# ------------------------------------------------------------------------------------------------ due_timeouts

def test_a_question_times_out_exactly_fourteen_days_after_it_was_asked():
    on_the_dot = question(Q.ASKED, question_id="dot", asked_at=NOW - QUESTION_TIMEOUT)
    a_second_short = question(Q.ASKED, question_id="short", asked_at=NOW - QUESTION_TIMEOUT + timedelta(seconds=1))
    assert [q.question_id for q in due_timeouts([on_the_dot, a_second_short], NOW)] == ["dot"]


def test_asked_and_unanswered_questions_time_out_with_or_without_a_reply():
    old = NOW - timedelta(days=20)
    asked = question(Q.ASKED, question_id="a", asked_at=old)
    unanswered = question(Q.UNANSWERED, question_id="u", asked_at=old)
    replied = question(
        Q.ASKED, question_id="r", asked_at=old, reply_text="words", reply_at=NOW - timedelta(days=1)
    )
    assert {q.question_id for q in due_timeouts([asked, unanswered, replied], NOW)} == {"a", "u", "r"}


def test_a_recent_reply_does_not_shorten_the_wait():
    fresh = question(Q.ASKED, asked_at=NOW - timedelta(days=2), reply_text="words", reply_at=NOW - timedelta(days=1))
    assert due_timeouts([fresh], NOW) == []


def test_queued_pending_and_closed_questions_never_time_out():
    qs = [
        question(Q.QUEUED, queued_at=NOW - timedelta(days=400)),
        question(Q.PENDING, question_id="p", appended_at=NOW - timedelta(days=400)),
        question(Q.CLOSED, question_id="c", asked_at=NOW - timedelta(days=400)),
    ]
    assert due_timeouts(qs, NOW) == []


def test_timeouts_come_in_queue_order():
    old = NOW - timedelta(days=30)
    qs = [
        question(Q.ASKED, question_id="b", asked_at=old, queue_key=NOW - timedelta(days=5)),
        question(Q.UNANSWERED, question_id="a", asked_at=old, queue_key=NOW - timedelta(days=50)),
    ]
    assert [q.question_id for q in due_timeouts(qs, NOW)] == ["a", "b"]
