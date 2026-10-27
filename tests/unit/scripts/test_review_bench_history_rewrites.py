"""Bench 7 reads P2: a review's rewrite as history is not an unbased revive, a bare move to current still is, and the
plan_date_passed case and the turn bar say what P2 changed."""
from datetime import date

import pytest

from scripts.consolidation import review_bench_cases as cases
from scripts.consolidation import review_bench_support as support
from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_basis import ReviewBasis
from src.domain.fact_review_reasons import answer_reason, basis_reason, fallback_reason

DAY = date(2026, 10, 15)
EVIDENCE = ReviewBasis("evidence", evidence_fact_id="abcd1234-feed-4bee-9c0d-talkfact01")
REPLY = ReviewBasis("reply", question_id="q-talk-1", quote="the talk went fine")
DATE_PASSED = ReviewBasis("date_passed", on=DAY)

HISTORY_REASONS = {
    "evidence": basis_reason("rewrite", EVIDENCE, as_history=True),
    "reply": basis_reason("rewrite", REPLY, as_history=True),
    "date passed": basis_reason("rewrite", DATE_PASSED, DAY, as_history=True),
    "happened": answer_reason(AnswerKind.HAPPENED, "q-talk-1", "review"),
    "not happened": answer_reason(AnswerKind.NOT_HAPPENED, "q-talk-1", "review"),
    "paid": answer_reason(AnswerKind.PAID, "q-fine-2", "review"),
    "not paid": answer_reason(AnswerKind.NOT_PAID, "q-fine-2", "review"),
    "not needed": answer_reason(AnswerKind.NOT_NEEDED, "q-gym-3", "review"),
    "renewed": answer_reason(AnswerKind.RENEWED, "q-gym-3", "review"),
    "not renewed": answer_reason(AnswerKind.NOT_RENEWED, "q-gym-3", "review"),
    "timeout": fallback_reason("q-talk-1", "history", "timeout"),
    "owner": fallback_reason("q-talk-1", "history", "owner"),
}


def _review_move(reason: str) -> dict:
    return {"actor": "review", "to": "current", "reason": reason}


@pytest.mark.parametrize("name", sorted(HISTORY_REASONS))
def test_a_review_history_rewrite_is_not_an_unbased_revive(name):
    assert support.unbased_revives([_review_move(HISTORY_REASONS[name])]) == []


def test_the_reasons_are_the_c4_texts():
    assert HISTORY_REASONS["evidence"] == "rewritten as history on abcd1234"
    assert HISTORY_REASONS["date passed"] == "rewritten as history: date passed 2026-10-15"
    assert HISTORY_REASONS["renewed"] == "renewed q=q-gym-3 (review)"


def test_a_bare_move_to_current_is_still_counted_beside_history_rewrites():
    bare = _review_move("looked fine")
    moves = [_review_move(r) for r in HISTORY_REASONS.values()]

    assert support.unbased_revives([*moves, bare]) == ["looked fine"]


def test_the_plan_date_passed_case_expects_a_question_or_a_history_not_an_archive():
    case = next(c for c in cases.stale_cases(support.utc_now()) if c.key == "plan_date_passed")

    assert case.expect == (
        "a horizon plan whose day passed and that nothing decides: asked with question happened (texts happened, "
        "not_happened, unknown; fallback history) or rewritten as history on evidence; never archived, never a "
        "date_passed history (its text reports no outcome)"
    )


def test_the_turn_bar_is_twelve_of_the_reviews_fifteen():
    assert support.G5_MAX_TURNS == 12
