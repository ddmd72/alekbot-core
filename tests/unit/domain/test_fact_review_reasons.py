"""The review's trail reasons: each builder's text, and whether the readers recognise it again (Facts S4 · T1b review H1)."""
from datetime import date, timedelta

import pytest

from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_basis import ReviewBasis
from src.domain.fact_review_reasons import (
    answer_reason,
    basis_reason,
    fallback_reason,
    gives_window_reason,
    is_revive_reason,
    kept_twice_reason,
    queued_reason,
)
from src.domain.fact_review_rules import fallback_window_days, revive_alert_due, revives_in_30_days
from tests.support.review_rule_builders import NOW, fact

DAY = timedelta(days=1)
EVIDENCE = ReviewBasis("evidence", evidence_fact_id="abcdef1234567890")
REPLY = ReviewBasis("reply", question_id="q1", quote="still")
PASSED = ReviewBasis("date_passed", on=date(2026, 10, 1))


def entry(reason, to="current"):
    return {"at": NOW, "from": "stale", "to": to, "actor": "review", "reason": reason}


# (builder output, counts as a revive, gives the next silence a window)
CASES = {
    "revive on evidence": (basis_reason("revive", EVIDENCE), True, True),
    "revive on reply": (basis_reason("revive", REPLY), True, True),
    "yes by digit": (answer_reason(AnswerKind.STILL_TRUE, "q1", "digit"), True, True),
    "yes by button": (answer_reason(AnswerKind.STILL_TRUE, "q1", "button"), True, True),
    "yes in the cabinet": (answer_reason(AnswerKind.STILL_TRUE, "q1", "cabinet"), True, True),
    "do not remember": (answer_reason(AnswerKind.DONT_KNOW, "q1"), True, True),
    "fallback revive on timeout": (fallback_reason("q1", "revive", "timeout"), True, True),
    "fallback revive by the owner": (fallback_reason("q1", "revive", "owner"), True, True),
    "rewrite on evidence": (basis_reason("rewrite", EVIDENCE), False, True),
    "rewrite on reply": (basis_reason("rewrite", REPLY), False, True),
    "archive on evidence": (basis_reason("archive", EVIDENCE), False, False),
    "archive on reply": (basis_reason("archive", REPLY), False, False),
    "archive on a passed date": (basis_reason("archive", PASSED, date(2026, 10, 1)), False, False),
    "invalidate on evidence": (basis_reason("invalidate", EVIDENCE), False, False),
    "invalidate on reply": (basis_reason("invalidate", REPLY), False, False),
    "ended": (answer_reason(AnswerKind.ENDED, "q1", "digit"), False, False),
    "never": (answer_reason(AnswerKind.NEVER, "q1", "cabinet"), False, False),
    "fallback archive on timeout": (fallback_reason("q1", "archive", "timeout"), False, False),
    "queued for the owner": (queued_reason("q1"), False, False),
    "kept twice": (kept_twice_reason("q1"), False, False),
}


@pytest.mark.parametrize("name", CASES)
def test_the_alert_reader_recognises_exactly_the_revive_builders(name):
    reason, is_revive, _ = CASES[name]
    assert is_revive_reason(reason) is is_revive
    assert revives_in_30_days([entry(reason)], NOW) == (1 if is_revive else 0)


@pytest.mark.parametrize("name", CASES)
def test_the_window_reader_recognises_the_revive_and_rewrite_builders(name):
    reason, _, gives_window = CASES[name]
    assert gives_window_reason(reason) is gives_window
    old = fact(reported_date=NOW - 400 * DAY, expires_at=NOW + 20 * DAY, lifecycle_events=[entry(reason)])
    # a window given now is 20 days (doubled: 40); one not recognised is measured from reported_date and caps at 365
    assert fallback_window_days(old) == (40 if gives_window else 365)


@pytest.mark.parametrize("name", [n for n in CASES if CASES[n][1]])
def test_a_revive_entry_that_leads_elsewhere_is_not_a_revive(name):
    assert revives_in_30_days([entry(CASES[name][0], to="archived")], NOW) == 0


def test_three_revives_of_any_builder_raise_the_alert():
    reasons = [CASES[n][0] for n in CASES if CASES[n][1]][:3]
    assert revive_alert_due([entry(r) for r in reasons], NOW)
    assert not revive_alert_due([entry(r) for r in reasons[:2]], NOW)


def test_the_reasons_read_as_the_review_lists_them():
    assert basis_reason("revive", EVIDENCE) == "revived on abcdef12"
    assert basis_reason("revive", REPLY) == "revived on reply q=q1"
    assert basis_reason("rewrite", EVIDENCE) == "rewritten on abcdef12"
    assert basis_reason("archive", REPLY) == "archived on reply q=q1"
    assert basis_reason("invalidate", EVIDENCE) == "invalidated on abcdef12"
    assert basis_reason("archive", PASSED, date(2026, 10, 1)) == "archived: date passed 2026-10-01"
    assert queued_reason("q1") == "queued for the owner q=q1"
    assert kept_twice_reason("q1") == "kept twice: queued q=q1"
    assert answer_reason(AnswerKind.STILL_TRUE, "q1", "digit") == "confirmed q=q1 (digit)"
    assert answer_reason(AnswerKind.ENDED, "q1", "button") == "ended q=q1 (button)"
    assert answer_reason(AnswerKind.NEVER, "q1", "cabinet") == "never q=q1 (cabinet)"
    assert answer_reason(AnswerKind.DONT_KNOW, "q1") == "did not remember q=q1"
    assert answer_reason(AnswerKind.DONT_KNOW, "q1", "digit") == "did not remember q=q1"
    assert fallback_reason("q1", "revive", "timeout") == "no answer to q=q1: revive"
    assert fallback_reason("q1", "archive", "timeout") == "no answer to q=q1: archive"
    assert fallback_reason("q1", "archive", "owner") == "did not remember q=q1"


@pytest.mark.parametrize("junk", [None, 7, "", "Revived on abc", "confirmed: q=1", "queued for the owner q=1", "revived", b"revived on x"])
def test_other_text_is_no_reason_of_either_reader(junk):
    assert not is_revive_reason(junk) and not gives_window_reason(junk)


def test_a_fallback_reason_without_the_revive_suffix_is_not_a_revive():
    assert not is_revive_reason("no answer to q=q1")
    assert not is_revive_reason("no answer to q=q1: archive")


def test_supersede_has_no_basis_reason_and_a_date_needs_an_archive_and_a_date():
    with pytest.raises(ValueError):
        basis_reason("supersede", EVIDENCE)
    with pytest.raises(ValueError):
        basis_reason("revive", PASSED, date(2026, 10, 1))
    with pytest.raises(ValueError):
        basis_reason("archive", PASSED)


def test_a_basis_without_the_thing_it_names_has_no_reason():
    with pytest.raises(ValueError):
        basis_reason("revive", ReviewBasis("evidence"))
    with pytest.raises(ValueError):
        basis_reason("revive", ReviewBasis("reply"))


def test_an_answer_other_than_do_not_remember_needs_a_source():
    for kind in (AnswerKind.STILL_TRUE, AnswerKind.ENDED, AnswerKind.NEVER):
        with pytest.raises(ValueError):
            answer_reason(kind, "q1")
