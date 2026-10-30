"""The fan-out gives every enabled user a sync state (USER_DRIVE_INDEXING_RFC §7.1, §12.1): a minimal one, created only
if none exists, dated by the fan-out. Staleness then covers a user whose ticks never land (indexer unreachable,
401/404/503), and the tick, search and the Cabinet treat that state exactly like no state yet."""
import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_index import CURRENT_VERSIONS, DriveSyncState, EpochPhase
from src.domain.drive_index_params import STALE_ALERT_AFTER
from src.domain.drive_search import DriveSearchRequest
from src.services.drive_search_service import DriveSearchService

from .drive_index_fakes import ROOT, T0
from .test_drive_indexing_service import EXTRACTOR_VERSIONS, World
from .test_drive_sync_scheduler_service import env  # noqa: F401  (the scheduler test environment)
from .drive_index_fakes import U_SCOPE


@pytest.fixture
def fan(env):  # noqa: F811
    return env


async def test_an_enabled_user_without_a_state_gets_a_minimal_one(fan):
    await fan.svc.fan_out()
    state = await fan.repo.get_sync_state(U_SCOPE)
    assert state == DriveSyncState(user_id="u", connection_id="u", created_at=fan.clock.now)


async def test_an_existing_state_is_never_replaced(fan):
    await fan.repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u", epoch=4, cursor="c", created_at=T0))
    fan.clock.advance(3600)
    await fan.svc.fan_out()
    state = await fan.repo.get_sync_state(U_SCOPE)
    assert (state.epoch, state.cursor, state.created_at) == (4, "c", T0)


async def test_a_disabled_user_gets_no_state(fan):
    fan.users.enabled["u"] = False
    await fan.svc.fan_out()
    assert await fan.repo.get_sync_state(U_SCOPE) is None


async def test_ticks_that_never_land_raise_one_staleness_alert(fan):
    for _ in range(int(STALE_ALERT_AFTER.total_seconds() // 600) + 6):
        await fan.svc.fan_out()
        fan.clock.advance(600)
    assert len(fan.alerts.posts) == 1 and "never" in fan.alerts.posts[0]


async def test_create_if_absent_reports_whether_it_wrote():
    repo = InMemoryDriveIndexRepository()
    assert await repo.create_sync_state_if_absent(DriveSyncState(user_id="u", connection_id="u", created_at=T0)) is True
    assert await repo.create_sync_state_if_absent(DriveSyncState(user_id="u", connection_id="u", epoch=9)) is False
    assert (await repo.get_sync_state(U_SCOPE)).epoch == 0


async def test_a_tick_onboards_from_a_minimal_state_and_keeps_its_date():
    w = World()
    doc = w.drive.add("a.pdf")
    created = T0.replace(hour=1)
    await w.repo.create_sync_state_if_absent(DriveSyncState(user_id="u", connection_id="u", created_at=created))
    await w.settle()
    state = await w.repo.get_sync_state(U_SCOPE)
    assert state.created_at == created and state.epoch == 1 and state.epoch_phase is EpochPhase.IDLE
    assert dict(state.applied_versions) == {**CURRENT_VERSIONS, **EXTRACTOR_VERSIONS} and state.root_item_id == ROOT
    assert doc in w.carded()


async def test_the_cabinet_and_search_say_building_for_a_minimal_state():
    w = World()
    await w.repo.create_sync_state_if_absent(DriveSyncState(user_id="u", connection_id="u", created_at=T0))
    search = DriveSearchService(w.repo, w.emb, w.users, w.drive, clock=w.clock, nonce=lambda: "n",
                                embedding_space="test-space/8")
    status = await search.indexing_status("u")
    assert status.enabled and status.building
    result = await search.search("u", DriveSearchRequest(query="a"))
    assert result.envelope.building and result.cards == ()


async def test_firestore_create_if_absent_is_a_document_create(mock_env_config):
    from unittest.mock import AsyncMock

    from google.api_core.exceptions import AlreadyExists

    from src.adapters.firestore_drive_index_repo import state_to_doc
    from tests.unit.adapters.firestore_drive_fakes import STATES, Db, make_repo

    db = Db()
    repo = make_repo(db, mock_env_config)
    doc = db.cols[STATES].document("u")
    doc.create = AsyncMock()
    minimal = DriveSyncState(user_id="u", connection_id="u", created_at=T0)
    assert await repo.create_sync_state_if_absent(minimal) is True
    doc.create.assert_awaited_once_with(state_to_doc(minimal))
    doc.create.side_effect = AlreadyExists("there")
    assert await repo.create_sync_state_if_absent(minimal) is False
    doc.set.assert_not_awaited()


async def test_a_lost_grant_after_the_grace_purge_gets_no_new_state_and_no_second_notice(fan):
    """Only a working connection gets a minimal state: a lost grant with no state is what the gate treats as nothing
    to do, so the purge at the end of the grace period is not undone and the one notice is not sent again."""
    from datetime import timedelta

    from src.domain.drive_index_params import ACCESS_LOSS_TTL
    from src.domain.user_drive import DriveConnection

    await fan.repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u", root_item_id=ROOT, created_at=T0))
    fan.drive.per_user["u"] = DriveConnection(connected=True, needs_reconnect=True)
    await fan.svc.fan_out()
    assert len(fan.notifier.calls) == 1
    fan.clock.advance((ACCESS_LOSS_TTL + timedelta(days=1)).total_seconds())
    await fan.repo.delete_sync_state(U_SCOPE)         # the tick's grace-period purge
    for _ in range(3):
        await fan.svc.fan_out()
        fan.clock.advance(600)
    assert await fan.repo.get_sync_state(U_SCOPE) is None
    assert len(fan.notifier.calls) == 1


async def test_a_disconnected_user_gets_no_minimal_state(fan):
    from src.domain.user_drive import DriveConnection

    fan.drive.per_user["u"] = DriveConnection(connected=False)
    await fan.svc.fan_out()
    assert await fan.repo.get_sync_state(U_SCOPE) is None
