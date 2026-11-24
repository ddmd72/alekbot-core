"""An object written while workspace files carried an `untrusted` metadata key still reads fine: the key is ignored
(the entry has no such field), and a new version written over it does not copy the key. Wire level: the raw object
metadata of the fake bucket."""
from src.adapters.gcs_workspace_adapter import GcsWorkspaceAdapter
from src.domain.workspace import WorkspaceWriteMeta
from tests.support.gcs_fake import FakeGcs

BUCKET = "workspace-test"
P = "references/external_tasks/2026-11-23-a1b2c3d4.md"


async def test_an_old_object_with_an_untrusted_key_reads_and_lists_and_its_next_version_drops_the_key():
    fake = FakeGcs(buckets={BUCKET: "versioned"})
    store = GcsWorkspaceAdapter(BUCKET, client=fake.client())
    first = await store.put("usr-1", P, b"result", WorkspaceWriteMeta(origin="background:external_task:a1"), 0)
    fake.live(BUCKET, f"usr-1/{P}").metadata["untrusted"] = "external_task:a1b2c3d4-0000"

    blob = await store.read("usr-1", P)
    assert blob.data == b"result" and blob.entry.origin == "background:external_task:a1"
    assert not hasattr(blob.entry, "untrusted")
    assert [e.path for e in await store.list("usr-1", "")] == [P]

    await store.put("usr-1", P, b"v2", WorkspaceWriteMeta(origin="chat"), first.generation)
    assert "untrusted" not in fake.live(BUCKET, f"usr-1/{P}").metadata
