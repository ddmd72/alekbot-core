"""The review pass's result rules (T12c S-M3): one result per fact, which current documents the pass may write, the documents
it made current, the plan date of a trail reason and when a failed write is checked for a change outside the review. The
scope keeps only its maps and calls these."""
from datetime import date

import pytest

from src.domain.fact_review_basis import ReviewBasis
from src.domain.fact_review_outcome import MADE_CURRENT, ReviewDecision
from src.domain.fact_review_rules import (
    UPDATE_FACT_PLACE,
    asked_refusal,
    decided_refusal,
    made_current_documents,
    may_write_current,
    needs_change_check,
    reason_plan_date,
    result_refusal,
)
from src.domain.fact_update_outcome import EXPIRY_REFUSALS, UpdateRefusal
from tests.support.review_rule_builders import fact

PLACE = UPDATE_FACT_PLACE
OUTCOMES = ["revived", "rewritten", "archived", "invalidated", "superseded"]


def decision(outcome: str) -> ReviewDecision:
    return ReviewDecision("f-batch", outcome, "f-written", None)  # type: ignore[arg-type]


class TestResultRefusal:
    def test_a_fact_with_no_result_is_free_for_a_write_and_an_ask(self):
        assert result_refusal(PLACE, asked=False, decision=None, write=True) is None
        assert result_refusal(PLACE, asked=False, decision=None, write=False) is None

    @pytest.mark.parametrize("write", [True, False])
    @pytest.mark.parametrize("decided", [None, *OUTCOMES])
    def test_an_ask_holds_the_fact_whatever_else_happened(self, write, decided):
        got = result_refusal(PLACE, asked=True, decision=decision(decided) if decided else None, write=write)

        assert got == asked_refusal(PLACE)

    @pytest.mark.parametrize("outcome", sorted(set(OUTCOMES) - set(MADE_CURRENT)))
    def test_a_closing_write_decides_the_fact_for_every_later_call(self, outcome):
        assert result_refusal(PLACE, asked=False, decision=decision(outcome), write=True) == decided_refusal(PLACE)
        assert result_refusal(PLACE, asked=False, decision=decision(outcome), write=False) == decided_refusal(PLACE)

    @pytest.mark.parametrize("outcome", sorted(MADE_CURRENT))
    def test_a_write_may_follow_one_that_left_the_lineage_current_but_an_ask_may_not(self, outcome):
        assert result_refusal(PLACE, asked=False, decision=decision(outcome), write=True) is None
        assert result_refusal(PLACE, asked=False, decision=decision(outcome), write=False) == decided_refusal(PLACE)


class TestMayWriteCurrent:
    def test_an_open_question_or_a_pass_made_current_opens_the_document(self):
        assert may_write_current(None, True) is True
        assert may_write_current(decision("revived"), False) is True
        assert may_write_current(decision("rewritten"), False) is True

    @pytest.mark.parametrize("outcome", sorted(set(OUTCOMES) - set(MADE_CURRENT)))
    def test_a_closing_decision_opens_nothing_by_itself(self, outcome):
        assert may_write_current(decision(outcome), False) is False
        assert may_write_current(decision(outcome), True) is True

    def test_a_current_fact_with_no_question_and_no_decision_stays_as_it_is(self):
        assert may_write_current(None, False) is False


def test_the_documents_made_current_are_the_ones_the_pass_left_for_the_lineages_it_kept_current():
    left = {"L1": fact("a"), "L2": fact("b"), "L3": fact("c")}
    decisions = {"L1": decision("revived"), "L2": decision("archived"), "L3": decision("rewritten")}

    assert made_current_documents(decisions, left) == {left["L1"].id, left["L3"].id}
    assert made_current_documents({}, left) == set()


class TestReasonPlanDate:
    def test_only_a_date_passed_basis_has_a_plan_date(self):
        passed = ReviewBasis("date_passed", on=date(2026, 9, 1))

        assert reason_plan_date(fact("f1"), passed, "UTC") == date(2026, 9, 1)
        assert reason_plan_date(fact("f1"), ReviewBasis("evidence", evidence_fact_id="f-other"), "UTC") is None
        assert reason_plan_date(fact("f1"), None, "UTC") is None


class TestNeedsChangeCheck:
    def test_an_expiry_refusal_is_about_the_update_itself(self):
        for refusal in EXPIRY_REFUSALS:
            assert needs_change_check(refusal) is False

    def test_any_other_failure_is_checked_for_a_change_outside_the_review(self):
        assert needs_change_check(None) is True
        for refusal in set(UpdateRefusal) - set(EXPIRY_REFUSALS):
            assert needs_change_check(refusal) is True
