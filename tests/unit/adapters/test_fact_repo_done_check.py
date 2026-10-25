"""Facts S3 · T14 done-check: a lineage reads in version order whatever its document ids, and a failed biographical
cache refresh is logged and swallowed, never written."""
import pytest

from src.adapters.firestore_repo import FirestoreFactRepository
from src.services.biographical_context_service import BiographicalContextService
from tests.support.fact_kit import ACC, make_fact
from tests.support.fact_store import INDEXES_FILE, FactStore, make_env_config

CONTEXT_COLLECTION = "eval_user_context"


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


async def test_a_lineage_reads_in_version_order_even_when_the_document_ids_sort_the_other_way(store):
    for doc_id, version in (("a-newest", 3), ("m-middle", 2), ("z-oldest", 1)):
        await store.repo.add_fact(make_fact(doc_id, id=doc_id, lineage_id="L", version=version))

    got = await store.repo.get_lineage(ACC, "L")

    assert [(f.id, f.version) for f in got] == [("z-oldest", 1), ("m-middle", 2), ("a-newest", 3)]


async def test_a_failed_cache_refresh_is_logged_with_the_cause_and_writes_no_cache(store, monkeypatch):
    errors = []
    monkeypatch.setattr("src.adapters.firestore_repo.logger.error", lambda msg, *a, **k: errors.append(str(msg)))

    class Failing(BiographicalContextService):
        async def refresh_context(self, *args, **kwargs):
            raise RuntimeError("context service down")

    repo = FirestoreFactRepository(store.db, make_env_config(), embedding_service=store.embedder,
                                   biographical_context_service=Failing(None))

    assert await repo.refresh_biographical_context_cache(ACC) is None

    assert store.db.collection(CONTEXT_COLLECTION).docs.get(ACC) is None
    assert len(errors) == 1 and "context service down" in errors[0] and ACC in errors[0]
