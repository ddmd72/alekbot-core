"""Mutation P3: `list_user_ids` reads the collection with a one-field projection (never whole documents)."""
from types import SimpleNamespace

from src.adapters.firestore_task_config_repository import FirestoreTaskConfigRepository
from tests.support.firestore_eval import EvalDb

COL = "eval_task_config"


async def test_list_user_ids_uses_primary_list_id_projection():
    db = EvalDb()
    db.collection(COL).docs["usr-2"] = {"primary_list_id": "L2", "subscriptions": {}}
    db.collection(COL).docs["usr-1"] = {"subscriptions": {}}
    repo = FirestoreTaskConfigRepository(db, SimpleNamespace(task_config_collection=COL))
    assert await repo.list_user_ids() == ["usr-1", "usr-2"]
    assert [("select", ("primary_list_id",))] in db.collection(COL).executed
