"""A decided update is held to the parser's rules (facts S4 · T3c review M1): `typed_update_refusal` refuses on the typed
path what `parse_fact_update` refuses on the model's, in the parser's words, and the model's texts are unchanged.

`Expiry` itself checks no bounds: a stored legacy value is read into one as it is (`stored_expiry`)."""
from datetime import date

import pytest

from src.domain.entities import ExpiryKind, FactState, TemporalClass
from src.domain.exceptions import FactExpiryError
from src.domain.fact_expiry import EXPIRY_NONE, MAX_HORIZON_DAYS, MAX_YEAR, MIN_YEAR, Expiry, expiry_refusal, stored_expiry
from src.domain.fact_review_answers import ARCHIVE_ONLY
from src.domain.fact_update import ParsedFactUpdate, parse_fact_update, typed_update_refusal

STALE = "only the system marks a fact stale; to retire a fact use ARCHIVED, INVALIDATED or SUPERSEDED"
NO_SUCCESSOR = "state SUPERSEDED needs superseded_by: the id of the fact that replaces this one"
SUPERSEDED_ALONE = (
    "state SUPERSEDED closes a fact in favour of superseded_by and cannot be combined with content, tags, metadata or "
    "temporal_class; send those as a separate update"
)
SUCCESSOR_ONLY = "superseded_by is accepted only together with state SUPERSEDED"
EMPTY_SUCCESSOR = "superseded_by must be the id of the replacing fact, a non-empty string"
EMPTY_CONTENT = "content must be a non-empty string; leave the key out to keep the current text"
EMPTY_TAGS = "tags must be a non-empty list of strings; leave the key out to keep the current tags"
_MODEL_TAIL = (
    ". Use one of {kind: date, date: YYYY-MM-DD}, {kind: horizon, days: N} or {kind: none}. Nothing was written. "
    "Resend this call with a valid expiry before retiring any fact it replaces."
)


def horizon(days):
    return Expiry(ExpiryKind.HORIZON, days=days)


# ------------------------------------------------------------------------------------------------ the typed path

@pytest.mark.parametrize("update, refusal", [
    (ParsedFactUpdate(state=FactState.STALE), STALE),
    (ParsedFactUpdate(state=FactState.SUPERSEDED), NO_SUCCESSOR),
    (ParsedFactUpdate(state=FactState.SUPERSEDED, superseded_by="f2", content="new text"), SUPERSEDED_ALONE),
    (ParsedFactUpdate(state=FactState.SUPERSEDED, superseded_by="f2", tags=["x"]), SUPERSEDED_ALONE),
    (ParsedFactUpdate(state=FactState.SUPERSEDED, superseded_by="f2", metadata={"k": "v"}), SUPERSEDED_ALONE),
    (ParsedFactUpdate(state=FactState.SUPERSEDED, superseded_by="f2", temporal_class=TemporalClass.STABLE),
     SUPERSEDED_ALONE),
    (ParsedFactUpdate(superseded_by="f2"), SUCCESSOR_ONLY),
    (ParsedFactUpdate(state=FactState.ARCHIVED, superseded_by="f2"), SUCCESSOR_ONLY),
    (ParsedFactUpdate(state=FactState.SUPERSEDED, superseded_by="  "), EMPTY_SUCCESSOR),
    (ParsedFactUpdate(content=""), EMPTY_CONTENT),
    (ParsedFactUpdate(content="   "), EMPTY_CONTENT),
    (ParsedFactUpdate(tags=[]), EMPTY_TAGS),
    (ParsedFactUpdate(tags=["ok", 3]), EMPTY_TAGS),
], ids=[
    "stale", "superseded without successor", "superseded with content", "superseded with tags",
    "superseded with metadata", "superseded with temporal class", "successor alone", "successor with archived",
    "blank successor", "empty content", "blank content", "empty tags", "a tag that is no string",
])
def test_each_rule_of_the_parser_refuses_a_typed_update_in_its_words(update, refusal):
    assert typed_update_refusal(update) == refusal


@pytest.mark.parametrize("expiry, refusal", [
    (horizon(0), f"expiry: kind horizon needs `days` as a whole number from 1 to {MAX_HORIZON_DAYS}, got 0"),
    (horizon(-3), f"expiry: kind horizon needs `days` as a whole number from 1 to {MAX_HORIZON_DAYS}, got -3"),
    (horizon(MAX_HORIZON_DAYS + 1),
     f"expiry: kind horizon needs `days` as a whole number from 1 to {MAX_HORIZON_DAYS}, got {MAX_HORIZON_DAYS + 1}"),
    (horizon(True), f"expiry: kind horizon needs `days` as a whole number from 1 to {MAX_HORIZON_DAYS}, got True"),
    (Expiry(ExpiryKind.DATE, on=date(MIN_YEAR - 1, 6, 1)),
     f"expiry: the year of `date` must be between {MIN_YEAR} and {MAX_YEAR}, got {MIN_YEAR - 1}-06-01"),
    (Expiry(ExpiryKind.DATE, on=date(MAX_YEAR + 1, 1, 1)),
     f"expiry: the year of `date` must be between {MIN_YEAR} and {MAX_YEAR}, got {MAX_YEAR + 1}-01-01"),
], ids=["zero days", "negative days", "over the maximum", "a bool", "year before the minimum", "year after the maximum"])
def test_an_expiry_out_of_the_parsers_bounds_is_refused(expiry, refusal):
    assert expiry_refusal(expiry) == refusal
    assert typed_update_refusal(ParsedFactUpdate(state=FactState.CURRENT, expiry=expiry)) == refusal


@pytest.mark.parametrize("update", [
    ParsedFactUpdate(),
    ARCHIVE_ONLY,
    ParsedFactUpdate(state=FactState.INVALIDATED),
    ParsedFactUpdate(state=FactState.CURRENT, expiry=EXPIRY_NONE),
    ParsedFactUpdate(state=FactState.CURRENT, expiry=horizon(1)),
    ParsedFactUpdate(state=FactState.CURRENT, expiry=horizon(MAX_HORIZON_DAYS)),
    ParsedFactUpdate(content="works at Beta", tags=["work"], expiry=Expiry(ExpiryKind.DATE, on=date(MAX_YEAR, 12, 31))),
    ParsedFactUpdate(state=FactState.SUPERSEDED, superseded_by="f2"),
    ParsedFactUpdate(content="x", expiry=Expiry(ExpiryKind.DATE, on=date(MIN_YEAR, 1, 1))),
])
def test_an_update_within_the_rules_passes(update):
    assert typed_update_refusal(update) is None


@pytest.mark.parametrize("updates", [
    {"state": "current", "expiry": {"kind": "horizon", "days": 30}},
    {"content": "works at Beta", "tags": ["work"], "expiry": {"kind": "date", "date": "2027-01-01"}},
    {"state": "superseded", "superseded_by": "f2"},
    {"state": "archived", "expiry": {"kind": "horizon", "days": 30}},
    {"reported_date": "2026-01-05T10:00:00+00:00"},
])
def test_whatever_the_parser_accepts_the_typed_check_accepts(updates):
    assert typed_update_refusal(parse_fact_update(updates, "Europe/Lisbon")) is None


def test_expiry_itself_still_holds_a_stored_legacy_value_out_of_bounds():
    assert stored_expiry(ExpiryKind.HORIZON, 0, None, "UTC") == horizon(0)
    assert expiry_refusal(horizon(0)) is not None


# ------------------------------------------------------------------------------------------------ the model path, unchanged

@pytest.mark.parametrize("updates, text", [
    ({"state": "stale"}, STALE),
    ({"state": "superseded"}, NO_SUCCESSOR),
    ({"state": "superseded", "superseded_by": "f2", "content": "x"}, SUPERSEDED_ALONE),
    ({"superseded_by": "f2"}, SUCCESSOR_ONLY),
    ({"state": "superseded", "superseded_by": " "}, EMPTY_SUCCESSOR),
    ({"content": ""}, EMPTY_CONTENT),
    ({"tags": []}, EMPTY_TAGS),
], ids=["stale", "no successor", "superseded with content", "successor alone", "blank successor", "empty content",
        "empty tags"])
def test_the_model_path_refuses_with_the_text_it_always_had(updates, text):
    with pytest.raises(ValueError) as refused:
        parse_fact_update(updates)
    assert str(refused.value) == text and not isinstance(refused.value, FactExpiryError)


@pytest.mark.parametrize("expiry, why", [
    ({"kind": "horizon", "days": 0}, f"kind horizon needs `days` as a whole number from 1 to {MAX_HORIZON_DAYS}, got 0"),
    ({"kind": "horizon", "days": "90x"},
     f"kind horizon needs `days` as a whole number from 1 to {MAX_HORIZON_DAYS}, got '90x'"),
    ({"kind": "date", "date": "1800-01-01"}, f"the year of `date` must be between {MIN_YEAR} and {MAX_YEAR}, got 1800-01-01"),
], ids=["zero days", "days not a number", "year before the minimum"])
def test_the_model_paths_expiry_refusal_keeps_its_text(expiry, why):
    with pytest.raises(FactExpiryError) as refused:
        parse_fact_update({"state": "current", "expiry": expiry})
    assert str(refused.value) == f"Invalid expiry: {why}{_MODEL_TAIL}"


def test_the_model_path_still_accepts_days_sent_as_a_whole_float_or_a_digit_string():
    assert parse_fact_update({"state": "current", "expiry": {"kind": "horizon", "days": 14.0}}).expiry == horizon(14)
    assert parse_fact_update({"state": "current", "expiry": {"kind": "horizon", "days": "90"}}).expiry == horizon(90)
