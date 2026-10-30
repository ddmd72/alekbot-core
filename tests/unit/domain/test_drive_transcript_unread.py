"""Recordings in "unread" and "not read yet" (USER_DRIVE_INDEXING_RFC §27.11 "Coverage"; plan rulings G2, G3, G20), in
the domain, the in-memory repository and the Firestore count queries. Synthetic records only."""
from types import SimpleNamespace

import pytest

from src.adapters.firestore_drive_index_repo import IN_VALUES_MAX, FirestoreDriveIndexRepository
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_access import access_counts
from src.domain.drive_index import DriveIndexRecord, Reason, RecordState
from src.domain.drive_index_policy import ADMITTED_KINDS, TRANSCRIBED_KINDS, TRIAGED_KINDS
from src.domain.drive_search import is_not_read_yet, is_unread

from tests.unit.adapters.firestore_drive_chunk_fakes import ChunkDb, make_chunk_repo
from tests.unit.adapters.firestore_drive_fakes import RECS, executed
from tests.unit.services.drive_index_fakes import U_SCOPE

MEDIA = frozenset(TRANSCRIBED_KINDS)
DOCS = frozenset({"pdf"})


def rec(item_id="m", **kw):
    return DriveIndexRecord(**{"user_id": "u", "item_id": item_id, "kind": "audio", "state": RecordState.PENDING, **kw})


def test_pending_media_not_read_yet_when_enabled():
    assert is_not_read_yet(rec(), DOCS, frozenset(), MEDIA)
    assert is_unread(rec(), DOCS, frozenset(), MEDIA)
    assert is_not_read_yet(rec(), None) and is_unread(rec(), None)        # the policy read now: media admitted
    assert not is_not_read_yet(rec(indexed_generation=1), DOCS, frozenset(), MEDIA)
    assert not is_not_read_yet(rec(card_capped=True), DOCS, frozenset(), MEDIA)


def test_pending_media_not_counted_when_disabled():
    assert not is_not_read_yet(rec(), DOCS, frozenset())
    assert not is_unread(rec(), DOCS, frozenset(), frozenset())
    assert not is_unread(rec(state=RecordState.FAILED), DOCS, frozenset())


@pytest.mark.parametrize("stub", ["no_speech", "no_audio"])
def test_no_speech_stub_not_unread(stub):
    stubbed = rec(state=RecordState.METADATA_ONLY, reason=Reason.NO_TEXT, transcript_stub=stub,
                  transcript_state="current")
    assert not is_unread(stubbed, DOCS, frozenset(), MEDIA)
    assert not is_unread(stubbed)


@pytest.mark.parametrize("state, reason", [(RecordState.FAILED, None),
                                           (RecordState.METADATA_ONLY, Reason.TOO_LARGE),
                                           (RecordState.METADATA_ONLY, Reason.MACHINE_TEXT),
                                           (RecordState.METADATA_ONLY, Reason.USER_CHUNK_CAP)])
def test_failed_media_unread(state, reason):
    failed = rec(state=state, reason=reason)
    assert is_unread(failed, DOCS, frozenset(), MEDIA) and is_unread(failed)
    assert not is_not_read_yet(failed, DOCS, frozenset(), MEDIA)          # could not be read, not waiting


def test_documents_are_unchanged_by_the_media_set():
    scan = rec("d", kind="pdf", state=RecordState.METADATA_ONLY, reason=Reason.NO_TEXT)
    assert is_unread(scan, DOCS, frozenset(), MEDIA) and is_unread(scan, DOCS, frozenset())


def test_access_counts_follow_the_same_sets():
    closed = rec(inaccessible_since=__import__("datetime").datetime(2026, 10, 1))
    assert access_counts([closed], DOCS, frozenset(), MEDIA).waiting == 1
    assert access_counts([closed], DOCS, frozenset()).waiting == 0


def test_count_unread_kinds_within_in_limit():
    assert len(TRANSCRIBED_KINDS) <= IN_VALUES_MAX
    assert len(ADMITTED_KINDS | TRANSCRIBED_KINDS) <= IN_VALUES_MAX


class TestRepositories:
    def _records(self):
        return [rec("p"), rec("p2", indexed_generation=1), rec("f", state=RecordState.FAILED),
                rec("big", state=RecordState.METADATA_ONLY, reason=Reason.TOO_LARGE),
                rec("silent", kind="video", state=RecordState.METADATA_ONLY, reason=Reason.NO_TEXT,
                    transcript_stub="no_speech"),
                rec("doc", kind="pdf", state=RecordState.FAILED)]

    async def test_in_memory_counts_media_only_when_given(self):
        mem = InMemoryDriveIndexRepository()
        await mem.upsert_records(self._records())
        assert await mem.count_unread("u", ADMITTED_KINDS, TRIAGED_KINDS) == 1
        assert await mem.count_unread("u", ADMITTED_KINDS, TRIAGED_KINDS, transcribed=MEDIA) == 4
        assert await mem.count_unread("u", ADMITTED_KINDS, TRIAGED_KINDS, pending=True, transcribed=MEDIA) == 1
        assert await mem.count_unread("u", ADMITTED_KINDS, TRIAGED_KINDS, pending=False, transcribed=MEDIA) == 3

    async def test_firestore_and_memory_unread_counts_agree_for_media(self, mock_env_config):
        """Each Firestore class is answered by the in-memory rule applied to the records its filters would match."""
        db = ChunkDb()
        repo = make_chunk_repo(db, mock_env_config)
        db.cols[RECS].respond = lambda ops: [[SimpleNamespace(alias="n", value=1)]]
        queries = repo.unread_count_queries("u", frozenset(), frozenset(), None, MEDIA)
        assert len(queries) == 5                                  # failed, pending, too_large, machine_text, chunk_cap
        mem = InMemoryDriveIndexRepository()
        await mem.upsert_records([r for r in self._records() if r.kind != "pdf"])
        # classes the Firestore side sums: failed 1 + pending 1 (p; p2 has a generation) + too_large 1 = 3, and the
        # silent stub (`no_text`) is in none of them
        assert await mem.count_unread("u", frozenset(), frozenset(), transcribed=MEDIA) == 3

    async def test_no_new_index_for_media_unread_classes(self, mock_env_config):
        from tests.unit.adapters.test_firestore_drive_index_chunk_provisioning import _declared
        db = ChunkDb()
        repo = make_chunk_repo(db, mock_env_config)
        for pending in (True, False, None):
            await repo.count_unread(U_SCOPE, {"pdf"}, {"image"}, pending=pending, transcribed=MEDIA)
        declared = _declared("development_drive_index_records")
        seen = executed(db, RECS)
        assert seen
        for ops in seen:
            prefix = {(op[1], "EQ") for op in ops if op[0] == "where" and op[2] in ("==", "IS_NULL", "in")}
            assert any({(p, "EQ") for p, _ in i} == prefix for i in declared), ops

    async def test_the_media_classes_leave_out_no_text(self, mock_env_config):
        db = ChunkDb()
        repo = make_chunk_repo(db, mock_env_config)
        await repo.count_unread(U_SCOPE, frozenset(), frozenset(), transcribed=MEDIA)
        reasons = [op[3] for ops in executed(db, RECS) for op in ops if op[:3] == ("where", "reason", "==")]
        assert sorted(reasons) == ["machine_text", "too_large", "user_chunk_cap"]
        for ops in executed(db, RECS):
            assert ("where", "kind", "in", ["audio", "video"]) in ops

    async def test_default_queries_are_unchanged(self, mock_env_config):
        db = ChunkDb()
        repo = make_chunk_repo(db, mock_env_config)
        await repo.count_unread(U_SCOPE, {"pdf"})
        assert len(executed(db, RECS)) == 6
