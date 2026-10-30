"""The repository side of the fan-out's cap alert (drive_cap_alert, USER_DRIVE_INDEXING_RFC §5.4): `count_capped` answers
alike on both adapters (Firestore over the evaluating fake), as two count aggregations with equality filters only, which
the single-field indexes serve (no composite index, none of the fields exempt); and the two cap flags are the fan-out's
alone: `update_sync_flags` writes them, `save_sync_state` never does, a stored document reads them back."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.adapters.firestore_drive_index_repo import state_from_doc, state_to_doc
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.connection_scope import ConnectionScope
from src.domain.drive_cap_alert import CapCounts
from src.domain.drive_index import DriveIndexRecord, DriveSyncState, Reason, RecordState, Scope

from tests.unit.adapters.firestore_drive_eval_fakes import RECS, STATES, EvalDb, eval_transactions, make_eval_repo

_ROOT = Path(__file__).resolve().parents[3]
T = datetime(2026, 10, 1, tzinfo=timezone.utc)
CAP_FLAGS = ("card_cap_alerted", "chunk_cap_alerted")
A = ConnectionScope(user_id="user-a", connection_id="user-a")
B = ConnectionScope(user_id="user-b", connection_id="user-b")
NOBODY = ConnectionScope(user_id="nobody", connection_id="nobody")


def rec(item, *, user="user-a", **kw):
    kw.setdefault("state", RecordState.PENDING)
    return DriveIndexRecord(user_id=user, connection_id=user, item_id=item, name=f"{item}.epub", kind="epub", parent_id="root",
                            scope=Scope.NORMAL, **kw)


RECORDS = [
    rec("card-1", card_capped=True),
    rec("card-2", card_capped=True),
    rec("chunk-1", state=RecordState.METADATA_ONLY, reason=Reason.USER_CHUNK_CAP),
    rec("chunk-2", state=RecordState.METADATA_ONLY, reason=Reason.USER_CHUNK_CAP),
    rec("chunk-3", state=RecordState.METADATA_ONLY, reason=Reason.USER_CHUNK_CAP),
    rec("both", state=RecordState.METADATA_ONLY, reason=Reason.USER_CHUNK_CAP, card_capped=True),
    rec("too-large", state=RecordState.METADATA_ONLY, reason=Reason.TOO_LARGE),
    rec("indexed", state=RecordState.INDEXED, indexed_generation=1),
    rec("other-user", user="user-b", card_capped=True),
]


@pytest.fixture
def db():
    return EvalDb()


@pytest.fixture
def repos(db, mock_env_config):
    with eval_transactions():
        yield InMemoryDriveIndexRepository(), make_eval_repo(db, mock_env_config)


async def test_both_adapters_count_each_cap_for_the_user_only(repos):
    for repo in repos:
        await repo.upsert_records(list(RECORDS))
    mem, fs = repos
    assert await mem.count_capped(A) == await fs.count_capped(A) == CapCounts(card=3, chunk=4)
    assert await fs.count_capped(B) == CapCounts(card=1, chunk=0)
    assert await fs.count_capped(NOBODY) == CapCounts()


async def test_firestore_counts_with_aggregations_never_reading_a_record(repos, db):
    _, fs = repos
    await fs.upsert_records(list(RECORDS))
    db.cols[RECS].executed.clear()
    await fs.count_capped(A)
    card, chunk = sorted(db.cols[RECS].executed, key=len)
    for ops in (card, chunk):
        assert ops[-1][0] == "count"                                       # an aggregation, no document returned
        filters = ops[:-1]
        assert all(op[0] == "where" and op[2] == "==" for op in filters)   # equality filters only, no order, no limit
    assert {op[1:] for op in card[:-1]} == {("user_id", "==", "user-a"), ("card_capped", "==", True)}
    assert {op[1:] for op in chunk[:-1]} == {("user_id", "==", "user-a"), ("state", "==", "metadata_only"),
                                             ("reason", "==", "user_chunk_cap")}


def test_the_count_fields_keep_their_single_field_indexes():
    """Equality-only queries are served by merging single-field indexes (the `count_by_state` precedent), so neither
    query needs a composite; an exemption of one of their fields would break that."""
    raw = json.loads((_ROOT / "config" / "firestore.indexes.json").read_text())
    exempt = {idx["fieldPath"] for idx in raw
              if idx["collectionGroup"] == "development_drive_index_records" and "fields" not in idx}
    assert not exempt & {"user_id", "card_capped", "state", "reason"}


async def test_update_sync_flags_writes_the_cap_flags_and_a_save_leaves_them(repos, db):
    for repo in repos:
        await repo.save_sync_state(DriveSyncState(user_id="user-a", connection_id="user-a", created_at=T))
        await repo.update_sync_flags(A, card_cap_alerted=True)
        got = await repo.get_sync_state(A)
        assert got.card_cap_alerted and not got.chunk_cap_alerted
        await repo.update_sync_flags(A, card_cap_alerted=False, chunk_cap_alerted=True)
        got = await repo.get_sync_state(A)
        assert not got.card_cap_alerted and got.chunk_cap_alerted
        # the tick's save carries a state read before the flag went up: the flag stays as stored
        await repo.save_sync_state(DriveSyncState(user_id="user-a", connection_id="user-a", created_at=T, epoch=2))
        got = await repo.get_sync_state(A)
        assert got.epoch == 2 and got.chunk_cap_alerted and not got.card_cap_alerted
    assert db.cols[STATES].docs["user-a"]["chunk_cap_alerted"] is True


def test_the_cap_flags_are_not_in_the_ticks_document_and_read_back_from_a_stored_one():
    doc = state_to_doc(DriveSyncState(user_id="user-a", connection_id="user-a", card_cap_alerted=True, chunk_cap_alerted=True))
    assert not set(CAP_FLAGS) & set(doc)
    back = state_from_doc("user-a", {**doc, "card_cap_alerted": True, "chunk_cap_alerted": True})
    assert back.card_cap_alerted and back.chunk_cap_alerted
