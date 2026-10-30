"""Release reads the card cap flag (USER_DRIVE_INDEXING_RFC §5.4), never the reason, in both implementations: best
card_rank first, then newest modified_at; a record with a structural reason and the flag is returned."""
from datetime import datetime, timedelta, timezone

from google.cloud import firestore

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_index import DriveIndexRecord, Reason, RecordState

from tests.unit.adapters.firestore_drive_fakes import PROJECTION, Db, executed, make_repo

NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)


def rec(item_id, **kw):
    return DriveIndexRecord(user_id="u", connection_id="u", item_id=item_id, state=RecordState.METADATA_ONLY, **kw)


async def test_in_memory_release_reads_the_flag():
    repo = InMemoryDriveIndexRepository()
    await repo.upsert_records([
        rec("old", card_capped=True, reason=Reason.KIND_NOT_ADMITTED, card_rank=2, modified_at=NOW),
        rec("new", card_capped=True, reason=Reason.KIND_NOT_ADMITTED, card_rank=2,
            modified_at=NOW + timedelta(days=1)),
        rec("doc", card_capped=True, reason=Reason.KIND_NOT_ADMITTED, card_rank=0, modified_at=NOW),
        rec("repo", card_capped=True, reason=Reason.OPAQUE_ROOT, card_rank=3, modified_at=NOW),
        rec("free", reason=Reason.KIND_NOT_ADMITTED, card_rank=0, modified_at=NOW),
    ])
    assert [r.item_id for r in await repo.card_capped("u", 10)] == ["doc", "new", "old", "repo"]


async def test_firestore_release_queries_the_flag(mock_env_config):
    db = Db()
    repo = make_repo(db, mock_env_config)
    await repo.card_capped("u", 7)
    assert executed(db) == [[("where", "user_id", "==", "u"), ("where", "card_capped", "==", True),
                             ("order_by", "card_rank", "ASCENDING"),
                             ("order_by", "modified_at", firestore.Query.DESCENDING), ("limit", 7), PROJECTION]]
