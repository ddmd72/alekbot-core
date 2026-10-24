"""`review_outcome` and `ReviewBatch` (Facts S4 · T6a1): the outcome of a batch fact is read from the state of the document
its write left; the batch is plain data."""
import pytest

from src.domain.entities import FactState
from src.domain.fact_review_batch import ReviewBatch
from src.domain.fact_review_outcome import MADE_CURRENT, ReviewDecision, review_outcome
from tests.support.review_rule_builders import fact, question


@pytest.mark.parametrize("state, outcome", [
    (FactState.ARCHIVED, "archived"), (FactState.INVALIDATED, "invalidated"), (FactState.SUPERSEDED, "superseded"),
])
def test_a_closing_state_is_its_outcome(state, outcome):
    batch_fact = fact("f1")
    assert review_outcome(batch_fact, batch_fact.model_copy(update={"state": state})) == outcome


def test_the_same_document_current_again_is_revived():
    batch_fact = fact("f1")
    assert review_outcome(batch_fact, batch_fact.model_copy(update={"state": FactState.CURRENT})) == "revived"


def test_a_current_new_version_is_rewritten():
    batch_fact = fact("f1")
    written = batch_fact.model_copy(update={"id": "f1-v2", "version": 2, "state": FactState.CURRENT})
    assert review_outcome(batch_fact, written) == "rewritten"


def test_a_closed_new_version_is_its_closing_state():
    batch_fact = fact("f1")
    written = batch_fact.model_copy(update={"id": "f1-v2", "version": 2, "state": FactState.ARCHIVED})
    assert review_outcome(batch_fact, written) == "archived"


def test_a_stale_document_is_no_outcome():
    batch_fact = fact("f1")
    with pytest.raises(ValueError, match="leaves no fact stale"):
        review_outcome(batch_fact, batch_fact)


def test_only_revived_and_rewritten_leave_the_lineage_current():
    assert MADE_CURRENT == frozenset({"revived", "rewritten"})


def test_a_decision_and_a_batch_are_plain_data():
    decision = ReviewDecision("f1", "revived", "f1", "evidence")
    batch = ReviewBatch((fact("f1"),), (question(),))
    assert (decision.fact_id, decision.outcome, decision.written_id, decision.basis_kind) == ("f1", "revived", "f1", "evidence")
    assert [f.id for f in batch.facts] == ["f1"] and [q.question_id for q in batch.questions] == ["q1"]
    assert ReviewBatch((fact("f1"),)).questions == ()
