"""`DriveSyncState.applied_scope` (MULTI_DRIVE_RFC §11.3): the scope key the stored records were enumerated under is stored
and read by both index repositories; a state stored before 2b has none and reads the default scope ("")."""
import pytest

from src.adapters.firestore_drive_index_repo import state_from_doc, state_to_doc
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.connection_scope import ConnectionScope
from src.domain.drive_index import DriveSyncState

KEY = "0123456789abcdef"
SCOPE = ConnectionScope(user_id="usr-1", connection_id="conn-1")


def test_firestore_document_round_trip():
    state = DriveSyncState(user_id="usr-1", connection_id="conn-1", applied_scope=KEY)
    doc = state_to_doc(state)
    assert doc["applied_scope"] == KEY
    assert state_from_doc("usr-1", doc).applied_scope == KEY


def test_a_document_stored_before_2b_reads_the_default_scope():
    doc = state_to_doc(DriveSyncState(user_id="usr-1", connection_id="conn-1"))
    del doc["applied_scope"]
    assert state_from_doc("usr-1", doc).applied_scope == ""


@pytest.mark.parametrize("key", [KEY, ""])
async def test_the_in_memory_repository_keeps_it(key):
    repo = InMemoryDriveIndexRepository()
    await repo.save_sync_state(DriveSyncState(user_id="usr-1", connection_id="conn-1", applied_scope=key))
    assert (await repo.get_sync_state(SCOPE)).applied_scope == key
