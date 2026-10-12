"""The expected place names on the bundled table (src/assets/geo/README.md "Expected results";
USER_DRIVE_INDEXING_RFC §5.3). Measured with scripts/geo/measure_place_preference.py."""
import pytest

from src.adapters.geo_place_table import load_place_rows
from src.domain.place_lookup import PlaceIndex


@pytest.fixture(scope="module")
def index():
    return PlaceIndex(load_place_rows())


@pytest.mark.parametrize("lat,lon,expected", [
    (41.1480, -8.6110, "Porto, Portugal"),                   # central Porto, not the parish
    (41.1406, -8.6131, "Porto, Portugal"),                   # Ribeira, not Vila Nova de Gaia across the river
    (40.6782, -73.9442, "New York, United States"),          # Brooklyn
    (40.7081, -73.9571, "New York, United States"),          # Williamsburg
    (39.4373, -0.4654, "Valencia, Spain"),                   # Torrent
    (43.7384, 7.4246, "Monaco, Monaco"),                     # not Nice across the border
    (47.1410, 9.5215, "Vaduz, Liechtenstein"),               # not Feldkirch or Buchs across the border
    (46.6930, 11.7300, "Brixen (Bressanone), Italy"),        # the Dolomites
    (0.0, -30.0, None),                                      # open sea
])
def test_expected_place_names(index, lat, lon, expected):
    assert index.lookup(lat, lon) == expected
