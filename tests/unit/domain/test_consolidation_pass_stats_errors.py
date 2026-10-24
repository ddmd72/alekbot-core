"""Facts S3 · T8: tool-error turns, expiry errors and abandoned refusals in the pass stats (plan §3.12).

Reviewer edit (Facts S4 T6b, S1–S3 review L2 and L6): the stats take typed `PassCall`s (`tests/support/pass_calls.py`
builds what the agent's `_pass_call` builds for each old call) and the lines are rendered by the agent
(`_summary_line`, `_limit_alert`, `_abandoned_alert`); every scenario and expected count is the old one."""
from src.agents.consolidation_agent import _abandoned_alert, _limit_alert, _pass_call, _summary_line
from src.domain.consolidation_pass_stats import OUTCOME_BORN_DUE, OUTCOME_ERROR, OUTCOME_EXPIRY, OUTCOME_OK
from src.ports.llm_port import ToolCall
from tests.support.pass_calls import create, merge, search, stats, turn, update

USER = "abcdef1234567890-secret-tail"


def alert(s) -> str:
    return _abandoned_alert(s, "model-x", USER)


def model_update(fact_id: str, **updates):
    """An update as the agent types it from the model's call (the state is read as the update parser reads it)."""
    return _pass_call(ToolCall(name="update_fact", args={"fact_id": fact_id, "updates": updates}), "UTC")


def test_a_pass_without_errors_counts_nothing():
    s = stats()
    turn(s, [search(), create("x")], [OUTCOME_OK, OUTCOME_OK])

    assert (s.error_turns, s.expiry_errors, s.born_due, s.abandoned) == (0, 0, 0, 0)


def test_a_turn_counts_once_however_many_of_its_calls_failed():
    s = stats()
    turn(s, [create("a"), create("b"), create("c")], [OUTCOME_EXPIRY, OUTCOME_ERROR, OUTCOME_OK])
    turn(s, [create("d")], [OUTCOME_OK])

    assert s.error_turns == 1 and s.expiry_errors == 1


def test_expiry_errors_include_born_due_refusals():
    s = stats()
    turn(s, [create("a"), create("b")], [OUTCOME_EXPIRY, OUTCOME_BORN_DUE])

    assert (s.expiry_errors, s.born_due) == (2, 1)


def test_fewer_outcomes_than_calls_are_paired_from_the_front():
    s = stats()
    turn(s, [create("a"), create("b")], [OUTCOME_ERROR])
    turn(s, [create("c")], [])

    assert s.error_turns == 1 and s.expiry_errors == 0


def test_a_refused_create_that_is_resent_is_not_abandoned():
    s = stats()
    turn(s, [create("walks daily")], [OUTCOME_EXPIRY])
    turn(s, [create("walks daily")], [OUTCOME_OK])

    assert s.expiry_errors == 1 and s.abandoned == 0


def test_a_refused_create_never_resent_is_abandoned():
    s = stats()
    turn(s, [create("walks daily")], [OUTCOME_EXPIRY])
    turn(s, [create("something else")], [OUTCOME_OK])

    assert s.abandoned == 1


def test_a_refused_create_resent_as_a_merge_with_the_same_text_is_answered():
    s = stats()
    turn(s, [create("walks daily")], [OUTCOME_EXPIRY])
    turn(s, [merge("walks daily")], [OUTCOME_OK])

    assert s.abandoned == 0


def test_a_resend_that_is_refused_again_does_not_answer_the_first_refusal():
    s = stats()
    turn(s, [create("walks daily")], [OUTCOME_EXPIRY])
    turn(s, [create("walks daily")], [OUTCOME_EXPIRY])

    assert s.abandoned == 2


def test_a_success_before_the_refusal_does_not_answer_it():
    s = stats()
    turn(s, [create("walks daily")], [OUTCOME_OK])
    turn(s, [create("walks daily")], [OUTCOME_EXPIRY])

    assert s.abandoned == 1


def test_a_refused_update_is_answered_only_by_a_later_successful_update_of_the_same_fact():
    s = stats()
    turn(s, [update("f1", content="x"), update("f2", content="y")], [OUTCOME_EXPIRY, OUTCOME_EXPIRY])
    turn(s, [update("f1", content="x", expiry="none"), create("f2")], [OUTCOME_OK, OUTCOME_OK])

    assert s.abandoned == 1                                  # f2 got a create, not an update
    assert "update fact f2" in alert(s) and "update fact f1" not in alert(s)


def test_a_born_due_refusal_is_never_abandoned():
    s = stats()
    turn(s, [create("the trip on the 3rd")], [OUTCOME_BORN_DUE])                # the model discards it, as it was told
    turn(s, [update("f1", content="x")], [OUTCOME_BORN_DUE])

    assert s.born_due == 2 and s.abandoned == 0


def test_a_non_expiry_error_is_never_abandoned():
    s = stats()
    turn(s, [create("a"), update("f1")], [OUTCOME_ERROR, OUTCOME_ERROR])

    assert s.error_turns == 1 and s.abandoned == 0


def test_the_stats_line_and_the_limit_alert_show_the_counts():
    s = stats()
    turn(s, [create("a"), create("b")], [OUTCOME_EXPIRY, OUTCOME_BORN_DUE])

    assert "tool-error turns 1 (expiry 2, born due 1, abandoned 1)" in _summary_line(s, "model-x", "medium", USER, 0, 1.0)
    assert "tool-error turns 1 (expiry 2, born due 1, abandoned 1)" in _limit_alert(s, "model-x", "medium", USER, 1.0)


def test_the_counts_without_born_due_follow_the_plan_shape():
    s = stats()
    turn(s, [create("a")], [OUTCOME_EXPIRY])

    assert "tool-error turns 1 (expiry 1, abandoned 1)" in _summary_line(s, "model-x", "medium", USER, 0, 1.0)


def test_the_abandoned_alert_names_the_pass_and_lengths_and_never_the_text_or_the_full_user_id():
    s = stats()
    turn(s, [create("a very private fact"), update("1234567890abcdef", content="hidden")], [OUTCOME_EXPIRY, OUTCOME_EXPIRY])

    text = alert(s)

    assert text.startswith("🚨") and "`stage1`" in text and "abandoned 2" in text
    assert f"create (text {len('a very private fact')} chars)" in text and "update fact 12345678" in text
    assert "private" not in text and "hidden" not in text and "1234567890abcdef" not in text
    assert "abcdef12" in text and USER not in text and "abcdef123" not in text


def test_the_abandoned_alert_lists_at_most_five_but_counts_all():
    s = stats()
    for i in range(7):
        turn(s, [create(f"fact {i}")], [OUTCOME_EXPIRY])

    text = alert(s)

    assert "abandoned 7" in text and text.count("create (text") == 5


def test_a_closing_update_after_a_refused_content_update_leaves_it_abandoned():
    s = stats()
    turn(s, [update("f1", content="new text")], [OUTCOME_EXPIRY])
    turn(s, [update("f1", closing=True, state="invalidated")], [OUTCOME_OK])  # retires the fact without its new text

    assert s.abandoned == 1


def test_every_closing_state_in_any_case_leaves_it_abandoned():
    for state in ("invalidated", "ARCHIVED", " Superseded "):
        s = stats()
        turn(s, [update("f1", content="new text")], [OUTCOME_EXPIRY])
        turn(s, [model_update("f1", state=state)], [OUTCOME_OK])
        assert s.abandoned == 1, state


def test_a_non_closing_update_after_a_refused_content_update_answers_it():
    for later in (model_update("f1", content="new text", expiry="none"), model_update("f1", state="current", content="t"),
                  model_update("f1", tags=["a"])):
        s = stats()
        turn(s, [update("f1", content="new text")], [OUTCOME_EXPIRY])
        turn(s, [later], [OUTCOME_OK])
        assert s.abandoned == 0, later
