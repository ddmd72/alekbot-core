"""The drive index repository for the scope walk (DRIVE_SCOPE_WALK_RFC §5.2, §5.9; plan §6 U-1 core), on both adapters
(the Firestore one over the evaluating fake): `has_pending_listing` (a walk is running) and `scope_ancestor_records`
(the skeletons re-derivation reads), each an equality query with a limit and no ordering, so no composite index; and
the new sync-state fields, which Firestore hands back as lists, read back as tuples."""
import pytest

from src.adapters.firestore_drive_index_repo import state_from_doc, state_to_doc
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.connection_scope import ConnectionScope
from src.domain.drive_index import DriveIndexRecord, DriveSyncState, Reason, RecordState

from tests.unit.adapters.firestore_drive_eval_fakes import RECS, STATES, EvalDb, eval_transactions, make_eval_repo

SCOPE = ConnectionScope(user_id="usr-1", connection_id="conn-1")
OTHER = ConnectionScope(user_id="usr-1", connection_id="conn-2")


@pytest.fixture(params=["memory", "firestore"])
def adapter(request, mock_env_config):
    if request.param == "memory":
        yield InMemoryDriveIndexRepository(), None
        return
    db = EvalDb()
    with eval_transactions():
        yield make_eval_repo(db, mock_env_config), db


def _rec(item_id, connection_id="conn-1", **kw):
    return DriveIndexRecord("usr-1", item_id, connection_id=connection_id, is_folder=True, **kw)


def _skeleton(item_id, connection_id="conn-1"):
    return _rec(item_id, connection_id, state=RecordState.METADATA_ONLY, reason=Reason.SCOPE_ANCESTOR)


def _no_ordering(db):
    """Every record query the adapter ran is equality-only and unordered (no composite index needed)."""
    if db is None:
        return True
    ops = [op for query in db.cols[RECS].executed for op in query]
    return not [op for op in ops if op[0] == "order_by" or (op[0] == "where" and op[2] != "==")]


class TestHasPendingListing:
    async def test_a_folder_with_a_listing_pending_is_seen(self, adapter):
        repo, db = adapter
        await repo.upsert_records([_rec("f-rd"), _rec("f-coimbra", reobserve_pending=True)])
        assert await repo.has_pending_listing(SCOPE)
        assert _no_ordering(db)

    async def test_none_pending_is_false(self, adapter):
        repo, _ = adapter
        await repo.upsert_records([_rec("f-rd"), _skeleton("f-coimbra")])
        assert not await repo.has_pending_listing(SCOPE)

    async def test_a_pending_folder_of_another_connection_is_not_seen(self, adapter):
        repo, _ = adapter
        await repo.upsert_records([_rec("f-rd"), _rec("f-springfield", "conn-2", reobserve_pending=True)])
        assert not await repo.has_pending_listing(SCOPE)
        assert await repo.has_pending_listing(OTHER)


class TestScopeAncestorRecords:
    async def test_skeletons_are_returned_and_normal_folders_are_not(self, adapter):
        repo, db = adapter
        await repo.upsert_records([_skeleton("f-coimbra"), _skeleton("f-lisbon"), _rec("f-springfield"),
                                   _rec("f-rd", reobserve_pending=True), _skeleton("f-porto", "conn-2")])
        found = await repo.scope_ancestor_records(SCOPE, 10)
        assert sorted(r.item_id for r in found) == ["f-coimbra", "f-lisbon"]
        assert all(r.reason is Reason.SCOPE_ANCESTOR and r.connection_id == "conn-1" for r in found)
        assert _no_ordering(db)

    async def test_the_limit_bounds_the_answer(self, adapter):
        repo, _ = adapter
        await repo.upsert_records([_skeleton(f"f-{n}") for n in range(5)])
        assert len(await repo.scope_ancestor_records(SCOPE, 2)) == 2

    async def test_no_skeleton_is_an_empty_list(self, adapter):
        repo, _ = adapter
        await repo.upsert_records([_rec("f-rd")])
        assert await repo.scope_ancestor_records(SCOPE, 10) == []


WALK_STATE = dict(applied_whole=False, applied_roots=("f-rd", "f-coimbra"), removing_roots=("f-springfield",),
                  ancestry_dirty=True, walking=True, walk_listed=40, walk_blocked=2)


class TestStateFields:
    def test_the_document_round_trip_gives_tuples(self):
        doc = state_to_doc(DriveSyncState(user_id="usr-1", connection_id="conn-1", **WALK_STATE))
        assert doc["applied_roots"] == ["f-rd", "f-coimbra"] and doc["removing_roots"] == ["f-springfield"]
        back = state_from_doc("usr-1", doc)
        assert {name: getattr(back, name) for name in WALK_STATE} == WALK_STATE
        assert isinstance(back.applied_roots, tuple) and isinstance(back.removing_roots, tuple)

    def test_a_document_without_them_reads_the_defaults(self):
        doc = state_to_doc(DriveSyncState(user_id="usr-1", connection_id="conn-1", **WALK_STATE))
        for name in WALK_STATE:
            del doc[name]
        back = state_from_doc("usr-1", doc)
        assert (back.applied_whole, back.applied_roots, back.removing_roots, back.ancestry_dirty, back.walking,
                back.walk_listed, back.walk_blocked) == (False, (), (), False, False, 0, 0)

    async def test_both_repositories_keep_them(self, adapter):
        repo, db = adapter
        await repo.save_sync_state(DriveSyncState(user_id="usr-1", connection_id="conn-1", **WALK_STATE))
        if db is not None:
            stored = db.cols[STATES].docs
            assert [d["applied_roots"] for d in stored.values()] == [["f-rd", "f-coimbra"]]   # a list in Firestore
        back = await repo.get_sync_state(SCOPE)
        assert {name: getattr(back, name) for name in WALK_STATE} == WALK_STATE
