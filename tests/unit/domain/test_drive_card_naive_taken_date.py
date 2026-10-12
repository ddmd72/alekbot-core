"""A photo taken date without an offset is read as UTC, so it compares with stored dates (§3.1, §8.5)."""
from datetime import datetime, timezone

from src.domain.drive_card import effective_date
from src.domain.drive_index import DriveChange

T0 = datetime(2026, 10, 11, tzinfo=timezone.utc)


class TestNaiveTakenDate:
    def test_naive_taken_is_utc(self):
        change = DriveChange(item_id="a", name="IMG.HEIC", facets={"photo": {"takenDateTime": "2025-08-14T10:00:00"}})
        taken = effective_date(change, T0)
        assert taken == datetime(2025, 8, 14, 10, tzinfo=timezone.utc)
        assert taken < T0   # comparable with aware datetimes, no TypeError

    def test_offset_is_kept(self):
        change = DriveChange(item_id="a", name="IMG.HEIC",
                             facets={"photo": {"takenDateTime": "2025-08-14T10:00:00+02:00"}})
        assert effective_date(change, T0).utcoffset().total_seconds() == 7200
