"""`delete_user_transcripts_except` (USER_DRIVE_INDEXING_RFC §27.9.12, Δ186, Δ214): a scanning purge that pages the user's
heads by document id and deletes the unkept ones with their parts; a page made only of kept heads is not the end."""
from datetime import datetime, timezone

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_transcript import Segment, StitchedTranscript, StoredTranscript, transcript_doc_id

from tests.unit.adapters.firestore_drive_eval_fakes import eval_transactions, make_eval_repo
from tests.unit.adapters.firestore_drive_mirror_fakes import NameAwareDb
from tests.unit.adapters.firestore_drive_transcript_fakes import transcript_doc_fakes

T = datetime(2026, 10, 21, 9, 30, tzinfo=timezone.utc)
STORE, PARTS = "eval_drive_transcripts", "eval_drive_transcript_parts"


@pytest.fixture(params=["memory", "firestore"])
def adapter(request, mock_env_config):
    if request.param == "memory":
        yield InMemoryDriveIndexRepository(), None
        return
    db = NameAwareDb()
    mock_env_config.drive_transcripts_collection = STORE
    mock_env_config.drive_transcript_parts_subcollection = PARTS
    with eval_transactions(), transcript_doc_fakes():
        yield make_eval_repo(db, mock_env_config), db


async def _put(repo, user, key):
    stored = StoredTranscript(key, StitchedTranscript((Segment(0.0, None, "x"),), ("uk",), 1, 5.0), last_used_at=T)
    await repo.put_transcript(user, stored)


async def _left(repo, user, keys):
    return sorted([k for k in keys if await repo.get_transcript(user, k) is not None])


async def _purge(repo, user, limit, keep):
    deleted, cursor, calls = 0, None, 0
    while True:
        done, cursor = await repo.delete_user_transcripts_except(user, limit, keep, cursor)
        deleted, calls = deleted + done, calls + 1
        if cursor is None:
            return deleted, calls


async def test_except_deletes_the_unkept_and_returns_none_at_the_end(adapter):
    repo, _ = adapter
    for key in ("k1", "k2", "k3"):
        await _put(repo, "u", key)
    await _put(repo, "v", "k1")
    assert await repo.delete_user_transcripts_except("u", 10, frozenset({"k2"})) == (2, None)
    assert await _left(repo, "u", ("k1", "k2", "k3")) == ["k2"]
    assert await _left(repo, "v", ("k1",)) == ["k1"]
    assert await repo.delete_user_transcripts_except("u", 10, frozenset({"k2"})) == (0, None)


async def test_except_scans_past_a_page_of_pinned_heads(adapter):
    repo, _ = adapter
    pinned = [f"a{n}" for n in range(5)]                  # sort before the deletable ones, more than one page of them
    doomed = [f"b{n}" for n in range(4)]
    for key in pinned + doomed:
        await _put(repo, "u", key)
    first, cursor = await repo.delete_user_transcripts_except("u", 2, frozenset(pinned))
    assert (first, cursor is not None) == (0, True)       # a page of kept heads deletes nothing and is not the end
    deleted, calls = await _purge(repo, "u", 2, frozenset(pinned))
    assert deleted == 4 and calls >= 3
    assert await _left(repo, "u", pinned + doomed) == sorted(pinned)


async def test_except_cursor_resumes_after_the_given_id(adapter):
    repo, _ = adapter
    for key in ("k1", "k2", "k3", "k4"):
        await _put(repo, "u", key)
    deleted, cursor = await repo.delete_user_transcripts_except("u", 2, frozenset())
    assert (deleted, cursor) == (2, transcript_doc_id("u", "k2"))
    assert await _left(repo, "u", ("k1", "k2", "k3", "k4")) == ["k3", "k4"]
    assert await repo.delete_user_transcripts_except("u", 2, frozenset(), cursor) == (2, transcript_doc_id("u", "k4"))
    assert await repo.delete_user_transcripts_except("u", 2, frozenset(), transcript_doc_id("u", "k4")) == (0, None)


async def test_except_deletes_parts_of_deleted_heads_only(adapter):
    repo, db = adapter
    if db is None:
        pytest.skip("parts are separate documents on Firestore only")
    for key in ("kept", "gone"):
        await _put(repo, "u", key)
        db.collection(f"{STORE}/{transcript_doc_id('u', key)}/{PARTS}").docs["t-0"] = {"user_id": "u", "index": 0}
    await repo.delete_user_transcripts_except("u", 10, frozenset({"kept"}))
    assert len(db.collection(f"{STORE}/{transcript_doc_id('u', 'kept')}/{PARTS}").docs) == 1
    assert db.collection(f"{STORE}/{transcript_doc_id('u', 'gone')}/{PARTS}").docs == {}
    assert transcript_doc_id("u", "gone") not in db.collection(STORE).docs


async def test_except_sends_only_equality_and_the_document_id(adapter):
    repo, db = adapter
    if db is None:
        pytest.skip("Firestore wire only")
    await _put(repo, "u", "k1")
    db.collection(STORE).executed.clear()
    await repo.delete_user_transcripts_except("u", 5, frozenset(), transcript_doc_id("u", "a"))
    (ops,) = db.collection(STORE).executed
    assert [op[1:3] for op in ops if op[0] == "where"] == [("user_id", "=="), ("__name__", ">")]   # single-field indexes
