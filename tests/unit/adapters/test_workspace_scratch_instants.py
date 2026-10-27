"""The scratch TTL clock of `GcsWorkspaceAdapter` (P16): a `scratch/` write stamps `custom_time` with "now", an overwrite
refreshes it, any other folder carries none. The 7-day deletion itself is a bucket lifecycle rule (out of scope, cloud).
The SDK client is the boundary and is scripted."""
from datetime import timedelta

import pytest

import src.adapters.gcs_workspace_adapter as adapter_module
from src.adapters.gcs_workspace_adapter import GcsWorkspaceAdapter
from src.domain.workspace import WorkspaceWriteMeta
from tests.support.clock_instants import OCT_END, UTC_MIDNIGHT, pin_datetime


class _Blob:
    def __init__(self, name: str) -> None:
        self.name, self.generation, self.size = name, 7, 1
        self.metadata: dict = {}
        self.custom_time = None
        self.time_created = UTC_MIDNIGHT.utc
        self.time_deleted = None
        self.uploads: list = []
        self.custom_time_at_upload = "unset"

    def upload_from_string(self, data, content_type=None, if_generation_match=None):
        self.custom_time_at_upload = self.custom_time
        self.uploads.append((data, if_generation_match))


class _Bucket:
    def __init__(self) -> None:
        self.written: dict = {}

    def blob(self, key, generation=None):
        blob = self.written.setdefault(key, _Blob(key))
        return blob


class _Client:
    def __init__(self) -> None:
        self.bucket_obj = _Bucket()

    def bucket(self, name):
        return self.bucket_obj


async def _put(adapter, path: str, gen: int = 0):
    return await adapter.put("u1", path, b"x", WorkspaceWriteMeta(origin="chat"), gen)


@pytest.fixture
def world():
    client = _Client()
    return client, GcsWorkspaceAdapter("ws-bucket", client=client, new_id=lambda: "w")


async def test_scratch_write_carries_the_instant(world, monkeypatch):
    client, adapter = world
    pin_datetime(monkeypatch, adapter_module, UTC_MIDNIGHT)
    await _put(adapter, "scratch/d.md")
    assert client.bucket_obj.written["u1/scratch/d.md"].custom_time_at_upload == UTC_MIDNIGHT.utc


async def test_overwrite_later_refreshes_it(world, monkeypatch):
    client, adapter = world
    pin_datetime(monkeypatch, adapter_module, UTC_MIDNIGHT)
    await _put(adapter, "scratch/d.md")
    pin_datetime(monkeypatch, adapter_module, UTC_MIDNIGHT.utc + timedelta(days=6, hours=23))
    await _put(adapter, "scratch/d.md", 7)
    assert client.bucket_obj.written["u1/scratch/d.md"].custom_time_at_upload == UTC_MIDNIGHT.utc + timedelta(days=6, hours=23)


async def test_instant_is_aware_utc_across_a_month_end(world, monkeypatch):
    client, adapter = world
    pin_datetime(monkeypatch, adapter_module, OCT_END)
    await _put(adapter, "scratch/e.md")
    stamp = client.bucket_obj.written["u1/scratch/e.md"].custom_time_at_upload
    assert stamp.utcoffset() == timedelta(0) and stamp == OCT_END.utc


@pytest.mark.parametrize("path", ["projects/a.md", "references/r.md", "files/f.txt"])
async def test_other_folders_carry_no_custom_time(world, monkeypatch, path):
    client, adapter = world
    pin_datetime(monkeypatch, adapter_module, UTC_MIDNIGHT)
    await _put(adapter, path)
    assert client.bucket_obj.written[f"u1/{path}"].custom_time_at_upload is None
