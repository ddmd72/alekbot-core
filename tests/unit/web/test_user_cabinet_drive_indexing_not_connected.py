"""The Cabinet indexing switch when no drive is wired or connected (MULTI_DRIVE_RFC §4.4): GET reads false, PUT answers
404 "Drive not connected" both when the user has no connection and when the connection vanishes between the read and the
write. Technical ids only."""
from unittest.mock import MagicMock, patch

from quart import Quart

from src.composition.connector_registry import ConnectorRegistry
from src.domain.user_drive import DriveNotConnectedError
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from tests.support.cabinet_drive import CabinetDrive, cabinet_drive

_AUTH = {"Authorization": "Bearer token"}


def _app(drive: CabinetDrive = None) -> Quart:
    session_service = MagicMock()
    session_service.verify_access_token = MagicMock(return_value={"sub": "u1", "account_id": "a1", "role": "owner"})
    app = Quart("test_drive_indexing_not_connected")
    app.register_blueprint(create_user_cabinet_blueprint(
        invite_service=MagicMock(), session_service=session_service, user_repo=MagicMock(), fact_repo=MagicMock(),
        embedding_service=MagicMock(), user_drive=None, drive_search_service=None, drive_task_queue=None,
        connectors=ConnectorRegistry(),
        drive_connections=drive.connections if drive else None,
        oauth_credentials_port=drive.credentials if drive else None,
    ))
    return app


async def test_get_reads_false_when_no_drive_service_is_wired():
    async with _app().test_client() as client:
        response = await client.get("/api/drive/indexing", headers=_AUTH)
        assert response.status_code == 200 and await response.get_json() == {"enabled": False}


async def test_put_with_no_connection_is_404_drive_not_connected():
    drive = await cabinet_drive(state=None)
    async with _app(drive).test_client() as client:
        response = await client.put("/api/drive/indexing", json={"enabled": True}, headers=_AUTH)
        assert response.status_code == 404 and await response.get_json() == {"error": "Drive not connected"}


async def test_put_is_404_when_the_connection_vanishes_before_the_write():
    drive = await cabinet_drive(indexing=False)
    with patch.object(drive.connections, "set_indexing", side_effect=DriveNotConnectedError("conn-1")):
        async with _app(drive).test_client() as client:
            response = await client.put("/api/drive/indexing", json={"enabled": True}, headers=_AUTH)
            assert response.status_code == 404 and await response.get_json() == {"error": "Drive not connected"}
    assert (await drive.stored()).indexing_enabled is False
