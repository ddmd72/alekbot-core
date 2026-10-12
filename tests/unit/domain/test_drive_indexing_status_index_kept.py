"""`DriveIndexingStatus.index_kept`: a stored index exists and is inside the access-loss grace period
(USER_DRIVE_INDEXING_RFC §6, §7.7, ACCESS_LOSS_TTL). The Cabinet promises "kept about 30 days" only while true."""
from datetime import datetime, timedelta, timezone

from src.domain.drive_index_params import ACCESS_LOSS_TTL
from src.domain.drive_indexing_status import DriveIndexingStatus

NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
_INDEX = {"indexed": 3, "carded": 3}


def test_an_index_with_access_never_lost_is_kept():
    assert DriveIndexingStatus(enabled=True, counters=_INDEX).index_kept(NOW) is True


def test_inside_the_grace_period_it_is_kept():
    lost = NOW - ACCESS_LOSS_TTL + timedelta(hours=1)
    assert DriveIndexingStatus(enabled=True, counters=_INDEX, access_lost_at=lost).index_kept(NOW) is True


def test_past_the_grace_period_it_is_not():
    lost = NOW - ACCESS_LOSS_TTL
    assert DriveIndexingStatus(enabled=True, counters=_INDEX, access_lost_at=lost).index_kept(NOW) is False


def test_no_records_means_nothing_is_kept():
    """After the purge the sync state is gone: no access_lost_at, no counters, and no index to keep."""
    assert DriveIndexingStatus(enabled=True, counters={}).index_kept(NOW) is False
    assert DriveIndexingStatus(enabled=True, counters={"carded": 0}).index_kept(NOW) is False
