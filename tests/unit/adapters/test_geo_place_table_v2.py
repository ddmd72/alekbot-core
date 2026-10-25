"""Loading geo table v2 (src/adapters/geo_place_table.py; USER_DRIVE_INDEXING_RFC §25.4.3): alternate names, the
country's names shared per country, the large place linked by id; the bundled table answers place forms in the
languages §25 names."""
import pytest

from src.adapters.geo_place_table import DEFAULT_COUNTRY_TABLE, DEFAULT_PLACE_TABLE, load_place_rows
from src.domain.place_lookup import PlaceIndex

_HEADER = "geonameid\tlat\tlon\tname_en\tname_local\tcountry\tweight\tpopulation\talt_names\tlarge_id\n"


def _tables(tmp_path):
    places = tmp_path / "places.tsv"
    places.write_text(_HEADER
                      + "10\t41.1496\t-8.6109\tPorto\tPorto\tPortugal\t5500000\t1600000\tOporto|Порту\t\n"
                      + "11\t41.1844\t-8.6963\tMatosinhos\tMatosinhos\tPortugal\t220000\t220000\tМатозіньюш\t10\n"
                      + "12\t48.5\t7.8\tKehl\tKehl\tGermany\t40000\t40000\t\t99\n", encoding="utf-8")
    countries = tmp_path / "countries.tsv"
    countries.write_text("country\tnames\nPortugal\tPortugalia|Португалия\n", encoding="utf-8")
    return places, countries


def test_v2_columns_are_loaded(tmp_path):
    rows = {r.name_en: r for r in load_place_rows(*_tables(tmp_path))}
    assert rows["Porto"].alt_names == ("Oporto", "Порту") and rows["Porto"].large is None
    assert rows["Matosinhos"].large is rows["Porto"]
    assert rows["Matosinhos"].country_names == ("Portugalia", "Португалия")
    assert rows["Matosinhos"].country_names is rows["Porto"].country_names        # one tuple per country
    assert rows["Kehl"].country_names == () and rows["Kehl"].large is None         # unknown id: no large place


def test_a_v1_table_still_loads_without_forms(tmp_path):
    p = tmp_path / "places.tsv"
    p.write_text("lat\tlon\tname_en\tname_local\tcountry\n41.89\t12.51\tRome\tRoma\tItaly\n", encoding="utf-8")
    (row,) = load_place_rows(p)
    assert (row.alt_names, row.country_names, row.large) == ((), (), None)


def test_the_bundled_tables_are_v2():
    assert DEFAULT_PLACE_TABLE.name == "places_v2.tsv" and DEFAULT_COUNTRY_TABLE.name == "countries_v2.tsv"


@pytest.fixture(scope="module")
def index():
    return PlaceIndex(load_place_rows())


@pytest.mark.slow
@pytest.mark.parametrize("lat,lon,expected", [
    (41.1496, -8.6109, {"Porto", "Oporto", "Порту", "Portugal", "Португалія", "Португалия"}),
    (52.5244, 13.4105, {"Berlin", "Берлін", "Берлин", "Germany", "Alemania", "Германия"}),
    (41.8919, 12.5113, {"Rome", "Roma", "Рим", "Italy", "Italia"}),
    (53.5507, 9.9930, {"Hamburg", "Hambourg", "Гамбург", "Germany", "Allemagne", "Німеччина"}),
    (49.8383, 24.0232, {"Lviv", "Львів", "Львов", "Ukraine", "Україна", "Украина"}),
])
def test_bundled_place_forms(index, lat, lon, expected):
    place = index.place(lat, lon)
    assert place is not None and expected <= set(place.forms)
    assert place.name == index.lookup(lat, lon)


@pytest.mark.slow
def test_a_small_place_names_its_large_neighbour(index):
    place = index.place(38.6979, -9.4215)            # Cascais, a coastal town 25 km west of Lisbon
    assert place is not None and place.name.startswith("Cascais")
    assert place.forms[0] == "Cascais" and len(place.forms) > 3
    row = index.resolve(38.6979, -9.4215)
    assert row is not None and row.large is not None and row.large.large is None
