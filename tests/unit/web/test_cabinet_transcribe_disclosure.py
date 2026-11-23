"""The transcription disclosure sentence (USER_DRIVE_INDEXING_RFC §27.12, plan G24): strings in every Cabinet
language, shown only while the user's drive_transcribe_enabled flag is on (and, since T-R34, the kill switch: its own
cases are test_cabinet_transcribe_switch_gate.py), with no toggle."""
from unittest.mock import AsyncMock, MagicMock
from pathlib import Path

import pytest
from quart import Quart

from src.domain.drive_indexing_status import DriveIndexingStatus
from src.services.drive_search_service import DriveSearchService
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from tests.support.cabinet_session_kit import cabinet_session_for

from .test_cabinet_drive_index_strings import LANGS, _dicts
from src.composition.connector_registry import ConnectorRegistry
from tests.support.cabinet_drive import cabinet_drive

_AUTH: dict = {}   # the Bearer header of the app's one signed-in session, set by `_app()`


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


async def _app(flag):
    # the flag is the drive connection's transcription switch (MULTI_DRIVE_RFC §3 Removed, §4.4)
    drive = await cabinet_drive(transcribe=flag)
    search = AsyncMock(spec=DriveSearchService)
    search.indexing_status.return_value = DriveIndexingStatus(enabled=True, counters={})
    user_repo = MagicMock()
    cabinet_sessions, _bearer = cabinet_session_for("u1", "a1")
    _AUTH.update(_bearer)
    app = Quart("test_transcribe_disclosure")
    app.register_blueprint(create_user_cabinet_blueprint(
        invite_service=MagicMock(), cabinet_sessions=cabinet_sessions, user_repo=user_repo,
        fact_repo=MagicMock(), embedding_service=MagicMock(), user_drive_service=None,
        drive_search_service=search, drive_task_queue=AsyncMock(),
        drive_transcribe_switch_on=True, connectors=ConnectorRegistry(),   # the switch is on: the flag decides (T-R34)
        drive_connections=drive.connections, oauth_credentials_port=drive.credentials,
    ))
    return app


