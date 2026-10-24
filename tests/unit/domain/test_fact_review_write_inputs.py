"""`review_reason` and `evidence_moment` (Facts S4 · T6a fix round, L1/L2): the trail reason and the evidence moment of a
review write, from its action, basis and the fact the evidence or successor id was read as."""
from datetime import date, datetime, timedelta, timezone

from src.domain.fact_review_basis import ReviewBasis
from src.domain.fact_review_question import QuestionStatus
from src.domain.fact_review_reasons import basis_reason, evidence_moment, review_reason, superseded_reason
from tests.support.review_rule_builders import NOW, fact, question

SAID = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)
EVIDENCE = ReviewBasis("evidence", evidence_fact_id="old-id-of-neighbour")
REPLY = ReviewBasis("reply", question_id="q1", quote="still")
PASSED = ReviewBasis("date_passed", on=date(2026, 10, 1))


def test_an_evidence_reason_cites_the_document_it_was_read_as():
    named = fact("neighbour-v2")
    assert review_reason("revive", EVIDENCE, named, None) == basis_reason(
        "revive", ReviewBasis("evidence", evidence_fact_id="neighbour-v2")
    )


def test_an_evidence_reason_with_nothing_read_keeps_the_given_id():
    assert review_reason("archive", EVIDENCE, None, None) == "archived on old-id-o"


def test_a_supersede_names_the_successor_as_read_else_as_given():
    assert review_reason("supersede", None, fact("succ-v2"), "succ-v1") == superseded_reason("succ-v2")
    assert review_reason("supersede", None, None, "succ-v1") == superseded_reason("succ-v1")


def test_reply_and_date_passed_reasons_are_the_basis_builders():
    assert review_reason("rewrite", REPLY, None, None) == "rewritten on reply q=q1"
    assert review_reason("archive", PASSED, None, None, date(2026, 10, 1)) == "archived: date passed 2026-10-01"


def test_no_basis_is_no_reason():
    assert review_reason("revive", None, None, None) is None


def test_the_evidence_moment_is_the_evidence_facts_reported_date():
    assert evidence_moment(EVIDENCE, fact("n", reported_date=SAID), None) == SAID
    assert evidence_moment(EVIDENCE, None, None) is None


def test_the_evidence_moment_of_a_reply_is_when_the_owner_replied():
    replied = question(QuestionStatus.ASKED, reply_text="still", reply_at=NOW - timedelta(hours=1))
    assert evidence_moment(REPLY, None, replied) == NOW - timedelta(hours=1)
    assert evidence_moment(REPLY, None, None) is None


def test_date_passed_and_no_basis_have_no_evidence_moment():
    assert evidence_moment(PASSED, fact("n", reported_date=SAID), None) is None
    assert evidence_moment(None, fact("n", reported_date=SAID), None) is None
