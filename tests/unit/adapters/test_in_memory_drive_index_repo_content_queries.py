"""The chunk-cap release query and the `unread` count on the in-memory repository (USER_DRIVE_INDEXING_RFC §5.4,
§10.2): the Firestore adapter must give the same answers (plan part D)."""
from datetime import datetime, timedelta, timezone

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.connection_scope import ConnectionScope
from src.domain.drive_index import DriveIndexRecord, Reason, RecordState
from tests.unit.services.drive_index_fakes import U_SCOPE

NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
KINDS = frozenset({"pdf", "markdown"})


def rec(item_id, kind="pdf", state=RecordState.METADATA_ONLY, reason=None, **kw):
    return DriveIndexRecord(user_id="u", connection_id="u", item_id=item_id, name=item_id, kind=kind, state=state, reason=reason, **kw)


async def test_chunk_capped_lists_refusals_best_rank_then_newest():
    repo = InMemoryDriveIndexRepository()
    await repo.upsert_records([
        rec("old-doc", reason=Reason.USER_CHUNK_CAP, card_rank=0, modified_at=NOW - timedelta(days=2)),
        rec("new-doc", reason=Reason.USER_CHUNK_CAP, card_rank=0, modified_at=NOW),
        rec("image", kind="image", reason=Reason.USER_CHUNK_CAP, card_rank=1, modified_at=NOW + timedelta(days=1)),
        rec("no-text", reason=Reason.NO_TEXT, card_rank=0, modified_at=NOW),
        DriveIndexRecord(user_id="v", connection_id="v", item_id="other-user", state=RecordState.METADATA_ONLY,
                         reason=Reason.USER_CHUNK_CAP),
    ])
    assert [r.item_id for r in await repo.chunk_capped(U_SCOPE, 10)] == ["new-doc", "old-doc", "image"]
    assert [r.item_id for r in await repo.chunk_capped(U_SCOPE, 1)] == ["new-doc"]


async def test_count_unread_follows_is_unread():
    repo = InMemoryDriveIndexRepository()
    await repo.upsert_records([
        rec("pending", state=RecordState.PENDING),                                  # unread
        rec("pending-readable", state=RecordState.PENDING, indexed_generation=1),  # its old chunks serve
        rec("failed", state=RecordState.FAILED, reason=Reason.EXTRACT_ERROR),       # unread
        rec("scan", reason=Reason.NO_TEXT),                                         # unread
        rec("log", kind="markdown", reason=Reason.MACHINE_TEXT),                    # unread
        rec("capped", reason=Reason.USER_CHUNK_CAP),                                # unread
        rec("big", reason=Reason.TOO_LARGE),                                        # unread
        rec("indexed", state=RecordState.INDEXED, indexed_generation=1),
        rec("hidden", reason=Reason.DOT_NAME),
        rec("photo", kind="image", reason=Reason.PHOTOGRAPH),
        rec("code", kind="code", state=RecordState.FAILED),                         # kind not counted
    ])
    assert await repo.count_unread(U_SCOPE, KINDS) == 6
    assert await repo.count_unread(U_SCOPE, frozenset()) == 0
    assert await repo.count_unread(ConnectionScope(user_id="nobody", connection_id="nobody"), KINDS) == 0
