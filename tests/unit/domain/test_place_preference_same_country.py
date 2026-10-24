"""A heavier place names a photo instead of the nearest one only within the nearest place's country, and only
when it is at least PLACE_PREFER_WEIGHT_FACTOR times heavier (USER_DRIVE_INDEXING_RFC §5.3)."""
from src.domain.place_lookup import PLACE_PREFER_WEIGHT_FACTOR, PlaceIndex, PlaceRow


def _row(name, lat, lon, country, weight):
    return PlaceRow(lat, lon, name, name, country, weight=weight)


def test_a_heavier_place_across_the_border_never_names_the_photo():
    rows = [_row("Vaduz", 47.1415, 9.5215, "Liechtenstein", 15_591),
            _row("Feldkirch", 47.2333, 9.6000, "Austria", 100_000),
            _row("Buchs", 47.1667, 9.4667, "Switzerland", 40_000)]
    assert PlaceIndex(rows).lookup(47.1410, 9.5215) == "Vaduz, Liechtenstein"


def test_a_same_country_place_below_the_factor_does_not_take_over():
    rows = [_row("Near", 42.0, 12.0, "X", 100_000), _row("Bigger", 42.05, 12.05, "X", 250_000)]
    assert PlaceIndex(rows, prefer_weight_factor=3.0).lookup(42.0, 12.0) == "Near, X"


def test_a_same_country_place_at_the_factor_takes_over():
    rows = [_row("Esplugues", 41.3768, 2.0886, "Spain", 46_000), _row("Barcelona", 41.3874, 2.1686, "Spain", 1_620_343)]
    assert PlaceIndex(rows, prefer_weight_factor=3.0).lookup(41.3771, 2.0889) == "Barcelona, Spain"
    rows = [_row("Near", 42.0, 12.0, "X", 100_000), _row("Triple", 42.05, 12.05, "X", 300_000)]
    assert PlaceIndex(rows, prefer_weight_factor=3.0).lookup(42.0, 12.0) == "Triple, X"


def test_the_heaviest_same_country_place_wins_even_when_a_foreign_one_is_heavier():
    rows = [_row("Village", 43.0, 7.0, "A", 10_000), _row("Town", 43.03, 7.03, "A", 50_000),
            _row("City", 43.05, 7.05, "A", 200_000), _row("Metropolis", 43.04, 7.0, "B", 5_000_000)]
    assert PlaceIndex(rows).lookup(43.0, 7.0) == "City, A"


def test_the_preference_stays_within_the_prefer_radius():
    rows = [_row("Near", 42.0, 12.0, "X", 1_000), _row("Far", 42.2, 12.0, "X", 10_000_000)]   # ~22 km away
    assert PlaceIndex(rows).lookup(42.0, 12.0) == "Near, X"


def test_the_factor_is_a_parameter_with_the_measured_default():
    rows = [_row("Near", 42.0, 12.0, "X", 100_000), _row("Bigger", 42.05, 12.05, "X", 250_000)]
    assert PlaceIndex(rows, prefer_weight_factor=2.0).lookup(42.0, 12.0) == "Bigger, X"
    assert PlaceIndex(rows).lookup(42.0, 12.0) == ("Bigger, X" if PLACE_PREFER_WEIGHT_FACTOR <= 2.5 else "Near, X")


def test_resolve_returns_the_row_lookup_names():
    rows = [_row("Near", 42.0, 12.0, "X", 100_000)]
    index = PlaceIndex(rows)
    assert index.resolve(42.0, 12.0) is rows[0]
    assert index.resolve(0.0, -30.0) is None and index.resolve(float("nan"), 0.0) is None
