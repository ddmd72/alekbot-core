"""P2-1: the trail reasons of a history rewrite and of the new answers, and how the readers recognise them."""
from datetime import date, timedelta

import pytest

from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_basis import ReviewBasis
from src.domain.fact_review_reasons import (
    answer_reason, basis_reason, fallback_reason, gives_window_reason, is_revive_reason, is_rewrite_reason,
    review_reason,
)
from src.domain.fact_review_rules import fallback_window_days, revives_in_window
from tests.support.review_rule_builders import NOW, fact

DAY = timedelta(days=1)
EVIDENCE = ReviewBasis("evidence", evidence_fact_id="abcdef1234567890")
REPLY = ReviewBasis("reply", question_id="q1", quote="the concert was great")
PASSED = ReviewBasis("date_passed", on=date(2026, 12, 5))
QID = "qa1b2"

# (builder output, the exact text the contract names)
NEW_REASONS = {
    "history on evidence": (basis_reason("rewrite", EVIDENCE, as_history=True), "rewritten as history on abcdef12"),
    "history on reply": (basis_reason("rewrite", REPLY, as_history=True), "rewritten as history on reply q=q1"),
    "history on a passed date": (
        basis_reason("rewrite", PASSED, date(2026, 12, 5), as_history=True), "rewritten as history: date passed 2026-12-05",
    ),
    "happened": (
        answer_reason(AnswerKind.HAPPENED, QID, "digit"), f"rewritten as history q={QID}: happened (digit)",
    ),
    "not happened": (
        answer_reason(AnswerKind.NOT_HAPPENED, QID, "button"), f"rewritten as history q={QID}: not_happened (button)",
    ),
    "not needed": (
        answer_reason(AnswerKind.NOT_NEEDED, QID, "cabinet"), f"rewritten as history q={QID}: not_needed (cabinet)",
    ),
    "paid": (answer_reason(AnswerKind.PAID, QID, "cabinet"), f"rewritten as history q={QID}: paid (cabinet)"),
    "not paid": (answer_reason(AnswerKind.NOT_PAID, QID, "digit"), f"rewritten as history q={QID}: not_paid (digit)"),
    "renewed": (answer_reason(AnswerKind.RENEWED, QID, "button"), f"renewed q={QID} (button)"),
    "not renewed": (answer_reason(AnswerKind.NOT_RENEWED, QID, "digit"), f"rewritten q={QID}: not_renewed (digit)"),
    "silence writes history": (fallback_reason(QID, "history", "timeout"), f"rewritten as history: no answer to q={QID}"),
    "do not remember writes history": (
        fallback_reason(QID, "history", "owner"), f"rewritten as history: did not remember q={QID}",
    ),
}


def entry(reason, to="current"):
    return {"at": NOW, "from": "stale", "to": to, "actor": "review", "reason": reason}


@pytest.mark.parametrize("name", NEW_REASONS)
def test_each_reason_reads_as_the_contract_names_it(name):
    built, expected = NEW_REASONS[name]
    assert built == expected


@pytest.mark.parametrize("name", NEW_REASONS)
def test_every_new_reason_is_a_rewrite_that_gives_a_window_and_none_is_a_revive(name):
    reason, _ = NEW_REASONS[name]
    assert is_rewrite_reason(reason) and gives_window_reason(reason)
    assert not is_revive_reason(reason)
    assert revives_in_window([entry(reason)], NOW) == 0


def test_a_window_is_counted_from_a_renewal():
    renewed = answer_reason(AnswerKind.RENEWED, QID, "digit")
    old = fact(reported_date=NOW - 400 * DAY, expires_at=NOW + 20 * DAY, lifecycle_events=[entry(renewed)])
    # The window runs from the renewal (20 days to expires_at, doubled), not from reported_date (capped at 365).
    assert fallback_window_days(old) == 40


def test_the_rewrite_prefixes_and_nothing_else():
    for reason in ("rewritten on abc", "rewritten as history: x", "rewritten q=q1: x", "renewed q=q1 (digit)"):
        assert is_rewrite_reason(reason)
    for reason in (None, 3, "renewed", "rewritten", "Renewed q=q1", "archived: date passed 2026-12-05"):
        assert not is_rewrite_reason(reason)


def test_as_history_goes_with_a_rewrite_only():
    for action in ("revive", "archive", "invalidate", "supersede"):
        with pytest.raises(ValueError):
            basis_reason(action, EVIDENCE, as_history=True)


def test_a_history_basis_needs_what_it_names():
    with pytest.raises(ValueError):
        basis_reason("rewrite", ReviewBasis("evidence"), as_history=True)
    with pytest.raises(ValueError):
        basis_reason("rewrite", ReviewBasis("reply"), as_history=True)
    with pytest.raises(ValueError):
        basis_reason("rewrite", PASSED, as_history=True)


def test_without_the_flag_a_passed_date_still_backs_only_the_s4_archive_reason():
    assert basis_reason("archive", PASSED, date(2026, 12, 5)) == "archived: date passed 2026-12-05"
    with pytest.raises(ValueError):
        basis_reason("rewrite", PASSED, date(2026, 12, 5))


def test_review_reason_passes_the_flag_and_cites_the_evidence_document_read():
    named = fact("e9f8e7d6c5", lineage_id="L-e")
    assert review_reason("rewrite", EVIDENCE, named, None, as_history=True) == "rewritten as history on e9f8e7d6"
    assert review_reason("rewrite", PASSED, None, None, date(2026, 12, 5), as_history=True) == (
        "rewritten as history: date passed 2026-12-05"
    )
    assert review_reason("rewrite", EVIDENCE, named, None) == "rewritten on e9f8e7d6"


def test_revive_and_archive_fallbacks_keep_their_s4_reasons():
    assert fallback_reason(QID, "revive", "timeout") == f"no answer to q={QID}: revive"
    assert fallback_reason(QID, "archive", "timeout") == f"no answer to q={QID}: archive"
    assert fallback_reason(QID, "revive", "owner") == f"did not remember q={QID}"
    assert fallback_reason(QID, "archive", "owner") == f"did not remember q={QID}"


def test_every_new_answer_needs_a_source():
    for kind in (AnswerKind.HAPPENED, AnswerKind.RENEWED, AnswerKind.NOT_RENEWED, AnswerKind.PAID):
        with pytest.raises(ValueError):
            answer_reason(kind, QID)
