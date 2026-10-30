"""The Cabinet's drive index block (USER_DRIVE_INDEXING_RFC §6): whether indexing can run on this deployment
(`available`: a drive queue is wired; the first deploy pass has none), when access was lost, and whether a stored
index is still kept, so the card never promises indexing or a 30-day keep that is not there."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart

from src.domain.drive_index_params import ACCESS_LOSS_TTL
from src.domain.drive_indexing_status import DriveIndexingStatus
from src.domain.user_drive import DriveConnection
from src.ports.task_queue import TaskQueue
from src.ports.user_drive_port import UserDrivePort
from src.services.drive_search_service import DriveSearchService
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from src.composition.connector_registry import ConnectorRegistry

_AUTH = {"Authorization": "Bearer token"}


def _app(status, queue):
    drive = MagicMock(spec=UserDrivePort)
    drive.connection = AsyncMock(return_value=DriveConnection(connected=False))
    drive.display_name = "OneDrive"
    search = AsyncMock(spec=DriveSearchService)
    search.indexing_status.return_value = status
    session_service = MagicMock()
    session_service.verify_access_token = MagicMock(return_value={"sub": "u1", "account_id": "a1", "role": "owner"})
    app = Quart("test_drive_index_card")
    app.register_blueprint(create_user_cabinet_blueprint(
        invite_service=MagicMock(), session_service=session_service, user_repo=MagicMock(),
        fact_repo=MagicMock(), embedding_service=MagicMock(), user_drive=drive,
        drive_search_service=search, drive_task_queue=queue, connectors=ConnectorRegistry(),
    ))
    return app


async def _indexing(status, queue=None):
    async with _app(status, queue).test_client() as client:
        return (await (await client.get("/api/drive/status", headers=_AUTH)).get_json())["indexing"]


@pytest.mark.parametrize("wired", [True, False])
async def test_available_says_whether_a_drive_queue_is_wired(wired):
    queue = AsyncMock(spec=TaskQueue) if wired else None
    block = await _indexing(DriveIndexingStatus(enabled=False, counters={}), queue)
    assert block["available"] is wired


async def test_access_lost_at_and_a_kept_index_inside_the_grace_period():
    lost = datetime.now(timezone.utc) - timedelta(days=2)
    block = await _indexing(DriveIndexingStatus(enabled=True, counters={"indexed": 4, "carded": 4},
                                                access_lost_at=lost))
    assert block["access_lost_at"] == lost.isoformat()
    assert block["index_kept"] is True


async def test_past_the_grace_period_the_index_is_not_kept():
    lost = datetime.now(timezone.utc) - ACCESS_LOSS_TTL - timedelta(days=1)
    block = await _indexing(DriveIndexingStatus(enabled=True, counters={"indexed": 4, "carded": 4},
                                                access_lost_at=lost))
    assert block["index_kept"] is False


async def test_no_index_is_never_kept():
    block = await _indexing(DriveIndexingStatus(enabled=True, counters={}, building=True))
    assert block["access_lost_at"] is None and block["index_kept"] is False
