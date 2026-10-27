"""CompanionCacheRepository kit (P11 · G3-6): the docstring of `src/ports/companion_cache_repository.py` checked against
`FirestoreCompanionCacheRepository` on the evaluating Firestore fake (H1), with the index refusal ON (the adapter issues
no query). Assertions are on the raw stored documents and on what the readers return.

P11-G3-4 is pinned through the real `CompanionContextAssemblerService` (the one reader, `get_summary` with the sender's
account), over the real memory and cache repositories; `query_phrases=[]` keeps embeddings out, so the assembler needs no
embedding port.

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
from tests.support.redelivery_world import require

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
CACHE, REC = "eval_companion_context_cache", "eval_companion_records"
SESSION = "slack:C1"
SUMMARY = "Practised the past tense with a cookbook recipe"


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
    await store.repo.save_summary(SESSION, "a1", "Practised the future tense")
    assert await store.repo.get_summary(SESSION) == "Practised the future tense"
    assert list(store.docs) == [SESSION]


async def test_a_session_that_never_saved_reads_none_and_sessions_are_independent(store):
    assert await store.repo.get_summary(SESSION) is None
    await store.repo.save_summary(SESSION, "a1", SUMMARY)
    assert await store.repo.get_summary("slack:C2") is None


async def test_the_owning_account_reads_its_summary_through_the_assembler(store):
    """The passing control of P11-G3-4."""
    await store.repo.save_summary(SESSION, "a1", SUMMARY)
    assert await store.summary_seen_by("a1") == SUMMARY


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "P11-G3-4: the cache does not apply the memory port's account rule (RFC §3 assumes one account per companion "
    "session; the owner rules which holds)"))
async def test_a_summary_is_not_read_by_another_account(store):
    """The companion memory port says session_id alone is not a sufficient tenancy guarantee; the cache is keyed by
    session only, so another account reading the same session key gets this account's summary, and each save overwrites
    the other's. Flipping fix (not one line, so outside P11, it changes a signature): key the document
    `{account_id}:{session_id}` and give `get_summary` an `account_id` (port, adapter,
    `companion_context_assembler_service.py` call). If the owner picks RFC §3 instead, delete this xfail."""
    await store.repo.save_summary(SESSION, "a1", SUMMARY)
    require(await store.summary_seen_by("a1") == SUMMARY, "the owning account reads its summary")
    assert await store.summary_seen_by("a2") is None
