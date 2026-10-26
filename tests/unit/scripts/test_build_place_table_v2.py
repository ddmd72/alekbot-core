"""Geo table v2's build (scripts/geo/build_place_table.py; USER_DRIVE_INDEXING_RFC §25.4.3): deterministic and
reproducible from synthetic GeoNames-shaped inputs, the names rule per language, the country names and the large-place
key. The bundled table's sources record is checked for the inputs it names. No network."""
import json
import zipfile
from pathlib import Path

import pytest

from scripts.geo import build_place_table as build

_ASSETS = Path(__file__).resolve().parents[3] / "src" / "assets" / "geo"


def _city(gid, name, lat, lon, cc, population, code="PPL"):
    cols = [""] * 19
    cols[0], cols[1], cols[2], cols[4], cols[5], cols[6], cols[7], cols[8], cols[14] = (
        gid, name, name, str(lat), str(lon), "P", code, cc, str(population))
    return "\t".join(cols)


def _country(iso, name, languages, gid):
    cols = [""] * 19
    cols[0], cols[4], cols[15], cols[16] = iso, name, languages, gid
    return "\t".join(cols)


def _alt(aid, gid, lang, name, preferred=False, short=False, colloquial=False, historic=False):
    flags = ["1" if f else "" for f in (preferred, short, colloquial, historic)]
    return "\t".join([str(aid), gid, lang, name, *flags, "", ""])


def _inputs(tmp_path: Path):
    cities = [
        _city("10", "Porto", 41.1496, -8.6109, "PT", 1_600_000, "PPLA"),
        _city("11", "Matozinhos", 41.1844, -8.6963, "PT", 92_000),
        _city("12", "Foz do Douro", 41.1512, -8.6766, "PT", 40_000, "PPLX"),   # a city section: left out
        _city("20", "Lviv", 49.8383, 24.0232, "UA", 720_000, "PPLA"),
        _city("30", "Far Village", 10.0, 10.0, "PT", 6_000),
    ]
    with zipfile.ZipFile(tmp_path / "cities5000.zip", "w") as zf:
        zf.writestr("cities5000.txt", "\n".join(cities) + "\n")
    (tmp_path / "countryInfo.txt").write_text(
        "# comment\n" + "\n".join([_country("PT", "Portugal", "pt-PT,mwl", "900"),
                                   _country("UA", "Ukraine", "uk,ru-UA", "901")]) + "\n", encoding="utf-8")
    alternates = [
        _alt(1, "10", "en", "Porto"), _alt(2, "10", "es", "Oporto", preferred=True),
        _alt(3, "10", "ru", "Порту"), _alt(4, "10", "uk", "Порту"), _alt(5, "10", "mwl", "Puorto"),
        _alt(6, "11", "pt", "Matosinhos", preferred=True), _alt(7, "11", "en", "Matosinhos"),
        _alt(8, "20", "en", "Lvov", historic=True), _alt(9, "20", "en", "Lviv", preferred=True),
        _alt(10, "20", "ru", "Львов", preferred=True), _alt(11, "20", "ru", "Львову"),
        _alt(12, "20", "uk", "Львів"), _alt(13, "20", "fr", "Lviv"), _alt(14, "20", "fr", "Lvov", preferred=True),
        _alt(15, "20", "en", "City of Lions", colloquial=True),
        _alt(16, "900", "en", "Portugal", preferred=True), _alt(17, "900", "uk", "Португалія", preferred=True),
        _alt(18, "900", "ru", "Португальская Республика"), _alt(19, "900", "ru", "Португалия", preferred=True),
        _alt(20, "901", "uk", "Україна", preferred=True), _alt(21, "901", "ru", "Украина"),
        _alt(22, "999", "en", "Unrelated place"),
    ]
    with zipfile.ZipFile(tmp_path / "alternateNamesV2.zip", "w") as zf:
        zf.writestr("alternateNamesV2.txt", "\n".join(alternates) + "\n")
    return tmp_path / "cities5000.zip", tmp_path / "countryInfo.txt", tmp_path / "alternateNamesV2.zip"


@pytest.fixture
def built(tmp_path):
    rows, countries = build.build(*_inputs(tmp_path))
    return {r[0]: dict(zip(build.PLACE_COLUMNS, r)) for r in rows}, dict(countries)


def test_the_same_inputs_give_byte_identical_tables(tmp_path):
    inputs = _inputs(tmp_path)
    outputs = []
    for run in ("a", "b"):
        build.write(tmp_path / run, *build.build(*inputs))
        outputs.append([(tmp_path / run / name).read_bytes() for name in (build.PLACES_FILE, build.COUNTRIES_FILE)])
    assert outputs[0] == outputs[1]
    header = (tmp_path / "a" / build.PLACES_FILE).read_text(encoding="utf-8").splitlines()[0]
    assert header.split("\t") == list(build.PLACE_COLUMNS)


def test_rows_are_sorted_and_city_sections_left_out(built):
    places, _ = built
    assert set(places) == {"10", "11", "20", "30"}


def test_v1_columns_keep_their_rule(built):
    places, _ = built
    assert places["10"]["name_en"] == "Porto" and places["10"]["name_local"] == "Porto"
    assert places["10"]["weight"] == str(1_600_000 * 3) and places["10"]["population"] == "1600000"
    assert places["20"]["name_en"] == "Lviv" and places["20"]["name_local"] == "Львів"
    assert places["11"]["name_local"] == "Matosinhos" and places["11"]["country"] == "Portugal"


def test_alternate_names_follow_the_names_rule(built):
    places, _ = built
    # GeoNames' own name, then en/es/fr/uk/ru; preferred names win; colloquial, historic and inflected extras go
    assert places["20"]["alt_names"].split("|") == ["Lvov", "Львов"]
    assert places["10"]["alt_names"].split("|") == ["Oporto", "Порту"]   # no Mirandese "Puorto"; uk and ru agree
    assert places["11"]["alt_names"].split("|") == ["Matozinhos"]


def test_country_names(built):
    _, countries = built
    assert countries["Portugal"].split("|") == ["Португалія", "Португалия"]
    assert countries["Ukraine"].split("|") == ["Україна", "Украина"]


def test_the_large_place_key(built):
    places, _ = built
    assert places["11"]["large_id"] == "10"            # Matosinhos is about 8 km from Porto
    assert places["10"]["large_id"] == ""              # large itself
    assert places["30"]["large_id"] == ""              # none within PLACE_MAX_DISTANCE_KM


def test_the_sources_record_names_hashes_and_refuses_another_snapshot(tmp_path):
    paths = dict(zip(build.INPUTS, _inputs(tmp_path)))
    (tmp_path / "cities5000.zip.last-modified").write_text("Tue, 13 Oct 2026 01:57:19 GMT", encoding="utf-8")
    record = build.sources(paths, places=4, countries=2)
    assert set(record["inputs"]) == set(build.INPUTS)
    assert record["inputs"]["cities5000.zip"]["last_modified"] == "Tue, 13 Oct 2026 01:57:19 GMT"
    assert all(len(entry["sha256"]) == 64 for entry in record["inputs"].values())
    assert build.snapshot_differs(record, paths) == []
    with zipfile.ZipFile(paths["cities5000.zip"], "a") as zf:
        zf.writestr("extra.txt", "changed")
    assert build.snapshot_differs(record, paths) == ["cities5000.zip"]


def test_the_bundled_table_records_its_snapshot():
    record = json.loads((_ASSETS / build.SOURCES_FILE).read_text(encoding="utf-8"))
    assert set(record["inputs"]) == set(build.INPUTS)
    assert all(len(entry["sha256"]) == 64 and entry["bytes"] > 0 for entry in record["inputs"].values())
    assert record["languages"] == ["en", "es", "fr", "uk", "ru"] and record["large_population"] == 100_000
    lines = (_ASSETS / build.PLACES_FILE).read_text(encoding="utf-8").count("\n") - 1
    assert record["places"] == lines
