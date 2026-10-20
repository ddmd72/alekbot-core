"""Wire tests for GcsWorkspaceAdapter at the google-cloud-storage SDK boundary (AGENT_WORKSPACE_RFC §5.2–§5.3;
docs/how_to/ADAPTER_WIRE_TESTING.md: the SDK client is mocked, never the port)."""
from datetime import datetime, timezone
from unittest.mock import MagicMock, call

import pytest
from google.api_core.exceptions import NotFound, PreconditionFailed

from src.adapters.gcs_workspace_adapter import GcsWorkspaceAdapter
from src.domain.exceptions import WorkspaceConflict
from src.domain.workspace import WorkspaceDeleteMeta, WorkspaceWriteMeta

T = datetime(2026, 10, 20, 6, 1, tzinfo=timezone.utc)
KEY = "u1/projects/a.md"
F = "reminder:1729401234567@2026-10-20T06:00:00+00:00"


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


class TestPut:
    async def test_key_metadata_content_type_and_create_precondition(self, client):
        bucket = client.bucket.return_value
        blob = bucket.blob.return_value = _Blob()
        entry = await _adapter(client).put("u1", "projects/a.md", b"hello", WorkspaceWriteMeta(origin="chat"), 0)
        client.bucket.assert_called_with("ws-bucket")
        bucket.blob.assert_called_once_with(KEY)
        assert blob.metadata == {"origin": "chat", "write_id": "w1"}
        blob.upload_from_string.assert_called_once_with(
            b"hello", content_type="text/markdown; charset=utf-8", if_generation_match=0,
        )
        assert (entry.path, entry.generation, entry.write_id, entry.origin, entry.live) == (
            "projects/a.md", 101, "w1", "chat", True)
        assert blob.custom_time is None

    async def test_fire_restored_from_and_overwrite_precondition(self, client):
        blob = client.bucket.return_value.blob.return_value = _Blob(name="u1/files/lease.pdf")
        await _adapter(client).put("u1", "files/lease.pdf", b"%PDF",
                                   WorkspaceWriteMeta(origin="background:execute_reminder", fire=F, restored_from=101),
                                   105)
        assert blob.metadata == {"origin": "background:execute_reminder", "write_id": "w1", "fire": F,
                                 "restored_from": "101"}
        blob.upload_from_string.assert_called_once_with(b"%PDF", content_type="application/pdf",
                                                        if_generation_match=105)

    async def test_content_type_is_case_insensitive(self, client):
        blob = client.bucket.return_value.blob.return_value = _Blob(name="u1/NOTES.MD")
        await _adapter(client).put("u1", "NOTES.MD", b"x", WorkspaceWriteMeta(origin="chat"), 0)
        blob.upload_from_string.assert_called_once_with(
            b"x", content_type="text/markdown; charset=utf-8", if_generation_match=0)

    async def test_custom_time_only_under_scratch(self, client):
        blob = client.bucket.return_value.blob.return_value = _Blob(name="u1/scratch/d.md")
        await _adapter(client).put("u1", "scratch/d.md", b"x", WorkspaceWriteMeta(origin="chat"), 0)
        assert isinstance(blob.custom_time, datetime) and blob.custom_time.tzinfo is not None

    async def test_412_on_our_own_landed_write_is_success(self, client):
        bucket = client.bucket.return_value
        bucket.blob.return_value = _Blob()
        bucket.blob.return_value.upload_from_string.side_effect = PreconditionFailed("412")
        bucket.get_blob.return_value = _Blob(generation=102, metadata={"origin": "chat", "write_id": "w1"})
        entry = await _adapter(client).put("u1", "projects/a.md", b"x", WorkspaceWriteMeta(origin="chat"), 0)
        assert entry.generation == 102
        bucket.get_blob.assert_called_once_with(KEY)

    async def test_412_with_another_write_id_is_a_conflict(self, client):
        bucket = client.bucket.return_value
        bucket.blob.return_value = _Blob()
        bucket.blob.return_value.upload_from_string.side_effect = PreconditionFailed("412")
        bucket.get_blob.return_value = _Blob(generation=102, metadata={"write_id": "someone-else"})
        with pytest.raises(WorkspaceConflict):
            await _adapter(client).put("u1", "projects/a.md", b"x", WorkspaceWriteMeta(origin="chat"), 101)


class TestDelete:
    async def test_delete_patches_then_deletes_the_live_object(self, client):
        bucket = client.bucket.return_value
        blob = bucket.blob.return_value = _Blob()
        order = []
        blob.patch.side_effect = lambda **kw: order.append(("patch", kw))
        blob.delete.side_effect = lambda **kw: order.append(("delete", kw))
        await _adapter(client).delete("u1", "projects/a.md", 101, WorkspaceDeleteMeta(
            deleted_by="chat", deleted_fire=F, renamed_to="projects/b.md"))
        assert order == [("patch", {"if_generation_match": 101}), ("delete", {"if_generation_match": 101})]
        assert blob.metadata == {"deleted_by": "chat", "delete_id": "w1", "deleted_fire": F,
                                 "renamed_to": "projects/b.md"}
        # Two blobs for the live object, never a generation-addressed one (§5.3): patch on the first, delete on a
        # fresh one, because the SDK's delete() sends the generation patch() reloaded.
        assert bucket.blob.call_args_list == [call(KEY), call(KEY)]

    async def test_minimal_delete_metadata(self, client):
        blob = client.bucket.return_value.blob.return_value = _Blob()
        await _adapter(client).delete("u1", "projects/a.md", 101, WorkspaceDeleteMeta(deleted_by="chat"))
        assert blob.metadata == {"deleted_by": "chat", "delete_id": "w1", "deleted_fire": None, "renamed_to": None}

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

    async def test_404_on_our_own_landed_delete_is_success(self, client):
        bucket = client.bucket.return_value
        bucket.blob.return_value = _Blob()
        bucket.blob.return_value.delete.side_effect = NotFound("404")
        bucket.get_blob.return_value = None
        client.list_blobs.return_value = [_Blob(generation=101, metadata={"delete_id": "w1"}, time_deleted=T)]
        await _adapter(client).delete("u1", "projects/a.md", 101, WorkspaceDeleteMeta(deleted_by="chat"))

    async def test_404_without_our_delete_id_is_file_not_found(self, client):
        bucket = client.bucket.return_value
        bucket.blob.return_value = _Blob()
        bucket.blob.return_value.delete.side_effect = NotFound("404")
        bucket.get_blob.return_value = None
        client.list_blobs.return_value = [_Blob(generation=101, metadata={"delete_id": "other"}, time_deleted=T)]
        with pytest.raises(FileNotFoundError):
            await _adapter(client).delete("u1", "projects/a.md", 101, WorkspaceDeleteMeta(deleted_by="chat"))

    async def test_412_with_a_live_object_is_a_conflict(self, client):
        bucket = client.bucket.return_value
        bucket.blob.return_value = _Blob()
        bucket.blob.return_value.patch.side_effect = PreconditionFailed("412")
        bucket.get_blob.return_value = _Blob(generation=102)
        with pytest.raises(WorkspaceConflict):
            await _adapter(client).delete("u1", "projects/a.md", 101, WorkspaceDeleteMeta(deleted_by="chat"))

    async def test_missing_object_is_file_not_found(self, client):
        bucket = client.bucket.return_value
        bucket.blob.return_value = _Blob()
        bucket.blob.return_value.patch.side_effect = NotFound("404")
        bucket.get_blob.return_value = None
        client.list_blobs.return_value = []
        with pytest.raises(FileNotFoundError):
            await _adapter(client).delete("u1", "projects/a.md", 101, WorkspaceDeleteMeta(deleted_by="chat"))


class TestRead:
    async def test_two_gets_metadata_then_media_at_that_generation(self, client):
        bucket = client.bucket.return_value
        bucket.get_blob.return_value = _Blob(generation=101, size=4, metadata={"origin": "chat"})
        media = bucket.blob.return_value = _Blob()
        media.download_as_bytes.return_value = b"data"
        blob = await _adapter(client).read("u1", "projects/a.md")
        bucket.get_blob.assert_called_once_with(KEY, generation=None)
        bucket.blob.assert_called_once_with(KEY, generation=101)
        assert blob.data == b"data" and blob.entry.generation == 101 and blob.entry.origin == "chat"

    async def test_old_generation(self, client):
        bucket = client.bucket.return_value
        bucket.get_blob.return_value = _Blob(generation=99, time_deleted=T)
        bucket.blob.return_value = _Blob()
        blob = await _adapter(client).read("u1", "projects/a.md", generation=99)
        bucket.get_blob.assert_called_once_with(KEY, generation=99)
        assert not blob.entry.live and blob.entry.deleted_at == T

    async def test_missing(self, client):
        client.bucket.return_value.get_blob.return_value = None
        with pytest.raises(FileNotFoundError):
            await _adapter(client).read("u1", "projects/a.md")

    async def test_vanished_between_the_two_gets(self, client):
        bucket = client.bucket.return_value
        bucket.get_blob.return_value = _Blob()
        bucket.blob.return_value = _Blob()
        bucket.blob.return_value.download_as_bytes.side_effect = NotFound("404")
        with pytest.raises(FileNotFoundError):
            await _adapter(client).read("u1", "projects/a.md")


class TestList:
    async def test_exact_name_for_a_file_prefix(self, client):
        client.list_blobs.return_value = [_Blob(), _Blob(name=KEY + ".bak", generation=102)]
        entries = await _adapter(client).list("u1", "projects/a.md")
        client.list_blobs.assert_called_once_with("ws-bucket", prefix=KEY, versions=False)
        assert [e.path for e in entries] == ["projects/a.md"]

    async def test_folder_with_versions(self, client):
        client.list_blobs.return_value = [
            _Blob(name="u1/projects/a.md", generation=101, time_deleted=T,
                  metadata={"origin": "chat", "deleted_by": "chat", "delete_id": "d1",
                            "renamed_to": "projects/c.md", "deleted_fire": F}),
            _Blob(name="u1/projects/b.md", generation=102,
                  metadata={"origin": "background:execute_reminder", "fire": F, "write_id": "w9",
                            "restored_from": "99"}),
        ]
        a, b = await _adapter(client).list("u1", "projects/", include_versions=True)
        client.list_blobs.assert_called_once_with("ws-bucket", prefix="u1/projects/", versions=True)
        assert (a.live, a.deleted_at, a.deleted_by, a.delete_id, a.renamed_to, a.deleted_fire) == (
            False, T, "chat", "d1", "projects/c.md", F)
        assert (b.live, b.origin, b.fire, b.write_id, b.restored_from, b.updated) == (
            True, "background:execute_reminder", F, "w9", 99, T)

    async def test_whole_workspace(self, client):
        client.list_blobs.return_value = []
        await _adapter(client).list("u1", "")
        client.list_blobs.assert_called_once_with("ws-bucket", prefix="u1/", versions=False)


def test_no_client_is_built_at_construction():
    adapter = GcsWorkspaceAdapter("ws-bucket")
    assert adapter._client is None
