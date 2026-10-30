"""The drive specialist's preflight reads (DRIVE_SPECIALIST_RFC §5.3): DriveSearchService.search_state over the same
facts a search reads first, UserDriveService.status, and DriveOperations.preflight reading both in parallel."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from src.domain.connection_scope import ConnectionScope
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_connection_status import DriveConnectionStatus
from src.domain.drive_index import MIGRATION_GATE_KEY, DriveSyncState
from src.domain.drive_line import DriveLine
from src.domain.drive_preflight import DrivePreflight
from src.domain.drive_search_line import SearchLine
from src.infrastructure.drive_operations import DriveOperations
from src.ports.drive_index_repository import DriveIndexRepository
from src.ports.embedding_service import EmbeddingService
from src.ports.user_drive_port import UserDrivePort
from src.services.drive_search_service import DriveSearchService
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.drive_connections import connection_service, seed

NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)


async def _search_service(enabled=True, state=None):
    """The search service over the user's one connection (tag a1); the indexing switch is the connection's
    (MULTI_DRIVE_RFC §3)."""
    repo = AsyncMock(spec=DriveIndexRepository)
    repo.get_sync_state.return_value = state
    drive = AsyncMock(spec=UserDrivePort)
    conns = FakeDriveConnectionRepository()
    await seed(conns, "u1", "conn-1", tag="a1", indexing_enabled=enabled)
    svc = DriveSearchService(repo, AsyncMock(spec=EmbeddingService), drive, connection_service(conns),
                             embedding_space="s", clock=lambda: NOW)
    return svc, repo, drive


async def test_search_state_reads_the_gate_facts_and_runs_no_stream():
    state = DriveSyncState(user_id="u1", root_item_id="root", applied_versions={MIGRATION_GATE_KEY: "1"})
    svc, repo, _ = await _search_service(state=state)
    assert await svc.search_state("u1") is SearchLine.ON
    repo.get_sync_state.assert_awaited_once_with(ConnectionScope("u1", "conn-1"))   # connection-scoped (§8)
    repo.get_records.assert_not_called()


async def test_search_state_off_when_indexing_is_off():
    svc, _, _ = await _search_service(enabled=False)
    assert await svc.search_state("u1") is SearchLine.OFF


async def test_user_drive_service_passes_the_connection_through():
    # the state is the connection's, read through DriveConnectionService, not the port's (MULTI_DRIVE_RFC §4.4, §6)
    conns = FakeDriveConnectionRepository()
    await seed(conns, "u1", "conn-1", state=ConnectionState.NEEDS_RECONNECT)
    service = UserDriveService(AsyncMock(spec=UserDrivePort), connection_service(conns))
    assert await service.status("u1") == DriveConnectionStatus(connected=True, needs_reconnect=True)


def _ops(drive=None, search=None):
    return DriveOperations(conversion_service=AsyncMock(spec=FileConversionService), drive_service=drive,
                           drive_search=search)


async def test_preflight_reads_both_lines():
    drive = AsyncMock(spec=UserDriveService)
    drive.status.return_value = DriveConnectionStatus(connected=False)
    search = AsyncMock(spec=DriveSearchService)
    search.search_state.return_value = SearchLine.REBUILDING
    assert await _ops(drive, search).preflight("u1") == DrivePreflight(DriveLine.NOT_CONNECTED, SearchLine.REBUILDING)


async def test_preflight_without_a_drive_is_not_configured_and_reads_nothing():
    search = AsyncMock(spec=DriveSearchService)
    assert await _ops(None, search).preflight("u1") == DrivePreflight(DriveLine.NOT_CONFIGURED, SearchLine.OFF)
    search.search_state.assert_not_called()


async def test_a_failed_read_leaves_its_line_optimistic():
    drive = AsyncMock(spec=UserDriveService)
    drive.status.side_effect = RuntimeError("graph down")
    search = AsyncMock(spec=DriveSearchService)
    search.search_state.side_effect = RuntimeError("firestore down")
    assert await _ops(drive, search).preflight("u1") == DrivePreflight(DriveLine.CONNECTED, SearchLine.ON)


async def test_without_a_search_service_the_search_line_is_off():
    drive = AsyncMock(spec=UserDriveService)
    drive.status.return_value = DriveConnectionStatus(connected=True)
    assert await _ops(drive, None).preflight("u1") == DrivePreflight(DriveLine.CONNECTED, SearchLine.OFF)
