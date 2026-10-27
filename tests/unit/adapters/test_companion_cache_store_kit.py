"""CompanionCacheRepository kit (P11, G3 store 6): the docstring of `src/ports/companion_cache_repository.py` checked against
`FirestoreCompanionCacheRepository` on the evaluating Firestore fake (H1), with the index refusal ON (the adapter issues
no query). Assertions are on the raw stored documents and on what the readers return.

The read through the real `CompanionContextAssemblerService` (the one reader) runs over the real memory and cache
repositories; `query_phrases=[]` keeps embeddings out, so the assembler needs no embedding port. The summary's tenancy
(keyed by session only) is not pinned: the companion stores are to be reworked with the agent workspace.

`_adapter(db)` is the one place the adapter is constructed.
"""
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.adapters.firestore_companion_cache_repository import FirestoreCompanionCacheRepository
from src.adapters.firestore_companion_memory_repository import FirestoreCompanionMemoryRepository
from src.services.companion_context_assembler_service import CompanionContextAssemblerService
from tests.support.firestore_eval import EvalDb

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
CACHE, REC = "eval_companion_context_cache", "eval_companion_records"
SESSION = "slack:C1"
SUMMARY = "Plays tennis on Sundays"


def _adapter(db: EvalDb) -> FirestoreCompanionCacheRepository:
    return FirestoreCompanionCacheRepository(db, SimpleNamespace(companion_context_cache_collection=CACHE))


class Store:
    def __init__(self) -> None:
        self.db = EvalDb(indexes=INDEXES_FILE, groups={CACHE: "development_companion_context_cache",
                                                       REC: "development_companion_records"})
        self.repo = _adapter(self.db)
        memory = FirestoreCompanionMemoryRepository(self.db, SimpleNamespace(companion_records_collection=REC))
        self.assembler = CompanionContextAssemblerService(memory, self.repo, None, None, None)

    @property
    def docs(self) -> dict:
        return self.db.collection(CACHE).docs

    async def summary_seen_by(self, account: str, session: str = SESSION):
        context = await self.assembler.assemble_context(session, account, [], include_own_records=False)
        return context.session_summary


@pytest.fixture
def store():
    return Store()


async def test_save_then_get_returns_the_summary_and_the_document_carries_session_account_and_server_time(store):
    await store.repo.save_summary(SESSION, "a1", SUMMARY)
    assert await store.repo.get_summary(SESSION) == SUMMARY
    doc = store.docs[SESSION]
    assert (doc["session_id"], doc["account_id"], doc["summary"]) == (SESSION, "a1", SUMMARY)
    assert isinstance(doc["refreshed_at"], datetime) and doc["refreshed_at"].tzinfo is not None


async def test_a_second_save_replaces_the_whole_document(store):
    await store.repo.save_summary(SESSION, "a1", SUMMARY)
    await store.repo.save_summary(SESSION, "a1", "Practises guitar 1 h/day")
    assert await store.repo.get_summary(SESSION) == "Practises guitar 1 h/day"
    assert list(store.docs) == [SESSION]


async def test_a_session_that_never_saved_reads_none_and_sessions_are_independent(store):
    assert await store.repo.get_summary(SESSION) is None
    await store.repo.save_summary(SESSION, "a1", SUMMARY)
    assert await store.repo.get_summary("slack:C2") is None


async def test_the_owning_account_reads_its_summary_through_the_assembler(store):
    await store.repo.save_summary(SESSION, "a1", SUMMARY)
    assert await store.summary_seen_by("a1") == SUMMARY
