"""The profiler's answer and the code checks of every value it proposes (fact profile RFC §4.4, §10.2): the answer
schema, then label words, time points, places and people, each drop counted under its reason."""
import pytest
from pydantic import ValidationError

from src.domain.drive_grade import TextEvidence
from src.domain.fact_profile import ProfilePerson, ProfileTimePoint, UnresolvedPlace
from src.domain.fact_profile_answer import AnswerPerson, AnswerPlace, AnswerTimePoint, ProfileAnswer
from src.domain.fact_profile_derive import (
    LABEL_CAP, TIME_CAP, check_date, check_labels, check_people, check_places, check_time_points, name_keys,
)
from src.domain.drive_keys import word_form_keys
from src.domain.place_lookup import PlaceRow
from src.domain.place_names import PlaceNames, atlas_digest, country_key, fold

INSURANCE = ("Alex's car insurance with Example Mutual Insurance, policy MP-2026-118204, runs 01/04/2026–31/03/2027 "
             "and costs 412.00 EUR.")
ROME_TRIP = "Rome trip 10–14 Aug 2025, Albergo Immaginario, booking RM-55120."


def answer(**over):
    base = dict(label_words=[], time_role="timeless", time_points=[], places=[], people=[], tax_or_fine="none",
                user_identity=False, pet=False, state_or_law=False, about_person="none", subject="lifestyle",
                money_flow=False, use_areas=[], kinds=[], sensitivity="ordinary")
    base.update(over)
    return base


# ------------------------------------------------------------------------------------------------ the answer schema

def test_a_complete_answer_parses_and_stores_time_points_keyed_from():
    parsed = ProfileAnswer.model_validate(answer(time_points=[
        {"from": "2026-04-01", "to": "2027-03-31", "precision": "day", "source": "01/04/2026–31/03/2027"}]))
    assert parsed.time_points[0].from_ == "2026-04-01"
    assert parsed.stored()["time_points"] == [
        {"from": "2026-04-01", "to": "2027-03-31", "precision": "day", "source": "01/04/2026–31/03/2027"}]


@pytest.mark.parametrize("field", list(answer()))
def test_a_missing_field_fails_the_parse(field):
    raw = answer()
    del raw[field]
    with pytest.raises(ValidationError):
        ProfileAnswer.model_validate(raw)


@pytest.mark.parametrize("over", [
    {"subject": "travelling"}, {"subject": "family"}, {"subject": "agent_directives"},
    {"use_areas": ["none"]}, {"use_areas": ["biography"]}, {"kinds": ["habit"]},
    {"time_role": "period"}, {"sensitivity": "secret"}, {"tax_or_fine": "fee"}, {"about_person": "self"},
    {"time_points": [{"from": "2026", "to": "", "precision": "decade", "source": "2026"}]},
    {"extra": True},
])
def test_an_out_of_enum_or_unknown_value_fails_the_parse(over):
    with pytest.raises(ValidationError):
        ProfileAnswer.model_validate(answer(**over))


def test_none_is_a_subject_and_the_four_flag_areas_are_use_areas():
    parsed = ProfileAnswer.model_validate(answer(subject="none", use_areas=["family", "people", "pets", "state_law"]))
    assert parsed.subject == "none" and len(parsed.use_areas) == 4


# ------------------------------------------------------------------------------------------------ label words

def labels(words, text=INSURANCE, metadata_values=()):
    known = set(word_form_keys(text))
    for value in metadata_values:
        known |= set(word_form_keys(value))
    drops = {}
    return check_labels(words, known, drops), drops


def test_labels_are_lowercased_and_spaces_collapsed():
    assert labels(["  Vehicle   Cover "])[0] == ("vehicle cover",)


@pytest.mark.parametrize("word, reason", [
    ("", "label_empty"), ("   ", "label_empty"),
    ("annual vehicle cover renewal", "label_too_long"),
    ("cover 2026", "label_has_number"),
    ("record", "label_record_word"), ("details", "label_record_word"),
    ("--", "label_no_keys"),
    ("partner", "label_relation_word"), ("mothers day", "label_relation_word"),
    ("insurance", "label_repeats_text"), ("car insurance", "label_repeats_text"),
])
def test_a_label_failing_a_check_is_dropped_and_counted(word, reason):
    kept, drops = labels([word])
    assert kept == () and drops == {reason: 1}


def test_a_record_word_inside_a_phrase_is_kept():
    assert labels(["status update"])[0] == ("status update",)


def test_a_label_with_one_new_word_is_kept():
    assert labels(["insurance renewal"])[0] == ("insurance renewal",)


def test_a_label_repeating_a_metadata_value_is_dropped():
    kept, drops = labels(["premium"], text="car cover", metadata_values=["premium plan"])
    assert kept == () and drops == {"label_repeats_text": 1}


def test_a_duplicate_label_by_word_form_is_dropped():
    kept, drops = labels(["vehicle cover", "vehicle covers"])
    assert kept == ("vehicle cover",) and drops == {"label_duplicate": 1}


def test_labels_over_the_cap_are_dropped():
    words = [f"theme {chr(ord('a') + i)}{chr(ord('a') + i)}x" for i in range(LABEL_CAP + 2)]
    kept, drops = labels(words, text="nothing")
    assert len(kept) == LABEL_CAP and drops == {"label_over_cap": 2}


# ------------------------------------------------------------------------------------------------ time points

def evidence(text, *metadata_lines):
    texts = [text, *metadata_lines]
    return TextEvidence(texts), "\n".join(texts).casefold()


def points(text, pts, metadata_lines=(), metadata_values=()):
    ev, raw = evidence(text, *metadata_lines)
    drops = {}
    kept = check_time_points([AnswerTimePoint.model_validate(p) for p in pts], ev, raw, list(metadata_values), drops)
    return kept, drops


def pt(start, end="", precision="day", source=""):
    return {"from": start, "to": end, "precision": precision, "source": source}


def test_the_insurance_span_is_one_day_precision_point():
    kept, drops = points(INSURANCE, [pt("2026-04-01", "2027-03-31", source="01/04/2026–31/03/2027")])
    assert kept == (ProfileTimePoint("2026-04-01", "2027-03-31", "day", "01/04/2026–31/03/2027"),) and drops == {}


def test_a_day_read_from_its_parts_in_the_source():
    kept, drops = points(ROME_TRIP, [pt("2025-08-10", "2025-08-14", source="10–14 Aug 2025")])
    assert kept == (ProfileTimePoint("2025-08-10", "2025-08-14", "day", "10–14 Aug 2025"),) and drops == {}


def test_a_month_and_a_year():
    kept, drops = points(ROME_TRIP, [pt("2025-08", precision="month", source="Aug 2025"),
                                     pt("2025", precision="year", source="2025")])
    assert [(p.start, p.precision) for p in kept] == [("2025-08", "month"), ("2025", "year")] and drops == {}


def test_a_month_given_as_a_metadata_value():
    kept, _ = points("monthly fee", [pt("2026-11", precision="month", source="2026-11")],
                     metadata_lines=["since: 2026-11"], metadata_values=["2026-11"])
    assert [p.start for p in kept] == ["2026-11"]


@pytest.mark.parametrize("p, reason", [
    (pt("2026-04-02", source="01/04/2026"), "time_date_not_in_source"),
    (pt("2026-05-01", source="first of May"), "time_source_not_printed"),
    (pt("2026-4-1", source="01/04/2026"), "time_bad_format"),
    (pt("2026-02-30", source="01/04/2026"), "time_bad_format"),
    (pt("2026-04", precision="day", source="01/04/2026"), "time_bad_format"),
    (pt("1850-04-01", source="01/04/1850"), "time_year_out_of_range"),
    (pt("2027", precision="year", source="2026"), "time_date_not_in_source"),
    (pt("2027-04", precision="month", source="April 2026"), "time_date_not_in_source"),
])
def test_a_point_failing_a_check_is_dropped_and_counted(p, reason):
    kept, drops = points("Valid 01/04/2026 and 01/04/1850, from April 2026 for 2026.", [p])
    assert kept == () and drops == {reason: 1}


def test_an_end_before_the_start_keeps_the_start_alone():
    kept, drops = points(INSURANCE, [pt("2027-03-31", "2026-04-01", source="01/04/2026–31/03/2027")])
    assert kept == (ProfileTimePoint("2027-03-31", "", "day", "01/04/2026–31/03/2027"),)
    assert drops == {"time_to_before_from": 1}


def test_an_unprinted_end_keeps_the_start_alone():
    kept, drops = points(INSURANCE, [pt("2026-04-01", "2027-05-01", source="01/04/2026–31/03/2027")])
    assert kept[0].end == "" and drops == {"time_to_time_date_not_in_source": 1}


def test_duplicates_and_points_over_the_cap_are_dropped():
    text = " ".join(f"{d:02d}/01/2026" for d in range(1, TIME_CAP + 3))
    pts = [pt(f"2026-01-{d:02d}", source=f"{d:02d}/01/2026") for d in range(1, TIME_CAP + 3)]
    kept, drops = points(text, [pts[0], *pts])
    assert len(kept) == TIME_CAP and drops == {"time_duplicate": 1, "time_over_cap": 2}


MONTHS_AND_YEARS = "Renewed in November 2026; paid 2026-11-05; ref 2027-02 noted; Nov the 3rd, year 2028; since 1999."


@pytest.mark.parametrize("value, precision, source, outcome", [
    ("2026-11", "month", "November 2026", (True, "month_year_span")),
    ("2026-11", "month", "2026-11-05", (True, "iso_in_month")),
    ("2028-11", "month", "Nov the 3rd", (True, "parts")),
    ("2027-02", "month", "2027-02", (True, "yyyy_mm_in_source")),
    ("2026-10", "month", "November 2026", (False, "time_date_not_in_source")),
    ("2026-12", "month", "December 2026", (False, "time_source_not_printed")),
    ("1850-01", "month", "1850-01", (False, "time_year_out_of_range")),
    ("2026-1", "month", "November 2026", (False, "time_bad_format")),
    ("1999", "year", "since 1999", (True, "year")),
    ("2030", "year", "2030", (False, "time_source_not_printed")),
    ("1800", "year", "1800", (False, "time_year_out_of_range")),
    ("26", "year", "26", (False, "time_bad_format")),
])
def test_months_and_years_are_read_from_their_source(value, precision, source, outcome):
    ev, raw = evidence(MONTHS_AND_YEARS)
    assert check_date(ev, raw, [], value, precision, source) == outcome


def test_check_date_says_how_it_read_the_date():
    ev, raw = evidence(INSURANCE)
    assert check_date(ev, raw, [], "2026-04-01", "day", "01/04/2026–31/03/2027") == (True, "date_grade")
    assert check_date(ev, raw, [], "2026", "century", "2026") == (False, "time_bad_format")


# ------------------------------------------------------------------------------------------------ places

def row(gid, name, country, weight=1, large=None, alt=(), country_names=()):
    return PlaceRow(0.0, 0.0, name, name, country, weight, alt_names=alt, country_names=country_names, large=large,
                    geonameid=gid)


ROMA = row("3169070", "Rome", "Italy", 2_000_000, alt=("Roma",), country_names=("Italia",))
ATLAS_ROWS = [
    ROMA,
    row("100", "Ostia", "Italy", large=ROMA, country_names=("Italia",)),
    row("200", "Springfield", "United States"),
    row("201", "Springfield", "United States"),
    row("300", "Lisbon", "Portugal", alt=("Lisboa",)),
    row("400", "London", "United Kingdom"),
]
ATLAS = PlaceNames(ATLAS_ROWS)


def places(text, pls):
    ev, _ = evidence(text)
    drops = {}
    keys, unresolved = check_places([AnswerPlace.model_validate(p) for p in pls], ev, ATLAS, drops)
    return keys, unresolved, drops


def test_the_rome_trip_resolves_to_its_city_and_country_keys():
    keys, unresolved, drops = places(ROME_TRIP, [{"city": "Rome", "country": "Italy", "source": "Rome"}])
    assert keys == ("pl:3169070", "co:italy") and unresolved == () and drops == {}


def test_a_place_with_a_large_neighbour_carries_its_key_and_keys_are_unique():
    keys, _, _ = places("Ostia and Rome", [{"city": "Ostia", "country": "Italia", "source": "Ostia"},
                                           {"city": "Roma", "country": "", "source": "Rome"}])
    assert keys == ("pl:100", "pl:3169070", "co:italy")


def test_a_country_alone_gives_its_country_key():
    assert places("a week in Italy", [{"city": "", "country": "Italy", "source": "Italy"}])[0] == ("co:italy",)


# A country the atlas does not contradict survives its unresolved city (an unknown city, or several rows all inside
# it); a city the atlas places in another country contradicts the pair, so neither key is written.
@pytest.mark.parametrize("city, country, reason, kept", [
    ("Rome", "Atlantis", "unknown_country", ()),
    ("", "", "empty", ()),
    ("Lisbon", "Italy", "not_in_country", ()),
    ("Coimbra", "Portugal", "not_in_atlas", ("co:portugal",)),
    ("Coimbra", "", "not_in_atlas", ()),
    ("Springfield", "USA", "ambiguous", ("co:united_states",)),
])
def test_an_unresolved_place_is_kept_apart_with_its_reason(city, country, reason, kept):
    keys, unresolved, drops = places("Rome Lisbon Coimbra Springfield", [
        {"city": city, "country": country, "source": "Rome"}])
    assert keys == kept and unresolved == (UnresolvedPlace(city, country),)
    assert drops == {f"place_unresolved:{reason}": 1}


def test_an_unprinted_place_is_dropped():
    keys, unresolved, drops = places(ROME_TRIP, [{"city": "Lisbon", "country": "Portugal", "source": "Lisbon"},
                                                 {"city": "Lisbon", "country": "Portugal", "source": " "}])
    assert (keys, unresolved, drops) == ((), (), {"place_not_printed": 2})


def test_country_names_fold_case_accents_and_aliases():
    assert ATLAS.resolve("LISBOA", "portugal") == (("pl:300", "co:portugal"), None)
    assert ATLAS.resolve("London", "UK") == (("pl:400", "co:united_kingdom"), None)
    assert fold("Saint-Étienne’s") == "saint etienne s" and country_key("United States") == "co:united_states"


def test_the_atlas_digest_follows_what_resolution_reads():
    assert atlas_digest(ATLAS_ROWS) == atlas_digest(list(ATLAS_ROWS))
    renamed = [row("300", "Lisbon", "Portugal", alt=("Lisboa", "Olisipo")), *ATLAS_ROWS[1:]]
    assert atlas_digest(renamed) != atlas_digest(ATLAS_ROWS)
    assert len(atlas_digest(ATLAS_ROWS)) == 16


# ------------------------------------------------------------------------------------------------ people

def people(text, pps):
    ev, _ = evidence(text)
    drops = {}
    return check_people([AnswerPerson.model_validate(p) for p in pps], ev, drops), drops


def test_a_printed_name_gets_word_form_and_skeleton_keys():
    (person,), drops = people("Lunch with Mariia on Sunday.", [{"name": "Mariia", "role": "Mother", "source": "Mariia"}])
    assert person == ProfilePerson("Mariia", "mother", name_keys("Mariia"), word_form_keys("mother"))
    assert any(k.startswith("x:") for k in person.name_keys) and drops == {}


def test_a_name_printed_in_another_script_counts_through_its_skeleton():
    (person,), _ = people("Обід з Марія у неділю.", [{"name": "Mariia", "role": "mother", "source": "Марія"}])
    assert person.name == "Mariia"


def test_a_role_without_a_name_counts_when_its_source_is_printed():
    (person,), _ = people("Tea with my mother.", [{"name": "", "role": "mother", "source": "my mother"}])
    assert (person.name, person.name_keys, person.role) == ("", (), "mother")


@pytest.mark.parametrize("p, reason", [
    ({"name": " ", "role": "", "source": "x"}, "person_empty"),
    ({"name": "Clara Moreira", "role": "partner", "source": "Clara"}, "person_not_printed"),
    ({"name": "", "role": "mother", "source": "mum"}, "person_not_printed"),
    ({"name": "", "role": "mother", "source": ""}, "person_not_printed"),
])
def test_a_person_failing_a_check_is_dropped_and_counted(p, reason):
    kept, drops = people("Tea with my mother.", [p])
    assert kept == () and drops == {reason: 1}
