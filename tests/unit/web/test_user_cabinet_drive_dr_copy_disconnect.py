"""Disconnecting the drive turns the deep research copy off, and the switch is offered only while a drive is
linked (owner, 2026-10-14). Before, a switch left on stayed visible after a disconnect so it could be turned off;
now the disconnect itself resets it, so a hidden switch is never left on."""
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from quart import Quart

from src.ports.user_drive_port import UserDrivePort
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from src.composition.connector_registry import ConnectorRegistry

_AUTH = {"Authorization": "Bearer token"}
_HTML = Path(__file__).resolve().parents[3] / "src" / "web" / "static" / "cabinet.html"


def _app(user_repo, user_drive):
    session_service = MagicMock()
    session_service.verify_access_token = MagicMock(return_value={"sub": "u1", "account_id": "a1", "role": "owner"})
    app = Quart("test_drive_dr_copy_disconnect")
    app.register_blueprint(create_user_cabinet_blueprint(
        invite_service=MagicMock(), session_service=session_service, user_repo=user_repo,
        fact_repo=MagicMock(), embedding_service=MagicMock(), user_drive=user_drive, connectors=ConnectorRegistry(),
    ))
    return app


def _repo(enabled):
    repo = MagicMock()
    profile = SimpleNamespace(config=SimpleNamespace(drive_copy_deep_research=enabled))
    repo.get_user = AsyncMock(return_value=profile)
    repo.update_user = AsyncMock()
    return repo, profile


def _drive():
    drive = MagicMock(spec=UserDrivePort)
    drive.disconnect = AsyncMock()
    return drive


async def test_disconnect_turns_the_copy_off():
    repo, profile = _repo(enabled=True)
    drive = _drive()
    async with _app(repo, drive).test_client() as client:
        resp = await client.delete("/api/drive/disconnect", headers=_AUTH)
    assert resp.status_code == 200
    drive.disconnect.assert_awaited_once_with("u1")
    assert profile.config.drive_copy_deep_research is False
    repo.update_user.assert_awaited_once_with(profile)


async def test_disconnect_with_the_copy_off_writes_nothing():
    repo, profile = _repo(enabled=False)
    async with _app(repo, _drive()).test_client() as client:
        assert (await client.delete("/api/drive/disconnect", headers=_AUTH)).status_code == 200
    repo.update_user.assert_not_called()


async def test_a_failed_reset_does_not_fail_the_disconnect():
    repo, _ = _repo(enabled=True)
    repo.update_user.side_effect = RuntimeError("firestore down")
    drive = _drive()
    async with _app(repo, drive).test_client() as client:
        assert (await client.delete("/api/drive/disconnect", headers=_AUTH)).status_code == 200
    drive.disconnect.assert_awaited_once_with("u1")


def test_the_switch_is_offered_only_while_a_drive_is_linked():
    html = _HTML.read_text(encoding="utf-8")
    line = re.search(r"const drCopyHtml = (.*?)\n", html).group(1)
    assert line.startswith("linked && ")
    assert "dr_copy === true ||" not in line
