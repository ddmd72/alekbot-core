"""A photo's taken date compares with stored dates (§3.1, §8.5).

MULTI_DRIVE_RFC §7.2: the adapter parses the provider's timestamp into the typed media facts and reads an offset-less one
as UTC (tests/unit/adapters/test_onedrive_neutral_shapes.py); the domain refuses a naive `taken_at`, so the effective
date is always aware."""
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.drive_card import effective_date
from src.domain.drive_index import DriveChange
from src.domain.drive_media_facts import DriveMediaFacts

T0 = datetime(2026, 10, 11, tzinfo=timezone.utc)


class TestNaiveTakenDate:
    def test_naive_taken_is_utc(self):
        with pytest.raises(ValueError):
            DriveMediaFacts(taken_at=datetime(2025, 8, 14, 10))
        change = DriveChange(item_id="a", name="IMG.HEIC",
                             media=DriveMediaFacts(taken_at=datetime(2025, 8, 14, 10, tzinfo=timezone.utc)))
        taken = effective_date(change, T0)
        assert taken == datetime(2025, 8, 14, 10, tzinfo=timezone.utc)
        assert taken < T0   # comparable with aware datetimes, no TypeError

    def test_offset_is_kept(self):
        change = DriveChange(item_id="a", name="IMG.HEIC", media=DriveMediaFacts(
            taken_at=datetime(2025, 8, 14, 10, tzinfo=timezone(timedelta(hours=2)))))
        assert effective_date(change, T0).utcoffset().total_seconds() == 7200
