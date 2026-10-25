"""Geo table v2's place forms (USER_DRIVE_INDEXING_RFC §25.2.1 `place_forms`, §25.4.3, §25.4.8): the spellings of a
photo's place — English, local, the alternate names in en/es/fr/uk/ru, the country's names, and the nearest place of
100 000 people or more — written on the index side only. Synthetic rows."""
from src.domain.drive_index import CURRENT_VERSIONS, GEO_TABLE_VERSION
from src.domain.place_lookup import PLACE_LARGE_POPULATION, Place, PlaceIndex, PlaceRow, place_forms

_GERMANY = ("Germany", "Alemania", "Allemagne", "Німеччина", "Германия")
_HAMBURG = PlaceRow(53.5511, 9.9937, "Hamburg", "Hamburg", "Germany", 1_841_179,
                    alt_names=("Hambourg", "Amburgo", "Гамбург"), country_names=_GERMANY)
_LUENEBURG = PlaceRow(53.2464, 10.4115, "Lüneburg", "Lüneburg", "Germany", 75_000,
                      alt_names=("Luneburgo", "Люнебург"), country_names=_GERMANY, large=_HAMBURG)


def test_geo_table_version_is_2_and_current():
    assert GEO_TABLE_VERSION == 2 and CURRENT_VERSIONS["geo_table"] == 2


def test_large_city_threshold():
    assert PLACE_LARGE_POPULATION == 100_000


def test_forms_list_own_names_then_country_then_the_large_city():
    assert place_forms(_LUENEBURG) == ("Lüneburg", "Luneburgo", "Люнебург", "Germany", "Alemania", "Allemagne",
                                       "Німеччина", "Германия", "Hamburg", "Hambourg", "Amburgo", "Гамбург")


def test_forms_are_deduplicated_ignoring_case_first_spelling_kept():
    row = PlaceRow(1.0, 1.0, "Roma", "Roma", "Italy", 10, alt_names=("ROMA", "Rome", "rome"),
                   country_names=("Italy", "Italia"))
    assert place_forms(row) == ("Roma", "Rome", "Italy", "Italia")


def test_a_v1_row_has_its_names_and_country_only():
    assert place_forms(PlaceRow(41.9, 12.5, "Rome", "Roma", "Italy")) == ("Rome", "Roma", "Italy")


def test_place_carries_the_display_name_and_the_forms():
    index = PlaceIndex([_HAMBURG, _LUENEBURG])
    place = index.place(53.25, 10.41)
    assert place == Place(name="Lüneburg, Germany", forms=place_forms(_LUENEBURG))
    assert place.name == index.lookup(53.25, 10.41)


def test_forms_follow_the_row_the_preference_chose():
    index = PlaceIndex([_HAMBURG, PlaceRow(53.5692, 10.1708, "Barsbüttel", "Barsbüttel", "Germany", 13_000,
                                           large=_HAMBURG)])
    place = index.place(53.5692, 10.1708)       # Barsbüttel's centre: Hamburg outweighs it within 15 km
    assert place is not None and place.name == "Hamburg, Germany" and place.forms == place_forms(_HAMBURG)


def test_no_place_no_forms():
    index = PlaceIndex([_HAMBURG])
    assert index.place(0.0, -30.0) is None and index.place(float("nan"), 0.0) is None
