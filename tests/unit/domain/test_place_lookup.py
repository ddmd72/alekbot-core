from src.domain.place_lookup import PlaceIndex, PlaceRow

ROWS = [PlaceRow(41.8919, 12.5113, "Rome", "Roma", "Italy"),
        PlaceRow(41.1579, -8.6291, "Porto", "Porto", "Portugal"),
        PlaceRow(50.4501, 30.5234, "Kyiv", "Київ", "Ukraine"),
        PlaceRow(51.5085, -0.1257, "London", "London", "United Kingdom")]


def test_nearest_city_with_local_name():
    idx = PlaceIndex(ROWS)
    assert idx.lookup(41.9028, 12.4964) == "Rome (Roma), Italy"
    assert idx.lookup(41.16, -8.63) == "Porto, Portugal"


def test_local_name_omitted_when_same():
    assert PlaceIndex(ROWS).lookup(51.5, -0.12) == "London, United Kingdom"


def test_beyond_radius_is_none():
    assert PlaceIndex(ROWS).lookup(0.0, -30.0) is None


def test_crosses_grid_cell_boundary():
    idx = PlaceIndex([PlaceRow(42.0001, 12.0001, "Edge", "Edge", "X")])
    assert idx.lookup(41.9999, 11.9999) == "Edge, X"


def test_corrupt_coordinates_are_no_place():
    idx = PlaceIndex(ROWS)
    assert idx.lookup(float("nan"), 12.5) is None and idx.lookup(41.9, float("inf")) is None
    assert idx.lookup(95.0, 12.5) is None


def test_wraps_across_the_antimeridian():
    idx = PlaceIndex([PlaceRow(0.0, 179.9, "Dateline", "Dateline", "X")])
    assert idx.lookup(0.0, -179.95) == "Dateline, X"


def test_within_the_prefer_radius_the_heaviest_place_wins():
    rows = [PlaceRow(41.1485, -8.6110, "Porto", "Porto", "Portugal", weight=3 * 232_000),
            PlaceRow(41.1500, -8.6600, "Lordelo do Ouro", "Lordelo do Ouro", "Portugal", weight=22_000),
            PlaceRow(41.1240, -8.6120, "Vila Nova de Gaia", "Vila Nova de Gaia", "Portugal", weight=302_000)]
    assert PlaceIndex(rows).lookup(41.1520, -8.6550) == "Porto, Portugal"


def test_beyond_the_prefer_radius_the_nearest_place_wins():
    rows = [PlaceRow(42.0, 12.0, "Big", "Big", "X", weight=1_000_000),
            PlaceRow(42.2, 12.3, "Small", "Small", "X", weight=1_000)]
    assert PlaceIndex(rows).lookup(42.25, 12.35) == "Small, X"
