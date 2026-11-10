"""Derivation gaps that lost a correct model answer (fact profile RFC §3-§4): compact metadata dates, short range ends,
the country kept beside an unresolved city, atlas short names, the retraction prefix. Invented cast only."""
import pytest

from src.domain.drive_grade import TextEvidence
from src.domain.fact_profile_answer import AnswerPlace, AnswerTimePoint, ProfileAnswer
from src.domain.fact_profile_derive import _short_range_ends, check_date, check_places, derive_profile
from src.domain.fact_profile_fingerprint import DERIVE_VERSION
from src.domain.fact_profile_periods import period_ranges
from src.domain.place_lookup import PlaceRow
from src.domain.place_names import PlaceNames


def evidence(*texts):
    return TextEvidence(list(texts)), "\n".join(texts).casefold()


def row(gid, name, country, weight=100_000, **kw):
    return PlaceRow(0.0, 0.0, name, name, country, weight, geonameid=gid, **kw)


ATLAS = PlaceNames([
    row("1", "Lisboa", "Portugal"), row("2", "Porto", "Portugal"),
    row("3", "Coimbra am Mondego", "Portugal"),          # stored under a longer name
    row("4", "Rome", "Italy"),
    row("5", "Frankfurt am Main", "Germany"), row("6", "Frankfurt (Oder)", "Germany"),
])


def full_answer(**over):
    base = dict(label_words=[], time_role="timeless", time_points=[], places=[], people=[], tax_or_fine="none",
                user_identity=False, pet=False, state_or_law=False, about_person="none", subject="lifestyle",
                money_flow=False, use_areas=[], kinds=[], sensitivity="ordinary")
    base.update(over)
    return ProfileAnswer.model_validate(base)


# 1. compact metadata dates

@pytest.mark.parametrize("source, value, precision", [
    ("aug_2025", "2025-08", "month"),
    ("Aug_14_2025", "2025-08-14", "day"),
    ("Jul_14_1982", "1982-07-14", "day"),
])
def test_a_compact_metadata_date_reads_as_its_text_form(source, value, precision):
    ev, raw = evidence(f"event_date: {source}")
    assert check_date(ev, raw, [source], value, precision, source)[0] is True


def test_a_compact_date_with_another_month_is_still_dropped():
    ev, raw = evidence("event_date: aug_2025")
    assert check_date(ev, raw, [], "2025-09", "month", "aug_2025") == (False, "time_date_not_in_source")


def test_a_compact_year_month_with_an_underscore_is_read():
    ev, raw = evidence("period: 2025_08")
    assert check_date(ev, raw, [], "2025-08", "month", "2025_08")[0] is True


# 2. short range ends

@pytest.mark.parametrize("sep", ["–", "-"])
def test_a_short_range_end_is_the_full_year_of_the_start_century(sep):
    ev, raw = evidence(f"Car insurance MP-2026-118204 runs 2026{sep}27.")
    assert check_date(ev, raw, [], "2027", "year", f"2026{sep}27") == (True, "year_short_range_end")


def test_a_short_range_end_crossing_a_century_is_the_next_century():
    ev, raw = evidence("Coverage 2099–01.")
    assert _short_range_ends("2099–01") == {"2101"}


def test_a_wrong_short_range_end_is_dropped():
    ev, raw = evidence("Car insurance MP-2026-118204 runs 2026-27.")
    assert check_date(ev, raw, [], "2028", "year", "2026-27") == (False, "time_date_not_in_source")


def test_a_range_point_keeps_its_end_through_check_time_points():
    out = derive_profile(full_answer(time_role="validity", time_points=[
        {"from": "2026", "to": "2027", "precision": "year", "source": "2026–27"}]),
        "Car insurance MP-2026-118204 runs 2026–27.", None,
        ATLAS)
    assert [(p.start, p.end) for p in out.profile.time_points] == [("2026", "2027")]
    assert not any(k.startswith("time_to_") for k in out.profile.profile_drops)


# 3. the country survives an unresolved city

def test_an_unresolved_city_keeps_the_country_key():
    ev, _ = evidence("Rome trip 10–14 Aug 2025, hotel Albergo Immaginario.")
    drops = {}
    keys, unresolved = check_places(
        [AnswerPlace.model_validate({"city": "Albergo Immaginario", "country": "Italy", "source": "Albergo Immaginario"})],
        ev, ATLAS, drops)
    assert keys == ("co:italy",)
    assert [(u.city, u.country) for u in unresolved] == [("Albergo Immaginario", "Italy")]
    assert drops == {"place_unresolved:not_in_atlas": 1}


def test_a_city_the_atlas_places_in_another_country_does_not_keep_the_country():
    ev, _ = evidence("Lisboa and Rome.")
    drops = {}
    keys, unresolved = check_places(
        [AnswerPlace.model_validate({"city": "Lisboa", "country": "Italy", "source": "Lisboa"})], ev, ATLAS, drops)
    assert keys == () and len(unresolved) == 1
    assert drops == {"place_unresolved:not_in_country": 1}


def test_an_ambiguous_city_keeps_the_country():
    ev, _ = evidence("Place: Frankfurt.")
    keys, _ = check_places(
        [AnswerPlace.model_validate({"city": "Frankfurt", "country": "Germany", "source": "Frankfurt"})],
        ev, ATLAS, {})
    assert keys == ("co:germany",)


def test_an_unresolved_city_with_an_unknown_country_gives_no_key():
    ev, _ = evidence("Place: Nowhereville.")
    keys, unresolved = check_places(
        [AnswerPlace.model_validate({"city": "Nowhereville", "country": "Atlantis", "source": "Nowhereville"})],
        ev, ATLAS, {})
    assert keys == () and len(unresolved) == 1


# 4. atlas short names

def test_a_short_name_resolves_to_the_longer_stored_name():
    assert ATLAS.resolve("Coimbra", "Portugal") == (("pl:3", "co:portugal"), None)


def test_a_short_name_resolves_without_a_country_when_unambiguous():
    assert ATLAS.resolve("Coimbra", "")[0] == ("pl:3", "co:portugal")


def test_an_ambiguous_short_name_stays_ambiguous():
    assert ATLAS.resolve("Frankfurt", "Germany") == ((), "ambiguous")


def test_an_exact_name_outranks_a_short_name():
    atlas = PlaceNames([row("7", "Coimbra", "Portugal"), row("3", "Coimbra am Mondego", "Portugal")])
    assert atlas.resolve("Coimbra", "Portugal")[0][0] == "pl:7"


def test_a_name_the_atlas_lacks_is_still_not_in_atlas():
    assert ATLAS.resolve("Faro", "Portugal") == ((), "not_in_atlas")
    assert ATLAS.resolve("Rome", "Portugal") == ((), "not_in_country")


def test_a_query_by_short_name_reaches_the_longer_stored_row():
    assert ATLAS.query_keys("Coimbra", "Portugal") == ("pl:3",)


# 5. the retraction prefix

def test_the_month_of_a_retraction_prefix_is_not_a_time_point():
    text = "No longer true (the owner said so in March 2026): Alex plans a Rome trip."
    out = derive_profile(full_answer(time_role="event", time_points=[
        {"from": "2026-03", "to": "", "precision": "month", "source": "March 2026"}]), text, None, ATLAS)
    assert out.profile.time_points == ()
    assert out.profile.profile_drops == {"time_source_not_printed": 1}


def test_a_date_of_the_retracted_fact_itself_is_kept():
    text = "No longer true (the owner said so in March 2026): Rome trip 10–14 Aug 2025."
    out = derive_profile(full_answer(time_role="event", time_points=[
        {"from": "2025-08-10", "to": "2025-08-14", "precision": "day", "source": "10–14 Aug 2025"}]), text, None,
        ATLAS)
    assert [(p.start, p.end) for p in out.profile.time_points] == [("2025-08-10", "2025-08-14")]


def test_a_month_printed_in_both_prefix_and_fact_is_kept():
    text = "No longer true (the owner said so in March 2026): Weekend in Coimbra in March 2026."
    out = derive_profile(full_answer(time_role="event", time_points=[
        {"from": "2026-03", "to": "", "precision": "month", "source": "March 2026"}]), text, None, ATLAS)
    assert len(out.profile.time_points) == 1


# 6. the derivation version

def test_the_derivation_version_moved_past_the_stored_one():
    assert DERIVE_VERSION == "3"


# 7. quarters and seasons as month ranges

@pytest.mark.parametrize("source, start, end", [
    ("Q1 2027", "2027-01", "2027-03"),
    ("1st quarter 2027", "2027-01", "2027-03"),
    ("second quarter 2026", "2026-04", "2026-06"),
    ("T3 2026", "2026-07", "2026-09"),
    ("primer trimestre de 2027", "2027-01", "2027-03"),
    ("quatrième trimestre 2026", "2026-10", "2026-12"),
    ("перший квартал 2027", "2027-01", "2027-03"),
    ("spring 2026", "2026-03", "2026-05"),
    ("summer 2026", "2026-06", "2026-08"),
    ("fall 2026", "2026-09", "2026-11"),
    ("verano de 2026", "2026-06", "2026-08"),
    ("automne 2026", "2026-09", "2026-11"),
    ("winter 2026", "2026-12", "2027-02"),
    ("зима 2026", "2026-12", "2027-02"),
])
def test_a_quarter_or_season_is_a_range_of_months(source, start, end):
    ev, raw = evidence(f"Periodic car inspection in {source}.")
    assert check_date(ev, raw, [], start, "month", source)[0] is True
    assert check_date(ev, raw, [], end, "month", source)[0] is True


def test_a_month_outside_the_quarter_is_dropped():
    ev, raw = evidence("Payment due in Q1 2027.")
    assert check_date(ev, raw, [], "2027-04", "month", "Q1 2027") == (False, "time_date_not_in_source")


def test_a_quarter_is_stored_as_one_month_range_point():
    out = derive_profile(full_answer(time_role="validity", time_points=[
        {"from": "2027-04", "to": "2027-06", "precision": "month", "source": "Q2 2027"}]),
        "Car insurance renews in Q2 2027.", None, ATLAS)
    assert [(p.start, p.end, p.precision) for p in out.profile.time_points] == [("2027-04", "2027-06", "month")]
    assert out.profile.profile_drops == {}


def test_a_season_is_stored_as_one_month_range_point_and_winter_crosses_the_year():
    out = derive_profile(full_answer(time_role="validity", time_points=[
        {"from": "2026-03", "to": "2026-05", "precision": "month", "source": "spring 2026"}]),
        "Weekend in Coimbra in spring 2026.", None, ATLAS)
    assert [(p.start, p.end) for p in out.profile.time_points] == [("2026-03", "2026-05")]


def test_winter_crosses_the_year_in_a_stored_point():
    out = derive_profile(full_answer(time_role="validity", time_points=[
        {"from": "2026-12", "to": "2027-02", "precision": "month", "source": "winter 2026"}]),
        "Periodic car inspection in winter 2026.", None, ATLAS)
    assert [(p.start, p.end) for p in out.profile.time_points] == [("2026-12", "2027-02")]


def test_period_ranges_lists_every_period_in_a_source():
    assert period_ranges("Q4 2026 to Q1 2027") == [("2026-10", "2026-12"), ("2027-01", "2027-03")]
