"""`days` sent as a digit string is accepted like a whole-number float; anything else still refuses (Facts S3, T5 review F7)."""
import pytest

from src.domain.exceptions import FactExpiryError
from src.domain.fact_argument_parsing import parse_reported_date
from src.domain.fact_expiry import parse_expiry


@pytest.mark.parametrize("raw,days", [("90", 90), (" 14 ", 14), ("1", 1)])
def test_a_digit_string_is_a_whole_number_of_days(raw, days):
    assert parse_expiry({"kind": "horizon", "days": raw}).days == days


@pytest.mark.parametrize("raw", ["0", "-3", "2.5", "", "ten", "٣", "999999999"])
def test_other_strings_are_still_refused(raw):
    with pytest.raises(FactExpiryError, match="days"):
        parse_expiry({"kind": "horizon", "days": raw})


def test_the_reported_date_refusal_shows_the_offset_form_not_z():
    with pytest.raises(ValueError) as refused:
        parse_reported_date("Thu 12:00 CEST")
    assert "+01:00" in str(refused.value) and "Z" not in str(refused.value).split("such as")[1]
