"""One contract for both WorkspaceStorePort implementations (AGENT_WORKSPACE_RFC §5.3, §17): the in-memory fake the
service and handler tests use, and GcsWorkspaceAdapter over the SDK-surface emulator. A drift between them fails here."""
import pytest

from google.api_core.exceptions import NotFound, PreconditionFailed

from src.adapters.gcs_workspace_adapter import GcsWorkspaceAdapter
from src.domain.exceptions import WorkspaceConflict
from src.domain.workspace import WorkspaceDeleteMeta, WorkspaceWriteMeta
from tests.unit.adapters.gcs_workspace_emulator import FakeGcsClient
from tests.unit.services.workspace_fakes import InMemoryWorkspaceStore

CHAT = WorkspaceWriteMeta(origin="chat")
P = "projects/a.md"
F = "reminder:1729401234567@2026-10-20T06:00:00+00:00"


@pytest.fixture(params=["fake", "gcs"])
def store(request):
    if request.param == "fake":
        return InMemoryWorkspaceStore()
    return GcsWorkspaceAdapter("ws-bucket", client=FakeGcsClient())


async def test_create_then_read(store):
    e = await store.put("u1", P, b"v1", CHAT, 0)
    blob = await store.read("u1", P)
    assert blob.data == b"v1"
    assert (blob.entry.generation, blob.entry.origin, blob.entry.live, blob.entry.size) == (e.generation, "chat", True, 2)


async def test_create_on_an_existing_path_conflicts(store):
    await store.put("u1", P, b"v1", CHAT, 0)
    with pytest.raises(WorkspaceConflict):
        await store.put("u1", P, b"v2", CHAT, 0)


async def test_overwrite_keeps_the_previous_version(store):
    e1 = await store.put("u1", P, b"v1", CHAT, 0)
    e2 = await store.put("u1", P, b"v2", CHAT, e1.generation)
    versions = {v.generation: v for v in await store.list("u1", P, include_versions=True)}
    assert set(versions) == {e1.generation, e2.generation}
    assert not versions[e1.generation].live and versions[e1.generation].deleted_at is not None
    assert versions[e2.generation].live
    assert (await store.read("u1", P, generation=e1.generation)).data == b"v1"
    assert [v.generation for v in await store.list("u1", P)] == [e2.generation]


async def test_stale_overwrite_conflicts(store):
    e1 = await store.put("u1", P, b"v1", CHAT, 0)
    await store.put("u1", P, b"v2", CHAT, e1.generation)
    with pytest.raises(WorkspaceConflict):
        await store.put("u1", P, b"v3", CHAT, e1.generation)


async def test_delete_keeps_a_noncurrent_version(store):
    e = await store.put("u1", P, b"v1", CHAT, 0)
    await store.delete("u1", P, e.generation, WorkspaceDeleteMeta(
        deleted_by="background:execute_reminder", deleted_fire=F, renamed_to="projects/b.md"))
    with pytest.raises(FileNotFoundError):
        await store.read("u1", P)
    [v] = await store.list("u1", P, include_versions=True)
    assert (v.live, v.deleted_by, v.deleted_fire, v.renamed_to) == (
        False, "background:execute_reminder", F, "projects/b.md")
    assert v.delete_id and v.deleted_at is not None
    assert (await store.read("u1", P, generation=e.generation)).data == b"v1"


async def test_stale_delete_conflicts_and_leaves_the_file(store):
    e1 = await store.put("u1", P, b"v1", CHAT, 0)
    await store.put("u1", P, b"v2", CHAT, e1.generation)
    with pytest.raises(WorkspaceConflict):
        await store.delete("u1", P, e1.generation, WorkspaceDeleteMeta(deleted_by="chat"))
    assert (await store.read("u1", P)).data == b"v2"


async def test_delete_of_a_missing_file(store):
    with pytest.raises(FileNotFoundError):
        await store.delete("u1", P, 5, WorkspaceDeleteMeta(deleted_by="chat"))


async def test_exact_names_for_a_file_prefix(store):
    await store.put("u1", P, b"a", CHAT, 0)
    await store.put("u1", P + ".bak", b"b", CHAT, 0)
    assert [e.path for e in await store.list("u1", P)] == [P]
    assert sorted(e.path for e in await store.list("u1", "projects/")) == [P, P + ".bak"]
    assert len(await store.list("u1", "")) == 2


async def test_users_are_isolated(store):
    await store.put("u1", P, b"a", CHAT, 0)
    assert await store.list("u2", "") == []
    with pytest.raises(FileNotFoundError):
        await store.read("u2", P)


async def test_unknown_generation_is_missing(store):
    await store.put("u1", P, b"a", CHAT, 0)
    with pytest.raises(FileNotFoundError):
        await store.read("u1", P, generation=7)


async def test_write_meta_round_trips(store):
    await store.put("u1", P, b"a", WorkspaceWriteMeta(origin="voice:send_to_chat", fire=F, restored_from=55), 0)
    [e] = await store.list("u1", P)
    assert (e.origin, e.fire, e.restored_from) == ("voice:send_to_chat", F, 55)
    assert e.write_id


async def test_retried_write_that_landed_is_success():
    client = FakeGcsClient()
    adapter = GcsWorkspaceAdapter("ws-bucket", client=client)
    client.lose_next_write_response = True
    e = await adapter.put("u1", P, b"v1", CHAT, 0)
    assert (await adapter.read("u1", P)).entry.generation == e.generation


@pytest.mark.parametrize("lost_as", [PreconditionFailed, NotFound])
async def test_retried_delete_that_landed_is_success(lost_as):
    client = FakeGcsClient()
    adapter = GcsWorkspaceAdapter("ws-bucket", client=client)
    e = await adapter.put("u1", P, b"v1", CHAT, 0)
    client.lose_next_delete_response_as = lost_as
    await adapter.delete("u1", P, e.generation, WorkspaceDeleteMeta(deleted_by="chat"))
    with pytest.raises(FileNotFoundError):
        await adapter.read("u1", P)
    [v] = await adapter.list("u1", P, include_versions=True)
    assert not v.live and v.delete_id


async def test_failed_delete_leaves_no_stale_metadata_on_the_next_one():
    client = FakeGcsClient()
    adapter = GcsWorkspaceAdapter("ws-bucket", client=client)
    e = await adapter.put("u1", P, b"v1", CHAT, 0)
    with pytest.raises(WorkspaceConflict):  # patches the live version, then the delete never lands
        await adapter.delete("u1", P, e.generation + 1, WorkspaceDeleteMeta(deleted_by="chat", renamed_to="x.md"))
    await adapter.delete("u1", P, e.generation, WorkspaceDeleteMeta(deleted_by="chat"))
    [v] = await adapter.list("u1", P, include_versions=True)
    assert v.renamed_to is None


def test_the_emulator_erases_on_a_generation_addressed_delete():
    """Guards the guard: the emulator models the trap, so the contract above would catch an adapter that used it."""
    client = FakeGcsClient()
    blob = client.bucket("b").blob("u1/" + P)
    blob.metadata = {}
    blob.upload_from_string(b"v1", if_generation_match=0)
    client.bucket("b").blob("u1/" + P, generation=blob.generation).delete()
    assert client.list_blobs("b", prefix="u1/", versions=True) == []
