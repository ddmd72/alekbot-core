"""The atlas rows a profile question offers (fact profile RFC §4.7): `PlaceNames.candidates`, `keys_of`,
`canonical_country` and `local_place_key`, and `resolve` unchanged by the refactor. A fixture atlas from the invented
universe: Rome in two countries, Porto and Lisbon once each, no Springfield."""
import pytest

from src.domain.place_lookup import PlaceRow
from src.domain.place_names import PlaceNames, local_place_key

US = ("United States",)
ITALY = ("Italia", "Italy")
ROME_US = PlaceRow(1.0, 1.0, "Rome", "Rome", "United States", 10, country_names=US, geonameid="2000")
ROME_IT = PlaceRow(2.0, 2.0, "Rome", "Roma", "Italy", 1000, country_names=ITALY, geonameid="1000")
PORTO = PlaceRow(3.0, 3.0, "Porto", "Porto", "Portugal", 1000, country_names=("Portugal",), geonameid="3000")
LISBON = PlaceRow(4.0, 4.0, "Lisbon", "Lisboa", "Portugal", 1000, country_names=("Portugal",), geonameid="4000")
# The lighter Rome first in the table: the order of the candidates is by weight, not by table order.
NAMES = PlaceNames([ROME_US, ROME_IT, PORTO, LISBON])


def test_rome_without_a_country_is_ambiguous_with_both_rows_heaviest_first():
    assert NAMES.candidates("Rome", "") == ("ambiguous", (ROME_IT, ROME_US))


def test_rome_in_the_united_states_resolves_to_its_one_row():
    assert NAMES.candidates("Rome", "United States") == (None, (ROME_US,))


def test_a_place_the_atlas_lacks_has_no_rows():
    assert NAMES.candidates("Springfield", "") == ("not_in_atlas", ())
    assert NAMES.candidates("Springfield", "Portugal") == ("not_in_atlas", ())


def test_a_city_named_in_a_country_without_it_offers_the_rows_of_its_name_elsewhere():
    assert NAMES.candidates("Porto", "Italy") == ("not_in_country", (PORTO,))


def test_other_reasons_carry_no_rows():
    assert NAMES.candidates("Porto", "Atlantis") == ("unknown_country", ())
    assert NAMES.candidates("", "") == ("empty", ())


def test_equal_weights_keep_the_table_order():
    a = PlaceRow(1.0, 1.0, "Rome", "Roma", "Italy", 10, country_names=ITALY, geonameid="5000")
    b = PlaceRow(2.0, 2.0, "Rome", "Rome", "United States", 10, country_names=US, geonameid="6000")
    assert PlaceNames([a, b]).candidates("Rome", "") == ("ambiguous", (a, b))


def test_keys_of_a_row_equals_the_keys_resolve_gives_it():
    assert NAMES.keys_of("2000") == NAMES.resolve("Rome", "United States")[0] == ("pl:2000", "co:united_states")
    assert NAMES.keys_of("3000") == NAMES.resolve("Porto", "Portugal")[0]


def test_keys_of_carries_the_large_neighbour_as_resolve_does():
    near = PlaceRow(5.0, 5.0, "Shelbyville", "Shelbyville", "Portugal", 10, country_names=("Portugal",), large=PORTO,
                    geonameid="7000")
    names = PlaceNames([PORTO, near])
    assert names.keys_of("7000") == names.resolve("Shelbyville", "Portugal")[0] == ("pl:7000", "pl:3000", "co:portugal")


def test_keys_of_a_row_that_left_the_atlas_is_empty():
    assert NAMES.keys_of("9000") == ()
    assert NAMES.keys_of("") == ()


def test_canonical_country_reads_names_and_aliases():
    assert NAMES.canonical_country("usa") == "United States"
    assert NAMES.canonical_country("italia") == "Italy"
    assert NAMES.canonical_country("portugal") == "Portugal"
    assert NAMES.canonical_country("Atlantis") is None
    assert NAMES.canonical_country("") is None


def test_local_place_key_is_the_folded_city():
    assert local_place_key("Springfield") == "lp:springfield"
    assert local_place_key("  SHELBYVILLE ") == "lp:shelbyville"
    assert local_place_key("Shelby-ville") == "lp:shelby_ville"


def test_local_place_key_refuses_an_empty_city():
    for city in ("", "  ", "-"):
        with pytest.raises(ValueError):
            local_place_key(city)


def test_resolve_is_unchanged_on_the_same_table():
    assert NAMES.resolve("Rome", "") == ((), "ambiguous")
    assert NAMES.resolve("Rome", "Italy") == (("pl:1000", "co:italy"), None)
    assert NAMES.resolve("Roma", "Italia") == (("pl:1000", "co:italy"), None)
    assert NAMES.resolve("Rome", "usa") == (("pl:2000", "co:united_states"), None)
    assert NAMES.resolve("Lisboa", "") == (("pl:4000", "co:portugal"), None)
    assert NAMES.resolve("Springfield", "") == ((), "not_in_atlas")
    assert NAMES.resolve("Springfield", "Portugal") == ((), "not_in_atlas")
    assert NAMES.resolve("Porto", "United States") == ((), "not_in_country")
    assert NAMES.resolve("Porto", "Atlantis") == ((), "unknown_country")
    assert NAMES.resolve("", "Portugal") == (("co:portugal",), None)
    assert NAMES.resolve("", "") == ((), "empty")
