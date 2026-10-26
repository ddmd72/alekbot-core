"""A foreign heavier place names a photo only when the photo is inside its footprint and outside the nearest
place's own footprint (USER_DRIVE_INDEXING_RFC §5.3): a city across a river does not swallow another city."""
from src.domain.place_lookup import PlaceIndex, PlaceRow

_BRAZZAVILLE = PlaceRow(-4.2661, 15.2832, "Brazzaville", "Brazzaville", "Republic of the Congo", weight=5_946_000)
_KINSHASA = PlaceRow(-4.3276, 15.3136, "Kinshasa", "Kinshasa", "Democratic Republic of the Congo",
                     weight=48_000_000)


def test_a_photo_inside_the_nearest_places_footprint_keeps_it():
    # 7.6 km from the Kinshasa row, inside its 10.4 km footprint, and 0 km from Brazzaville's
    assert PlaceIndex([_BRAZZAVILLE, _KINSHASA]).lookup(-4.2661, 15.2832) == "Brazzaville, Republic of the Congo"


def test_the_nearest_places_footprint_decides_between_two_foreign_places():
    def lookup(town_weight):
        rows = [PlaceRow(42.0, 12.0, "Town", "Town", "A", weight=town_weight),
                PlaceRow(42.0, 12.06, "City", "City", "B", weight=100_000_000)]   # footprint 15 km
        return PlaceIndex(rows).lookup(42.0, 12.02)        # 1.7 km from Town, 3.3 km from City
    assert lookup(1_000_000) == "City, B"                  # Town's footprint 1.5 km: the photo is outside it
    assert lookup(4_000_000) == "Town, A"                  # Town's footprint 3.0 km: the photo is inside it


def test_the_same_country_rule_ignores_the_nearest_places_footprint():
    rows = [PlaceRow(41.1844, -8.6963, "Matosinhos", "Matosinhos", "Portugal", weight=46_000),
            PlaceRow(41.1496, -8.6109, "Porto", "Porto", "Portugal", weight=1_620_343)]
    assert PlaceIndex(rows).lookup(41.1844, -8.6963) == "Porto, Portugal"
