"""The untrusted mark rides in the object metadata of a workspace version (EXTERNAL_TASK_QUEUE_RFC §5.9): written on put
when the write carries it, read back on every entry, absent otherwise. Wire level: the raw object metadata of the fake
bucket."""
from src.adapters.gcs_workspace_adapter import GcsWorkspaceAdapter
from src.domain.workspace import WorkspaceWriteMeta
from tests.support.gcs_fake import FakeGcs

BUCKET = "workspace-test"
P = "references/external_tasks/2026-11-23-a1b2c3d4.md"
MARK = "external_task:a1b2c3d4-0000"


def _world():
    fake = FakeGcs(buckets={BUCKET: "versioned"})
    return fake, GcsWorkspaceAdapter(BUCKET, client=fake.client())


async def test_a_marked_write_stores_the_mark_and_every_entry_reads_it():
    fake, store = _world()
    written = await store.put("usr-1", P, b"result", WorkspaceWriteMeta(origin="background:external_task:a1", untrusted=MARK), 0)
    assert written.untrusted == MARK
    assert fake.live(BUCKET, f"usr-1/{P}").metadata["untrusted"] == MARK
    assert (await store.read("usr-1", P)).entry.untrusted == MARK
    assert [e.untrusted for e in await store.list("usr-1", "")] == [MARK]


async def test_an_unmarked_write_has_no_mark_and_no_metadata_key():
    fake, store = _world()
    written = await store.put("usr-1", "projects/a.md", b"notes", WorkspaceWriteMeta(origin="chat"), 0)
    assert written.untrusted is None
    assert "untrusted" not in fake.live(BUCKET, "usr-1/projects/a.md").metadata
    assert (await store.read("usr-1", "projects/a.md")).entry.untrusted is None


async def test_each_version_has_its_own_mark():
    _, store = _world()
    first = await store.put("usr-1", P, b"v1", WorkspaceWriteMeta(origin="chat"), 0)
    second = await store.put("usr-1", P, b"v2", WorkspaceWriteMeta(origin="chat", untrusted=MARK), first.generation)
    versions = {e.generation: e.untrusted for e in await store.list("usr-1", P, include_versions=True)}
    assert versions == {first.generation: None, second.generation: MARK}
