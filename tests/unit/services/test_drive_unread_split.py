""""Not read yet" and "could not be read" are two facts (USER_DRIVE_INDEXING_RFC §10.2 `unread`; final review F5-2, F4-1).

`unread` counted documents still waiting (queued, held for next month, scheduled by an upgrade pass) as documents that
"could not be read": Smart told the user a document was unreadable while it was only in the queue. The envelope now
says "N documents are not read yet" (pending, no readable generation) apart from "N documents could not be read" (the
terminal demotes: failed, too large, no text, machine text, chunk cap). While the index's applied policy is not the
current POLICY_VERSION (the flip's upgrade pass is re-planning metadata-only records), documents are still being read
whatever the counters say. The counters are refreshed every tick, not only when the tick settles."""
from dataclasses import replace

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus
from src.domain.drive_index import POLICY_VERSION, DriveIndexRecord, Reason, RecordState
from src.domain.drive_index_policy import SLICE2_CONTENT_KINDS
from src.domain.drive_search import (
    NOT_READ_YET_COUNTER, STATUS_OK, UNREAD_COUNTER, DriveSearchEnvelope, DriveSearchRequest, DriveSearchResult,
    is_not_read_yet, render_results,
)

from tests.unit.adapters.firestore_drive_chunk_fakes import ChunkDb, make_chunk_repo
from tests.unit.adapters.firestore_drive_fakes import RECS, executed

from .drive_content_world import ContentWorld, admitted  # noqa: F401  (fixture)
from .test_drive_search_service import World
from .drive_index_fakes import U_SCOPE

DOCS = frozenset({"pdf"})


def rec(item_id, **kw):
    return DriveIndexRecord(**{"user_id": "u", "connection_id": "u", "item_id": item_id, "kind": "pdf", "state": RecordState.PENDING, **kw})


class TestDomain:
    def test_not_read_yet_is_a_pending_document_without_a_readable_generation(self):
        assert is_not_read_yet(rec("p"), DOCS)
        assert not is_not_read_yet(rec("p", indexed_generation=1), DOCS)            # searchable meanwhile
        assert not is_not_read_yet(rec("f", state=RecordState.FAILED), DOCS)         # could not be read
        assert not is_not_read_yet(rec("n", state=RecordState.METADATA_ONLY, reason=Reason.NO_TEXT), DOCS)
        assert not is_not_read_yet(rec("c", kind="code"), DOCS)
        # held by the card cap, not waiting (the "index is full" line covers it): keyed on the flag, whatever the state
        assert not is_not_read_yet(rec("k", card_capped=True), DOCS)

    def _render(self, **env):
        return render_results(DriveSearchResult(status=STATUS_OK, request=DriveSearchRequest(query="lease"),
                                                envelope=DriveSearchEnvelope(**env)))

    def test_the_two_counts_are_two_lines(self):
        text = self._render(unread=2, not_read_yet=5)
        assert "5 documents are not read yet, so their content was not searched." in text
        assert "2 documents could not be read, so their content was not searched." in text

    def test_waiting_documents_are_never_called_unreadable(self):
        text = self._render(not_read_yet=5)
        assert "could not be read" not in text and "5 documents are not read yet" in text

    def test_while_the_policy_is_being_applied_documents_are_still_being_read(self):
        assert "Documents are still being read: 4 are not read yet" in self._render(reading=True, not_read_yet=4)
        quiet = self._render(reading=True)
        assert "Documents are still being read" in quiet and "not searched" in quiet


class TestRepositories:
    """`pending=True` counts the documents not read yet, `pending=False` those that could not be read."""

    async def test_in_memory_count_by_class(self):
        repo = InMemoryDriveIndexRepository()
        await repo.upsert_records([
            rec("p1"), rec("p2"), rec("f", state=RecordState.FAILED),
            rec("t", state=RecordState.METADATA_ONLY, reason=Reason.TOO_LARGE), rec("k", card_capped=True),
        ])
        assert await repo.count_unread(U_SCOPE, DOCS) == 5          # the legacy total is unchanged
        assert await repo.count_unread(U_SCOPE, DOCS, pending=True) == 2
        assert await repo.count_unread(U_SCOPE, DOCS, pending=False) == 2

    async def test_firestore_pending_is_its_own_class(self, mock_env_config):
        db = ChunkDb()
        repo = make_chunk_repo(db, mock_env_config)
        await repo.count_unread(U_SCOPE, {"pdf"}, pending=True)
        pending = executed(db, RECS)
        assert len(pending) == 1 and ("where", "state", "==", "pending") in pending[0]
        assert ("where", "card_capped", "==", False) in pending[0]
        db2 = ChunkDb()
        repo2 = make_chunk_repo(db2, mock_env_config)
        await repo2.count_unread(U_SCOPE, {"pdf"}, pending=False)
        terminal = executed(db2, RECS)
        assert len(terminal) == 5 and not any(("where", "state", "==", "pending") in ops for ops in terminal)


class TestEnvelope:
    async def test_could_not_be_read_excludes_the_waiting_ones(self):
        w = World()
        w.drive.add("doc.pdf")
        await w.settle()
        state = await w.repo.get_sync_state(U_SCOPE)
        w.repo.states["u"] = replace(state, counters={**state.counters, UNREAD_COUNTER: 5, NOT_READ_YET_COUNTER: 3})
        env = (await w.search("doc")).envelope
        assert (env.unread, env.not_read_yet, env.reading) == (2, 3, False)

    async def test_an_applied_policy_behind_the_current_one_reads_as_still_reading(self):
        w = World()
        w.drive.add("doc.pdf")
        await w.settle()
        state = await w.repo.get_sync_state(U_SCOPE)
        w.repo.states["u"] = replace(state, applied_versions={**state.applied_versions, "policy": POLICY_VERSION - 1})
        assert (await w.search("doc")).envelope.reading


@pytest.mark.usefixtures("admitted")
async def test_the_unread_counters_are_refreshed_on_a_tick_that_does_not_settle(monkeypatch):
    from src.services import drive_indexing_service as svc_mod
    w = ContentWorld()
    for n in range(4):
        w.parser.outcomes[f"scan{n}.pdf"] = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=1)
        w.add_doc(f"scan{n}.pdf", "")
    w.reader.unavailable = True                       # every scan waits on the reader: pending, not unreadable
    monkeypatch.setattr(svc_mod, "TICK_FILE_BUDGET", 1)
    seen = []
    for _ in range(12):
        before = len(w.queue.tasks)
        await w.tick()
        continued = any(":cont:" in (t["key"] or "") for t in w.queue.tasks[before:])
        state = await w.repo.get_sync_state(U_SCOPE)
        truth = sum(1 for (u, _), r in w.repo.records.items() if u == "u" and is_not_read_yet(r, SLICE2_CONTENT_KINDS))
        if continued and truth:
            seen.append((state.counters.get(NOT_READ_YET_COUNTER, 0), truth))
        if not continued:
            break
        w.clock.advance(60)
    assert seen and all(counted == truth for counted, truth in seen)


async def test_the_not_read_yet_count_has_its_declared_composite(mock_env_config):
    from tests.unit.adapters.test_firestore_drive_index_chunk_provisioning import _declared
    db = ChunkDb()
    repo = make_chunk_repo(db, mock_env_config)
    await repo.count_unread(U_SCOPE, {"pdf"}, {"image"}, pending=True)
    await repo.count_unread(U_SCOPE, {"pdf"}, {"image"}, pending=False)
    declared = _declared("development_drive_index_records")
    for ops in executed(db, RECS):
        prefix = {(op[1], "EQ") for op in ops if op[0] == "where" and op[2] in ("==", "IS_NULL", "in")}
        assert any({(p, "EQ") for p, _ in i} == prefix for i in declared), ops
