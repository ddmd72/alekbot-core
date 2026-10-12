"""A place with no known weight has a zero footprint; at its own centre it is still not "outside" it, so a heavier
foreign place never takes its name there (USER_DRIVE_INDEXING_RFC §5.3)."""
from src.domain.place_lookup import PlaceIndex, PlaceRow


def test_a_weightless_place_keeps_its_name_at_its_own_centre():
    rows = [PlaceRow(42.0, 12.0, "Hamlet", "Hamlet", "A", weight=0),
            PlaceRow(42.0, 12.02, "City", "City", "B", weight=100_000_000)]   # 1.7 km away, footprint 15 km
    assert PlaceIndex(rows).lookup(42.0, 12.0) == "Hamlet, A"
