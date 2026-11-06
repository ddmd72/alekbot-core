"""The Cabinet status carries the scope change's progress (DRIVE_SCOPE_WALK_RFC §8): the five scope fields read from the
stored sync state, `building` true while a diff walk runs, and false for an idle index whose only pending re-listing
belongs to an inaccessible folder. Invented cast only."""
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_index import DriveSyncState
from src.ports.embedding_service import EmbeddingService
from src.services.drive_search_service import DriveSearchService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.drive_connections import connection_service, seed
from tests.support.fake_drive_adapter import FakeDriveAdapter
from tests.unit.services.drive_transcript_fakes import record

NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)
ROOT = "drive-top"


@pytest.fixture
async def world():
    class World:
        pass
    w = World()
    w.repo = FakeDriveConnectionRepository()
    w.index = InMemoryDriveIndexRepository()
    w.service = DriveSearchService(w.index, AsyncMock(spec=EmbeddingService), FakeDriveAdapter("fake_a"),
                                   connection_service(w.repo), embedding_space="space-a", clock=lambda: NOW)
    w.conn = await seed(w.repo, connection_id="conn-1", tag="a1", indexing_enabled=True)
    return w


def _state(world, **fields) -> DriveSyncState:
    return replace(DriveSyncState(user_id=world.conn.user_id, connection_id="conn-1", root_item_id=ROOT), **fields)


async def test_the_five_fields_are_read_from_the_sync_state(world):
    await world.index.save_sync_state(_state(world, removing_roots=("f-rd", ROOT), walking=True,
                                             walk_listed=10, walk_blocked=2))
    status = await world.service.indexing_status("usr-1", connection_id="conn-1")
    assert (status.scope_removing, status.scope_removing_outside) == (1, True)
    assert (status.scope_walking, status.scope_listed, status.scope_blocked) == (True, 10, 2)


async def test_a_removed_folder_alone_counts_without_the_outside_flag(world):
    await world.index.save_sync_state(_state(world, removing_roots=("f-coimbra",)))
    status = await world.service.indexing_status("usr-1", connection_id="conn-1")
    assert (status.scope_removing, status.scope_removing_outside) == (1, False)
    assert (status.scope_walking, status.scope_listed, status.scope_blocked) == (False, 0, 0)


async def test_an_idle_state_has_no_scope_progress(world):
    await world.index.save_sync_state(_state(world))
    status = await world.service.indexing_status("usr-1", connection_id="conn-1")
    assert (status.scope_removing, status.scope_removing_outside, status.scope_walking,
            status.scope_listed, status.scope_blocked) == (0, False, False, 0, 0)


async def test_building_is_true_while_a_walk_runs_and_false_when_idle(world):
    await world.index.save_sync_state(_state(world))
    assert (await world.service.indexing_status("usr-1", connection_id="conn-1")).building is False
    await world.index.save_sync_state(_state(world, walking=True))
    assert (await world.service.indexing_status("usr-1", connection_id="conn-1")).building is True


async def test_a_pending_listing_of_an_inaccessible_folder_is_not_building(world):
    await world.index.save_sync_state(_state(world))
    await world.index.upsert_records([record(
        "f-springfield", kind="folder", reobserve_pending=True, inaccessible_since=NOW,
        user_id=world.conn.user_id, connection_id="conn-1")])
    status = await world.service.indexing_status("usr-1", connection_id="conn-1")
    assert status.building is False
