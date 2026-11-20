"""`DriveSyncState.applied_scope` (MULTI_DRIVE_RFC §11.3): the scope key the stored records were enumerated under is kept
by the in-memory repository; the Firestore codec's round trip and the default scope ("") of a state stored before 2b are
in the every-field case (test_firestore_drive_index_repo_serialization.py)."""
import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.connection_scope import ConnectionScope
from src.domain.drive_index import DriveSyncState

KEY = "0123456789abcdef"
SCOPE = ConnectionScope(user_id="usr-1", connection_id="conn-1")


@pytest.mark.parametrize("key", [KEY, ""])
async def test_the_in_memory_repository_keeps_it(key):
    repo = InMemoryDriveIndexRepository()
    await repo.save_sync_state(DriveSyncState(user_id="usr-1", connection_id="conn-1", applied_scope=key))
    assert (await repo.get_sync_state(SCOPE)).applied_scope == key
