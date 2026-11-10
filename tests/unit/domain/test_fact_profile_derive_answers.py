"""The owner's answers in the derivation (fact profile RFC §4.7): a place answer before the atlas, a person or date
answer of the fact's lineage on its review item, an answered item dropped and the flag cleared, and a fact no answer
names derived byte for byte as before. Invented cast and fixture atlas; dates only 2026-10-01 and the year 2026."""
from datetime import datetime, timezone
from typing import Any, Dict, Sequence

from src.domain.confirmed_people import parse_confirmed_people
from src.domain.fact_profile import ProfilePerson, ProfileTimePoint, UnresolvedPlace
from src.domain.fact_profile_answer import ProfileAnswer
from src.domain.fact_profile_derive import derive_profile, name_keys, owner_time_point
from src.domain.place_lookup import PlaceRow
from src.domain.place_names import PlaceNames
from src.domain.profile_answers import (
    EMPTY_ANSWERS, DateAnswer, PersonAnswer, PlaceAnswer, ProfileAnswers, date_point_key, person_point_key,
    place_point_key,
)
from src.domain.drive_keys import word_form_keys

AT = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
LINEAGE = "lin-1"
NAMES = PlaceNames([
    PlaceRow(1.0, 1.0, "Rome", "Rome", "United States", 10, country_names=("United States",), geonameid="2000"),
    PlaceRow(2.0, 2.0, "Rome", "Roma", "Italy", 1000, country_names=("Italia", "Italy"), geonameid="1000"),
    PlaceRow(3.0, 3.0, "Porto", "Porto", "Portugal", 1000, country_names=("Portugal",), geonameid="3000"),
    PlaceRow(4.0, 4.0, "Lisbon", "Lisboa", "Portugal", 1000, country_names=("Portugal",), geonameid="4000"),
])
HOUSEHOLD = parse_confirmed_people({
    "user": {"name": "Alex Doe", "forms": ["Alex"]},
    "people": [{"name": "Clara Moreira", "forms": ["Clara"], "relation": "partner"},
               {"name": "Jamie Doe", "forms": ["Jamie"], "relation": "daughter"}]}).entries()


def model(places=(), people=(), items=(), points=(), quality: str = "needs_review") -> ProfileAnswer:
    return ProfileAnswer.model_validate(dict(
        label_words=[], time_role="timeless", time_points=list(points),
        places=[{"city": c, "country": k, "source": c} for c, k in places],
        people=[{"name": n, "role": r, "source": s} for n, r, s in people], quality=quality,
        review_items=[{"field": f, "mention": m} for f, m in items], tax_or_fine="none", user_identity=False,
        pet=False, state_or_law=False, about_person="none", subject="work", money_flow=False, use_areas=[], kinds=[],
        sensitivity="ordinary"))


def answers(*items: Any) -> ProfileAnswers:
    out = EMPTY_ANSWERS
    for item in items:
        out = out.with_answer(item)
    return out


def rome(resolution: str = "atlas", geonameid: str = "2000") -> PlaceAnswer:
    return PlaceAnswer(key=place_point_key("Rome", ""), city="Rome", country="", resolution=resolution,
                       geonameid=geonameid if resolution == "atlas" else "", question_id="pq1", answered_at=AT)


def springfield_local() -> PlaceAnswer:
    return PlaceAnswer(key=place_point_key("Springfield", ""), city="Springfield", country="Portugal",
                       resolution="local", question_id="pq2", answered_at=AT)


def doe(resolution: str = "entry", name: str = "Alex Doe", mention: str = "Doe",
        lineage: str = LINEAGE) -> PersonAnswer:
    return PersonAnswer(key=person_point_key(lineage, mention), lineage_id=lineage, mention=mention,
                        resolution=resolution, name=name if resolution == "entry" else "", question_id="pq3",
                        answered_at=AT)


def acme_start(mention: str = "in 2026", start: str = "2026-10-01", resolution: str = "date") -> DateAnswer:
    return DateAnswer(key=date_point_key(LINEAGE), lineage_id=LINEAGE, mention=mention, resolution=resolution,
                      start=start if resolution == "date" else "", precision="day" if resolution == "date" else "",
                      question_id="pq4", answered_at=AT)


def derive(text: str, answer: ProfileAnswer, stored: ProfileAnswers = EMPTY_ANSWERS,
           listed: Sequence[Any] = HOUSEHOLD, lineage: str = LINEAGE):
    return derive_profile(answer, text, None, NAMES, 2026, listed, stored, lineage).profile


def drops_of(profile) -> Dict[str, int]:
    return dict(profile.profile_drops)


# ------------------------------------------------------------------------------------------------ places

ROME_FACT = "Alex has a trip to Rome"


def test_rome_answered_with_the_lighter_row_takes_that_rows_keys_and_is_no_longer_unresolved():
    profile = derive(ROME_FACT, model(places=[("Rome", "")]), answers(rome()))
    assert profile.place_keys == ("pl:2000", "co:united_states")
    assert profile.places_unresolved == ()
    assert drops_of(profile) == {"place_answered:atlas": 1}


def test_springfield_kept_as_written_in_portugal_gives_the_local_key_and_the_country():
    profile = derive("Alex visits Springfield", model(places=[("Springfield", "")]), answers(springfield_local()))
    assert profile.place_keys == ("lp:springfield", "co:portugal")
    assert profile.places_unresolved == ()


def test_a_local_answer_in_a_country_the_atlas_lacks_gives_the_local_key_only():
    answer = PlaceAnswer(key=place_point_key("Springfield", ""), city="Springfield", country="Atlantis",
                         resolution="local", question_id="pq2", answered_at=AT)
    profile = derive("Alex visits Springfield", model(places=[("Springfield", "")]), answers(answer))
    assert profile.place_keys == ("lp:springfield",)


def test_dont_remember_leaves_the_place_unresolved_as_before():
    without = derive(ROME_FACT, model(places=[("Rome", "")]))
    profile = derive(ROME_FACT, model(places=[("Rome", "")]), answers(rome("unknown")))
    assert profile == without
    assert profile.places_unresolved == (UnresolvedPlace("Rome", ""),)


def test_a_row_that_left_the_atlas_falls_back_to_resolution_and_is_counted():
    profile = derive(ROME_FACT, model(places=[("Rome", "")]), answers(rome(geonameid="9000")))
    assert profile.place_keys == ()
    assert profile.places_unresolved == (UnresolvedPlace("Rome", ""),)
    assert drops_of(profile) == {"place_answer_stale": 1, "place_unresolved:ambiguous": 1}


def test_the_answer_reaches_a_place_named_by_another_name_of_the_same_country():
    stored = PlaceAnswer(key=place_point_key("Rome", "Italy"), city="Rome", country="Italy",
                         resolution="atlas", geonameid="1000", question_id="pq1", answered_at=AT)
    profile = derive(ROME_FACT, model(places=[("Rome", "Italia")]), answers(stored))
    assert drops_of(profile) == {"place_answered:atlas": 1}


def test_a_fact_printing_porto_derives_byte_identical_with_and_without_answers():
    text, answer = "Hanna lives in Porto", model(places=[("Porto", "Portugal")], people=[("Hanna", "", "Hanna")])
    stored = answers(rome(), springfield_local(), doe(lineage="lin-2"))
    assert derive(text, answer, stored) == derive(text, answer)
    assert derive(text, answer, stored).stored() == derive(text, answer).stored()


def test_a_places_item_naming_an_answered_place_is_dropped_and_the_flag_clears():
    answer = model(places=[("Rome", "")], items=[("places", "Rome")])
    assert derive(ROME_FACT, answer).review_needed is True
    profile = derive(ROME_FACT, answer, answers(rome()))
    assert (profile.review_needed, profile.review_items) == (False, ())
    assert drops_of(profile)["review_item_answered"] == 1


def test_a_place_answered_unknown_keeps_its_item_and_the_flag():
    answer = model(places=[("Rome", "")], items=[("places", "Rome")])
    profile = derive(ROME_FACT, answer, answers(rome("unknown")))
    assert profile.review_needed is True and len(profile.review_items) == 1


# ------------------------------------------------------------------------------------------------ people

DOE_FACT = "Doe has a trip to Lisbon"
DOE_ANSWER = model(people=[("Doe", "", "Doe")], items=[("people", "Doe")])
# A Doe who is not the user: Jamie Doe goes to Example School.
SCHOOL_FACT = "Doe goes to Example School"


def test_doe_answered_jamie_doe_keeps_the_printed_name_and_takes_the_relation():
    profile = derive(SCHOOL_FACT, DOE_ANSWER, answers(doe(name="Jamie Doe")))
    assert [(p.name, p.role, p.from_list) for p in profile.people] == [("Doe", "daughter", False)]
    assert profile.people[0].role_keys == word_form_keys("daughter")
    assert (profile.review_needed, profile.review_items) == (False, ())
    assert drops_of(profile) == {"review_item_answered": 1}


def test_doe_answered_the_user_leaves_no_person():
    profile = derive(DOE_FACT, DOE_ANSWER, answers(doe(name="Alex Doe")))
    assert profile.people == () and profile.review_needed is False


def test_not_on_my_list_keeps_the_people_and_drops_the_item():
    without = derive(DOE_FACT, DOE_ANSWER)
    profile = derive(DOE_FACT, DOE_ANSWER, answers(doe("not_listed")))
    assert profile.people == without.people
    assert (profile.review_needed, profile.review_items) == (False, ())


def test_dont_remember_keeps_the_item_and_the_flag():
    profile = derive(DOE_FACT, DOE_ANSWER, answers(doe("unknown")))
    assert profile == derive(DOE_FACT, DOE_ANSWER)


def test_a_person_known_only_by_the_relation_takes_the_entrys_name_from_the_list():
    answer = model(people=[("", "daughter", "daughter")], items=[("people", "daughter")])
    profile = derive("The daughter goes to Example School", answer,
                     answers(doe(name="Jamie Doe", mention="daughter")))
    assert profile.people == (ProfilePerson("Jamie Doe", "daughter", name_keys("Jamie Doe"), word_form_keys("daughter"),
                                            from_list=True),)


def test_an_answered_mention_the_people_lack_adds_the_entry_from_the_list():
    answer = model(items=[("people", "Doe")])
    profile = derive(SCHOOL_FACT, answer, answers(doe(name="Jamie Doe")))
    assert [(p.name, p.role, p.from_list) for p in profile.people] == [("Jamie Doe", "daughter", True)]


def test_an_entry_no_longer_on_the_list_keeps_the_item_and_is_counted():
    without_jamie = HOUSEHOLD[:2]
    profile = derive(SCHOOL_FACT, DOE_ANSWER, answers(doe(name="Jamie Doe")), listed=without_jamie)
    assert profile.review_needed is True and len(profile.review_items) == 1
    assert drops_of(profile) == {"person_answer_stale": 1}


def test_a_person_answer_of_another_lineage_changes_nothing():
    assert derive(DOE_FACT, DOE_ANSWER, answers(doe(lineage="lin-2"))) == derive(DOE_FACT, DOE_ANSWER)


def test_the_flag_stays_while_an_item_is_left():
    answer = model(people=[("Doe", "", "Doe")], places=[("Hamburg", "")],
                   items=[("people", "Doe"), ("places", "Hamburg")])
    profile = derive("Doe has a trip to Hamburg", answer, answers(doe()))
    assert profile.review_needed is True
    assert [(i.field, i.mention) for i in profile.review_items] == [("places", "Hamburg")]


# ------------------------------------------------------------------------------------------------ dates

ACME_FACT = "Alex started at Acme in 2026"
YEAR_POINT = {"from": "2026", "to": "", "precision": "year", "source": "2026"}


def test_a_date_answer_replaces_the_points_read_from_the_flagged_words():
    answer = model(points=[YEAR_POINT], items=[("time_points", "in 2026")])
    assert derive(ACME_FACT, answer).time_points == (ProfileTimePoint("2026", "", "year", "2026"),)
    profile = derive(ACME_FACT, answer, answers(acme_start()))
    assert profile.time_points == (ProfileTimePoint("2026-10-01", "", "day", "in 2026"),)
    assert (profile.review_needed, profile.review_items) == (False, ())


def test_a_date_answer_without_a_point_to_replace_adds_its_point():
    answer = model(items=[("time_points", "at Acme")])
    profile = derive(ACME_FACT, answer, answers(acme_start(mention="at Acme")))
    assert profile.time_points == (ProfileTimePoint("2026-10-01", "", "day", "at Acme"),)


def test_a_date_answer_about_other_words_changes_nothing():
    answer = model(points=[YEAR_POINT], items=[("time_points", "in 2026")])
    assert derive(ACME_FACT, answer, answers(acme_start(mention="at Acme"))) == derive(ACME_FACT, answer)


def test_dont_remember_a_date_keeps_the_points_and_the_item():
    answer = model(points=[YEAR_POINT], items=[("time_points", "in 2026")])
    assert derive(ACME_FACT, answer, answers(acme_start(resolution="unknown"))) == derive(ACME_FACT, answer)


def test_a_date_answer_that_does_not_read_is_not_applied_and_is_counted():
    answer = model(points=[YEAR_POINT], items=[("time_points", "in 2026")])
    profile = derive(ACME_FACT, answer, answers(acme_start(start="2026-10-1")))
    assert profile.time_points == (ProfileTimePoint("2026", "", "year", "2026"),)
    assert profile.review_needed is True and drops_of(profile) == {"date_answer_unreadable": 1}


def test_owner_points_are_read_in_the_forms_the_profiler_writes():
    def point(start: str, precision: str, end: str = ""):
        return owner_time_point(DateAnswer(key="date:x", lineage_id="x", mention="in 2026", resolution="date",
                                           start=start, end=end, precision=precision, question_id="q",
                                           answered_at=AT))
    assert point("2026", "year") == ProfileTimePoint("2026", "", "year", "in 2026")
    assert point("2026-10", "month") is not None
    assert point("2026-10-01", "day", "2026-10-01") is not None
    assert point("2026-13", "month") is None
    assert point("2026-10-01", "month") is None
    assert point("2026-10-01", "day", "2026-01-01") is None
    assert point("1826", "year") is None


def test_without_a_lineage_no_person_or_date_answer_applies():
    answer = model(people=[("Doe", "", "Doe")], items=[("people", "Doe")])
    assert derive(DOE_FACT, answer, answers(doe(lineage="")), lineage="") == derive(DOE_FACT, answer, lineage="")
