"""The owner's answers to profile questions (fact profile RFC §4.7): the stored shape, its keys and the answers digest
of `profile_derivation`. Invented cast and places only; the date answer uses Alex's start at Acme (2026-10-01)."""
from datetime import datetime, timezone
from typing import Any, Dict

import pytest

from src.domain.fact_profile_answer import ProfileAnswer
from src.domain.place_lookup import PlaceRow
from src.domain.place_names import PlaceNames
from src.domain.profile_answers import (
    EMPTY_ANSWERS, DateAnswer, PersonAnswer, PlaceAnswer, ProfileAnswers, answers_digest, date_point_key,
    parse_profile_answers, person_point_key, place_point_key, profile_answers_document,
)

AT = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
LINEAGE = "lin-1"
NAMES = PlaceNames([
    PlaceRow(1.0, 1.0, "Rome", "Rome", "United States", 10, country_names=("United States",), geonameid="2000"),
    PlaceRow(2.0, 2.0, "Rome", "Roma", "Italy", 1000, country_names=("Italia", "Italy"), geonameid="1000"),
])
ROME = place_point_key("Rome", "")


def place(resolution: str = "atlas", key: str = ROME, **over: Any) -> PlaceAnswer:
    fields: Dict[str, Any] = dict(key=key, city="Rome", country="", resolution=resolution,
                                  geonameid="2000" if resolution == "atlas" else "", question_id="pq1", answered_at=AT)
    fields.update(over)
    return PlaceAnswer(**fields)


def person(resolution: str = "entry", mention: str = "Doe", **over: Any) -> PersonAnswer:
    fields: Dict[str, Any] = dict(key=person_point_key(LINEAGE, mention), lineage_id=LINEAGE, mention=mention,
                                  resolution=resolution, name="Jamie Doe" if resolution == "entry" else "",
                                  question_id="pq2", answered_at=AT)
    fields.update(over)
    return PersonAnswer(**fields)


def dated(resolution: str = "date", **over: Any) -> DateAnswer:
    fields: Dict[str, Any] = dict(key=date_point_key(LINEAGE), lineage_id=LINEAGE, mention="at Acme",
                                  resolution=resolution, start="2026-10-01" if resolution == "date" else "",
                                  precision="day" if resolution == "date" else "", question_id="pq3", answered_at=AT)
    fields.update(over)
    return DateAnswer(**fields)


def answers(*items: Any) -> ProfileAnswers:
    out = EMPTY_ANSWERS
    for item in items:
        out = out.with_answer(item)
    return out


def model_answer(places=(), items=(), quality: str = "needs_review") -> ProfileAnswer:
    return ProfileAnswer.model_validate(dict(
        label_words=[], time_role="timeless", time_points=[],
        places=[{"city": c, "country": k, "source": c} for c, k in places], people=[], quality=quality,
        review_items=[{"field": f, "mention": m} for f, m in items], tax_or_fine="none", user_identity=False,
        pet=False, state_or_law=False, about_person="none", subject="work", money_flow=False, use_areas=[], kinds=[],
        sensitivity="ordinary"))


# ------------------------------------------------------------------------------------------------ stored shape

def test_the_document_round_trips_every_kind():
    stored = answers(place(), place("local", key=place_point_key("Springfield", "Portugal"), city="Springfield",
                                    country="Portugal"),
                     person(), person("not_listed", mention="Clara"), dated(), dated("unknown", key="date:lin-2"))
    assert parse_profile_answers(profile_answers_document(stored)) == stored


def test_no_lists_is_no_answers():
    assert parse_profile_answers({}) == EMPTY_ANSWERS


@pytest.mark.parametrize("broken", [
    {"places": [{**profile_answers_document(answers(place()))["places"][0], "geonameid": ""}]},
    {"places": [{**profile_answers_document(answers(place("local")))["places"][0], "city": ""}]},
    {"people": [{**profile_answers_document(answers(person()))["people"][0], "name": " "}]},
    {"dates": [{**profile_answers_document(answers(dated()))["dates"][0], "start": ""}]},
    {"places": [{**profile_answers_document(answers(place()))["places"][0], "key": ""}]},
    {"places": [{**profile_answers_document(answers(place()))["places"][0], "key": "person:x"}]},
    {"places": [{**profile_answers_document(answers(place()))["places"][0], "resolution": "guess"}]},
    {"places": profile_answers_document(answers(place()))["places"] * 2},
    {"places": [{**profile_answers_document(answers(place()))["places"][0], "answered_at": None}]},
], ids=["atlas-without-geonameid", "local-without-city", "entry-without-name", "date-without-start", "empty-key",
        "key-of-another-kind", "unknown-resolution", "key-twice", "no-time"])
def test_a_broken_document_raises(broken):
    with pytest.raises(ValueError):
        parse_profile_answers(broken)


def test_a_second_answer_for_a_key_is_refused():
    with pytest.raises(ValueError):
        answers(place()).with_answer(place("local"))


def test_keys_fold_case_accents_and_hyphens():
    assert place_point_key("ROME", "united states") == place_point_key("Rome", "United States")
    assert place_point_key("Rome", "") == "place:rome|"
    assert person_point_key(LINEAGE, "DOE") == "person:lin-1:doe"
    assert date_point_key(LINEAGE) == "date:lin-1"


# ------------------------------------------------------------------------------------------------ digest

def test_no_effective_answer_named_is_an_empty_digest():
    rome_fact = model_answer(places=[("Rome", "")], items=[("people", "Doe"), ("time_points", "at Acme")])
    assert answers_digest(EMPTY_ANSWERS, rome_fact, LINEAGE, NAMES) == ""
    assert answers_digest(answers(place("unknown"), person("unknown"), dated("unknown")), rome_fact, LINEAGE,
                          NAMES) == ""
    assert answers_digest(answers(place()), None, LINEAGE, NAMES) == ""


def test_each_kind_named_makes_a_digest_of_sixteen_hex():
    for item, fact in ((place(), model_answer(places=[("Rome", "")])),
                       (person(), model_answer(items=[("people", "Doe")])),
                       (dated(), model_answer(items=[("time_points", "at Acme")]))):
        digest = answers_digest(answers(item), fact, LINEAGE, NAMES)
        assert len(digest) == 16 and int(digest, 16) >= 0


def test_the_digest_changes_with_the_resolution_and_the_value():
    fact = model_answer(places=[("Rome", "")])
    atlas = answers_digest(answers(place()), fact, LINEAGE, NAMES)
    other_row = answers_digest(answers(place(geonameid="1000")), fact, LINEAGE, NAMES)
    local = answers_digest(answers(place("local")), fact, LINEAGE, NAMES)
    assert len({atlas, other_row, local}) == 3


def test_unknown_answers_are_never_in_the_digest():
    fact = model_answer(places=[("Rome", "")], items=[("people", "Doe")])
    assert (answers_digest(answers(place(), person("unknown")), fact, LINEAGE, NAMES)
            == answers_digest(answers(place()), fact, LINEAGE, NAMES))


def test_answers_the_fact_does_not_name_are_ignored():
    fact = model_answer(places=[("Rome", "")])
    alone = answers_digest(answers(place()), fact, LINEAGE, NAMES)
    springfield = place("local", key=place_point_key("Springfield", ""), city="Springfield")
    other_lineage = person(key=person_point_key("lin-2", "Doe"), lineage_id="lin-2")
    assert answers_digest(answers(place(), springfield, other_lineage), fact, LINEAGE, NAMES) == alone
    assert answers_digest(answers(person()), fact, LINEAGE, NAMES) == ""


def test_a_place_key_uses_the_atlas_name_of_the_country():
    stored = answers(place(key=place_point_key("Rome", "Italy"), country="Italy", geonameid="1000"))
    assert answers_digest(stored, model_answer(places=[("Rome", "Italia")]), LINEAGE, NAMES) != ""


def test_a_confident_answer_names_no_person_or_date():
    fact = model_answer(items=[("people", "Doe"), ("time_points", "at Acme")], quality="confident")
    assert answers_digest(answers(person(), dated()), fact, LINEAGE, NAMES) == ""


def test_a_date_answer_about_other_words_is_not_named():
    fact = model_answer(items=[("time_points", "in 2026")])
    assert answers_digest(answers(dated()), fact, LINEAGE, NAMES) == ""
