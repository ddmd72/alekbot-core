"""Drive status/disconnect in the Cabinet (docs/10_rfcs/USER_DRIVE_RFC.md §4.1)."""
from unittest.mock import AsyncMock, MagicMock

from quart import Quart

from src.ports.user_drive_port import UserDrivePort
from src.web.user_cabinet_app import create_user_cabinet_blueprint

_AUTH = {"Authorization": "Bearer token"}


def _app(user_drive):
    session_service = MagicMock()
    session_service.verify_access_token = MagicMock(return_value={"sub": "u1", "account_id": "a1", "role": "owner"})
    app = Quart("test_drive_cabinet")
    app.register_blueprint(create_user_cabinet_blueprint(
        invite_service=MagicMock(), session_service=session_service, user_repo=MagicMock(),
        fact_repo=MagicMock(), embedding_service=MagicMock(), user_drive=user_drive,
    ))
    return app


def _drive(connected=True):
    drive = MagicMock(spec=UserDrivePort)
    drive.is_connected = AsyncMock(return_value=connected)
    drive.disconnect = AsyncMock()
    drive.display_name = "OneDrive"
    return drive


class TestDriveCabinet:
    async def test_status_connected(self):
        async with _app(_drive()).test_client() as client:
            resp = await client.get("/api/drive/status", headers=_AUTH)
        assert (await resp.get_json()) == {"connected": True, "provider": "OneDrive"}

    async def test_status_without_drive_wired(self):
        async with _app(None).test_client() as client:
            resp = await client.get("/api/drive/status", headers=_AUTH)
        assert (await resp.get_json()) == {"connected": False, "provider": ""}

    async def test_disconnect(self):
        drive = _drive()
        async with _app(drive).test_client() as client:
            resp = await client.delete("/api/drive/disconnect", headers=_AUTH)
        assert resp.status_code == 200
        drive.disconnect.assert_awaited_once_with("u1")

    async def test_disconnect_without_drive_wired_is_501(self):
        async with _app(None).test_client() as client:
            resp = await client.delete("/api/drive/disconnect", headers=_AUTH)
        assert resp.status_code == 501
