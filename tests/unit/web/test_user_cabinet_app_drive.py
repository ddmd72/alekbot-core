"""Drive status/disconnect in the Cabinet (docs/10_rfcs/USER_DRIVE_RFC.md §4.1), on the user's drive connection
(MULTI_DRIVE_RFC §4.3, §4.4, §11.7; `tests/support/cabinet_drive.py`)."""
from unittest.mock import AsyncMock, MagicMock

from quart import Quart

from src.domain.drive_connection_state import ConnectionState
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from src.composition.connector_registry import ConnectorRegistry
from tests.support.cabinet_drive import cabinet_drive

_AUTH = {"Authorization": "Bearer token"}


def _app(drive):
    session_service = MagicMock()
    session_service.verify_access_token = MagicMock(return_value={"sub": "u1", "account_id": "a1", "role": "owner"})
    user_repo = MagicMock()
    user_repo.get_user = AsyncMock(return_value=None)
    app = Quart("test_drive_cabinet")
    app.register_blueprint(create_user_cabinet_blueprint(
        invite_service=MagicMock(), session_service=session_service, user_repo=user_repo,
        fact_repo=MagicMock(), embedding_service=MagicMock(), user_drive=None, connectors=ConnectorRegistry(),
        drive_connections=drive.connections if drive else None,
        oauth_credentials_port=drive.credentials if drive else None,
        credential_revocation=drive.revocation if drive else None,
    ))
    return app


class TestDriveCabinet:
    async def test_status_connected(self):
        drive = await cabinet_drive()
        async with _app(drive).test_client() as client:
            resp = await client.get("/api/drive/status", headers=_AUTH)
        assert (await resp.get_json()) == {"connected": True, "provider": "OneDrive", "needs_reconnect": False,
                                           "credential_id": drive.connection.credential_id}   # §5.2 Reconnect link

    async def test_status_without_drive_wired(self):
        async with _app(None).test_client() as client:
            resp = await client.get("/api/drive/status", headers=_AUTH)
        assert (await resp.get_json()) == {"connected": False, "provider": ""}

    async def test_disconnect(self):
        # MULTI_DRIVE_RFC §4.3, §4.4: the connection ends (DISCONNECTED) and its grant is removed under the revoke rule
        drive = await cabinet_drive()
        async with _app(drive).test_client() as client:
            resp = await client.delete("/api/drive/disconnect", headers=_AUTH)
        assert resp.status_code == 200
        assert (await drive.stored()).state is ConnectionState.DISCONNECTED
        assert await drive.has_grant() is False

    async def test_disconnect_without_drive_wired_is_501(self):
        async with _app(None).test_client() as client:
            resp = await client.delete("/api/drive/disconnect", headers=_AUTH)
        assert resp.status_code == 501
