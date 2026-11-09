"""The document FirestoreBlueprintRepository writes on save and removes on delete (mutation survivors of the module).

The production adapter calls `doc_ref.set(data)` / `doc_ref.delete()` without `await` (P10-D3, P10-D4, see
tests/unit/adapters/test_prompt_blueprint_store_kit.py), so on the awaiting `EvalDb` nothing is written. These tests use a
collection whose document handle applies a write at call time and hands back an awaitable: the write lands whether or not
the adapter awaits it, so the tests stay valid once the defect is fixed.
"""
import asyncio

import pytest

from src.adapters.prompt_v3.firestore_blueprint_repository import FirestoreBlueprintRepository
from src.domain.prompt_v3.blueprint import Blueprint
from tests.support.firestore_eval import EvalCollection, EvalDb, EvalDocRef

COL = "eval_blueprints_written"
ORDER = ["properties", "cognitive_process", "policies"]


class _EagerRef(EvalDocRef):
    def set(self, data, merge=False):  # type: ignore[override]
        self._apply("set", data, merge)
        return _done()

    def delete(self, option=None):  # type: ignore[override]
        self._apply("delete", None, option)
        return _done()


def _done() -> "asyncio.Future[None]":
    future = asyncio.get_running_loop().create_future()
    future.set_result(None)
    return future


class _EagerCollection(EvalCollection):
    def document(self, doc_id=None):  # type: ignore[override]
        return _EagerRef(self, doc_id or "generated")


@pytest.fixture
def world():
    db = EvalDb()
    db.cols[COL] = _EagerCollection(COL, db)
    return db, FirestoreBlueprintRepository(db=db, collection_name=COL), db.cols[COL].docs


async def test_save_writes_all_three_fields_under_the_blueprint_id(world):
    _, repo, docs = world
    await repo.save(Blueprint(id="bp_x", outer_class="Alpha extends Agent", class_order=ORDER))
    assert docs == {"bp_x": {"blueprint_id": "bp_x", "outer_class": "Alpha extends Agent", "class_order": ORDER}}


async def test_saved_blueprint_reads_back_equal(world):
    _, repo, _ = world
    blueprint = Blueprint(id="bp_x", outer_class="Alpha extends Agent", class_order=ORDER)
    await repo.save(blueprint)
    assert await repo.get("bp_x") == blueprint
    assert await repo.exists("bp_x") is True


async def test_delete_removes_the_document(world):
    _, repo, docs = world
    await repo.save(Blueprint(id="bp_x", outer_class="Alpha extends Agent", class_order=ORDER))
    await repo.save(Blueprint(id="bp_y", outer_class="Beta extends Agent", class_order=ORDER))
    await repo.delete("bp_x")
    assert set(docs) == {"bp_y"}
    assert await repo.exists("bp_x") is False
