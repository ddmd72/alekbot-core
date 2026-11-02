"""The deleted-files search when the connection lookups fail: it degrades to stored paths, never raises."""
from tests.unit.services.test_drive_search_deleted_mounts import _doc, _service


async def _broken(*args, **kwargs):
    raise RuntimeError("down")


async def test_a_failed_mount_lookup_keeps_the_stored_path_of_a_hit():
    service, mirror, _ = await _service()
    await mirror.save_doc(_doc(1, "fake:d1"))
    service._connections.mounts = _broken
    found = await service.search_deleted("u1", "budget")
    assert "call1.m4a | Calls/call1.m4a |" in found.text


async def test_a_failed_tag_lookup_gives_no_mount_name():
    service, _, _ = await _service()
    service._connections.connection_of_tag = _broken
    assert await service.mount_name_of_tag("u1", "b2") is None
