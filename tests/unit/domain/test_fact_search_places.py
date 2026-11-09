"""Query-side place keys (fact profile RFC §5.3) and the places list of the fact search."""
from src.domain.fact_search_lists import SearchOutcome
from src.domain.place_lookup import PlaceRow
from src.domain.place_names import PlaceNames
from tests.support.fact_search_kit import fact, query, search

ITALY = ("Italia", "Italy")
ROME = PlaceRow(41.9, 12.5, "Rome", "Roma", "Italy", 2_800_000, country_names=ITALY, geonameid="100")
OSTIA = PlaceRow(41.7, 12.3, "Ostia", "Ostia", "Italy", 90_000, country_names=ITALY, large=ROME, geonameid="101")
PORTO_PT = PlaceRow(41.1, -8.6, "Porto", "Porto", "Portugal", 230_000, country_names=("Portugal",), geonameid="200")
SPRING_BIG = PlaceRow(1.0, 1.0, "Springfield", "Springfield", "United States", 160_000, country_names=("USA",),
                      geonameid="300")
SPRING_SMALL = PlaceRow(2.0, 2.0, "Springfield", "Springfield", "United States", 9_000, country_names=("USA",),
                        geonameid="301")
SPRING_PT = PlaceRow(3.0, 3.0, "Springfield", "Springfield", "Portugal", 9_000, country_names=("Portugal",),
                     geonameid="302")
NAMES = PlaceNames([ROME, OSTIA, PORTO_PT, SPRING_SMALL, SPRING_BIG, SPRING_PT])


def test_a_country_alone_reaches_its_cities_through_the_co_key():
    assert NAMES.query_keys("Italy", "") == ("co:italy",)
    assert NAMES.query_keys("Italia", "Italy") == ("co:italy",)


def test_a_country_name_with_another_country_is_still_a_country():
    assert NAMES.query_keys("Italy", "Portugal") == ("co:italy",)


def test_a_city_reaches_its_own_key_and_its_large_neighbours():
    assert NAMES.query_keys("Ostia", "Italy") == ("pl:101", "pl:100")
    assert NAMES.query_keys("rome", "") == ("pl:100",)
    assert NAMES.query_keys("Roma", "Italia") == ("pl:100",)


def test_an_ambiguous_city_takes_the_heaviest_row_and_the_country_narrows_it():
    assert NAMES.query_keys("Springfield", "United States") == ("pl:300",)
    assert NAMES.query_keys("Springfield", "Portugal") == ("pl:302",)
    assert NAMES.query_keys("Springfield", "") == ("pl:300",)


def test_a_weight_tie_takes_the_first_row_of_the_table():
    twin_a = PlaceRow(5.0, 5.0, "Twin", "Twin", "Italy", 1_000, geonameid="1")
    twin_b = PlaceRow(6.0, 6.0, "Twin", "Twin", "Italy", 1_000, geonameid="2")
    assert PlaceNames([twin_b, twin_a]).query_keys("Twin", "") == ("pl:2",)
    assert PlaceNames([twin_a, twin_b]).query_keys("Twin", "") == ("pl:1",)


def test_an_unknown_name_gives_no_key():
    assert NAMES.query_keys("Atlantis", "") == ()
    assert NAMES.query_keys("Atlantis", "Italy") == ()
    assert NAMES.query_keys("Porto", "Italy") == ()          # not in that country
    assert NAMES.query_keys("", "") == ()
    assert NAMES.query_keys("", "Italy") == ()                # a country passed as the country field alone names no place


def test_an_unknown_country_does_not_filter_a_known_city():
    assert NAMES.query_keys("Rome", "Atlantis") == ("pl:100",)


def places_outcome(keys) -> SearchOutcome:
    trip = fact("trip", "Rome trip 10-14 August 2025, Albergo Immaginario, booking RM-55120", places=["pl:100", "co:italy"])
    porto = fact("porto", places=["pl:200", "co:portugal"])
    return search([trip, porto], query(places=[("Italy", "")]), place_keys=frozenset(keys))


def test_the_co_key_of_a_country_query_reaches_a_city_fact():
    assert places_outcome(NAMES.query_keys("Italy", "")).lists["places"] == ["trip"]


def test_a_city_query_reaches_a_fact_stored_under_its_large_neighbour():
    ostia_trip = fact("ostia", places=["pl:100"])
    out = search([ostia_trip], query(places=[("Ostia", "Italy")]), place_keys=frozenset(NAMES.query_keys("Ostia", "Italy")))
    assert out.lists["places"] == ["ostia"]


def test_an_unknown_place_asks_for_no_list_and_a_key_nobody_carries_neither():
    assert "places" not in places_outcome(NAMES.query_keys("Atlantis", "")).lists
    assert "places" not in places_outcome(["pl:999"]).lists
