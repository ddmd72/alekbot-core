"""The texts the review model reads from `FactReviewScope` (Facts S4 · T6a1, plan §3.5): each names the argument's place,
none tells a pass with no search tool to search, and a refusal is one tagged failed result."""
import pytest

from src.domain.fact_review_rules import (
    ASK_HELD, CHANGED_OUTSIDE, UPDATES_SHAPE_REFUSAL, asked_refusal, changed_outside_refusal, decided_refusal,
    not_in_batch_refusal, not_under_review_refusal,
)
from src.domain.fact_tool_result import (
    REFUSED_ASK, REFUSED_BASIS, REFUSED_SCOPE, STATUS_ASKED, WRITTEN_STATUSES, outcome_of, refused_result,
)
from src.domain.consolidation_pass_stats import OUTCOME_ERROR, OUTCOME_OK

PLACED = [not_in_batch_refusal, asked_refusal, decided_refusal, changed_outside_refusal, not_under_review_refusal]


@pytest.mark.parametrize("text", PLACED)
@pytest.mark.parametrize("place", ["update_fact.fact_id", "ask_owner.fact_id"])
def test_every_scope_refusal_names_its_place_and_never_says_search(text, place):
    message = text(place)
    assert message.startswith(place) and "search" not in message.lower()


def test_the_scope_texts_say_what_each_rule_is():
    place = "update_fact.fact_id"
    assert not_in_batch_refusal(place) == (
        "update_fact.fact_id names no fact of this batch: use the fact_id of a fact listed for this review"
    )
    assert asked_refusal(place) == (
        "update_fact.fact_id: you asked the owner about this fact in this pass; it stays as it is until he answers"
    )
    assert decided_refusal(place) == "update_fact.fact_id: this fact was already decided in this pass; leave it"
    assert not_under_review_refusal(place) == (
        "update_fact.fact_id names a current fact with no question in this batch; leave it, it stays as it is"
    )


def test_the_changed_outside_text_is_the_plans_and_leaves_the_fact():
    assert CHANGED_OUTSIDE == "this fact changed outside the review; leave it, it stays as it is"
    assert changed_outside_refusal("update_fact.fact_id") == f"update_fact.fact_id: {CHANGED_OUTSIDE}"


def test_the_updates_shape_text_names_the_place_and_every_field():
    assert UPDATES_SHAPE_REFUSAL.startswith("update_fact.updates must be an object")
    for name in ("content", "tags", "metadata", "temporal_class", "state", "superseded_by", "expiry"):
        assert name in UPDATES_SHAPE_REFUSAL
    assert "reported_date" not in UPDATES_SHAPE_REFUSAL and "search" not in UPDATES_SHAPE_REFUSAL


def test_the_held_ask_text_says_code_writes_the_question():
    assert ASK_HELD.startswith("held: code writes the question") and "search" not in ASK_HELD


@pytest.mark.parametrize("tag", [REFUSED_BASIS, REFUSED_SCOPE, REFUSED_ASK])
def test_a_refusal_is_a_tagged_failed_result_counted_as_an_error(tag):
    result = refused_result("f1", tag, "why")
    assert result == {"fact_id": "f1", "status": "failed", "refused": tag, "message": "why"}
    assert outcome_of(result) == OUTCOME_ERROR


def test_the_tags_and_the_ask_status_are_spelled_once():
    assert (REFUSED_BASIS, REFUSED_SCOPE, REFUSED_ASK) == ("basis", "scope", "ask")
    assert STATUS_ASKED == "asked" and STATUS_ASKED not in WRITTEN_STATUSES
    assert outcome_of({"fact_id": "f1", "status": STATUS_ASKED, "message": ASK_HELD}) == OUTCOME_OK
