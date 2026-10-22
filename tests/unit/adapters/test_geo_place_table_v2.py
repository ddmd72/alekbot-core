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
                      + "10\t41.3888\t2.1590\tBarcelona\tBarcelona\tSpain\t5500000\t1600000\tBarcelone|Барселона\t\n"
                      + "11\t41.4500\t2.2474\tBadalona\tBadalona\tSpain\t220000\t220000\tБадалона\t10\n"
                      + "12\t48.5\t7.8\tKehl\tKehl\tGermany\t40000\t40000\t\t99\n", encoding="utf-8")
    countries = tmp_path / "countries.tsv"
    countries.write_text("country\tnames\nSpain\tEspaña|Испания\n", encoding="utf-8")
    return places, countries


def test_v2_columns_are_loaded(tmp_path):
    rows = {r.name_en: r for r in load_place_rows(*_tables(tmp_path))}
    assert rows["Barcelona"].alt_names == ("Barcelone", "Барселона") and rows["Barcelona"].large is None
    assert rows["Badalona"].large is rows["Barcelona"]
    assert rows["Badalona"].country_names == ("España", "Испания")
    assert rows["Badalona"].country_names is rows["Barcelona"].country_names        # one tuple per country
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


@pytest.mark.datafile
@pytest.mark.slow
@pytest.mark.parametrize("lat,lon,expected", [
    (41.3888, 2.1590, {"Barcelona", "Barcelone", "Барселона", "Spain", "España", "Испания", "Іспанія"}),
    (48.8534, 2.3488, {"Paris", "Париж", "France", "Francia", "Франция"}),
    (41.8919, 12.5113, {"Rome", "Roma", "Рим", "Italy", "Italia"}),
    (50.4547, 30.5238, {"Kyiv", "Київ", "Киев", "Ukraine", "Україна", "Украина"}),
    (49.8383, 24.0232, {"Lviv", "Львів", "Львов"}),
])
def test_bundled_place_forms(index, lat, lon, expected):
    place = index.place(lat, lon)
    assert place is not None and expected <= set(place.forms)
    assert place.name == index.lookup(lat, lon)


def test_a_small_place_names_its_large_neighbour(index):
    place = index.place(41.2351, 1.8119)             # Sitges, ~30 000 people, 35 km from Barcelona's suburbs
    assert place is not None and place.name.startswith("Sitges")
    assert place.forms[0] == "Sitges" and len(place.forms) > 3
    row = index.resolve(41.2351, 1.8119)
    assert row is not None and row.large is not None and row.large.large is None
