"""FirestoreDriveConnectionRepository refuses to read a stored connection that has an empty `created_at`, over the evaluating
Firestore fake. Technical ids only."""
import pytest

from src.adapters.firestore_drive_connection_repo import FirestoreDriveConnectionRepository
from src.domain.connection_scope import ConnectionScope
from src.domain.drive_connection import connection_doc_id
from tests.support.firestore_eval import EvalDb

CONNS, SETTINGS = "eval_drive_connections", "eval_drive_user_settings"


async def test_a_stored_connection_without_created_at_is_refused_on_read():
    db = EvalDb()
    repo = FirestoreDriveConnectionRepository(db, CONNS, SETTINGS)
    await db.collection(CONNS).document(connection_doc_id(ConnectionScope(user_id="usr-1", connection_id="conn-1"))).set({
        "connection_id": "conn-1", "user_id": "usr-1", "account_id": "acc-1", "credential_id": "cred-1",
        "kind": "fake_a", "drive_identity": "fake_a:d1", "ref_tag": "a1", "mount_name": "", "state": "active", "created_at": None})
    with pytest.raises(ValueError):
        await repo.get("usr-1", "conn-1")
