"""Geo table v2's place forms (USER_DRIVE_INDEXING_RFC §25.2.1 `place_forms`, §25.4.3, §25.4.8): the spellings of a
photo's place — English, local, the alternate names in en/es/fr/uk/ru, the country's names, and the nearest place of
100 000 people or more — written on the index side only. Synthetic rows."""
from src.domain.drive_index import CURRENT_VERSIONS, GEO_TABLE_VERSION
from src.domain.place_lookup import PLACE_LARGE_POPULATION, Place, PlaceIndex, PlaceRow, place_forms

_SPAIN = ("Spain", "España", "Espagne", "Іспанія", "Испания")
_VALENCIA = PlaceRow(39.4697, -0.3774, "Valencia", "Valencia", "Spain", 2_473_020,
                     alt_names=("Valence", "Валенсия", "Валенсія"), country_names=_SPAIN)
_SAGUNTO = PlaceRow(39.6799, -0.2784, "Sagunto", "Sagunto", "Spain", 65_000,
                    alt_names=("Sagonte", "Сагунто"), country_names=_SPAIN, large=_VALENCIA)


def test_geo_table_version_is_2_and_current():
    assert GEO_TABLE_VERSION == 2 and CURRENT_VERSIONS["geo_table"] == 2


def test_large_city_threshold():
    assert PLACE_LARGE_POPULATION == 100_000


def test_forms_list_own_names_then_country_then_the_large_city():
    assert place_forms(_SAGUNTO) == ("Sagunto", "Sagonte", "Сагунто", "Spain", "España", "Espagne", "Іспанія",
                                     "Испания", "Valencia", "Valence", "Валенсия", "Валенсія")


def test_forms_are_deduplicated_ignoring_case_first_spelling_kept():
    row = PlaceRow(1.0, 1.0, "Roma", "Roma", "Italy", 10, alt_names=("ROMA", "Rome", "rome"),
                   country_names=("Italy", "Italia"))
    assert place_forms(row) == ("Roma", "Rome", "Italy", "Italia")


def test_a_v1_row_has_its_names_and_country_only():
    assert place_forms(PlaceRow(41.9, 12.5, "Rome", "Roma", "Italy")) == ("Rome", "Roma", "Italy")


def test_place_carries_the_display_name_and_the_forms():
    index = PlaceIndex([_VALENCIA, _SAGUNTO])
    place = index.place(39.68, -0.28)
    assert place == Place(name="Sagunto, Spain", forms=place_forms(_SAGUNTO))
    assert place.name == index.lookup(39.68, -0.28)


def test_forms_follow_the_row_the_preference_chose():
    index = PlaceIndex([_VALENCIA, PlaceRow(39.4373, -0.4654, "Torrent", "Torrent", "Spain", 80_000,
                                            large=_VALENCIA)])
    place = index.place(39.4373, -0.4654)        # Torrent's centre: Valencia outweighs it within 15 km
    assert place is not None and place.name == "Valencia, Spain" and place.forms == place_forms(_VALENCIA)


def test_no_place_no_forms():
    index = PlaceIndex([_VALENCIA])
    assert index.place(0.0, -30.0) is None and index.place(float("nan"), 0.0) is None
