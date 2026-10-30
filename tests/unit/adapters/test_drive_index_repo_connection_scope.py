"""The drive index repository by connection scope (MULTI_DRIVE_RFC §8, E8, I3, I4, I8; T-9), on both adapters: the
in-memory reference and the Firestore adapter over the evaluating fake. Two connections of one user, and a connection
id under another user, share nothing: the same item id is two records, a sync state is per connection, a purge helper
deletes one connection's documents, and a stored document of another user is absent. The user-scoped stores (usage, the
initial build by account, the transcript store) span the user's connections; their increments lose nothing under
`asyncio.gather` (T-9). Technical ids only."""
import asyncio
from datetime import datetime, timezone

import pytest

from src.adapters.firestore_drive_index_repo import record_from_doc
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.connection_scope import ConnectionScope
from src.domain.drive_enrichment import UsageClass
from src.domain.drive_index import DriveChunk, DriveIndexRecord, DriveSyncState, RecordState, chunk_doc_id, record_doc_id
from src.domain.drive_transcript import RETAINED_DISCONNECT, StoredTranscript, TranscriptBatch, TranscriptJob
from src.domain.drive_usage import initial_build_doc_id

from tests.unit.adapters.firestore_drive_eval_fakes import CHUNKS, RECS, STATES, USAGE, EvalDb, eval_transactions, \
    make_eval_repo

T = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
A = ConnectionScope(user_id="usr-1", connection_id="conn-1")
B = ConnectionScope(user_id="usr-1", connection_id="conn-2")
OTHER = ConnectionScope(user_id="usr-2", connection_id="conn-1")   # the same connection id under another user


def _rec(scope: ConnectionScope, item_id: str = "item-1", **kw) -> DriveIndexRecord:
    return DriveIndexRecord(user_id=scope.user_id, connection_id=scope.connection_id, item_id=item_id,
                            name="report.pdf", state=RecordState.PENDING, **kw)


@pytest.fixture
def db():
    return EvalDb()


@pytest.fixture
def repos(db, mock_env_config):
    mock_env_config.drive_transcript_jobs_collection = "eval_drive_transcript_jobs"
    mock_env_config.drive_transcript_batches_collection = "eval_drive_transcript_batches"
    mock_env_config.drive_transcripts_collection = "eval_drive_transcripts"
    mock_env_config.drive_transcript_windows_subcollection = "windows"
    mock_env_config.drive_transcript_parts_subcollection = "parts"
    with eval_transactions():
        yield InMemoryDriveIndexRepository(), make_eval_repo(db, mock_env_config)


async def test_the_same_item_id_on_two_connections_is_two_records(repos, db):
    for repo in repos:
        await repo.upsert_records([_rec(A, size_bytes=1), _rec(B, size_bytes=2)])
        assert (await repo.get_records(A, ["item-1"]))["item-1"].size_bytes == 1
        assert (await repo.get_records(B, ["item-1"]))["item-1"].size_bytes == 2
        assert [r.connection_id for r in await repo.records_page(A, None, 10)] == ["conn-1"]
    stored = {doc_id: record_from_doc(data) for doc_id, data in db.collection(RECS).docs.items()}
    assert set(stored) == {record_doc_id(A, "item-1"), record_doc_id(B, "item-1")} == {"conn-1:item-1", "conn-2:item-1"}
    assert {r.user_id for r in stored.values()} == {"usr-1"}


async def test_a_write_without_a_connection_is_refused_before_any_write(repos, db):
    for repo in repos:
        with pytest.raises(ValueError):
            await repo.upsert_records([_rec(A), DriveIndexRecord(user_id="usr-1", item_id="item-2")])
        with pytest.raises(ValueError):
            await repo.write_chunks([DriveChunk(user_id="usr-1", item_id="item-1", generation=1, index=0, text="t", char_start=0,
                                               char_end=1, kind="pdf")], [[0.1]])
        with pytest.raises(ValueError):
            await repo.save_sync_state(DriveSyncState(user_id="usr-1"))
        assert await repo.get_records(A, ["item-1"]) == {}
    assert db.collection(RECS).docs == {} and db.collection(STATES).docs == {}


async def test_a_document_of_another_user_under_the_connection_id_is_absent(repos):
    for repo in repos:
        await repo.upsert_records([_rec(OTHER)])
        await repo.save_sync_state(DriveSyncState(user_id="usr-2", connection_id="conn-1", epoch=3))
        # I3: read under (usr-1, conn-1), the stored usr-2 documents are absent, never another user's data
        assert await repo.get_records(A, ["item-1"]) == {}
        assert await repo.records_page(A, None, 10) == []
        assert await repo.get_sync_state(A) is None
        assert await repo.acquire_lease(A, "tick-1", T, T, create=True) is None   # never taken, never overwritten
        assert (await repo.get_sync_state(OTHER)).epoch == 3


async def test_sync_state_and_lease_are_per_connection(repos, db):
    for repo in repos:
        assert (await repo.acquire_lease(A, "tick-a", T, T.replace(hour=13), create=True)).connection_id == "conn-1"
        taken = await repo.acquire_lease(B, "tick-b", T, T.replace(hour=13), create=True)
        assert taken is not None and taken.user_id == "usr-1" and taken.connection_id == "conn-2"
        assert sorted((s.user_id, s.connection_id) for s in await repo.list_sync_scopes()) == [
            ("usr-1", "conn-1"), ("usr-1", "conn-2")]
        await repo.delete_sync_state(A, "tick-a")
        assert await repo.get_sync_state(A) is None and (await repo.get_sync_state(B)).lease_owner == "tick-b"
    assert set(db.collection(STATES).docs) == {"conn-2"}


async def test_the_purge_helpers_delete_one_connections_documents(repos, db):
    for repo in repos:
        await repo.upsert_records([_rec(A), _rec(B)])
        await repo.write_chunks([DriveChunk(user_id="usr-1", connection_id=s.connection_id, item_id="item-1",
                                            generation=1, index=0, text="t", char_start=0, char_end=1, kind="pdf") for s in (A, B)], [[0.1], [0.2]])
        assert await repo.delete_user_chunks(A, 100) == 1
        assert await repo.delete_user_records(A, 100) == 1
        assert await repo.get_records(A, ["item-1"]) == {}
        assert list(await repo.get_records(B, ["item-1"])) == ["item-1"]
        assert [(c.item_id, c.generation) for c in await repo.chunks_page(B, None, 10)] == [("item-1", 1)]
        assert await repo.chunks_page(A, None, 10) == []
    assert set(db.collection(CHUNKS).docs) == {chunk_doc_id(B, "item-1", 1, 0)}


async def test_records_by_identity_reads_one_connection(repos):
    for repo in repos:
        await repo.upsert_records([_rec(A, identity="fake_a:drive-1:item-1"),
                                   _rec(B, identity="fake_a:drive-1:item-1"),
                                   _rec(A, item_id="item-2", identity="fake_a:drive-1:item-2")])
        found = await repo.records_by_identity(A, ["fake_a:drive-1:item-1", "fake_a:drive-1:missing"])
        assert {i: (r.connection_id, r.item_id) for i, r in found.items()} == {
            "fake_a:drive-1:item-1": ("conn-1", "item-1")}


async def test_transcript_jobs_and_batches_are_per_connection(repos):
    for repo in repos:
        await repo.save_transcript_job(TranscriptJob(user_id="usr-1", connection_id="conn-1", item_id="item-1",
                                                     content_key="k1"))
        await repo.save_transcript_job(TranscriptJob(user_id="usr-1", connection_id="conn-2", item_id="item-1",
                                                     content_key="k1"))
        assert [j.connection_id for j in await repo.jobs_by_content_key(A, "k1")] == ["conn-1"]
        assert await repo.create_batch(TranscriptBatch(name="batches/b1", user_id="usr-1", connection_id="conn-1",
                                                       next_poll_at=T))
        assert await repo.create_batch(TranscriptBatch(name="batches/b1", user_id="usr-1", connection_id="conn-2",
                                                       next_poll_at=T))
        assert [b.connection_id for b in await repo.get_open_batches(B, T, 10)] == ["conn-2"]
        assert await repo.delete_user_transcript_jobs(A, 10) == 1
        assert await repo.get_transcript_job(A, "item-1") is None
        assert (await repo.get_transcript_job(B, "item-1")).content_key == "k1"
        with pytest.raises(ValueError):
            await repo.save_transcript_job(TranscriptJob(user_id="usr-1", item_id="item-3", content_key="k3"))


async def test_a_transcript_key_in_use_spans_the_users_connections(repos):
    for repo in repos:
        await repo.upsert_records([_rec(B, transcript_key="fam:d:1")])
        assert await repo.transcript_key_in_use("usr-1", "fam:d:1")
        assert not await repo.transcript_key_in_use("usr-2", "fam:d:1")


async def test_a_retained_head_keeps_its_first_mark_and_a_missing_one_is_not_created(repos):
    later = T.replace(hour=14)
    for repo in repos:
        await repo.put_transcript("usr-1", StoredTranscript(content_key="fam:d:1", transcript=None, last_used_at=T))
        assert await repo.retain_transcript("usr-1", "fam:d:1", T, RETAINED_DISCONNECT)
        assert await repo.retain_transcript("usr-1", "fam:d:1", later, RETAINED_DISCONNECT)
        stored = await repo.get_transcript("usr-1", "fam:d:1")
        assert (stored.retained_at, stored.retained_reason) == (T, RETAINED_DISCONNECT)
        assert await repo.transcript_retained("usr-1", "fam:d:1")
        assert not await repo.retain_transcript("usr-1", "fam:d:none", T, RETAINED_DISCONNECT)
        assert await repo.get_transcript("usr-1", "fam:d:none") is None
        assert not await repo.transcript_retained("usr-1", "fam:d:none")
        # a later put of the same transcript (a summary refresh) keeps the retention it read
        await repo.put_transcript("usr-1", stored)
        assert (await repo.get_transcript("usr-1", "fam:d:1")).retained_at == T


async def test_the_initial_build_is_per_provider_account(repos, db):
    for repo in repos:
        build, covered = await repo.spend_initial_build("usr-1", "realm-x", "sub-1", 5.0, T)
        assert covered == 5.0 and build.spent_usd == 5.0
        assert (await repo.get_initial_build("usr-1", "realm-x", "sub-1")).spent_usd == 5.0
        assert (await repo.get_initial_build("usr-1", "realm-x", "sub-2")).spent_usd == 0.0   # another account
    assert initial_build_doc_id("usr-1", "realm-x", "sub-1") in db.collection(USAGE).docs


async def test_two_connections_concurrent_spends_and_reservations_lose_nothing(repos, db):
    """T-9 (I4): the month document is the user's; two connections' ticks write it at once."""
    for repo in repos:
        await asyncio.gather(*(repo.add_model_usage("usr-1", "2026-10", "2026-10-30", UsageClass.PROFILE.value,
                                                    0.25, 0.0, 1) for _ in range(20)),
                             *(repo.reserve_model_usd("usr-1", "2026-10", UsageClass.TRANSCRIBE.value, 0.5)
                               for _ in range(20)))
        usage = await repo.get_model_usage("usr-1", "2026-10")
        assert usage.profile_usd == pytest.approx(5.0) and usage.calls == 20
        assert usage.transcribe_reserved_usd == pytest.approx(10.0)
