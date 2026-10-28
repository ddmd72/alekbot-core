"""DriveNoteRepository: one case list, run against the in-memory reference and the Firestore adapter on the evaluating
fake (DRIVE_FILE_NOTES_RFC §3.1, §3.2, §4.4, §4.8, §5.1, §6.4, §7)."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from google.api_core.exceptions import Aborted, FailedPrecondition, ServiceUnavailable

from src.adapters.firestore_drive_note_repo import FirestoreDriveNoteRepository
from src.adapters.in_memory_drive_note_repo import InMemoryDriveNoteRepository
from src.domain.drive_index import DriveIndexStoreUnavailable
from src.domain.drive_note import (
    DriveNote, DriveNoteConflict, EraseResult, NoteAction, NoteBinding, NoteContent, NoteProvenance, NoteSnapshot, NoteState,
    binding_key, next_version, note_after_write, note_doc_id, ref_key,
)

from tests.unit.adapters.firestore_drive_eval_fakes import eval_transactions
from tests.unit.adapters.firestore_drive_note_fakes import NOTES, NoteDb

T = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)
U, OTHER, D1, D2 = "u", "other", "drive1", "drive2"
CHAT = NoteProvenance(origin="chat", session_id="s1", turn_id="t1")


class _Ticks:
    def __init__(self) -> None:
        self.now = T

    def __call__(self) -> datetime:
        self.now = self.now + timedelta(milliseconds=1)
        return self.now


@pytest.fixture(params=["memory", "firestore"])
def adapter(request):
    if request.param == "memory":
        yield InMemoryDriveNoteRepository(clock=_Ticks()), None
        return
    env = MagicMock()
    env.drive_notes_collection = NOTES
    db = NoteDb()
    with eval_transactions():
        yield FirestoreDriveNoteRepository(db, env), db


@pytest.fixture
def repo(adapter):
    return adapter[0]


def binding(item="a", drive=D1) -> NoteBinding:
    return NoteBinding(provider_key="onedrive", drive_id=drive, item_id=item, bound_at=T, bound_by="chat")


def written(note_id="n1", item="a", drive=D1, user=U, text="the contractor's invoice", doc_type="", areas=(), version=1,
            base=None, vector=None, state=NoteState.ACTIVE, action=NoteAction.SET, at=T):
    content = NoteContent(text, doc_type, tuple(areas))
    v = next_version(version - 1, content, action, CHAT, at)
    base = base or DriveNote(note_id=note_id, user_id=user, account_id="acc")
    note = note_after_write(base, v, state, CHAT, at, vector=vector, embedding_space="space-a" if vector else "",
                            embedding_pending=False, binding=binding(item, drive),
                            snapshot=NoteSnapshot(name=f"{item}.pdf", path=f"Docs/{item}.pdf", snapshot_at=at))
    return note, v


async def first(repo, **kw):
    """Write v1 and return the note as STORED (its `revision` is what the next write must carry)."""
    note, v = written(**kw)
    await repo.write_version(note, v, expected_version=0)
    return await repo.get(note.user_id, note.note_id)


async def seed_updated(adapter, note: DriveNote, updated_at: datetime):
    """Store `note` as is with a chosen `updated_at` (the cursor cases need equal timestamps)."""
    repo, db = adapter
    if db is None:
        repo.notes[(note.user_id, note.note_id)] = note.model_copy(update={"updated_at": updated_at})
        return
    from src.adapters.firestore_drive_note_repo import note_to_doc
    db.collection(NOTES).docs[note_doc_id(note.user_id, note.note_id)] = {**note_to_doc(note), "updated_at": updated_at}


# -- write_version: the SCD2 transaction -------------------------------------------------------------------------------

async def test_a_first_write_stores_the_parent_and_v1(repo):
    note, v = written()
    await repo.write_version(note, v, expected_version=0)
    stored = await repo.get(U, "n1")
    assert stored.model_copy(update={"updated_at": None, "revision": 0}) == note
    assert stored.updated_at is not None and stored.revision == 1
    [v1] = await repo.list_versions(U, "n1")
    assert (v1.version, v1.is_current, v1.valid_to, v1.text, v1.action) == (1, True, None, "the contractor's invoice", "set")
    assert (v1.origin, v1.session_id, v1.turn_id) == ("chat", "s1", "t1")


async def test_a_first_write_needs_the_document_not_to_exist(repo):
    await first(repo)
    note, v = written(text="other words")
    with pytest.raises(DriveNoteConflict):
        await repo.write_version(note, v, expected_version=0)
    assert (await repo.get(U, "n1")).text == "the contractor's invoice"
    assert len(await repo.list_versions(U, "n1")) == 1


async def test_a_second_write_closes_the_first_in_the_same_transaction(repo):
    base = await first(repo)
    at = T + timedelta(hours=1)
    note, v2 = written(text="bathroom invoice", version=2, base=base, at=at)
    await repo.write_version(note, v2, expected_version=1)
    v1, v2_stored = await repo.list_versions(U, "n1")
    assert (v1.is_current, v1.valid_to) == (False, at)
    assert v1.text == "the contractor's invoice" and v1.valid_from == T          # a closed version's content never changes
    assert (v2_stored.is_current, v2_stored.valid_to, v2_stored.text) == (True, None, "bathroom invoice")
    assert (await repo.get(U, "n1")).current_version == 2


async def test_a_stale_expected_version_is_a_conflict_and_writes_nothing(repo):
    base = await first(repo)
    note, v2 = written(text="bathroom", version=2, base=base)
    await repo.write_version(note, v2, expected_version=1)
    note3, v3 = written(text="race", version=2, base=base)
    with pytest.raises(DriveNoteConflict):
        await repo.write_version(note3, v3, expected_version=1)
    assert (await repo.get(U, "n1")).text == "bathroom"
    assert [v.version for v in await repo.list_versions(U, "n1")] == [1, 2]


async def test_a_version_that_does_not_follow_is_refused(repo):
    note, v = written(version=2)
    with pytest.raises(ValueError):
        await repo.write_version(note, v, expected_version=0)


async def test_updated_at_moves_on_every_write(repo):
    base = await first(repo)
    before = (await repo.get(U, "n1")).updated_at
    note, v2 = written(text="x", version=2, base=base)
    await repo.write_version(note, v2, expected_version=1)
    assert (await repo.get(U, "n1")).updated_at > before


# -- reads by id and by key --------------------------------------------------------------------------------------------

async def test_reads_are_per_user(repo):
    await first(repo)
    await first(repo, user=OTHER)
    assert (await repo.get(OTHER, "n1")).user_id == OTHER
    assert await repo.get("nobody", "n1") is None
    assert set(await repo.get_many(U, ["n1", "missing"])) == {"n1"}
    assert await repo.count(U) == 1 and await repo.count("nobody") == 0


async def test_find_bound_and_the_ref_key(repo):
    await first(repo, note_id="n1", item="a")
    await first(repo, note_id="n2", item="b", drive=D2)
    assert (await repo.find_bound(U, binding_key("onedrive", D1, "a"))).note_id == "n1"
    assert await repo.find_bound(U, binding_key("onedrive", D2, "a")) is None
    assert await repo.find_bound(OTHER, binding_key("onedrive", D1, "a")) is None
    assert [n.note_id for n in await repo.find_by_match_keys(U, [ref_key("b")])] == ["n2"]


async def test_find_by_match_keys_batches_any_number_of_keys(repo):
    for i in range(35):
        await first(repo, note_id=f"n{i:02d}", item=f"i{i:02d}")
    keys = [binding_key("onedrive", D1, f"i{i:02d}") for i in range(35)] + [ref_key("i00"), "", "i:none"]
    found = await repo.find_by_match_keys(U, keys)
    assert [n.note_id for n in found] == [f"n{i:02d}" for i in range(35)]   # deduplicated, by note id
    assert await repo.find_by_match_keys(U, []) == []


async def test_note_ids_leave_stubs_out(repo):
    await first(repo, note_id="n1", item="a")
    await first(repo, note_id="n2", item="b")
    await repo.erase(U, ["n2"])
    assert await repo.note_ids(U) == ["n1"]
    assert await repo.count(U) == 2   # the stub still counts until the indexer deletes it


# -- the tick's reads ---------------------------------------------------------------------------------------------------

async def test_changed_since_orders_by_the_server_clock_and_leaves_vectors_out(repo):
    await first(repo, note_id="n2", item="b", vector=[1.0, 0.0])
    await first(repo, note_id="n1", item="a", vector=[0.0, 1.0])
    page = await repo.changed_since(U, None, 10)
    assert [n.note_id for n in page] == ["n2", "n1"]
    assert all(n.note_vector is None for n in page)
    later = await repo.changed_since(U, page[0].updated_at, 10)
    assert [n.note_id for n in later] == ["n1"]
    assert await repo.changed_since(U, page[1].updated_at, 10) == []
    assert [n.note_id for n in await repo.changed_since(U, None, 1)] == ["n2"]


async def test_the_cursor_advances_through_notes_that_share_one_timestamp(adapter):
    repo, _ = adapter
    for note_id in ("n3", "n1", "n2"):
        note, _ = written(note_id=note_id, item=note_id)
        await seed_updated(adapter, note.model_copy(update={"current_version": 1}), T)
    first_page = await repo.changed_since(U, None, 2)
    assert [n.note_id for n in first_page] == ["n1", "n2"]
    rest = await repo.changed_since(U, first_page[-1].updated_at, 2, after_note_id=first_page[-1].note_id)
    assert [n.note_id for n in rest] == ["n3"]
    assert await repo.changed_since(U, T, 5) == []   # strictly after the time alone


async def test_embedding_pending_archived_and_active(repo):
    pending, v = written(note_id="n1", item="a")
    await repo.write_version(pending.model_copy(update={"embedding_pending": True}), v, expected_version=0)
    await first(repo, note_id="n2", item="b", vector=[1.0, 0.0])
    await first(repo, note_id="n3", item="c")
    assert [n.note_id for n in await repo.embedding_pending(U, 5)] == ["n1"]
    await repo.archive(U, "n2", archived_at=T + timedelta(days=1), last_seen_at=T)
    await repo.archive(U, "n3", archived_at=T + timedelta(days=2), last_seen_at=T)
    archived = await repo.archived(U, 5)
    assert [n.note_id for n in archived] == ["n3", "n2"] and all(n.note_vector is None for n in archived)
    assert [n.note_id for n in await repo.active(U, 5)] == ["n1"]
    await first(repo, note_id="n4", item="d")
    assert [n.note_id for n in await repo.active(U, 5)] == ["n4", "n1"]   # latest change first
    assert [n.note_id for n in await repo.archived(U, 1)] == ["n3"]


async def test_find_nearest_prefilters_state_and_space(repo):
    await first(repo, note_id="n1", item="a", vector=[1.0, 0.0])
    await first(repo, note_id="n2", item="b", vector=[0.7, 0.7])
    await first(repo, note_id="n3", item="c", vector=[1.0, 0.1])
    await repo.archive(U, "n1", archived_at=T, last_seen_at=T)
    await repo.archive(U, "n2", archived_at=T, last_seen_at=T)
    hits = await repo.find_nearest(U, [1.0, 0.0], state="archived", embedding_space="space-a", limit=5)
    assert [n.note_id for n, _ in hits] == ["n1", "n2"] and hits[0][1] < hits[1][1]
    assert all(n.note_vector is None for n, _ in hits)
    assert await repo.find_nearest(U, [1.0, 0.0], state="archived", embedding_space="space-b", limit=5) == []
    active = await repo.find_nearest(U, [1.0, 0.0], state="active", embedding_space="space-a", limit=5)
    assert [n.note_id for n, _ in active] == ["n3"]


# -- lifecycle: the parent in place ------------------------------------------------------------------------------------

async def test_archive_takes_only_an_active_note_and_bumps_updated_at(repo):
    await first(repo)
    before = (await repo.get(U, "n1")).updated_at
    snap = NoteSnapshot(name="a.pdf", path="Old/a.pdf", snapshot_at=T)
    assert await repo.archive(U, "n1", archived_at=T, last_seen_at=T - timedelta(hours=1), snapshot=snap)
    note = await repo.get(U, "n1")
    assert (note.state, note.archived_at, note.last_seen_at, note.snapshot.path) == (
        "archived", T, T - timedelta(hours=1), "Old/a.pdf")
    assert note.updated_at > before and note.current_version == 1          # no version for a lifecycle change
    assert len(await repo.list_versions(U, "n1")) == 1
    assert not await repo.archive(U, "n1", archived_at=T, last_seen_at=T)   # already archived
    assert not await repo.archive(U, "missing", archived_at=T, last_seen_at=T)


async def test_reactivate_takes_only_an_archived_note(repo):
    await first(repo)
    assert not await repo.reactivate(U, "n1", bound_at=T)
    await repo.archive(U, "n1", archived_at=T, last_seen_at=T)
    assert await repo.reactivate(U, "n1", bound_at=T + timedelta(days=1))
    note = await repo.get(U, "n1")
    assert (note.state, note.archived_at, note.last_seen_at) == ("active", None, T)
    assert (note.binding.bound_by, note.binding.bound_at, note.binding.item_id) == ("restore", T + timedelta(days=1), "a")


async def test_refresh_snapshot_and_set_vector(repo):
    await first(repo)
    assert await repo.refresh_snapshot(U, "n1", NoteSnapshot(name="b.pdf", path="New/b.pdf"))
    assert (await repo.get(U, "n1")).snapshot.path == "New/b.pdf"
    assert await repo.set_vector(U, "n1", [0.5, 0.5], "space-b", expected_version=1)
    note = await repo.get(U, "n1")
    assert (note.note_vector, note.embedding_space, note.embedding_pending) == ([0.5, 0.5], "space-b", False)


async def test_set_vector_never_gives_a_cleared_note_a_vector(repo):
    base = await first(repo)
    note, v2 = written(text="", version=2, base=base, state=NoteState.CLEARED, action=NoteAction.CLEAR)
    await repo.write_version(note, v2, expected_version=1)
    assert not await repo.set_vector(U, "n1", [1.0], "space-a", expected_version=2)
    assert (await repo.get(U, "n1")).note_vector is None


# -- erase ------------------------------------------------------------------------------------------------------------------

async def test_erase_deletes_every_version_and_leaves_a_contentless_stub(repo):
    base = await first(repo, vector=[1.0, 0.0])
    note, v2 = written(text="second", version=2, base=base, vector=[1.0, 0.0])
    await repo.write_version(note, v2, expected_version=1)
    before = (await repo.get(U, "n1")).updated_at
    assert await repo.erase(U, ["n1", "missing"]) == EraseResult(erased=1)
    stub = await repo.get(U, "n1")
    assert stub.erase_pending and stub.state == "cleared" and stub.current_version == 0
    assert (stub.text, stub.doc_type, stub.life_areas, stub.keys, stub.note_vector) == ("", "", [], [], None)
    assert stub.binding is None and stub.snapshot is None and stub.last_writer is None
    assert stub.match_keys == [binding_key("onedrive", D1, "a"), ref_key("a")] and stub.account_id == "acc"
    assert stub.updated_at > before
    assert await repo.list_versions(U, "n1") == []
    assert (await repo.erase(U, ["n1"])).erased == 1                     # idempotent
    assert (await repo.erase(OTHER, ["n1"])).erased == 0


async def test_a_new_note_on_a_stub_starts_at_v1_and_survives_delete_stub(repo):
    await first(repo)
    await repo.erase(U, ["n1"])
    stub = await repo.get(U, "n1")
    blind, v1 = written(text="fresh")
    with pytest.raises(DriveNoteConflict):        # a writer that did not read the stub's revision
        await repo.write_version(blind, v1, expected_version=0)
    note, v1 = written(text="fresh", base=stub)
    await repo.write_version(note, v1, expected_version=0)
    assert not await repo.delete_stub(U, "n1")
    assert (await repo.get(U, "n1")).text == "fresh"
    assert [v.version for v in await repo.list_versions(U, "n1")] == [1]


async def test_delete_stub_deletes_only_stubs(repo):
    await first(repo, note_id="n1", item="a")
    await first(repo, note_id="n2", item="b")
    await repo.erase(U, ["n2"])
    assert not await repo.delete_stub(U, "n1")
    assert await repo.delete_stub(U, "n2")
    assert await repo.get(U, "n2") is None and await repo.get(U, "n1") is not None
    assert not await repo.delete_stub(U, "n2")


# -- Firestore only --------------------------------------------------------------------------------------------------------

def _firestore(adapter):
    repo, db = adapter
    if db is None:
        pytest.skip("Firestore wire only")
    return repo, db


async def test_firestore_writes_updated_at_as_a_server_timestamp(adapter):
    from google.cloud import firestore
    repo, db = _firestore(adapter)
    await first(repo, vector=[1.0, 0.0])
    parent = [data for kind, doc_id, data in db.transactions[-1].raw if doc_id == "u:n1"]
    assert parent and parent[0]["updated_at"] is firestore.SERVER_TIMESTAMP


async def test_firestore_stores_the_vector_as_a_vector_and_drops_it_when_cleared(adapter):
    from google.cloud.firestore_v1.vector import Vector
    repo, db = _firestore(adapter)
    base = await first(repo, vector=[1.0, 0.0])
    assert isinstance(db.collection(NOTES).docs["u:n1"]["note_vector"], Vector)
    note, v2 = written(text="", version=2, base=base, state=NoteState.CLEARED, action=NoteAction.CLEAR)
    await repo.write_version(note, v2, expected_version=1)
    assert "note_vector" not in db.collection(NOTES).docs["u:n1"]


async def test_firestore_versions_live_under_the_parent(adapter):
    repo, db = _firestore(adapter)
    await first(repo)
    assert set(db.versions_of("u:n1")) == {"v1"}


async def test_firestore_stub_holds_only_its_fields(adapter):
    repo, db = _firestore(adapter)
    await first(repo, vector=[1.0, 0.0])
    await repo.erase(U, ["n1"])
    stored = db.collection(NOTES).docs["u:n1"]
    assert set(stored) == {"note_id", "user_id", "account_id", "state", "erase_pending", "match_keys", "revision",
                           "updated_at"}
    assert db.versions_of("u:n1") == {}


async def test_firestore_erase_of_a_long_history_takes_the_oldest_first(adapter):
    repo, db = _firestore(adapter)
    note = await first(repo)
    for n in range(2, 503):   # 502 versions: more than one transaction's writes
        nxt, v = written(text=f"t{n}", version=n, base=note)
        await repo.write_version(nxt, v, expected_version=n - 1)
        note = await repo.get(U, "n1")
    assert (await repo.erase(U, ["n1"])).erased == 1
    assert db.versions_of("u:n1") == {}
    assert len(db.commits) == 1 and len(db.commits[0]) == 3              # v1..v3 in a batch, the rest with the stub


async def test_firestore_errors_are_store_unavailable(adapter):
    repo, db = _firestore(adapter)
    db.collection(NOTES).fail = ServiceUnavailable("down")
    with pytest.raises(DriveIndexStoreUnavailable):
        await repo.find_bound(U, "i:x")
    db.collection(NOTES).fail = FailedPrecondition("the query requires an index")
    with pytest.raises(DriveIndexStoreUnavailable):
        await repo.changed_since(U, None, 5)


async def test_firestore_every_query_filters_the_user(adapter):
    repo, db = _firestore(adapter)
    await first(repo)
    await repo.find_bound(U, "i:x")
    await repo.find_by_match_keys(U, ["r:a"])
    await repo.note_ids(U)
    await repo.count(U)
    await repo.changed_since(U, None, 5)
    await repo.changed_since(U, T, 5, after_note_id="n0")
    await repo.embedding_pending(U, 5)
    await repo.archived(U, 5)
    await repo.active(U, 5)
    await repo.find_nearest(U, [1.0, 0.0], state="archived", embedding_space="s", limit=2)
    for ops in db.executed():
        assert ("where", "user_id", "==", U) in ops, ops


# -- fix round 1: the revision precondition, lifecycle stamps, set_vector's version -----------------------------------

async def test_a_lifecycle_write_after_the_read_fails_the_owner_write(repo):
    read = await first(repo)                                      # the owner's write is prepared from this read
    await repo.archive(U, "n1", archived_at=T, last_seen_at=T)    # the tick archives meanwhile
    note, v2 = written(text="late", version=2, base=read)
    with pytest.raises(DriveNoteConflict):
        await repo.write_version(note, v2, expected_version=1)
    stored = await repo.get(U, "n1")
    assert (stored.state, stored.text, stored.current_version) == ("archived", "the contractor's invoice", 1)
    fresh, v2 = written(text="late", version=2, base=stored, state=NoteState.ARCHIVED)
    await repo.write_version(fresh, v2, expected_version=1)        # re-read: it lands
    assert (await repo.get(U, "n1")).text == "late"


async def test_every_parent_write_bumps_the_revision(repo):
    note = await first(repo, vector=[1.0, 0.0])
    revisions = [note.revision]
    await repo.refresh_snapshot(U, "n1", NoteSnapshot(name="b.pdf"))
    revisions.append((await repo.get(U, "n1")).revision)
    await repo.set_vector(U, "n1", [0.0, 1.0], "space-a", expected_version=1)
    revisions.append((await repo.get(U, "n1")).revision)
    await repo.archive(U, "n1", archived_at=T, last_seen_at=T)
    revisions.append((await repo.get(U, "n1")).revision)
    await repo.reactivate(U, "n1", bound_at=T)
    revisions.append((await repo.get(U, "n1")).revision)
    await repo.erase(U, ["n1"])
    revisions.append((await repo.get(U, "n1")).revision)
    assert revisions == [1, 2, 3, 4, 5, 6]
    assert not await repo.reactivate(U, "n1", bound_at=T)        # a refused change bumps nothing
    assert (await repo.get(U, "n1")).revision == 6


async def test_lifecycle_writes_are_stamped_by_the_indexer(repo):
    await first(repo)

    async def owner_write():                                       # resets last_writer to the owner, state kept
        stored = await repo.get(U, "n1")
        note, v = written(text=f"owner {stored.current_version}", version=stored.current_version + 1, base=stored,
                          state=NoteState(stored.state))
        await repo.write_version(note, v, expected_version=stored.current_version)
        assert (await repo.get(U, "n1")).last_writer.origin == "chat"

    async def vector():
        return await repo.set_vector(U, "n1", [1.0], "space-a",
                                     expected_version=(await repo.get(U, "n1")).current_version)

    for change in (lambda: repo.refresh_snapshot(U, "n1", NoteSnapshot(name="b.pdf")),
                   lambda: repo.archive(U, "n1", archived_at=T, last_seen_at=T),
                   lambda: repo.reactivate(U, "n1", bound_at=T),
                   vector):
        await owner_write()
        assert await change()
        assert (await repo.get(U, "n1")).last_writer.origin == "indexer"


async def test_set_vector_stores_nothing_for_a_newer_version(repo):
    pending, v = written()
    await repo.write_version(pending.model_copy(update={"embedding_pending": True}), v, expected_version=0)
    stored = await repo.get(U, "n1")
    newer, v2 = written(text="B", version=2, base=stored.model_copy(update={"embedding_pending": True}))
    await repo.write_version(newer.model_copy(update={"embedding_pending": True}), v2, expected_version=1)
    assert not await repo.set_vector(U, "n1", [0.1, 0.2], "space-a", expected_version=1)   # A's vector, too late
    note = await repo.get(U, "n1")
    assert (note.note_vector, note.embedding_pending, note.text) == (None, True, "B")
    assert await repo.set_vector(U, "n1", [0.3, 0.4], "space-a", expected_version=2)
    assert (await repo.get(U, "n1")).note_vector == [0.3, 0.4]


async def test_archive_of_a_cleared_note_stamps_it_and_keeps_it_cleared(repo):
    base = await first(repo)
    note, v2 = written(text="", version=2, base=base, state=NoteState.CLEARED, action=NoteAction.CLEAR)
    await repo.write_version(note, v2, expected_version=1)
    seen = T - timedelta(hours=2)
    assert await repo.archive(U, "n1", archived_at=T, last_seen_at=seen)
    stored = await repo.get(U, "n1")
    assert (stored.state, stored.archived_at, stored.last_seen_at) == ("cleared", T, seen)
    assert not await repo.archive(U, "n1", archived_at=T + timedelta(days=1), last_seen_at=T)   # stamped once
    assert await repo.reactivate(U, "n1", bound_at=T)            # the file is back: the stamp goes, state stays
    stored = await repo.get(U, "n1")
    assert (stored.state, stored.archived_at, stored.last_seen_at) == ("cleared", None, seen)
    await repo.erase(U, ["n1"])
    assert not await repo.archive(U, "n1", archived_at=T, last_seen_at=T)   # never a stub


async def test_closing_a_missing_previous_version_is_tolerated(adapter):
    repo, db = adapter
    base = await first(repo)
    if db is None:
        repo.versions[(U, "n1")].pop(1)
    else:
        db.collection(f"{NOTES}/u:n1/versions").docs.pop("v1")
    note, v2 = written(text="two", version=2, base=base)
    await repo.write_version(note, v2, expected_version=1)
    assert [v.version for v in await repo.list_versions(U, "n1")] == [2]


async def test_firestore_a_contended_first_write_reruns_and_conflicts_writing_nothing(adapter):
    from unittest.mock import patch
    from tests.unit.adapters.firestore_drive_note_fakes import contended
    repo, db = _firestore(adapter)
    other = {"note_id": "n1", "user_id": U, "current_version": 1, "revision": 1, "text": "the other writer"}

    async def interloper():
        db.collection(NOTES).docs["u:n1"] = dict(other)

    note, v1 = written()
    with patch("src.adapters.firestore_drive_note_repo.firestore.async_transactional",
               side_effect=contended(db, interloper)):
        with pytest.raises(DriveNoteConflict):
            await repo.write_version(note, v1, expected_version=0)
    assert db.collection(NOTES).docs["u:n1"] == other and db.versions_of("u:n1") == {}


async def test_firestore_exhausted_transaction_retries_are_a_conflict(adapter):
    from unittest.mock import patch
    repo, db = _firestore(adapter)
    await first(repo)

    def giving_up(fn):
        async def run(transaction):
            # what the client raises once its retries are spent: a ValueError FROM the last Aborted
            raise ValueError("Failed to commit transaction in 5 attempts.") from Aborted("contention")
        return run

    with patch("src.adapters.firestore_drive_note_repo.firestore.async_transactional", side_effect=giving_up):
        note, v2 = written(text="x", version=2, base=await repo.get(U, "n1"))
        with pytest.raises(DriveNoteConflict):
            await repo.write_version(note, v2, expected_version=1)
        with pytest.raises(DriveNoteConflict):
            await repo.archive(U, "n1", archived_at=T, last_seen_at=T)


async def test_firestore_a_corrupt_document_is_not_reported_as_contention(adapter):
    from pydantic import ValidationError
    repo, db = _firestore(adapter)
    db.collection(NOTES).docs["u:n1"] = {"note_id": "n1", "user_id": U, "current_version": "not a number"}
    with pytest.raises(ValidationError):
        await repo.archive(U, "n1", archived_at=T, last_seen_at=T)


async def test_firestore_a_long_erase_redoes_its_batches_when_a_write_lands_in_between(adapter):
    from src.adapters.firestore_drive_note_repo import version_to_doc
    repo, db = _firestore(adapter)
    note = await first(repo)
    for n in range(2, 503):
        nxt, v = written(text=f"t{n}", version=n, base=note)
        await repo.write_version(nxt, v, expected_version=n - 1)
        note = await repo.get(U, "n1")
    real_batch = db.batch
    landed = []

    def batch():
        b = real_batch()
        commit = b.commit

        async def commit_then_write():
            await commit()
            if not landed:                       # a concurrent owner write lands after the first batch
                landed.append(True)
                parent = db.collection(NOTES).docs["u:n1"]
                parent["current_version"], parent["revision"] = 503, parent["revision"] + 1
                v503 = next_version(502, NoteContent("t503"), NoteAction.SET, CHAT, T)
                db.collection(f"{NOTES}/u:n1/versions").docs["v503"] = version_to_doc(v503)
        b.commit = commit_then_write
        return b

    db.batch = batch
    assert (await repo.erase(U, ["n1"])).erased == 1
    assert landed and db.versions_of("u:n1") == {}
    assert (await repo.get(U, "n1")).erase_pending


# -- fix round 2: a partial erase never stops part-way; only an exhausted-retries ValueError is a conflict ----------

async def test_a_conflict_partway_through_an_erase_all_erases_the_rest(adapter):
    from unittest.mock import patch
    repo, db = adapter
    for note_id, item in (("n1", "a"), ("n2", "b"), ("n3", "c")):
        await first(repo, note_id=note_id, item=item)
    if db is None:
        real = repo._erase_one

        async def flaky(user_id, note_id):
            if note_id == "n2":
                raise DriveNoteConflict("contention")
            return await real(user_id, note_id)

        repo._erase_one = flaky
        result = await repo.erase(U, ["n1", "n2", "n3", "missing"])
    else:
        calls = []

        def second_gives_up(fn):          # the stub transaction of n2 (the 2nd note) exhausts its retries
            async def run(transaction):
                calls.append(1)
                if len(calls) == 2:
                    raise ValueError("Failed to commit transaction in 5 attempts.") from Aborted("contention")
                return await fn(transaction)
            return run

        with patch("src.adapters.firestore_drive_note_repo.firestore.async_transactional",
                   side_effect=second_gives_up):
            result = await repo.erase(U, ["n1", "n2", "n3", "missing"])
    assert result == EraseResult(erased=2, left=("n2",))
    assert (await repo.get(U, "n1")).erase_pending and (await repo.get(U, "n3")).erase_pending
    untouched = await repo.get(U, "n2")
    assert not untouched.erase_pending and untouched.text == "the contractor's invoice"
    assert [v.version for v in await repo.list_versions(U, "n2")] == [1]


async def test_firestore_a_store_error_on_one_note_is_reported_not_raised(adapter):
    repo, db = _firestore(adapter)
    await first(repo, note_id="n1", item="a")
    await first(repo, note_id="n2", item="b")
    real = repo._erase_one

    async def failing(user_id, note_id):
        if note_id == "n1":
            raise ServiceUnavailable("down")
        return await real(user_id, note_id)

    repo._erase_one = failing
    assert await repo.erase(U, ["n1", "n2"]) == EraseResult(erased=1, left=("n1",))


async def test_firestore_any_other_value_error_keeps_its_type(adapter):
    from unittest.mock import patch
    repo, db = _firestore(adapter)
    await first(repo)

    def misuse(fn):
        async def run(transaction):
            raise ValueError("Transaction already in progress")          # no Aborted behind it
        return run

    with patch("src.adapters.firestore_drive_note_repo.firestore.async_transactional", side_effect=misuse):
        with pytest.raises(ValueError) as info:
            await repo.archive(U, "n1", archived_at=T, last_seen_at=T)
    assert not isinstance(info.value, DriveNoteConflict)
