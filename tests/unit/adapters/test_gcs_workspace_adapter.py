"""GcsWorkspaceAdapter at the google-cloud-storage SDK boundary (AGENT_WORKSPACE_RFC §5.3), the SDK client mocked.
Every other case moved to test_workspace_store_kit.py (the real adapter over the GCS fake). This one stays: the
read-back of a lost delete lists only the deleted name's prefix, a request the fake cannot observe (a listing of the
whole bucket gives the same answer after the exact-name filter)."""
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from google.api_core.exceptions import PreconditionFailed

from src.adapters.gcs_workspace_adapter import GcsWorkspaceAdapter
from src.domain.workspace import WorkspaceDeleteMeta

T = datetime(2026, 10, 20, 6, 1, tzinfo=timezone.utc)
KEY = "u1/projects/a.md"


class _Blob:
    """The storage.Blob attribute surface the adapter reads and writes; its methods are MagicMocks."""

    def __init__(self, name=KEY, generation=101, size=5, metadata=None, time_deleted=None):
        self.name, self.generation, self.size = name, generation, size
        self.metadata = dict(metadata or {})
        self.time_created, self.time_deleted = T, time_deleted
        self.custom_time = None
        self.upload_from_string = MagicMock()
        self.patch = MagicMock()
        self.delete = MagicMock()
        self.download_as_bytes = MagicMock(return_value=b"")


@pytest.fixture
def client():
    c = MagicMock()
    c.bucket.return_value = MagicMock()
    return c


def _adapter(client, ids=("w1", "w2", "w3")):
    it = iter(ids)
    return GcsWorkspaceAdapter("ws-bucket", client=client, new_id=lambda: next(it))


class TestDelete:
    async def test_412_on_our_own_landed_delete_is_success(self, client):
        bucket = client.bucket.return_value
        bucket.blob.return_value = _Blob()
        bucket.blob.return_value.delete.side_effect = PreconditionFailed("412")
        bucket.get_blob.return_value = None
        client.list_blobs.return_value = [
            _Blob(generation=99, metadata={"delete_id": "old"}, time_deleted=T),
            _Blob(generation=101, metadata={"delete_id": "w1"}, time_deleted=T),
            _Blob(name=KEY + ".bak", generation=120, metadata={}, time_deleted=T),
        ]
        await _adapter(client).delete("u1", "projects/a.md", 101, WorkspaceDeleteMeta(deleted_by="chat"))
        client.list_blobs.assert_called_once_with("ws-bucket", prefix=KEY, versions=True)
