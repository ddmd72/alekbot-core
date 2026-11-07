"""FirestoreBlueprintRepository kit (P10): the docstring of `src/ports/prompt_v3/blueprint_repository.py` and of the adapter
checked on the evaluating Firestore fake (H1). Documents are seeded as raw dicts where a test reads, because `save` and
`delete` do not write on the production client (P10-D3, P10-D4: the write coroutine is never awaited); the tests that need
the write are strict xfails.

`_adapter(db)` is the one place the adapter is constructed.
"""
import pytest

from src.adapters.prompt_v3.firestore_blueprint_repository import FirestoreBlueprintRepository
from src.domain.prompt_v3.blueprint import Blueprint
from tests.support.firestore_eval import EvalDb

COL = "eval_blueprints"
ORDER = ["properties", "cognitive_process", "policies"]
D3 = "P10-D3: FirestoreBlueprintRepository.save calls doc_ref.set(data) without await; on the async client no write happens"
D4 = "P10-D4: FirestoreBlueprintRepository.delete calls doc_ref.delete() without await; on the async client nothing is deleted"


def _adapter(db: EvalDb) -> FirestoreBlueprintRepository:
    return FirestoreBlueprintRepository(db=db, collection_name=COL)


class Store:
    def __init__(self) -> None:
        self.db = EvalDb()
        self.repo = _adapter(self.db)

    @property
    def docs(self) -> dict:
        return self.db.collection(COL).docs

    def seed(self, blueprint_id="bp_one", outer_class="Alek extends Agent", class_order=None) -> None:
        self.docs[blueprint_id] = {"blueprint_id": blueprint_id, "outer_class": outer_class,
                                   "class_order": list(class_order or ORDER)}


@pytest.fixture
def store() -> Store:
    return Store()


async def test_get_returns_every_field_of_a_stored_blueprint(store):
    store.seed()
    assert await store.repo.get("bp_one") == Blueprint(id="bp_one", outer_class="Alek extends Agent", class_order=ORDER)


async def test_get_of_a_missing_blueprint_raises_key_error(store):
    with pytest.raises(KeyError):
        await store.repo.get("nope")


async def test_get_validates_what_it_reads(store):
    store.seed(class_order=["properties", "properties"])
    with pytest.raises(ValueError):
        await store.repo.get("bp_one")


async def test_list_all_returns_every_blueprint(store):
    store.seed("bp_one")
    store.seed("bp_two", "Alek extends Router", ["properties"])
    listed = await store.repo.list_all()
    assert sorted(b.id for b in listed) == ["bp_one", "bp_two"]
    assert {b.id: (b.outer_class, b.class_order) for b in listed}["bp_two"] == ("Alek extends Router", ["properties"])


async def test_list_all_of_an_empty_store_is_empty(store):
    assert await store.repo.list_all() == []


async def test_exists(store):
    store.seed()
    assert await store.repo.exists("bp_one") is True
    assert await store.repo.exists("bp_two") is False


@pytest.mark.parametrize("bad", [
    Blueprint(id="bad", outer_class="", class_order=ORDER),
    Blueprint(id="bad", outer_class="Alek extends Agent", class_order=[]),
    Blueprint(id="bad", outer_class="Alek extends Agent", class_order=["a", "a"])])
async def test_saving_an_invalid_blueprint_raises_value_error_and_writes_nothing(store, bad):
    with pytest.raises(ValueError):
        await store.repo.save(bad)
    assert store.docs == {}


@pytest.mark.xfail(strict=True, reason=D3)
async def test_save_stores_every_field(store):
    await store.repo.save(Blueprint(id="bp_one", outer_class="Alek extends Agent", class_order=ORDER))
    assert store.docs == {"bp_one": {"blueprint_id": "bp_one", "outer_class": "Alek extends Agent", "class_order": ORDER}}


@pytest.mark.xfail(strict=True, reason=D3)
async def test_a_saved_blueprint_reads_back_equal_and_a_second_save_replaces_it(store):
    first = Blueprint(id="bp_one", outer_class="Alek extends Agent", class_order=ORDER)
    await store.repo.save(first)
    assert await store.repo.get("bp_one") == first
    second = Blueprint(id="bp_one", outer_class="Alek extends Router", class_order=["policies"])
    await store.repo.save(second)
    assert await store.repo.get("bp_one") == second


@pytest.mark.xfail(strict=True, reason=D4)
async def test_delete_removes_the_blueprint(store):
    store.seed()
    store.seed("bp_two")
    await store.repo.delete("bp_one")
    assert list(store.docs) == ["bp_two"]


async def test_delete_of_a_missing_blueprint_raises_key_error(store):
    with pytest.raises(KeyError):
        await store.repo.delete("nope")
