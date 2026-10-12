"""Place lookup at high latitudes: every longitude cell within reach is scanned (§5.3)."""
from src.domain.place_lookup import PlaceIndex, PlaceRow


class TestHighLatitudeCandidates:
    def test_place_two_longitude_cells_away_at_70_north_is_found(self):
        # at 70°N one degree of longitude is ~38 km, so a place 42 km away sits two cells over
        idx = PlaceIndex([PlaceRow(70.5, 22.05, "Far", "Far", "Norway")])
        assert idx.lookup(70.5, 20.95) == "Far, Norway"

    def test_nearer_place_beyond_the_3x3_grid_wins_over_a_farther_one(self):
        rows = [PlaceRow(70.5, 22.05, "Near", "Near", "X"), PlaceRow(70.9, 20.5, "Farther", "Farther", "X")]
        assert PlaceIndex(rows).lookup(70.5, 20.95) == "Near, X"

    def test_near_the_pole_every_longitude_cell_is_scanned_once(self):
        idx = PlaceIndex([PlaceRow(89.9, 100.0, "Pole", "Pole", "X")])
        assert idx.lookup(89.95, -80.0) == "Pole, X"

    def test_southern_high_latitude(self):
        idx = PlaceIndex([PlaceRow(-70.5, 22.05, "South", "South", "AQ")])
        assert idx.lookup(-70.5, 20.95) == "South, AQ"
