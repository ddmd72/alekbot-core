"""The transcription disclosure sentence (USER_DRIVE_INDEXING_RFC §27.12, plan G24): strings in every Cabinet
language, shown only while the user's drive_transcribe_enabled flag is on (and, since T-R34, the kill switch: its own
cases are test_cabinet_transcribe_switch_gate.py), with no toggle."""
from unittest.mock import AsyncMock, MagicMock
from pathlib import Path

import pytest
from quart import Quart

from src.domain.drive_indexing_status import DriveIndexingStatus
from src.domain.user import UserBotConfig
from src.domain.user_drive import DriveConnection
from src.ports.user_drive_port import UserDrivePort
from src.services.drive_search_service import DriveSearchService
from src.web.user_cabinet_app import create_user_cabinet_blueprint

from .test_cabinet_drive_index_strings import LANGS, _dicts
from src.composition.connector_registry import ConnectorRegistry


@pytest.mark.parametrize("lang", LANGS)
def test_the_sentence_exists_in_every_language(lang):
    _, dicts = _dicts()
    text = dicts[lang]["drive.transcribe_disclosure"]
    assert len(text) > 40


def test_the_sentences_are_translated():
    _, dicts = _dicts()
    assert len({dicts[lang]["drive.transcribe_disclosure"] for lang in LANGS}) == len(LANGS)


def test_disclosure_only_when_enabled():
    html = Path("src/web/static/cabinet.html").read_text(encoding="utf-8")
    assert 'ix.transcribe_enabled ? " " + t("drive.transcribe_disclosure") : ""' in html
    assert html.count("drive.transcribe_disclosure") == 5   # four dictionaries and the one conditional use
    assert "transcribe_toggle" not in html and "drive-transcribe-toggle" not in html   # no toggle (G24)


def _app(flag):
    drive = MagicMock(spec=UserDrivePort)
    drive.connection = AsyncMock(return_value=DriveConnection(connected=True))
    drive.display_name = "OneDrive"
    search = AsyncMock(spec=DriveSearchService)
    search.indexing_status.return_value = DriveIndexingStatus(enabled=True, counters={})
    user = MagicMock()
    user.config = UserBotConfig(drive_transcribe_enabled=flag)
    user_repo = MagicMock()
    user_repo.get_user = AsyncMock(return_value=user)
    session_service = MagicMock()
    session_service.verify_access_token = MagicMock(return_value={"sub": "u1", "account_id": "a1", "role": "owner"})
    app = Quart("test_transcribe_disclosure")
    app.register_blueprint(create_user_cabinet_blueprint(
        invite_service=MagicMock(), session_service=session_service, user_repo=user_repo,
        fact_repo=MagicMock(), embedding_service=MagicMock(), user_drive=drive,
        drive_search_service=search, drive_task_queue=AsyncMock(),
        drive_transcribe_switch_on=True, connectors=ConnectorRegistry(),   # the switch is on: the user's flag decides (T-R34)
    ))
    return app


@pytest.mark.parametrize("flag", [True, False])
async def test_status_returns_the_flag(flag):
    async with _app(flag).test_client() as client:
        body = await (await client.get("/api/drive/status", headers={"Authorization": "Bearer token"})).get_json()
    assert body["indexing"]["transcribe_enabled"] is flag


def test_flag_defaults_off():
    assert UserBotConfig().drive_transcribe_enabled is False
