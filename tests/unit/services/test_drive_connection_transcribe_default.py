"""A new drive connection has transcription on; a stored document without the flag still reads as off (no migration).
Technical ids only."""
from dataclasses import replace
from datetime import datetime, timezone

from src.adapters.firestore_drive_connection_repo import FirestoreDriveConnectionRepository
from src.domain.connection_scope import ConnectionScope
from src.domain.drive_connection import DriveConnection, connection_doc_id
from tests.support.firestore_eval import EvalDb
from tests.unit.services.test_drive_connection_service import World

CONNS, SETTINGS = "eval_drive_connections", "eval_drive_user_settings"


async def test_a_new_connection_created_by_the_service_has_transcription_on_and_indexing_off():
    w = World()
    conn = await w.service.activate(await w.consent(), "microsoft_onedrive:drive-a")
    stored = w.stored("usr-1", conn.connection_id)
    assert (stored.transcribe_enabled, stored.indexing_enabled) == (True, False)


async def test_a_connection_object_defaults_transcription_on():
    conn = DriveConnection(connection_id="conn-1", user_id="usr-1", account_id="acc-1", credential_id="cred-1",
                           kind="fake_a", drive_identity="fake_a:d1", ref_tag="a1",
                           created_at=datetime(2026, 10, 30, tzinfo=timezone.utc))
    assert conn.transcribe_enabled is True
    assert replace(conn, transcribe_enabled=False).transcribe_enabled is False


async def test_a_stored_connection_without_the_flag_reads_as_off():
    db = EvalDb()
    repo = FirestoreDriveConnectionRepository(db, CONNS, SETTINGS)
    await db.collection(CONNS).document(connection_doc_id(ConnectionScope(user_id="usr-1", connection_id="conn-1"))).set({
        "connection_id": "conn-1", "user_id": "usr-1", "account_id": "acc-1", "credential_id": "cred-1",
        "kind": "fake_a", "drive_identity": "fake_a:d1", "ref_tag": "a1", "mount_name": "Drive", "state": "active",
        "created_at": datetime(2026, 10, 30, tzinfo=timezone.utc)})
    stored = await repo.get("usr-1", "conn-1")
    assert stored is not None and stored.transcribe_enabled is False
