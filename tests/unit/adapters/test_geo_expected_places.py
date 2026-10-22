"""The expected place names on the bundled table, from the one list the measurement script also reads
(scripts/geo/expected_places.py; USER_DRIVE_INDEXING_RFC §5.3)."""
import pytest

from scripts.geo.expected_places import EXPECTED_PLACES
from src.adapters.geo_place_table import load_place_rows
from src.domain.place_lookup import PlaceIndex


@pytest.fixture(scope="module")
def index():
    return PlaceIndex(load_place_rows())


@pytest.mark.datafile
@pytest.mark.slow
@pytest.mark.parametrize("where,lat,lon,expected", EXPECTED_PLACES, ids=[case[0] for case in EXPECTED_PLACES])
def test_expected_place_name(index, where, lat, lon, expected):
    assert index.lookup(lat, lon) == expected
