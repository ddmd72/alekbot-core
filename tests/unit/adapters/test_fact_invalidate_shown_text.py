"""Cabinet delete of a stale row (owner decision 2026-10-23, FACT_LIFECYCLE_RFC §9 "S3 as built"): the page showed the
text of the id it sends. When that id was superseded by a version with another text, the Cabinet delete (`CabinetFactService.invalidate`, facts S4 T2c) refuses with
FactResolutionError (the route answers 409 and the page reloads) and writes nothing; the same-text case is pinned in
test_fact_repository_versions_kit.py.

Every document is seeded through a real write on the evaluating Firestore fake (see test_fact_repository_versions_kit).
"""
import copy

import pytest

from src.domain.entities import FactState
from src.domain.exceptions import FactResolutionError
from src.services.cabinet_fact_service import CabinetFactService
from tests.support.fact_kit import ACC, T0, make_fact
from tests.support.fact_store import INDEXES_FILE, FactStore


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


async def _seed(store, name, text, **over):
    fact = make_fact(name, **over)
    fact = fact.model_copy(update={"text": text})
    await store.repo.add_fact(fact)
    return fact


async def _seed_chain(store, old_text, new_text):
    await _seed(store, "v1", old_text, lineage_id="L", state=FactState.SUPERSEDED, is_current=False, valid_to=T0,
                superseded_by="f-v2")
    await _seed(store, "v2", new_text, lineage_id="L", version=2)


async def test_a_stale_row_whose_text_was_rewritten_is_refused_and_nothing_is_written(store):
    await _seed_chain(store, "mother is Maria", "mother is Maria Doe")
    before = copy.deepcopy(store.docs)

    with pytest.raises(FactResolutionError, match="rewritten after it was shown") as caught:
        await CabinetFactService(store.repo).invalidate("f-v1", ACC)

    assert caught.value.current_id == "f-v2" and not isinstance(caught.value, ValueError)
    assert store.docs == before


async def test_the_current_row_itself_is_invalidated_whatever_its_text(store):
    await _seed(store, "x", "lives in Springfield")

    await CabinetFactService(store.repo).invalidate("f-x", ACC)

    assert store.docs["f-x"]["state"] == "invalidated"


async def test_a_version_written_between_resolve_and_write_with_another_text_is_refused(store):
    await _seed(store, "v1", "works remotely", lineage_id="L")
    await _seed(store, "v2", "works remotely from Springfield", lineage_id="L", version=2)
    closure = {"state": "superseded", "is_current": False, "superseded_by": "f-v2", "valid_to": T0}
    store.interfere("f-v1", closure)    # a version write closes f-v1 between invalidate's resolve and its write
    v2_before = copy.deepcopy(store.docs["f-v2"])

    with pytest.raises(FactResolutionError, match="rewritten after it was shown"):
        await CabinetFactService(store.repo).invalidate("f-v1", ACC)

    assert store.docs["f-v2"] == v2_before
    assert store.docs["f-v1"]["state"] == "superseded"
