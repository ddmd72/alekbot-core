"""The account's review state: its fields, read-only mappings and shape checks (Facts S4 · T1a, plan §2.2, §3.7, §3.17)."""
import copy
from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from src.domain.fact_review_state import FactReviewState

NOW = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)


def test_a_fresh_state_has_nothing_open_nothing_sent_and_no_labels():
    state = FactReviewState(account_id="acc")
    assert state.current_question_id is None and state.sent == 0 and state.sent_on is None
    assert state.delivery_failed_on is None and state.message_language is None and state.queue_alert_at is None
    assert dict(state.offers) == {} and dict(state.labels) == {}


def test_the_mappings_are_read_only():
    state = FactReviewState(account_id="acc", offers={"f1": 1}, labels={"pl": {"question": "Czy to nadal prawda?"}})
    with pytest.raises(TypeError):
        state.offers["f1"] = 2
    with pytest.raises(TypeError):
        state.offers["f2"] = 1
    with pytest.raises(TypeError):
        state.labels["de"] = {}
    with pytest.raises(TypeError):
        state.labels["pl"]["question"] = "changed"
    assert state.offers["f1"] == 1 and state.labels["pl"]["question"] == "Czy to nadal prawda?"


def test_the_state_does_not_share_its_mappings_with_the_dicts_it_was_built_from_or_with_other_states():
    offers, labels = {"f1": 1}, {"pl": {"question": "q"}}
    state = FactReviewState(account_id="acc", offers=offers, labels=labels)
    offers["f1"] = 9
    offers["f2"] = 1
    labels["pl"]["question"] = "changed"
    assert dict(state.offers) == {"f1": 1} and state.labels["pl"]["question"] == "q"
    first, second = FactReviewState(account_id="a"), FactReviewState(account_id="b")
    assert first.offers is not second.offers and first.labels is not second.labels


def test_the_state_keeps_the_days_and_the_labels_it_was_given_and_round_trips():
    state = FactReviewState(
        account_id="acc", current_question_id="q1", sent_on=date(2026, 10, 24), sent=3,
        delivery_failed_on=date(2026, 10, 23), message_language="pl", offers={"f1": 1},
        labels={"pl": {"question": "Czy to nadal prawda?"}}, last_review_at=NOW,
    )
    dumped = state.model_dump()
    assert type(dumped["offers"]) is dict and type(dumped["labels"]) is dict and type(dumped["labels"]["pl"]) is dict
    assert FactReviewState.model_validate(dumped) == state
    assert FactReviewState.model_validate_json(state.model_dump_json()) == state


def test_the_state_cannot_be_changed_in_place():
    with pytest.raises(ValidationError):
        FactReviewState(account_id="acc").sent = 4


def test_the_sent_count_cannot_be_negative_and_the_account_is_required():
    with pytest.raises(ValidationError):
        FactReviewState(account_id="acc", sent=-1)
    with pytest.raises(ValidationError):
        FactReviewState(account_id="")


def test_sent_questions_are_counted_for_a_day():
    with pytest.raises(ValidationError, match="sent_on"):
        FactReviewState(account_id="acc", sent=1)
    assert FactReviewState(account_id="acc", sent=0).sent == 0
    assert FactReviewState(account_id="acc", sent=0, sent_on=date(2026, 10, 24)).sent == 0
    assert FactReviewState(account_id="acc", sent=1, sent_on=date(2026, 10, 24)).sent == 1


def test_an_offer_count_is_not_below_zero():
    with pytest.raises(ValidationError, match="not below zero"):
        FactReviewState(account_id="acc", offers={"f": -3})
    assert FactReviewState(account_id="acc", offers={"f": 0, "g": 2}).offers["g"] == 2


@pytest.mark.parametrize("language", ["uk", "en", "pl", "ru"])
def test_the_message_language_is_two_lowercase_letters(language):
    assert FactReviewState(account_id="acc", message_language=language).message_language == language


@pytest.mark.parametrize("language", ["", "u", "ukr", "UK", "Uk", "u1", "іе", "uk ", "uk\n"])
def test_any_other_shape_of_message_language_is_refused(language):
    with pytest.raises(ValidationError, match="two lowercase letters"):
        FactReviewState(account_id="acc", message_language=language)


@pytest.mark.parametrize("language", ["", "pol", "PL", "p1", "pl\n"])
def test_labels_are_kept_under_a_language_code(language):
    with pytest.raises(ValidationError, match="two lowercase letters"):
        FactReviewState(account_id="acc", labels={language: {"question": "q"}})


@pytest.mark.parametrize("field", [
    "enqueued_at", "started_at", "last_review_at", "last_failed_at", "unsuccessful_since", "last_digest_at",
    "undigested_since", "oldest_alert_at", "queue_alert_at",
])
def test_a_naive_time_is_refused(field):
    with pytest.raises(ValidationError, match="timezone"):
        FactReviewState(account_id="acc", **{field: datetime(2026, 10, 24, 9, 0)})


def test_a_deep_copy_is_a_valid_equal_state_that_shares_nothing():
    state = FactReviewState(
        account_id="acc", offers={"f1": 1}, labels={"pl": {"question": "q"}}, sent=2, sent_on=date(2026, 10, 24),
        message_language="pl", last_review_at=NOW,
    )
    for clone in (copy.deepcopy(state), state.model_copy(deep=True)):
        assert clone == state and clone is not state
        assert clone.offers is not state.offers and clone.labels is not state.labels
        assert clone.labels["pl"] is not state.labels["pl"]
        with pytest.raises(TypeError):
            clone.offers["f1"] = 5
        assert FactReviewState.model_validate(clone.model_dump()) == clone


def test_a_state_inside_another_structure_deep_copies_too():
    state = FactReviewState(account_id="acc", offers={"f1": 1})
    holder = {"state": state, "again": state}
    cloned = copy.deepcopy(holder)
    assert cloned["state"] == state and cloned["state"] is not state
