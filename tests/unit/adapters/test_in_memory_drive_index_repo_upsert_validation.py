"""The in-memory repository refuses a write batch whole, as the Firestore adapter does (USER_DRIVE_INDEXING_RFC §4):
a record due without a priority anywhere in the batch means nothing of the batch is written."""
from datetime import datetime, timezone

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_index import DriveIndexRecord

NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)


async def test_a_record_due_without_a_priority_refuses_the_whole_batch():
    repo = InMemoryDriveIndexRepository()
    good = DriveIndexRecord(user_id="u", connection_id="u", item_id="a", name="a.pdf", has_card=True)
    bad = DriveIndexRecord(user_id="u", connection_id="u", item_id="b", due_at=NOW, priority=None)
    with pytest.raises(ValueError):
        await repo.upsert_records([good, bad], {"a": [1.0, 0.0]})
    assert repo.records == {} and repo.vectors == {}
