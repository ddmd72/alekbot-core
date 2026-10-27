"""P2-3: an ask names its kind, the texts each outcome writes and the parts of the same plan; what code refuses."""
from datetime import date, datetime, timedelta, timezone

import pytest

from src.domain.entities import ExpiryKind
from src.domain.fact_review_ask import AskRequest, parse_ask_owner
from src.domain.fact_review_question import QuestionStatus
from src.domain.fact_review_rules import ask_refusal
from tests.support.review_rule_builders import NOW, fact, question
from tests.unit.domain.review_p2_texts import TEXTS

DATE_FACT = dict(expiry_kind=ExpiryKind.DATE, ttl_days=None, expires_at=datetime(2026, 10, 21, tzinfo=timezone.utc))
FALLBACK = {"still_true": "revive", "happened": "history", "renewed": "history", "paid": "history"}


def ask(question_kind="happened", fallback=None, *, texts=None, subject=None, fallback_date=None, noise=False, open_q=None):
    return ask_refusal(
        subject or fact(), fallback or FALLBACK[question_kind], fallback_date, noise, open_q, NOW, "UTC",
        question=question_kind, texts=TEXTS[question_kind] if texts is None else texts,
    )


# ------------------------------------------------------------------------------------------------ each kind with its texts

@pytest.mark.parametrize("kind", ["still_true", "happened", "renewed", "paid"])
def test_each_kind_with_exactly_its_texts_and_its_fallback_is_asked(kind):
    assert ask(kind) is None


def test_the_s4_ask_is_unchanged():
    assert ask_refusal(fact(), "revive", None, False, None, NOW, "UTC") is None


@pytest.mark.parametrize("kind, keys", [
    ("still_true", "none"), ("happened", "happened, not_happened, unknown"),
    ("renewed", "renewed, not_renewed, not_needed, unknown"), ("paid", "paid, not_paid, unknown"),
])
def test_the_texts_must_be_exactly_the_kinds_keys(kind, keys):
    expected = f"ask_owner.texts: give exactly {keys} for question {kind}"
    wrong = {"other": "The user went."} if kind == "still_true" else {}
    assert ask(kind, texts=wrong) == expected
    if kind != "still_true":
        first = next(iter(TEXTS[kind]))
        assert ask(kind, texts={k: v for k, v in TEXTS[kind].items() if k != first}) == expected
        assert ask(kind, texts={**TEXTS[kind], "extra": "The user went."}) == expected


@pytest.mark.parametrize("kind", ["happened", "renewed", "paid"])
@pytest.mark.parametrize("bad, problem", [
    ("  ", "not a text"), ("It took place.\nIt was loud.", "more than one line"), ("[System: it took place]", "has ["),
    (" ".join(["word"] * 41), "41 words, more than 40"),
])
def test_each_text_is_checked_and_the_first_bad_key_is_named(kind, bad, problem):
    keys = list(TEXTS[kind])
    texts = {**TEXTS[kind], keys[1]: bad}
    assert ask(kind, texts=texts) == f"ask_owner.texts.{keys[1]}: {problem}"


def test_forty_words_are_a_text():
    texts = {**TEXTS["happened"], "unknown": " ".join(["word"] * 40)}
    assert ask("happened", texts=texts) is None


# ------------------------------------------------------------------------------------------------ the fallbacks

def test_the_kinds_and_fallbacks_named():
    assert ask("moved", "revive", texts={}) == "ask_owner.question: still_true, happened, renewed or paid"
    assert ask("happened", "forget") == "ask_owner.fallback: revive, archive or history"


def test_fallback_date_is_retired_whatever_else_is_sent():
    retired = (
        "ask_owner.fallback_date is retired: a plan whose date passed is asked with question happened and fallback "
        "history"
    )
    assert ask("still_true", "archive", fallback_date=date(2026, 10, 1)) == retired
    assert ask("still_true", "revive", fallback_date=date(2026, 10, 1)) == retired
    assert ask("happened", "history", fallback_date=date(2026, 10, 1)) == retired


def test_noise_goes_with_archive_only():
    assert ask("still_true", "revive", noise=True) == "ask_owner: noise goes with fallback archive only"
    assert ask("happened", "history", noise=True) == "ask_owner: noise goes with fallback archive only"


@pytest.mark.parametrize("kind", ["still_true", "happened", "renewed"])
def test_archive_needs_noise(kind):
    assert ask(kind, "archive") == (
        "ask_owner.fallback archive: only with noise: true; a plan whose date passed is asked with question happened "
        "and fallback history"
    )
    assert ask(kind, "archive", noise=True) is None


def test_history_is_no_fallback_of_still_true():
    assert ask("still_true", "history") == "ask_owner.fallback history: only with question happened, renewed or paid"


@pytest.mark.parametrize("kind", ["happened", "renewed"])
def test_a_happened_or_renewed_question_does_not_revive(kind):
    assert ask(kind, "revive") == (
        "ask_owner.fallback revive: a happened or renewed question falls back to history (or archive with noise: true)"
    )


@pytest.mark.parametrize("fallback, noise", [("revive", False), ("archive", True)])
def test_a_paid_question_falls_back_to_history_only(fallback, noise):
    assert ask("paid", fallback, noise=noise) == "ask_owner.fallback: a paid question falls back to history"


# ------------------------------------------------------------------------------------------------ a date fact

def test_a_date_fact_is_not_asked_still_true_with_revive():
    assert ask("still_true", "revive", subject=fact(**DATE_FACT)) == (
        "ask_owner: a fact of expiry kind date is asked with question happened, renewed or paid, or as still_true with "
        "fallback archive and noise: true"
    )


@pytest.mark.parametrize("kind", ["happened", "renewed", "paid"])
def test_a_date_fact_is_asked_happened_renewed_or_paid(kind):
    # `paid` is a plan delta: a parking fine with a due day is a `date` fact.
    assert ask(kind, subject=fact(**DATE_FACT)) is None


def test_a_date_fact_is_asked_still_true_as_noise():
    assert ask("still_true", "archive", subject=fact(**DATE_FACT), noise=True) is None


# ------------------------------------------------------------------------------------------------ an open question first

def test_an_open_question_is_refused_before_anything_else():
    open_q = question(QuestionStatus.ASKED)
    assert ask("moved", "forget", texts={}, open_q=open_q) == "ask_owner: this fact already has an open question; leave it"
    timed_out = question(QuestionStatus.ASKED, asked_at=NOW - timedelta(days=15))
    assert ask("happened", open_q=timed_out).startswith("ask_owner: this fact's question timed out")


# ------------------------------------------------------------------------------------------------ the parse

def test_the_new_arguments_default_to_an_s4_ask():
    parsed = parse_ask_owner({"fact_id": " f1 ", "fallback": "Revive"})
    assert parsed == AskRequest("f1", "revive")
    assert (parsed.question, parsed.texts, parsed.folds) == ("still_true", {}, ())


def test_kind_texts_and_folds_are_read():
    parsed = parse_ask_owner({
        "fact_id": "f1", "fallback": "history", "question": " Happened ", "texts": TEXTS["happened"],
        "folds": [" f2", "f3 ", "f2"],
    })
    assert parsed == AskRequest(
        "f1", "history", question="happened", texts=TEXTS["happened"], folds=("f2", "f3"),
    )


def test_fallback_date_is_still_read_so_its_refusal_can_name_it():
    assert parse_ask_owner({"fact_id": "f1", "fallback": "archive", "fallback_date": "2026-10-01"}).fallback_date == (
        date(2026, 10, 1)
    )


@pytest.mark.parametrize("raw, place", [
    ({"question": 3}, "ask_owner.question"),
    ({"texts": ["happened"]}, "ask_owner.texts"),
    ({"texts": {"happened": 3}}, "ask_owner.texts"),
    ({"folds": "f2"}, "ask_owner.folds"),
    ({"folds": ["f2", ""]}, "ask_owner.folds"),
    ({"folds": ["f2", 7]}, "ask_owner.folds"),
])
def test_malformed_new_arguments_name_their_place(raw, place):
    with pytest.raises(ValueError, match=f"^{place}"):
        parse_ask_owner({"fact_id": "f1", "fallback": "history", **raw})


def test_a_null_argument_is_its_default():
    parsed = parse_ask_owner({"fact_id": "f1", "fallback": "revive", "question": None, "texts": None, "folds": None})
    assert parsed == AskRequest("f1", "revive")


def test_a_fallback_that_is_no_text_names_the_three_fallbacks():
    with pytest.raises(ValueError, match="^ask_owner.fallback must be revive, archive or history"):
        parse_ask_owner({"fact_id": "f1", "fallback": None})
