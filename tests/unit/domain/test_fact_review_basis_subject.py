"""`basis_refusal` with the batch fact being decided (Facts S4 · T6a fix round, M1): a reply basis is about the question's
fact lineage, so a follow-up on a version this pass wrote passes, and a reply about another fact is still refused."""
from datetime import date, timedelta

from src.domain.entities import ExpiryKind, FactState
from src.domain.fact_expiry import Expiry
from src.domain.fact_review_basis import ReviewBasis
from src.domain.fact_review_question import QuestionStatus
from src.domain.fact_review_rules import basis_refusal
from src.domain.fact_update import ParsedFactUpdate
from tests.support.review_rule_builders import NOW, fact, question

REPLY = ReviewBasis("reply", question_id="q1", quote="at Globex")
TAGS = ParsedFactUpdate(tags=["work", "globex"], expiry=Expiry(ExpiryKind.HORIZON, days=30))
TODAY = date(2026, 10, 24)


def replied(fact_id="f1"):
    return question(QuestionStatus.ASKED, fact_id=fact_id, reply_text="I work at Globex now",
                    reply_at=NOW - timedelta(hours=1))


def refusal(current, subject, q):
    return basis_refusal(current, TAGS, REPLY, named_fact=None, question=q, made_current={current.id}, history=None,
                         today=TODAY, tz="Europe/Paris", subject=subject)


def test_a_reply_backs_a_later_version_of_the_fact_it_was_asked_about():
    batch_fact = fact("f1", state=FactState.CURRENT)
    rewritten = batch_fact.model_copy(update={"id": "f1-v2", "version": 2, "text": "works at Globex"})
    assert refusal(rewritten, batch_fact, replied("f1")) is None


def test_without_the_subject_the_version_being_written_is_compared():
    batch_fact = fact("f1", state=FactState.CURRENT)
    rewritten = batch_fact.model_copy(update={"id": "f1-v2", "version": 2, "text": "works at Globex"})
    assert refusal(rewritten, None, replied("f1")) == "basis.question_id: the id of the question about this fact in the batch"
    assert refusal(batch_fact, None, replied("f1")) is None


def test_a_reply_about_another_fact_is_refused():
    other = fact("f2")
    assert refusal(other, other, replied("f1")) == "basis.question_id: the id of the question about this fact in the batch"
