"""Drive indexing in the Cabinet: status, toggle, task on toggle (USER_DRIVE_INDEXING_RFC §6, §7.6; RFC Δ2). The card acts
on the user's drive connection (MULTI_DRIVE_RFC §4.4, §11.7: the primary until stage 2), read from the real
DriveConnectionService (`tests/support/cabinet_drive.py`)."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart

from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_index_params import TICK_DEADLINE_S
from src.domain.drive_indexing_status import DriveIndexingStatus
from src.domain.drive_task_keys import DRIVE_SYNC_TASK
from src.ports.task_queue import TaskQueue
from src.services.drive_search_service import DriveSearchService
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from src.composition.connector_registry import ConnectorRegistry
from tests.support.cabinet_drive import CabinetDrive, cabinet_drive

_AUTH = {"Authorization": "Bearer token"}
NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)


async def _drive(connected=True, needs_reconnect=False, indexing=False) -> CabinetDrive:
    """The user's connection: ACTIVE (or flagged) with its grant stored; `connected=False`: a flagged connection whose
    grant is gone (the card reads not connected)."""
    state = ConnectionState.NEEDS_RECONNECT if needs_reconnect else ConnectionState.ACTIVE
    return await cabinet_drive(state=state, credential=connected, indexing=indexing)


def _app(drive=None, search=None, queue=None, user_repo=None):
    session_service = MagicMock()
    session_service.verify_access_token = MagicMock(return_value={"sub": "u1", "account_id": "a1", "role": "owner"})
    app = Quart("test_drive_indexing")
    app.register_blueprint(create_user_cabinet_blueprint(
        invite_service=MagicMock(), session_service=session_service, user_repo=user_repo or MagicMock(),
        fact_repo=MagicMock(), embedding_service=MagicMock(), user_drive_service=None,
        drive_search_service=search, drive_task_queue=queue, connectors=ConnectorRegistry(),
        drive_connections=drive.connections if drive else None,
        oauth_credentials_port=drive.credentials if drive else None,
    ))
    return app


def _search(status):
    search = AsyncMock(spec=DriveSearchService)
    search.indexing_status.return_value = status
    return search


class TestToggle:


    async def test_no_connection(self):
        # the switch is the connection's (MULTI_DRIVE_RFC §4.4): a user without a drive connection has nothing to switch
        async with _app(await cabinet_drive(state=None)).test_client() as client:
            resp = await client.put("/api/drive/indexing", headers=_AUTH, json={"enabled": True})
        assert resp.status_code == 404
