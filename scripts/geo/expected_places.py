"""
The expected place names on the bundled table (src/assets/geo/README.md "Expected results";
USER_DRIVE_INDEXING_RFC §5.3). One list, read by scripts/geo/measure_place_preference.py when picking the constants
and by tests/unit/adapters/test_geo_expected_places.py, which pins them.
"""
from __future__ import annotations

from typing import Optional, Sequence, Tuple

# (where the photo is taken, lat, lon, the place name it must get; None = no place)
EXPECTED_PLACES: Sequence[Tuple[str, float, float, Optional[str]]] = (
    ("Trastevere", 41.8894, 12.4700, "Rome (Roma), Italy"),             # the Vatican City row is nearer
    ("St. Peter's Square", 41.9022, 12.4568, "Vatican City, Vatican"),
    ("central Porto", 41.1480, -8.6110, "Porto, Portugal"),
    ("Porto, Ribeira", 41.1406, -8.6131, "Porto, Portugal"),            # not Vila Nova de Gaia
    ("Brooklyn", 40.6782, -73.9442, "New York, United States"),
    ("Brooklyn, Williamsburg", 40.7081, -73.9571, "New York, United States"),
    ("Torrent", 39.4373, -0.4654, "Valencia, Spain"),
    ("Monaco", 43.7384, 7.4246, "Monaco, Monaco"),                      # not Nice
    ("Vaduz", 47.1410, 9.5215, "Vaduz, Liechtenstein"),                 # not Feldkirch or Buchs
    ("San Marino (city)", 43.9356, 12.4473, "San Marino, San Marino"),  # not Serravalle
    ("Andorra la Vella", 42.5063, 1.5218, "Andorra la Vella, Andorra"),
    ("Gibraltar", 36.1408, -5.3536, "Gibraltar, Gibraltar"),            # not Algeciras
    ("Kehl", 48.5719, 7.8150, "Kehl, Germany"),                         # not Strasbourg
    ("Geneva", 46.2044, 6.1432, "Geneva (Genf), Switzerland"),
    ("Brazzaville", -4.2650, 15.2800, "Brazzaville, Republic of the Congo"),   # not Kinshasa
    ("the Dolomites (Plose)", 46.6930, 11.7300, "Brixen (Bressanone), Italy"),
    ("open sea", 0.0, -30.0, None),
)
