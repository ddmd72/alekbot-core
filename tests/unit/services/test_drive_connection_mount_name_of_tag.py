"""DriveConnectionService.mount_name_of_tag: the mount name a ref tag names, for the owner-notes source of the
deleted-files search."""
from dataclasses import replace
from datetime import datetime, timezone

from src.domain.drive_connection import DriveConnection
from src.domain.drive_connection_state import ConnectionState
from src.utils.logger import logger
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.drive_connections import connection_service

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


def _connection(connection_id: str, tag: str, name: str, identity: str) -> DriveConnection:
    return DriveConnection(connection_id=connection_id, user_id="u1", account_id="a", credential_id=f"cred-{tag}",
                           kind="fake", drive_identity=identity, ref_tag=tag, mount_name=name,
                           indexing_enabled=True, created_at=T0)


async def _broken(*args, **kwargs):
    raise RuntimeError("down")


async def _service():
    repo = FakeDriveConnectionRepository()
    await repo.create(_connection("conn-1", "a1", "Drive", "fake:d1"))
    await repo.create(_connection("conn-2", "b2", "Acme", "fake:d2"))
    return connection_service(repo)


async def test_a_tag_resolves_to_its_mount_name_and_an_unknown_or_empty_tag_to_none():
    service = await _service()
    assert await service.mount_name_of_tag("u1", "b2") == "Acme"
    assert await service.mount_name_of_tag("u1", "zz9") is None
    assert await service.mount_name_of_tag("u1", "") is None


async def test_a_failed_tag_lookup_gives_no_mount_name():
    service = await _service()
    service.connection_of_tag = _broken
    assert await service.mount_name_of_tag("u1", "b2") is None


async def test_a_disconnected_connection_still_gives_its_last_name():
    repo = FakeDriveConnectionRepository()
    await repo.create(replace(_connection("conn-1", "a1", "Old drive", "fake:d1"), state=ConnectionState.DISCONNECTED,
                              disconnected_at=T0))
    assert await connection_service(repo).mount_name_of_tag("u1", "a1") == "Old drive"


async def test_an_empty_tag_is_none_without_a_lookup():
    service = await _service()
    calls = []

    async def spy(*args, **kwargs):
        calls.append(args)

    service.connection_of_tag = spy
    assert await service.mount_name_of_tag("u1", "") is None
    assert calls == []


async def test_a_failed_lookup_logs_a_warning(monkeypatch):
    service = await _service()
    service.connection_of_tag = _broken
    warnings = []
    monkeypatch.setattr(logger, "warning", warnings.append)
    assert await service.mount_name_of_tag("u1", "b2") is None
    assert len(warnings) == 1 and "[DriveConnection] mount name lookup failed" in warnings[0]
