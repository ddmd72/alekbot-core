"""Mutation P3: `delete_user` removes the stored document."""
from types import SimpleNamespace

from src.adapters.firestore_user_repo import FirestoreUserRepository
from tests.support.firestore_eval import EvalDb

COL = "eval_users"


async def test_delete_user_removes_the_document():
    db = EvalDb()
    db.collection(COL).docs["usr-1"] = {"user_id": "usr-1"}
    db.collection(COL).docs["usr-2"] = {"user_id": "usr-2"}
    env = SimpleNamespace(domain_users_collection=COL, is_production=False)
    repo = FirestoreUserRepository(db, env, None)
    assert await repo.delete_user("usr-1") is True
    assert set(db.collection(COL).docs) == {"usr-2"}
