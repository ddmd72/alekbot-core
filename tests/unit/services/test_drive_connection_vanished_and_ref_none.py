"""Races and contract edges of slice 1b: a connection that vanishes between the liveness check and the settings write is
"not connected" (indexing and transcription switches), and FileConversionService refuses with the stale-ref line when
the connections service finds no drive ref in a text `drive_ref` called a drive ref. Technical ids only."""
import pytest
from unittest.mock import AsyncMock

from src.domain.drive_ref_refusal import stale_ref_line
from src.domain.user_drive import DriveNotConnectedError, DriveRefRefused
from src.ports.file_storage_port import FileStoragePort
from src.ports.user_drive_port import UserDrivePort
from src.services.file_conversion_service import FileConversionService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.drive_connections import connection_service, seed


class _VanishingRepo(FakeDriveConnectionRepository):
    """A repository whose document is gone by the time `update` runs (the read before it still saw the connection)."""

    async def update(self, user_id, connection_id, **fields):   # type: ignore[no-untyped-def]
        self.connections.pop((user_id, connection_id), None)
        return await super().update(user_id, connection_id, **fields)


@pytest.mark.parametrize("switch", ["set_indexing", "set_transcribe"])
async def test_a_connection_gone_before_the_write_is_not_connected(switch):
    repo = _VanishingRepo()
    await seed(repo, "u1", "conn-1", tag="a1")
    service = connection_service(repo)
    with pytest.raises(DriveNotConnectedError):
        await getattr(service, switch)("u1", "conn-1", True)
    assert repo.connections == {}


async def test_file_conversion_refuses_with_the_stale_line_when_no_ref_is_found():
    repo = FakeDriveConnectionRepository()
    await seed(repo, "u1", "conn-1", tag="a1")
    connections = connection_service(repo)

    async def finds_no_ref(user_id, text):   # type: ignore[no-untyped-def]
        return None

    connections.resolve_ref = finds_no_ref     # the service's contract allows None; the file service must still refuse
    svc = FileConversionService(storage=AsyncMock(spec=FileStoragePort), drive=AsyncMock(spec=UserDrivePort),
                                connections=connections)
    with pytest.raises(DriveRefRefused) as refused:
        await svc.get_drive_item("drive:a1:item-1", "u1")
    assert refused.value.line == stale_ref_line("drive:a1:item-1")
