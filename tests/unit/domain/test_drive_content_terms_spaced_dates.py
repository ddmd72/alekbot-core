"""Space-separated dates are exact terms on both sides (USER_DRIVE_INDEXING_RFC §26.5 Δ62): "07 05 2031" as a card
prints it gave only "2031". Validated, never a bare triple of numbers: not inside a phone/IBAN run, year 1900-2099."""
import pytest

from src.domain.drive_content_terms import content_terms, query_terms

NBSP = "\u00a0"
NNBSP = "\u202f"


def test_a_spaced_date_is_its_iso_terms_on_both_sides_with_both_readings():
    # day and month both <= 12: both readings, as the dotted form gives
    assert query_terms("07 05 2031") == tuple(sorted(("2031-05-07", "2031-07-05"), key=lambda t: (-len(t), t)))
    terms = content_terms("Valid until 07 05 2031 for the holder.")
    assert "2031-05-07" in terms and "2031-07-05" in terms


def test_one_reading_when_the_day_exceeds_twelve():
    assert query_terms("25 05 2031") == ("2031-05-25",)
    assert "2031-05-25" in content_terms("Expires 25 05 2031.")


@pytest.mark.parametrize("text", ["25  05  2031", f"25{NBSP}05{NBSP}2031", f"25{NNBSP}05 {NBSP}2031", "5 5 2031"])
def test_any_run_of_spaces_between_the_parts(text):
    assert "2031-05-25" in query_terms(text) or "2031-05-05" in query_terms(text)
    assert query_terms(text)[0].startswith("2031-")


def test_the_dotted_and_spaced_forms_meet():
    assert set(query_terms("07.05.2031")) == set(query_terms("07 05 2031"))


@pytest.mark.parametrize("text", ["25 05 1899", "25 05 2100", "25 05 3031"])
def test_a_year_outside_1900_2099_is_not_a_date(text):
    assert not any("-" in t for t in content_terms(text) + query_terms(text))


def test_an_impossible_date_is_not_a_date():
    assert not any("-" in t for t in content_terms("31 02 2031"))
    assert not any("-" in t for t in content_terms("45 13 2031"))


@pytest.mark.parametrize("text", [
    "+44 07 05 2031 12",          # a phone: the triple is part of a longer run of digit groups
    "PT50 07 05 2031 1234 5678",  # an IBAN
    "600 07 05 2031",
])
def test_a_triple_inside_a_phone_or_iban_run_is_not_a_date(text):
    assert not any(t.count("-") == 2 for t in content_terms(text))
    assert not any(t.count("-") == 2 for t in query_terms(text))


def test_a_date_beside_a_short_run_is_still_a_date():
    assert "2031-05-25" in content_terms("Issued 25 05 2031 in Springfield")
