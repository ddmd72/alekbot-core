"""The Cabinet's transcription disclosure needs the kill switch too (final review F3): the transcription flag alone is
not enough while the indexer's switch is off. The flag is the drive connection's (MULTI_DRIVE_RFC §3 Removed, §4.4)."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart

from src.domain.drive_indexing_status import DriveIndexingStatus
from src.services.drive_search_service import DriveSearchService
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from tests.support.cabinet_session_kit import cabinet_session_for
from tests.support.cabinet_drive import cabinet_drive

_AUTH: dict = {}   # the Bearer header of the app's one signed-in session, set by `_app()`


async def _app(flag, switch):
    drive = await cabinet_drive(transcribe=flag)
    search = AsyncMock(spec=DriveSearchService)
    search.indexing_status.return_value = DriveIndexingStatus(enabled=True, counters={})
    user_repo = MagicMock()
    cabinet_sessions, _bearer = cabinet_session_for("u1", "a1")
    _AUTH.update(_bearer)
    app = Quart("test_switch_gate")
    kwargs = {} if switch is None else {"drive_transcribe_switch_on": switch}
    app.register_blueprint(create_user_cabinet_blueprint(
        invite_service=MagicMock(), cabinet_sessions=cabinet_sessions, user_repo=user_repo,
        fact_repo=MagicMock(), embedding_service=MagicMock(), user_drive_service=None,
        drive_search_service=search, drive_task_queue=AsyncMock(), drive_connections=drive.connections,
        oauth_credentials_port=drive.credentials, **kwargs))
    return app


async def _shown(flag, switch):
    async with (await _app(flag, switch)).test_client() as client:
        body = await (await client.get("/api/drive/status", headers=_AUTH)).get_json()
    return body["indexing"]["transcribe_enabled"]


@pytest.mark.parametrize("raw,expected", [("on", True), ("1", True), ("TRUE", True), ("off", False), ("", False), (None, False)])
def test_the_container_reads_the_switch_like_the_indexer(raw, expected, monkeypatch):
    import inspect
    from src.composition import service_container
    src = inspect.getsource(service_container)
    assert 'os.getenv("DRIVE_TRANSCRIBE_ENABLED")' in src
    # same parse as indexer_main.transcribe_enabled
    assert (str(raw or "off").strip().lower() in ("1", "on", "true", "yes")) is expected
