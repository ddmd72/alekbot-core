"""DriveSearchService by connection (MULTI_DRIVE_RFC §6, §8, §9; slice 1b part D): which connection a search, a passages
read and the Cabinet status read; the connection's own indexing switch and status in the gate; a ref of no live
connection raises its code-built line; refs of two drives in one passages call are refused. The index reads here stop
at the gate (a sync state stub keyed by connection scope, part C's port convention)."""
from datetime import datetime, timezone
from typing import Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

from src.domain.connection_scope import ConnectionScope
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_excerpt_range_request import ExcerptRangeRequest
from src.domain.drive_excerpt_request import excerpt_request
from src.domain.drive_index import DriveSyncState
from src.domain.drive_ref_refusal import stale_ref_line
from src.domain.drive_search import STATUS_DISABLED, STATUS_DISCONNECTED, STATUS_OK, DriveSearchRequest
from src.domain.drive_search_line import SearchLine
from src.domain.drive_text_extent import ExtentState, ExtentWhy
from src.domain.user_drive import DrivePathError, DriveRefRefused
from src.ports.embedding_service import EmbeddingService
from src.services.drive_search_service import DriveSearchService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.drive_connection_service_stand_in import StandInDriveConnectionService, seed
from tests.support.handle_keyed_drive import HandleKeyedDrive

NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 1, 2, tzinfo=timezone.utc)


class _GateRepo:
    """The sync-state read of the index, by connection scope; no state stored (the index is not built yet)."""

    def __init__(self) -> None:
        self.states: Dict[ConnectionScope, DriveSyncState] = {}
        self.asked: List[ConnectionScope] = []

    async def get_sync_state(self, scope: ConnectionScope) -> Optional[DriveSyncState]:
        self.asked.append(scope)
        return self.states.get(scope)


@pytest.fixture
async def world():
    class World:
        pass
    w = World()
    w.repo = FakeDriveConnectionRepository()
    w.index = _GateRepo()
    w.service = DriveSearchService(w.index, AsyncMock(spec=EmbeddingService), HandleKeyedDrive(),
                                   StandInDriveConnectionService(w.repo), embedding_space="space-a",
                                   clock=lambda: NOW)
    return w


async def _two(world, *, first_indexing: bool = True):
    one = await seed(world.repo, connection_id="conn-1", tag="a1", indexing_enabled=first_indexing)
    two = await seed(world.repo, connection_id="conn-2", tag="b2", kind="fake_b", primary=False, created_at=T1)
    return one, two


class TestSearchReadsOneConnection:
    async def test_no_connection_is_not_connected(self, world):
        result = await world.service.search("usr-1", DriveSearchRequest(query="plan"))
        assert result.status == STATUS_DISCONNECTED and world.index.asked == []

    async def test_the_primarys_switch_off_is_disabled(self, world):
        await _two(world, first_indexing=False)
        result = await world.service.search("usr-1", DriveSearchRequest(query="plan"))
        assert result.status == STATUS_DISABLED

    async def test_the_primarys_index_is_read(self, world):
        one, _ = await _two(world)
        result = await world.service.search("usr-1", DriveSearchRequest(query="plan"))
        assert result.status == STATUS_OK and result.envelope.building and not result.envelope.reconnect_needed
        assert world.index.asked == [one.scope]

    async def test_a_folder_ref_reads_its_own_connection(self, world):
        _, two = await _two(world)
        await world.service.search("usr-1", DriveSearchRequest(query="plan", folder="drive:b2:folder-1"))
        assert world.index.asked == [two.scope]

    async def test_a_stale_folder_ref_raises_its_line(self, world):
        await _two(world)
        with pytest.raises(DriveRefRefused) as refused:
            await world.service.search("usr-1", DriveSearchRequest(query="plan", folder="drive:folder-1"))
        assert refused.value.line == stale_ref_line("drive:folder-1")

    async def test_a_flagged_primary_keeps_its_index_searchable(self, world):
        flagged = await seed(world.repo, state=ConnectionState.NEEDS_RECONNECT)
        result = await world.service.search("usr-1", DriveSearchRequest(query="plan"))
        assert result.status == STATUS_OK and result.envelope.building and result.envelope.reconnect_needed
        assert world.index.asked == [flagged.scope]

    async def test_the_search_line_is_the_primarys(self, world):
        await _two(world, first_indexing=False)
        assert await world.service.search_state("usr-1") is SearchLine.OFF
        assert await world.service.search_state("usr-2") is SearchLine.ON   # no drive: the drive line says it


class TestPassagesByRef:
    async def test_a_range_reads_the_refs_connection_and_tags_the_result(self, world):
        _, two = await _two(world)
        result = await world.service.excerpt_range("usr-1", ExcerptRangeRequest("drive:b2:item-1", 1, 2))
        assert world.index.asked == [two.scope] and result.ref_tag == "b2" and result.building

    async def test_a_stale_range_ref_raises_its_line(self, world):
        await _two(world)
        with pytest.raises(DriveRefRefused) as refused:
            await world.service.excerpt_range("usr-1", ExcerptRangeRequest("drive:item-1", 1, 2))
        assert refused.value.line == stale_ref_line("drive:item-1")

    async def test_files_of_two_drives_are_refused(self, world):
        await _two(world)
        with pytest.raises(DrivePathError):
            await world.service.excerpts("usr-1", excerpt_request("q", file_refs=["drive:a1:x", "drive:b2:y"]))

    async def test_no_connection_answers_not_connected(self, world):
        result = await world.service.excerpts("usr-1", excerpt_request("q"))
        assert result.status == STATUS_DISCONNECTED

    async def test_the_extent_gate_is_the_connections(self, world):
        one = await seed(world.repo, indexing_enabled=False)
        extent = await world.service.text_extent(one, "item-1")
        assert extent.state is ExtentState.NOT_INDEXED and extent.why is ExtentWhy.OFF


class TestStatusAndDeleted:
    async def test_the_cabinet_status_without_a_connection(self, world):
        status = await world.service.indexing_status("usr-1")
        assert status.enabled is False and status.counters == {}

    async def test_the_cabinet_status_reads_the_primarys_switch(self, world):
        await _two(world)
        status = await world.service.indexing_status("usr-1")
        assert status.enabled is True and status.building is True

    async def test_the_deleted_search_without_a_mirror_opens_nothing(self, world):
        await _two(world)
        found = await world.service.search_deleted("usr-1", "")
        assert found.items == [] and found.ref_tag == ""
