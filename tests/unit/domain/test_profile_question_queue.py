"""The order and the rate of profile questions (fact profile RFC §4.7): lifecycle questions go first whatever their
`queue_key`, a profile question only while none was claimed that owner-local day, the scopes of a read, and the derived
id. Invented cast."""
from datetime import date, timedelta

from src.domain.fact_review_rules import (
    in_scope, lifecycle_only, next_in_queue, profile_may_go, profile_question_id, queue_order,
)
from src.domain.fact_review_state import FactReviewState
from tests.support.profile_question_builders import NOW, profile_question
from tests.support.review_rule_builders import question as lifecycle_question

TODAY = date(2026, 10, 24)


def test_a_lifecycle_question_goes_before_a_profile_one_whatever_the_queue_key():
    profile = profile_question("place", question_id="pq-old", queue_key=NOW - timedelta(days=900))
    lifecycle = lifecycle_question(question_id="q-new", queue_key=NOW)
    assert [q.question_id for q in queue_order([profile, lifecycle])] == ["q-new", "pq-old"]
    assert next_in_queue([profile, lifecycle]) is lifecycle


def test_profile_questions_keep_the_queue_key_order_among_themselves():
    older = profile_question("place", question_id="pq-a", queue_key=NOW - timedelta(days=2))
    newer = profile_question("person", question_id="pq-b", queue_key=NOW - timedelta(days=1))
    assert [q.question_id for q in queue_order([newer, older])] == ["pq-a", "pq-b"]


def test_a_profile_question_is_skipped_while_not_allowed_and_a_lifecycle_one_is_not():
    profile = profile_question("place")
    lifecycle = lifecycle_question()
    assert next_in_queue([profile], profile_allowed=False) is None
    assert next_in_queue([profile, lifecycle], profile_allowed=False) is lifecycle
    assert next_in_queue([profile], profile_allowed=True) is profile


def test_one_profile_claim_per_owner_local_day():
    fresh = FactReviewState(account_id="acc")
    assert profile_may_go(fresh, TODAY)
    claimed = FactReviewState(account_id="acc", **fresh.claimed("pq1", TODAY, profile=True))
    assert not profile_may_go(claimed, TODAY)
    assert profile_may_go(claimed, TODAY + timedelta(days=1))


def test_a_lifecycle_claim_does_not_mark_the_profile_day_and_still_counts_for_the_cap():
    changes = FactReviewState(account_id="acc").claimed("q1", TODAY)
    assert "profile_claimed_on" not in changes and changes["sent"] == 1
    profile_changes = FactReviewState(account_id="acc").claimed("pq1", TODAY, profile=True)
    assert profile_changes["sent"] == 1 and profile_changes["profile_claimed_on"] == TODAY


def test_the_scopes_of_a_read():
    profile, lifecycle = profile_question("person"), lifecycle_question()
    assert [in_scope(q, "lifecycle") for q in (profile, lifecycle)] == [False, True]
    assert [in_scope(q, "profile") for q in (profile, lifecycle)] == [True, False]
    assert [in_scope(q, "all") for q in (profile, lifecycle)] == [True, True]
    assert lifecycle_only([profile, lifecycle]) == [lifecycle]


def test_the_profile_question_id_is_stable_distinct_and_fits_a_frame():
    first = profile_question_id("acc", "place:rome|")
    assert first == profile_question_id("acc", "place:rome|")
    assert first != profile_question_id("acc", "place:porto|portugal")
    assert first != profile_question_id("acc-2", "place:rome|")
    assert first.startswith("pq") and len(first) == 32
    assert profile_question_id("a", "bc") != profile_question_id("ab", "c")
