"""S3-D12: on a cleanup pass (stage2a, stage2b, cluster) a born-due refusal that no later success answers is abandoned
(🚨, gate G2); on stage1 and email it is not (the candidate may be discarded).

Reviewer edit (Facts S4 T6b, S1–S3 review L2 and L6): typed `PassCall` input (`tests/support/pass_calls.py`) and the
agent's renderer for the alert; the scenarios and counts are the old ones."""
import pytest

from src.agents.consolidation_agent import _abandoned_alert
from src.domain.consolidation_pass_stats import OUTCOME_BORN_DUE, OUTCOME_OK
from tests.support.pass_calls import create, merge, stats, turn, update

USER = "abcdef1234567890-secret-tail"
CLEANUP = ["stage2a", "stage2b", "cluster"]


@pytest.mark.parametrize("label", CLEANUP)
@pytest.mark.parametrize("call", [create("the trip on the 3rd"), merge("the trip on the 3rd"), update("f1", content="x")])
def test_an_unanswered_born_due_refusal_on_a_cleanup_pass_is_abandoned(label, call):
    s = stats(label)
    turn(s, [call], [OUTCOME_BORN_DUE])
    turn(s, [create("something else")], [OUTCOME_OK])

    assert (s.born_due, s.abandoned) == (1, 1)
    assert f"pass `{label}`" in _abandoned_alert(s, "model-x", USER)


@pytest.mark.parametrize("label", CLEANUP)
def test_a_born_due_refusal_answered_by_the_rewrite_is_not_abandoned(label):
    s = stats(label)
    turn(s, [create("the trip on the 3rd")], [OUTCOME_BORN_DUE])
    turn(s, [create("the trip on the 3rd")], [OUTCOME_OK])
    turn(s, [update("f1", content="x")], [OUTCOME_BORN_DUE])
    turn(s, [update("f1", content="x", expiry="none")], [OUTCOME_OK])

    assert s.abandoned == 0


@pytest.mark.parametrize("label", CLEANUP)
def test_closing_the_fact_does_not_answer_its_born_due_refusal(label):
    s = stats(label)
    turn(s, [update("f1", content="x")], [OUTCOME_BORN_DUE])
    turn(s, [update("f1", closing=True, state="superseded")], [OUTCOME_OK])

    assert s.abandoned == 1


@pytest.mark.parametrize("label", ["stage1", "email"])
def test_a_born_due_refusal_on_a_conversation_pass_is_never_abandoned(label):
    s = stats(label)
    turn(s, [create("the trip on the 3rd")], [OUTCOME_BORN_DUE])
    turn(s, [update("f1", content="x")], [OUTCOME_BORN_DUE])

    assert s.born_due == 2 and s.abandoned == 0
