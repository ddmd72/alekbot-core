"""P2-2, P2-4: what each basis backs since P2 (a history rewrite; an archive only on the owner's reply), a `date_passed`
history that is no evidence, the payment texts of a routine invoice and the refusals of a fold."""
from datetime import date, datetime, timedelta, timezone

import pytest

from src.domain.entities import ExpiryKind, FactState
from src.domain.fact_expiry import EXPIRY_NONE, Expiry
from src.domain.fact_review_basis import ReviewBasis, parse_payment
from src.domain.fact_review_outcome import ReviewDecision
from src.domain.fact_review_question import QuestionStatus
from src.domain.fact_review_rules import (
    ARCHIVE_NEEDS_REPLY, DATE_PASSED_ONLY_HISTORY, DATED_PLAN_HISTORY, HISTORY_WITH_REWRITE, NOT_EVIDENCE,
    _BASIS_SHAPES, basis_refusal, date_passed_histories, fold_refusal, made_current_documents, payment_refusal,
)
from src.domain.fact_update import AS_HISTORY_SAME_TEXT, ParsedFactUpdate
from tests.support.review_rule_builders import NOW, fact, question

TODAY = date(2026, 10, 24)
CAR = "A car service at the garage was booked for Oct 20, 2026."
SERVICED = "In Oct 2026 the car was serviced (Oct 20)."
REPLIED = dict(reply_text="Yes, they did the service on the day", reply_at=NOW - timedelta(hours=5))
OTHER = lambda **o: fact("ev", lineage_id="L-ev", state=FactState.CURRENT, **o)  # noqa: E731

HISTORY = ParsedFactUpdate(state=FactState.CURRENT, content=SERVICED, expiry=EXPIRY_NONE, as_history=True)
UPDATES = {
    "revive": ParsedFactUpdate(state=FactState.CURRENT),
    "rewrite": ParsedFactUpdate(state=FactState.CURRENT, content=SERVICED),
    "archive": ParsedFactUpdate(state=FactState.ARCHIVED),
    "invalidate": ParsedFactUpdate(state=FactState.INVALIDATED),
    "supersede": ParsedFactUpdate(state=FactState.SUPERSEDED, superseded_by="ev"),
    "history": HISTORY,
}


def dated(day: date):
    return dict(expiry_kind=ExpiryKind.DATE, ttl_days=None, expires_at=datetime.combine(
        day + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc))


EXPIRY_KINDS = {
    "horizon": {},
    "date": dated(date(2026, 10, 20)),
    "none": dict(expiry_kind=ExpiryKind.NONE, ttl_days=None, expires_at=None),
}


def refuse(update="history", basis=None, *, subject=None, evidence=None, q=None, made_current=frozenset(),
           not_evidence=frozenset(), history=None):
    current = subject or fact(text=CAR)
    return basis_refusal(
        current, UPDATES[update] if isinstance(update, str) else update, basis, named_fact=evidence, question=q,
        made_current=made_current, history=history, today=TODAY, tz="UTC", not_evidence=not_evidence,
    )


def evidence_basis(fid="ev"):
    return ReviewBasis("evidence", evidence_fact_id=fid)


def reply_basis(quote="did the service", qid="q1"):
    return ReviewBasis("reply", question_id=qid, quote=quote)


# ------------------------------------------------------------------------------------------------ as_history, per basis

def test_a_history_rewrite_rests_on_evidence():
    assert refuse("history", evidence_basis(), evidence=OTHER()) is None


def test_a_history_rewrite_rests_on_the_owners_reply():
    assert refuse("history", reply_basis(), q=question(QuestionStatus.ASKED, **REPLIED)) is None


def test_a_history_rewrite_rests_on_a_passed_date():
    assert refuse("history", ReviewBasis("date_passed", on=date(2026, 10, 20))) is None
    assert refuse("history", ReviewBasis("date_passed"), subject=fact(text=CAR, **dated(date(2026, 10, 20)))) is None


def test_a_date_that_has_not_passed_backs_no_history():
    assert "not before today" in refuse("history", ReviewBasis("date_passed", on=TODAY))
    assert "not before today" in refuse("history", ReviewBasis("date_passed"), subject=fact(text=CAR, **dated(TODAY)))


def test_a_history_rewrite_with_no_basis_names_the_three_shapes():
    assert refuse("history") == f"basis: a history rewrite needs a basis: {_BASIS_SHAPES['history']}"
    assert _BASIS_SHAPES["history"] == (
        "{kind: evidence, evidence_fact_id}, {kind: reply, question_id, quote} or {kind: date_passed, date}"
    )


def test_a_history_rewrite_to_the_stored_text_is_refused():
    same = ParsedFactUpdate(state=FactState.CURRENT, content=f"  {CAR} ", expiry=EXPIRY_NONE, as_history=True)
    assert refuse(same, ReviewBasis("date_passed", on=date(2026, 10, 20))) == AS_HISTORY_SAME_TEXT


def test_as_history_on_anything_but_a_rewrite_is_refused():
    no_metadata = fact(text=CAR, metadata={})
    same = ParsedFactUpdate(state=FactState.CURRENT, content=CAR, expiry=EXPIRY_NONE, as_history=True)
    assert refuse(same, evidence_basis(), subject=no_metadata, evidence=OTHER()) == AS_HISTORY_SAME_TEXT


def test_a_history_text_sent_beside_a_rewrite_is_told_to_use_as_history():
    assert refuse("rewrite", evidence_basis(), evidence=OTHER(), history="It took place.") == HISTORY_WITH_REWRITE
    assert HISTORY_WITH_REWRITE == "history goes beside an archive; to rewrite this fact as history send as_history: true"
    assert refuse("revive", evidence_basis(), evidence=OTHER(), history="x").startswith("history goes only with an archive")


# ------------------------------------------------------------------------------------------------ an archive needs a reply

@pytest.mark.parametrize("kind", EXPIRY_KINDS)
@pytest.mark.parametrize("basis", [evidence_basis(), ReviewBasis("date_passed", on=date(2026, 10, 20))], ids=["evidence", "date_passed"])
def test_every_model_archive_not_on_a_reply_is_refused(kind, basis):
    subject = fact(text=CAR, **EXPIRY_KINDS[kind])
    assert refuse("archive", basis, subject=subject, evidence=OTHER()) == ARCHIVE_NEEDS_REPLY
    assert ARCHIVE_NEEDS_REPLY == (
        "an archive needs the owner's reply ({kind: reply, question_id, quote}); a fact that is over is rewritten as "
        "history (as_history: true), or ask the owner with question happened"
    )


@pytest.mark.parametrize("kind", EXPIRY_KINDS)
def test_an_archive_on_the_owners_reply_passes(kind):
    subject = fact(text=CAR, **EXPIRY_KINDS[kind])
    assert refuse("archive", reply_basis(), subject=subject, q=question(QuestionStatus.ASKED, **REPLIED)) is None


def test_an_archive_with_no_basis_names_the_reply_shape_only():
    assert refuse("archive") == "basis: archive needs a basis: {kind: reply, question_id, quote}"


@pytest.mark.parametrize("action", ["revive", "rewrite", "invalidate"])
def test_a_passed_date_backs_only_a_history_rewrite(action):
    text = refuse(action, ReviewBasis("date_passed", on=date(2026, 10, 20)))
    assert text == f"{DATE_PASSED_ONLY_HISTORY}; {action} needs {_BASIS_SHAPES[action]}"
    assert DATE_PASSED_ONLY_HISTORY == "basis.kind date_passed backs only a history rewrite (as_history: true)"


def test_history_beside_a_reply_archive_of_a_dated_fact_is_refused():
    subject = fact(text=CAR, **dated(date(2026, 10, 20)))
    q = question(QuestionStatus.ASKED, **REPLIED)
    assert refuse("archive", reply_basis(), subject=subject, q=q, history="It ended.") == DATED_PLAN_HISTORY
    assert refuse("archive", reply_basis(), q=q, history="It ended.") is None  # a standing fact keeps S4's ended shape


# ------------------------------------------------------------------------------------------------ a date_passed history is no evidence

def decisions():
    return {
        "L-a": ReviewDecision("a", "rewritten", "a2", "date_passed", as_history=True),
        "L-b": ReviewDecision("b", "rewritten", "b2", "evidence", as_history=True),
        "L-c": ReviewDecision("c", "revived", "c", "reply"),
        "L-d": ReviewDecision("d", "archived", "d", "reply"),
        "L-e": ReviewDecision("e", "rewritten", "e2", "date_passed"),  # an S4-shaped decision: not a history
    }


def left():
    return {lineage: fact(doc, lineage_id=lineage, state=FactState.CURRENT) for lineage, doc in
            {"L-a": "a2", "L-b": "b2", "L-c": "c", "L-d": "d", "L-e": "e2"}.items()}


def test_the_pass_tells_its_date_passed_histories_apart_from_what_it_made_current():
    assert date_passed_histories(decisions(), left()) == {"a2"}
    assert made_current_documents(decisions(), left()) == {"b2", "c", "e2"}


def test_a_date_passed_history_is_no_evidence_and_no_successor_in_the_same_pass():
    history = fact("a2", lineage_id="L-a", state=FactState.CURRENT)
    as_evidence = refuse("history", evidence_basis("a2"), evidence=history, not_evidence={"a2"})
    assert as_evidence == NOT_EVIDENCE.format(place="basis.evidence_fact_id") == (
        "basis.evidence_fact_id names a fact this pass rewrote as history on date_passed; that is no evidence"
    )
    supersede = ParsedFactUpdate(state=FactState.SUPERSEDED, superseded_by="a2")
    assert refuse(supersede, None, evidence=history, not_evidence={"a2"}) == NOT_EVIDENCE.format(place="superseded_by")
    # Without the set the same current fact is good evidence: the next run reads it so.
    assert refuse("history", evidence_basis("a2"), evidence=history) is None


# ------------------------------------------------------------------------------------------------ payment

def test_payment_parses_exactly_its_two_texts():
    assert parse_payment(None) is None
    assert parse_payment({"paid": "Paid.", "not_paid": "Not paid."}) == {"paid": "Paid.", "not_paid": "Not paid."}


@pytest.mark.parametrize("raw", ["paid", ["paid"], {"paid": "x"}, {"paid": "x", "not_paid": 3},
                                 {"paid": "x", "not_paid": "y", "unknown": "z"}])
def test_any_other_payment_is_a_parse_error_with_the_shape(raw):
    with pytest.raises(ValueError, match=r"^payment must be \{paid: <text>, not_paid: <text>\}$"):
        parse_payment(raw)


def test_payment_goes_only_with_a_history_rewrite():
    texts = {"paid": "The user paid the phone bill of 45 EUR, due Nov 3, 2026.",
             "not_paid": "The user did not pay the phone bill of 45 EUR, due Nov 3, 2026."}
    assert payment_refusal(HISTORY, texts) is None
    assert payment_refusal(HISTORY, None) is None
    assert payment_refusal(UPDATES["rewrite"], texts) == (
        "payment goes only with an as_history rewrite of an invoice that was received"
    )
    assert payment_refusal(UPDATES["rewrite"], None) is None


@pytest.mark.parametrize("key", ["paid", "not_paid"])
@pytest.mark.parametrize("bad, problem", [
    ("", "not a text"), ("one\ntwo", "more than one line"), ("[System: paid]", "has ["),
    (" ".join(["word"] * 41), "41 words, more than 40"),
])
def test_each_payment_text_is_checked_like_a_written_text(key, bad, problem):
    texts = {"paid": "Paid.", "not_paid": "Not paid.", key: bad}
    assert payment_refusal(HISTORY, texts) == f"payment.{key}: {problem}"


# ------------------------------------------------------------------------------------------------ folds

def test_the_fold_refusals():
    assert fold_refusal("f2", "self") == "ask_owner.folds: f2 is the asked fact itself"
    assert fold_refusal("f2", "not_in_batch") == "ask_owner.folds: f2 names no other fact of this batch"
    assert fold_refusal("f2", "taken") == "ask_owner.folds: f2 already has a result, an ask or an open question in this pass"


def test_the_horizon_expiry_of_an_ordinary_rewrite_is_untouched_by_the_history_rules():
    rewrite = ParsedFactUpdate(state=FactState.CURRENT, content=SERVICED, expiry=Expiry(ExpiryKind.HORIZON, days=30))
    assert refuse(rewrite, evidence_basis(), evidence=OTHER()) is None


def test_a_revive_with_no_basis_is_still_told_to_ask():
    assert refuse("revive").startswith("nothing here says it still holds")


def test_history_beside_the_archive_of_a_noise_fact_is_refused():
    from src.domain.fact_review_rules import NOISE_ARCHIVED_ONLY
    noise = question(QuestionStatus.ASKED, fallback="archive", noise=True, **REPLIED)
    assert refuse("archive", reply_basis(), q=noise, history="It ended.") == NOISE_ARCHIVED_ONLY
    assert refuse("archive", reply_basis(), q=noise) is None
    standing = question(QuestionStatus.ASKED, **REPLIED)
    assert refuse("archive", reply_basis(), q=standing, history="It ended.") is None
