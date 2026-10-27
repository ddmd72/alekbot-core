"""P2-3: the owner's answers per question kind, the texts they write, the history fallback and the fallback helpers."""
from datetime import datetime, timezone

import pytest

from src.domain.entities import ContextPriority, ExpiryKind, FactState
from src.domain.fact_expiry import EXPIRY_NONE
from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_answers import (
    DECIDING_ANSWERS, answer_closes_fact, answer_fits, answer_updates, deciding_answer, history_fallback, written_text,
)
from src.domain.fact_review_options import OPTIONS
from src.domain.fact_review_reasons import answer_reason, fallback_reason
from src.domain.fact_review_rules import (
    confirmed_expiry, fallback_fields, fallback_revives, fallback_sets_aside, fallback_writes_history,
)
from src.domain.fact_update import ParsedFactUpdate
from src.domain.fact_update_lifecycle import finish_fact_update
from src.domain.fact_update_plan import plan_fact_update
from src.domain.fact_words import word_count
from tests.support.clock_instants import BERLIN, NEW_YORK, OCT_END
from tests.support.fact_write import ctx
from tests.support.review_rule_builders import NOW, fact, question
from tests.unit.domain.review_p2_texts import DIGEST_TEXTS, HAPPENED_TEXTS, PAID_TEXTS, RENEWED_TEXTS, TEXTS

A = AnswerKind
OWNER_ZONE = BERLIN  # the user's zone in these tests
QID = "q1"
CONCERT = "The user plans to go to the concert on Dec 5, 2026."
DATED = dict(text=CONCERT, expiry_kind=ExpiryKind.DATE, ttl_days=None, expires_at=datetime(2026, 12, 6, tzinfo=timezone.utc))


def asked(kind, **over):
    fallback = "revive" if kind == "still_true" else "history"
    return question(question=kind, fallback=fallback, texts=TEXTS[kind], **over)


def digest():
    return question(question="paid", fallback="history", texts=DIGEST_TEXTS, delivery="digest")


def kept_twice_happened():
    return question(question="happened", fallback="history", origin="kept_twice")


# ------------------------------------------------------------------------------------------------ which answers fit

@pytest.mark.parametrize("kind", list(AnswerKind))
@pytest.mark.parametrize("qkind", ["still_true", "happened", "renewed", "paid"])
def test_an_answer_fits_exactly_its_questions_options(kind, qkind):
    assert answer_fits(kind, asked(qkind)) is (kind in OPTIONS[qkind])


@pytest.mark.parametrize("kind", list(AnswerKind))
def test_a_digest_question_takes_paid_and_not_paid_only(kind):
    assert answer_fits(kind, digest()) is (kind in (A.PAID, A.NOT_PAID))


def test_the_deciding_answers_are_each_kinds_options_without_dont_know():
    assert dict(DECIDING_ANSWERS) == {
        "still_true": (A.STILL_TRUE, A.ENDED, A.NEVER),
        "happened": (A.HAPPENED, A.NOT_HAPPENED),
        "renewed": (A.RENEWED, A.NOT_RENEWED, A.NOT_NEEDED),
        "paid": (A.PAID, A.NOT_PAID),
    }


@pytest.mark.parametrize("qkind", ["still_true", "happened", "renewed", "paid"])
def test_deciding_answer_reads_against_the_questions_own_options(qkind):
    q = asked(qkind)
    for kind in AnswerKind:
        assert deciding_answer(kind.value, q) == (kind if kind in DECIDING_ANSWERS[qkind] else None)


def test_without_a_question_any_kinds_deciding_answer_is_read_and_never_dont_know():
    for kind in AnswerKind:
        assert deciding_answer(kind.value) == (None if kind is A.DONT_KNOW else kind)
    for raw in (None, 1, "", "Paid", "maybe", ["paid"]):
        assert deciding_answer(raw) is None


# ------------------------------------------------------------------------------------------------ what each answer writes

HISTORY_CASES = [
    ("happened", A.HAPPENED, "happened"),
    ("happened", A.NOT_HAPPENED, "not_happened"),
    ("renewed", A.NOT_NEEDED, "not_needed"),
    ("paid", A.PAID, "paid"),
    ("paid", A.NOT_PAID, "not_paid"),
]


@pytest.mark.parametrize("qkind, kind, key", HISTORY_CASES)
@pytest.mark.parametrize("source", ["digit", "button", "cabinet"])
def test_a_history_answer_writes_the_questions_stored_text_as_history(qkind, kind, key, source):
    stale = fact(**DATED)
    change = answer_updates(kind, stale, asked(qkind), NOW, OWNER_ZONE, source)
    assert change.update == ParsedFactUpdate(
        state=FactState.CURRENT, content=TEXTS[qkind][key], expiry=EXPIRY_NONE, as_history=True
    )
    assert change.reason == answer_reason(kind, QID, source) == f"rewritten as history q={QID}: {kind.value} ({source})"
    assert change.evidence_at == NOW and change.history is None and change.fields is None


def test_a_digest_answer_writes_the_payment_text():
    change = answer_updates(A.PAID, fact(text=PAID_TEXTS["unknown"]), digest(), NOW, OWNER_ZONE, "cabinet")
    assert change.update.content == DIGEST_TEXTS["paid"] and change.update.as_history


def test_renewed_rewrites_to_its_text_with_the_confirmed_window_and_keeps_the_priority():
    stale = fact(text="The user has a gym membership until Nov 2026.", context_priority=ContextPriority.HIGH)
    change = answer_updates(A.RENEWED, stale, asked("renewed"), NOW, OWNER_ZONE, "digit")
    assert change.update == ParsedFactUpdate(
        state=FactState.CURRENT, content=RENEWED_TEXTS["renewed"], expiry=confirmed_expiry(stale, NOW, OWNER_ZONE)
    )
    assert change.reason == f"renewed q={QID} (digit)"
    plan = plan_fact_update(stale, change.update, NOW)
    version = finish_fact_update(plan, stale, change.update, ctx("owner"), NOW, change.evidence_at, change.reason)
    version = version.plan.version_fact
    assert version.context_priority is ContextPriority.HIGH and version.expiry_kind is ExpiryKind.HORIZON


def test_not_renewed_rewrites_to_its_text_with_no_expiry_and_keeps_the_priority():
    stale = fact(text="The user has a gym membership until Nov 2026.", context_priority=ContextPriority.HIGH)
    change = answer_updates(A.NOT_RENEWED, stale, asked("renewed"), NOW, OWNER_ZONE, "button")
    assert change.update == ParsedFactUpdate(
        state=FactState.CURRENT, content=RENEWED_TEXTS["not_renewed"], expiry=EXPIRY_NONE
    )
    assert change.reason == f"rewritten q={QID}: not_renewed (button)"
    plan = plan_fact_update(stale, change.update, NOW)
    version = finish_fact_update(plan, stale, change.update, ctx("owner"), NOW, change.evidence_at, change.reason)
    assert version.plan.version_fact.context_priority is ContextPriority.HIGH


@pytest.mark.parametrize("qkind", ["happened", "renewed", "paid"])
def test_dont_know_on_a_history_question_writes_the_unknown_text_under_the_owner(qkind):
    stale = fact(**DATED)
    change = answer_updates(A.DONT_KNOW, stale, asked(qkind), NOW, OWNER_ZONE, "digit")
    assert change == history_fallback(stale, asked(qkind), NOW, OWNER_ZONE, "owner")
    assert change.update == ParsedFactUpdate(
        state=FactState.CURRENT, content=TEXTS[qkind]["unknown"], expiry=EXPIRY_NONE, as_history=True
    )
    assert change.reason == f"rewritten as history: did not remember q={QID}"


def test_dont_know_on_a_revive_or_archive_question_is_still_the_in_place_fallback():
    stale = fact()
    for q in (asked("still_true"), question(fallback="archive", noise=True)):
        assert answer_updates(A.DONT_KNOW, stale, q, NOW, OWNER_ZONE, "digit").fields == fallback_fields(stale, q, NOW, "owner")


def test_ended_on_a_dated_fact_archives_only_and_a_standing_fact_gets_its_history():
    assert answer_updates(A.ENDED, fact(**DATED), asked("still_true"), NOW, OWNER_ZONE, "digit").history is None
    standing = answer_updates(A.ENDED, fact(), asked("still_true"), NOW, OWNER_ZONE, "digit")
    assert standing.history is not None and standing.history.startswith("No longer true")


@pytest.mark.parametrize("kind, q", [
    (A.HAPPENED, "still_true"), (A.ENDED, "happened"), (A.RENEWED, "paid"), (A.PAID, "renewed"), (A.STILL_TRUE, "paid"),
])
def test_a_misfit_answer_raises(kind, q):
    with pytest.raises(ValueError, match=f"^{kind.value} is not an option of a {q} question$"):
        answer_updates(kind, fact(), asked(q), NOW, OWNER_ZONE, "digit")


def test_dont_know_on_a_digest_question_is_a_misfit():
    with pytest.raises(ValueError):
        answer_updates(A.DONT_KNOW, fact(), digest(), NOW, OWNER_ZONE, "cabinet")


# ------------------------------------------------------------------------------------------------ the texts by origin

def test_a_review_questions_text_is_the_stored_one_and_a_missing_key_raises():
    assert written_text(asked("happened"), "not_happened", CONCERT, NOW, OWNER_ZONE) == HAPPENED_TEXTS["not_happened"]
    with pytest.raises(ValueError):
        written_text(asked("happened"), "paid", CONCERT, NOW, OWNER_ZONE)


def test_a_kept_twice_text_is_built_in_the_owners_month():
    at = OCT_END.utc  # the last second of October in UTC is already November in Berlin
    q = kept_twice_happened()
    assert written_text(q, "happened", CONCERT, at, BERLIN) == (
        f"Took place as planned (the owner confirmed in Nov 2026): {CONCERT}"
    )
    assert written_text(q, "not_happened", CONCERT, at, BERLIN) == (
        f"Did not take place (the owner said so in Nov 2026): {CONCERT}"
    )
    assert written_text(q, "unknown", CONCERT, at, BERLIN) == (
        f"Planned; whether it took place is unknown (no answer by Nov 2026): {CONCERT}"
    )
    assert written_text(q, "happened", CONCERT, at, "UTC").endswith(f"in Oct 2026): {CONCERT}")
    assert written_text(q, "happened", CONCERT, at, NEW_YORK).endswith(f"in Oct 2026): {CONCERT}")


def test_a_kept_twice_question_has_no_other_text():
    for key in ("paid", "renewed", "not_needed"):
        with pytest.raises(ValueError):
            written_text(kept_twice_happened(), key, CONCERT, NOW, OWNER_ZONE)


def test_a_kept_twice_text_keeps_a_long_stored_text_whole_past_forty_words():
    long_text = " ".join(["word"] * 40)
    built = written_text(kept_twice_happened(), "unknown", long_text, NOW, OWNER_ZONE)
    assert built.endswith(long_text) and word_count(built) > 40


@pytest.mark.parametrize("kind, key", [(A.HAPPENED, "happened"), (A.NOT_HAPPENED, "not_happened"), (A.DONT_KNOW, "unknown")])
def test_a_kept_twice_answer_writes_the_code_built_text(kind, key):
    stale = fact(**DATED)
    change = answer_updates(kind, stale, kept_twice_happened(), NOW, OWNER_ZONE, "digit")
    assert change.update.content == written_text(kept_twice_happened(), key, CONCERT, NOW, OWNER_ZONE)
    assert change.update.as_history


# ------------------------------------------------------------------------------------------------ the history fallback

def test_the_history_fallback_of_silence():
    stale = fact(**DATED)
    change = history_fallback(stale, asked("happened"), NOW, OWNER_ZONE, "timeout")
    assert change.update == ParsedFactUpdate(
        state=FactState.CURRENT, content=HAPPENED_TEXTS["unknown"], expiry=EXPIRY_NONE, as_history=True
    )
    assert change.reason == fallback_reason(QID, "history", "timeout") == f"rewritten as history: no answer to q={QID}"
    assert change.evidence_at == NOW


def test_the_history_fallback_needs_a_history_question():
    for q in (asked("still_true"), question(fallback="archive", noise=True)):
        with pytest.raises(ValueError):
            history_fallback(fact(), q, NOW, OWNER_ZONE, "timeout")


# ------------------------------------------------------------------------------------------------ which answers close the fact

def test_only_ended_never_and_an_archive_fallback_set_the_fact_aside():
    for kind in AnswerKind:
        expected = kind in (A.ENDED, A.NEVER)
        assert answer_closes_fact(kind, asked("still_true")) is expected
    assert answer_closes_fact(A.DONT_KNOW, question(fallback="archive", noise=True))
    assert not answer_closes_fact(A.DONT_KNOW, asked("happened"))


# ------------------------------------------------------------------------------------------------ the fallback helpers

@pytest.mark.parametrize("fallback, revives, sets_aside, history", [
    ("revive", True, False, False), ("archive", False, True, False), ("history", False, False, True),
])
def test_the_fallback_helpers_are_exhaustive(fallback, revives, sets_aside, history):
    q = {
        "revive": asked("still_true"),
        "archive": question(fallback="archive", noise=True),
        "history": asked("happened"),
    }[fallback]
    assert (fallback_revives(q), fallback_sets_aside(q), fallback_writes_history(q)) == (revives, sets_aside, history)


def test_fallback_fields_refuses_a_history_fallback():
    with pytest.raises(ValueError, match="history_fallback"):
        fallback_fields(fact(), asked("happened"), NOW, "timeout")


def test_fallback_fields_still_revives_and_archives():
    stale = fact()
    assert fallback_fields(stale, asked("still_true"), NOW, "timeout")["state"] is FactState.CURRENT
    assert fallback_fields(stale, question(fallback="archive", noise=True), NOW, "timeout")["state"] is FactState.ARCHIVED


# ------------------------------------------------------------------------------------------------ noise is archived only

def test_no_longer_on_a_noise_question_archives_with_no_history_and_a_standing_fact_keeps_its_history():
    # Arbiter (owner's rule: archive only for noise and never-true): noise is never history.
    flyer = fact(text="The user wondered about a tennis lesson flyer.")
    noise = answer_updates(A.ENDED, flyer, question(fallback="archive", noise=True), NOW, OWNER_ZONE, "digit")
    assert noise.update == ParsedFactUpdate(state=FactState.ARCHIVED) and noise.history is None
    standing = answer_updates(A.ENDED, fact(text="The user goes to a tennis lesson."), asked("still_true"), NOW, OWNER_ZONE, "digit")
    assert standing.update == ParsedFactUpdate(state=FactState.ARCHIVED)
    assert standing.history is not None and standing.history.endswith("The user goes to a tennis lesson.")
