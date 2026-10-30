"""Reference semantics of DriveIndexRepository, pinned on the in-memory implementation."""
from datetime import datetime, timedelta, timezone

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_index import DriveIndexRecord, DriveSyncState, Priority, Reason, RecordState
from tests.unit.services.drive_index_fakes import U_SCOPE

NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)


def rec(item_id, **kw):
    return DriveIndexRecord(user_id="u", connection_id="u", item_id=item_id, **kw)


async def test_lease_is_exclusive_until_expiry_and_create_only_on_request():
    repo = InMemoryDriveIndexRepository()
    assert await repo.acquire_lease(U_SCOPE, "a", NOW, NOW + timedelta(minutes=31), create=False) is None
    assert repo.states == {}
    assert (await repo.acquire_lease(U_SCOPE, "a", NOW, NOW + timedelta(minutes=31), create=True)).lease_owner == "a"
    assert await repo.acquire_lease(U_SCOPE, "b", NOW, NOW + timedelta(minutes=31), create=True) is None
    assert (await repo.acquire_lease(U_SCOPE, "b", NOW + timedelta(minutes=32), NOW + timedelta(hours=1),
                                     create=True)).lease_owner == "b"
    await repo.release_lease(U_SCOPE, "a")   # not the owner any more: no-op
    assert repo.states["u"].lease_owner == "b"


async def test_save_never_touches_fanout_flags():
    repo = InMemoryDriveIndexRepository()
    await repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u"))
    await repo.update_sync_flags(U_SCOPE, stale_alerted=True, reconnect_notified=True)
    await repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u", epoch=3))
    st = await repo.get_sync_state(U_SCOPE)
    assert st.epoch == 3 and st.stale_alerted and st.reconnect_notified
    await repo.update_sync_flags("x", stale_alerted=True)   # missing state stays missing
    assert "x" not in repo.states


async def test_due_order_priority_then_time_then_id():
    repo = InMemoryDriveIndexRepository()
    await repo.upsert_records([
        rec("c", due_at=NOW, priority=Priority.FIRST_SEEN), rec("b", due_at=NOW, priority=Priority.EDIT),
        rec("a", due_at=NOW - timedelta(seconds=1), priority=Priority.FIRST_SEEN),
        rec("z", due_at=NOW + timedelta(seconds=1), priority=Priority.EDIT), rec("n"),
    ])
    assert [r.item_id for r in await repo.due_records(U_SCOPE, NOW, 10)] == ["b", "a", "c"]
    assert await repo.oldest_due_at(U_SCOPE, Priority.FIRST_SEEN) == NOW - timedelta(seconds=1)


async def test_vectors_follow_has_card():
    repo = InMemoryDriveIndexRepository()
    await repo.upsert_records([rec("a", has_card=True)], {"a": [1.0, 0.0]})
    await repo.upsert_records([rec("a", has_card=True, name="renamed")])      # no vector given: kept
    assert repo.vectors[("u", "a")] == [1.0, 0.0]
    await repo.upsert_records([rec("a", has_card=False)])                     # card dropped
    assert ("u", "a") not in repo.vectors and await repo.count_carded(U_SCOPE) == 0


async def test_eviction_candidate_is_highest_rank_oldest():
    repo = InMemoryDriveIndexRepository()
    await repo.upsert_records([rec("doc", has_card=True, card_rank=0, modified_at=NOW),
                               rec("old", has_card=True, card_rank=2, modified_at=NOW - timedelta(days=1)),
                               rec("new", has_card=True, card_rank=2, modified_at=NOW)],
                              {"doc": [1.0], "old": [1.0], "new": [1.0]})
    assert (await repo.lowest_ranked_carded("u")).item_id == "old"


async def test_capped_newest_first_and_counts():
    repo = InMemoryDriveIndexRepository()
    capped = dict(state=RecordState.METADATA_ONLY, reason=Reason.KIND_NOT_ADMITTED, card_capped=True)
    await repo.upsert_records([rec("a", modified_at=NOW - timedelta(days=1), **capped),
                               rec("b", modified_at=NOW, **capped), rec("c", state=RecordState.METADATA_ONLY)])
    assert [r.item_id for r in await repo.card_capped("u", 10)] == ["b", "a"]
    assert await repo.count_by_state(U_SCOPE) == {"metadata_only": 3, "carded": 0}


async def test_children_pages_seen_before_and_purge():
    repo = InMemoryDriveIndexRepository()
    await repo.upsert_records([rec(i, parent_id="p", seen_epoch=1 if i < "c" else 2) for i in "abcd"])
    assert [r.item_id for r in await repo.children(U_SCOPE, "p", "a", 2)] == ["b", "c"]
    assert [r.item_id for r in await repo.records_seen_before(U_SCOPE, 2, 10)] == ["a", "b"]
    assert await repo.delete_user_records("u", 3) == 3 and await repo.delete_user_records("u", 3) == 1
    assert await repo.delete_user_records("u", 3) == 0


async def test_search_reads():
    repo = InMemoryDriveIndexRepository()
    await repo.upsert_records([
        rec("a", kind="pdf", has_card=True, name_tokens=("w:scan",), effective_date=NOW),
        rec("b", kind="image", has_card=True, name_tokens=("w:img",), effective_date=NOW - timedelta(days=9)),
    ], {"a": [1.0, 0.0], "b": [0.0, 1.0]})
    nearest = await repo.find_nearest_cards("u", [1.0, 0.1], 5)
    assert [r.item_id for r, _ in nearest] == ["a", "b"] and nearest[0][1] < nearest[1][1]
    assert [r.item_id for r, _ in await repo.find_nearest_cards("u", [1.0, 0.0], 5, kind="image")] == ["b"]
    assert [r.item_id for r in await repo.find_by_name_keys("u", ["w:img", "w:x"], 5)] == ["b"]
    assert [r.item_id for r in await repo.filter_records("u", None, NOW - timedelta(days=1), None, 5)] == ["a"]
    assert set(await repo.card_vectors(U_SCOPE, ["a", "zz"])) == {"a"}


async def test_due_without_priority_is_refused():
    import pytest
    with pytest.raises(ValueError):
        await InMemoryDriveIndexRepository().upsert_records([rec("a", due_at=NOW)])
