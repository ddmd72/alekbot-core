"""A stored object over the file cap is refused on its metadata, before any media byte is fetched."""
import pytest

from src.adapters.gcs_workspace_adapter import GcsWorkspaceAdapter
from src.domain.exceptions import WorkspaceFileTooLarge, WorkspaceRefused
from src.domain.workspace import MAX_FILE_BYTES, WorkspaceWriteMeta

BUCKET = "workspace-bucket"
CHAT = WorkspaceWriteMeta(origin="chat")


@pytest.fixture
def fake(kit_gcs):
    return kit_gcs(buckets={BUCKET: "versioned"})


def _raw(fake, name, data):
    blob = fake.client().bucket(BUCKET).blob(name)
    blob.upload_from_string(data, content_type="text/markdown")
    return blob


async def test_an_object_over_the_cap_is_refused_without_a_download(fake, monkeypatch):
    store = GcsWorkspaceAdapter(BUCKET, client=fake.client())
    _raw(fake, "u1/projects/big.md", b"x" * (MAX_FILE_BYTES + 1))

    def tripwire(*args, **kwargs):
        raise AssertionError("the media was fetched")

    monkeypatch.setattr(type(fake.client().bucket(BUCKET).blob("u1/projects/big.md")), "download_as_bytes", tripwire)
    with pytest.raises(WorkspaceRefused) as raised:
        await store.read("u1", "projects/big.md")
    assert raised.value.reason == "caps" and "file over 10.0 MB" in str(raised.value)


async def test_a_smaller_cap_per_read_carries_the_size_and_the_limit(fake):
    store = GcsWorkspaceAdapter(BUCKET, client=fake.client())
    _raw(fake, "u1/projects/mid.md", b"x" * 2000)
    with pytest.raises(WorkspaceFileTooLarge) as raised:
        await store.read("u1", "projects/mid.md", max_bytes=1000)
    assert (raised.value.size, raised.value.limit) == (2000, 1000)
    assert len((await store.read("u1", "projects/mid.md", max_bytes=2000)).data) == 2000


async def test_an_object_at_the_cap_is_read(fake):
    store = GcsWorkspaceAdapter(BUCKET, client=fake.client())
    _raw(fake, "u1/projects/edge.md", b"x" * MAX_FILE_BYTES)
    assert len((await store.read("u1", "projects/edge.md")).data) == MAX_FILE_BYTES
