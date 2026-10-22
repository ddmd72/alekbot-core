import pytest

from src.adapters.geo_place_table import load_place_rows


def test_loads_tsv(tmp_path):
    p = tmp_path / "places.tsv"
    p.write_text("lat\tlon\tname_en\tname_local\tcountry\n41.89\t12.51\tRome\tRoma\tItaly\n", encoding="utf-8")
    rows = load_place_rows(p)
    assert rows[0].name_local == "Roma" and rows[0].lat == 41.89


@pytest.mark.datafile
@pytest.mark.slow
def test_bundled_table_loads_and_knows_rome():
    rows = load_place_rows()
    assert len(rows) > 20_000
    assert any(r.name_en == "Rome" and r.country == "Italy" for r in rows)
