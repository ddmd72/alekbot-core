"""The review state's moves: the rules the store used to hold (T12d, hexagonal review A1 / D-M2)."""
from datetime import date, datetime, timedelta, timezone

from src.domain.fact_review_state import FactReviewState

NOW = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)
EARLIER = NOW - timedelta(days=2)
TODAY = date(2026, 10, 24)


def _state(**fields) -> FactReviewState:
    return FactReviewState(account_id="acc", **fields)


def test_a_claim_names_the_question_and_starts_the_day_at_one():
    assert _state().claimed("q1", TODAY) == {"current_question_id": "q1", "sent": 1, "sent_on": TODAY}


def test_a_claim_on_the_same_day_adds_one_and_on_a_new_day_restarts():
    state = _state(sent=2, sent_on=TODAY)
    assert state.claimed("q3", TODAY)["sent"] == 3
    assert state.claimed("q3", date(2026, 10, 25))["sent"] == 1


def test_sent_today_counts_only_the_stored_day():
    state = _state(sent=2, sent_on=TODAY)
    assert state.sent_today(TODAY) == 2
    assert state.sent_today(date(2026, 10, 25)) == 0


def test_a_release_clears_the_pointer_only_when_it_names_the_question():
    state = _state(current_question_id="q1", sent=1, sent_on=TODAY)
    assert state.released("q1") == {"current_question_id": None}
    assert state.released("q2") == {}
    assert _state().released("q1") == {}


def test_a_failed_delivery_records_the_claims_day_while_the_pointer_names_the_question():
    state = _state(current_question_id="q1", sent=1, sent_on=TODAY)
    assert state.unrecorded_delivery_failure("q1") is None
    assert state.delivery_failed("q1") == {"delivery_failed_on": TODAY}


def test_a_failed_delivery_of_another_question_records_nothing_and_says_why():
    state = _state(current_question_id="q1", sent=1, sent_on=TODAY)
    assert "not the current question" in (state.unrecorded_delivery_failure("q2") or "")
    assert state.delivery_failed("q2") == {}


def test_a_failed_delivery_with_no_claim_day_records_nothing_and_says_why():
    state = _state(current_question_id="q1")
    assert "no claim day" in (state.unrecorded_delivery_failure("q1") or "")
    assert state.delivery_failed("q1") == {}


def test_the_alert_clock_starts_at_the_first_enqueue_or_failure_since_a_success_and_keeps_it():
    fresh = _state()
    assert fresh.enqueued(NOW) == {"enqueued_at": NOW, "unsuccessful_since": NOW}
    assert fresh.failed(NOW) == {"last_failed_at": NOW, "unsuccessful_since": NOW}
    running = _state(unsuccessful_since=EARLIER)
    assert running.enqueued(NOW)["unsuccessful_since"] == EARLIER
    assert running.failed(NOW)["unsuccessful_since"] == EARLIER


def test_a_success_clears_the_alert_clock():
    assert _state(unsuccessful_since=EARLIER).succeeded(NOW) == {"last_review_at": NOW, "unsuccessful_since": None}


def test_the_first_undigested_instant_is_kept_and_a_digest_clears_it():
    assert _state().undigested(NOW) == {"undigested_since": NOW}
    assert _state(undigested_since=EARLIER).undigested(NOW) == {"undigested_since": EARLIER}
    assert _state(undigested_since=EARLIER).digested(NOW) == {"last_digest_at": NOW, "undigested_since": None}


def test_removing_offers_drops_only_the_named_facts():
    state = _state(offers={"f1": 1, "f2": 2, "f3": 1})
    assert state.without_offers(["f2", "gone"]) == {"offers": {"f1": 1, "f3": 1}}
    assert state.without_offers([]) == {"offers": {"f1": 1, "f2": 2, "f3": 1}}


def test_a_language_replaces_its_own_labels_and_keeps_the_others():
    state = _state(labels={"pl": {"question": "q"}}, label_digests={"pl": "d1"})
    moved = state.with_language("pt", {"question": "p"}, "d2")
    assert moved == {
        "message_language": "pt",
        "labels": {"pl": {"question": "q"}, "pt": {"question": "p"}},
        "label_digests": {"pl": "d1", "pt": "d2"},
    }


def test_a_move_produces_a_state_the_model_accepts():
    state = _state()
    moved = FactReviewState.model_validate({**state.model_dump(), **state.claimed("q1", TODAY)})
    assert moved.current_question_id == "q1" and moved.sent == 1 and moved.sent_on == TODAY
