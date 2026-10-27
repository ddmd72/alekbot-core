"""The review model's `basis` and `ask_owner` arguments as domain values (Facts S4 · T1b review L8)."""
from datetime import date

import pytest

from src.domain.fact_review_ask import AskRequest, parse_ask_owner
from src.domain.fact_review_basis import ReviewBasis, parse_day, parse_review_basis
from src.domain.fact_review_rules import ask_refusal
from tests.support.review_rule_builders import NOW, fact


# ------------------------------------------------------------------------------------------------ basis

def test_each_kind_is_read_with_its_own_fields():
    assert parse_review_basis({"kind": "evidence", "evidence_fact_id": " f1 "}) == ReviewBasis("evidence", evidence_fact_id="f1")
    assert parse_review_basis({"kind": "reply", "question_id": "q1", "quote": "still"}) == ReviewBasis(
        "reply", question_id="q1", quote="still"
    )
    assert parse_review_basis({"kind": "date_passed", "date": "2026-10-01"}) == ReviewBasis("date_passed", on=date(2026, 10, 1))
    assert parse_review_basis({"kind": "date_passed"}) == ReviewBasis("date_passed")


def test_a_missing_field_is_left_for_the_basis_rules_to_name():
    assert parse_review_basis({"kind": "evidence"}) == ReviewBasis("evidence")
    assert parse_review_basis({"kind": "reply", "question_id": "q1"}) == ReviewBasis("reply", question_id="q1")
    assert parse_review_basis({"kind": "evidence", "evidence_fact_id": "  "}) == ReviewBasis("evidence")


def test_unknown_keys_are_ignored():
    assert parse_review_basis({"kind": "evidence", "evidence_fact_id": "f1", "why": "because"}).evidence_fact_id == "f1"


@pytest.mark.parametrize("raw", [None, "evidence", ["evidence"], 7])
def test_a_basis_that_is_not_an_object_names_the_shapes(raw):
    with pytest.raises(ValueError, match="basis must be an object.*evidence_fact_id"):
        parse_review_basis(raw)


@pytest.mark.parametrize("kind", [None, "", "Evidence", "date", "ask"])
def test_an_unknown_kind_names_the_kinds(kind):
    with pytest.raises(ValueError, match="basis.kind must be evidence, reply or date_passed"):
        parse_review_basis({"kind": kind})


@pytest.mark.parametrize(
    "raw,foreign",
    [
        ({"kind": "evidence", "evidence_fact_id": "f1", "quote": "x"}, "quote"),
        ({"kind": "evidence", "question_id": "q1"}, "question_id"),
        ({"kind": "evidence", "date": "2026-10-01"}, "date"),
        ({"kind": "reply", "question_id": "q1", "evidence_fact_id": "f1"}, "evidence_fact_id"),
        ({"kind": "date_passed", "quote": "x"}, "quote"),
        ({"kind": "date_passed", "date": "2026-10-01", "question_id": "q"}, "question_id"),
    ],
)
def test_a_field_of_another_kind_is_refused(raw, foreign):
    with pytest.raises(ValueError, match=f"does not take {foreign}"):
        parse_review_basis(raw)


@pytest.mark.parametrize("value", ["2026-13-01", "10/01/2026", "2026-10-1", 20261001, "20261001", "2026-W40-1", "tomorrow"])
def test_a_bad_date_names_its_place_and_shape(value):
    with pytest.raises(ValueError, match=r"basis\.date must be a date as YYYY-MM-DD"):
        parse_review_basis({"kind": "date_passed", "date": value})


@pytest.mark.parametrize("field,value", [("evidence_fact_id", 7), ("quote", ["x"])])
def test_a_field_that_is_not_text_is_refused(field, value):
    raw = {"kind": "evidence", field: value} if field == "evidence_fact_id" else {"kind": "reply", field: value}
    with pytest.raises(ValueError, match=f"basis.{field} must be text"):
        parse_review_basis(raw)


def test_a_basis_value_cannot_carry_a_field_of_another_kind():
    with pytest.raises(ValueError, match="does not carry quote"):
        ReviewBasis("evidence", evidence_fact_id="f1", quote="x")
    with pytest.raises(ValueError, match="does not carry on"):
        ReviewBasis("reply", question_id="q1", on=date(2026, 10, 1))
    with pytest.raises(ValueError, match="does not carry question_id"):
        ReviewBasis("date_passed", question_id="q1")


def test_parse_day_reads_iso_dates_and_nothing_else():
    assert parse_day("2026-10-01", "x") == date(2026, 10, 1)
    assert parse_day(" 2026-10-01 ", "x") == date(2026, 10, 1)
    assert parse_day(None, "x") is None and parse_day("", "x") is None


# ------------------------------------------------------------------------------------------------ ask_owner

def test_an_ask_is_read_with_its_defaults():
    assert parse_ask_owner({"fact_id": " f1 ", "fallback": "Revive "}) == AskRequest("f1", "revive")
    assert parse_ask_owner({"fact_id": "f1", "fallback": "archive", "fallback_date": "2026-10-01", "noise": True}) == AskRequest(
        "f1", "archive", date(2026, 10, 1), True
    )


@pytest.mark.parametrize("flag,expected", [(True, True), (False, False), (None, False), ("true", True), ("False", False)])
def test_noise_accepts_booleans_and_their_text(flag, expected):
    assert parse_ask_owner({"fact_id": "f1", "fallback": "archive", "noise": flag}).noise is expected


def test_the_fallback_is_kept_as_text_for_the_ask_rules_to_judge():
    assert parse_ask_owner({"fact_id": "f1", "fallback": "ask"}).fallback == "ask"


def test_question_text_the_model_sends_is_ignored():
    # P2 revised (C8): `question` is the ask's kind now; a question text the model sends is no kind, kept as given
    # (lowercased) and refused by the ask rules, so it is never sent to the owner
    request = parse_ask_owner({"fact_id": "f1", "fallback": "revive", "question": "Still true?"})
    assert request == AskRequest("f1", "revive", question="still true?")
    assert ask_refusal(
        fact(), request.fallback, None, False, None, NOW, "UTC", question=request.question, texts=request.texts,
    ) == "ask_owner.question: still_true, happened, renewed or paid"


@pytest.mark.parametrize("raw", [None, "f1", ["f1"]])
def test_ask_arguments_that_are_not_an_object_name_the_shape(raw):
    with pytest.raises(ValueError, match="ask_owner arguments must be an object"):
        parse_ask_owner(raw)


@pytest.mark.parametrize("fact_id", [None, "", "  ", 7])
def test_an_ask_needs_a_fact_id(fact_id):
    with pytest.raises(ValueError, match=r"ask_owner\.fact_id"):
        parse_ask_owner({"fact_id": fact_id, "fallback": "revive"})


@pytest.mark.parametrize("fallback", [None, 7, ["revive"]])
def test_an_ask_needs_a_fallback_as_text(fallback):
    with pytest.raises(ValueError, match=r"ask_owner\.fallback"):
        parse_ask_owner({"fact_id": "f1", "fallback": fallback})


def test_a_bad_fallback_date_or_noise_names_its_argument():
    with pytest.raises(ValueError, match=r"ask_owner\.fallback_date must be a date"):
        parse_ask_owner({"fact_id": "f1", "fallback": "archive", "fallback_date": "soon"})
    with pytest.raises(ValueError, match=r"ask_owner\.noise must be true or false"):
        parse_ask_owner({"fact_id": "f1", "fallback": "archive", "noise": "maybe"})
