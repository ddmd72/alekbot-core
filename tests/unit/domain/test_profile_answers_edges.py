"""The remaining branches of the stored answers shape (fact profile RFC §4.7): a null text reads as empty, a wrong type
is refused, and a fact's answer naming a review item of another field names no date. Invented cast only."""
from datetime import datetime, timezone

import pytest

from src.domain.fact_profile_answer import ProfileAnswer
from src.domain.place_lookup import PlaceRow
from src.domain.place_names import PlaceNames
from src.domain.profile_answers import (
    EMPTY_ANSWERS, answers_digest, parse_profile_answers,
)

AT = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
NAMES = PlaceNames([PlaceRow(3.0, 3.0, "Porto", "Porto", "Portugal", 1000, country_names=("Portugal",),
                             geonameid="3000")])


def stored_place(**over):
    fields = dict(key="place:porto|portugal", city="Porto", country="Portugal", resolution="atlas",
                  geonameid="3000", question_id="pq1", answered_at=AT)
    fields.update(over)
    return fields


def test_a_null_text_reads_as_empty():
    parsed = parse_profile_answers({"places": [stored_place(question_id=None)]})
    assert parsed.places["place:porto|portugal"].question_id == ""


def test_a_text_of_another_type_is_refused():
    with pytest.raises(ValueError, match="city must be a string"):
        parse_profile_answers({"places": [stored_place(city=7)]})


def test_an_entry_that_is_no_mapping_is_refused():
    with pytest.raises(ValueError, match="must be a mapping"):
        parse_profile_answers({"places": ["Porto"]})


def test_a_group_that_is_no_list_is_refused():
    with pytest.raises(ValueError, match="places must be a list"):
        parse_profile_answers({"places": "Porto"})


def test_the_document_that_is_no_mapping_is_refused():
    with pytest.raises(ValueError, match="must be a mapping"):
        parse_profile_answers(["Porto"])  # type: ignore[arg-type]


def test_a_review_item_of_another_field_names_no_answer():
    stored = parse_profile_answers({"places": [stored_place()]})
    answer = ProfileAnswer.model_validate(dict(
        label_words=[], time_role="timeless", time_points=[], places=[], people=[], quality="needs_review",
        review_items=[{"field": "places", "mention": "Porto"}], tax_or_fine="none", user_identity=False, pet=False,
        state_or_law=False, about_person="none", subject="work", money_flow=False, use_areas=[], kinds=[],
        sensitivity="ordinary"))
    assert answers_digest(stored, answer, "lin-1", NAMES) == ""
    assert answers_digest(EMPTY_ANSWERS, answer, "lin-1", NAMES) == ""
