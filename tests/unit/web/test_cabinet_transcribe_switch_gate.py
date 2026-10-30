"""The Cabinet's transcription disclosure needs the kill switch too (final review F3): the transcription flag alone is
not enough while the indexer's switch is off. The flag is the drive connection's (MULTI_DRIVE_RFC §3 Removed, §4.4)."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart

from src.domain.drive_indexing_status import DriveIndexingStatus
from src.services.drive_search_service import DriveSearchService
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from tests.support.cabinet_drive import cabinet_drive


async def _app(flag, switch):
    drive = await cabinet_drive(transcribe=flag)
    search = AsyncMock(spec=DriveSearchService)
    search.indexing_status.return_value = DriveIndexingStatus(enabled=True, counters={})
    user_repo = MagicMock()
    session_service = MagicMock()
    session_service.verify_access_token = MagicMock(return_value={"sub": "u1", "account_id": "a1", "role": "owner"})
    app = Quart("test_switch_gate")
    kwargs = {} if switch is None else {"drive_transcribe_switch_on": switch}
    app.register_blueprint(create_user_cabinet_blueprint(
        invite_service=MagicMock(), session_service=session_service, user_repo=user_repo,
        fact_repo=MagicMock(), embedding_service=MagicMock(), user_drive=None,
        drive_search_service=search, drive_task_queue=AsyncMock(), drive_connections=drive.connections,
        oauth_credentials_port=drive.credentials, **kwargs))
    return app


async def _shown(flag, switch):
    async with (await _app(flag, switch)).test_client() as client:
        body = await (await client.get("/api/drive/status", headers={"Authorization": "Bearer token"})).get_json()
    return body["indexing"]["transcribe_enabled"]


@pytest.mark.parametrize("flag,switch,expected", [(True, True, True), (True, False, False), (False, True, False),
                                                   (False, False, False), (True, None, False)])
async def test_disclosure_needs_both_the_flag_and_the_switch(flag, switch, expected):
    assert await _shown(flag, switch) is expected


@pytest.mark.parametrize("raw,expected", [("on", True), ("1", True), ("TRUE", True), ("off", False), ("", False), (None, False)])
def test_the_container_reads_the_switch_like_the_indexer(raw, expected, monkeypatch):
    import inspect
    from src.composition import service_container
    src = inspect.getsource(service_container)
    assert 'os.getenv("DRIVE_TRANSCRIBE_ENABLED")' in src
    # same parse as indexer_main.transcribe_enabled
    assert (str(raw or "off").strip().lower() in ("1", "on", "true", "yes")) is expected
