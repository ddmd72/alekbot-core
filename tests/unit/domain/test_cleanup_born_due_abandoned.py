"""S3-D12: on a cleanup pass (stage2a, stage2b, cluster) a born-due refusal that no later success answers is abandoned
(🚨, gate G2); on stage1 and email it is not (the candidate may be discarded)."""
import pytest

from src.domain.consolidation_pass_stats import OUTCOME_BORN_DUE, OUTCOME_OK, ConsolidationPassStats

USER = "abcdef1234567890-secret-tail"
CLEANUP = ["stage2a", "stage2b", "cluster"]


def stats(label):
    return ConsolidationPassStats(label, "model-x", "medium", 15, USER)


def turn(s, calls, outcomes):
    s.record_turn(calls)
    s.record_outcomes(calls, outcomes)


def create(text):
    return ("create_fact", {"content": text})


def merge(text):
    return ("merge_facts", {"merged_content": text, "fact_ids": ["a", "b"]})


def update(fact_id, **updates):
    return ("update_fact", {"fact_id": fact_id, "updates": updates})


@pytest.mark.parametrize("label", CLEANUP)
@pytest.mark.parametrize("call", [create("the trip on the 3rd"), merge("the trip on the 3rd"), update("f1", content="x")])
def test_an_unanswered_born_due_refusal_on_a_cleanup_pass_is_abandoned(label, call):
    s = stats(label)
    turn(s, [call], [OUTCOME_BORN_DUE])
    turn(s, [create("something else")], [OUTCOME_OK])

    assert (s.born_due, s.abandoned) == (1, 1)
    assert f"pass `{label}`" in s.format_abandoned_alert()


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
    turn(s, [update("f1", state="superseded")], [OUTCOME_OK])

    assert s.abandoned == 1


@pytest.mark.parametrize("label", ["stage1", "email"])
def test_a_born_due_refusal_on_a_conversation_pass_is_never_abandoned(label):
    s = stats(label)
    turn(s, [create("the trip on the 3rd")], [OUTCOME_BORN_DUE])
    turn(s, [update("f1", content="x")], [OUTCOME_BORN_DUE])

    assert s.born_due == 2 and s.abandoned == 0
