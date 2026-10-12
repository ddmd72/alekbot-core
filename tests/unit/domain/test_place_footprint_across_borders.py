"""Across a border, a heavier place names a photo only when the photo lies within that place's own footprint,
k * sqrt(weight) km capped at PLACE_PREFER_RADIUS_KM (USER_DRIVE_INDEXING_RFC §5.3): a photo in Trastevere is
Rome although the Vatican City row is nearer, while Monaco is never Nice and Kehl never Strasbourg."""
from src.domain.place_lookup import PlaceIndex, PlaceRow

_VATICAN = PlaceRow(41.9027, 12.4541, "Vatican City", "Vatican City", "Vatican", weight=2_487)
_ROME = PlaceRow(41.8919, 12.5113, "Rome", "Roma", "Italy", weight=6_956_685)


def test_a_photo_inside_a_foreign_citys_footprint_gets_the_city():
    # Trastevere: 2 km from the Vatican City row, 3.4 km from Rome's
    assert PlaceIndex([_VATICAN, _ROME]).lookup(41.8894, 12.4700) == "Rome (Roma), Italy"


def test_a_photo_outside_the_footprint_keeps_the_nearest_foreign_place():
    # St. Peter's Square: 0.2 km from the Vatican City row, 4.7 km from Rome's
    assert PlaceIndex([_VATICAN, _ROME]).lookup(41.9022, 12.4568) == "Vatican City, Vatican"


def test_a_border_town_is_not_named_after_the_city_across_the_river():
    rows = [PlaceRow(48.5730, 7.8152, "Kehl", "Kehl", "Germany", weight=39_584),
            PlaceRow(48.5839, 7.7455, "Strasbourg", "Strasbourg", "France", weight=824_535)]
    assert PlaceIndex(rows).lookup(48.5730, 7.8152) == "Kehl, Germany"


def test_the_footprint_branch_still_needs_the_weight_factor():
    rows = [PlaceRow(42.0, 12.0, "Near", "Near", "A", weight=1_000_000),
            PlaceRow(42.0, 12.02, "Heavier", "Heavier", "B", weight=2_000_000)]   # 1.7 km away, only 2x heavier
    assert PlaceIndex(rows, footprint_k=0.01).lookup(42.0, 12.0) == "Near, A"


def test_the_footprint_is_capped_at_the_prefer_radius():
    rows = [PlaceRow(42.0, 12.0, "Near", "Near", "A", weight=1_000),
            PlaceRow(42.17, 12.0, "Huge", "Huge", "B", weight=100_000_000)]       # ~19 km away
    assert PlaceIndex(rows, footprint_k=1.0).lookup(42.0, 12.0) == "Near, A"


def test_without_a_footprint_no_place_crosses_a_border():
    assert PlaceIndex([_VATICAN, _ROME], footprint_k=0.0).lookup(41.8894, 12.4700) == "Vatican City, Vatican"
