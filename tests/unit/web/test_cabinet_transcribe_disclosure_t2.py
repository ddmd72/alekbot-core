"""The Cabinet texts of the transcript mirror (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27.12, Δ202): the transcription
sentence names the `_Transcripts` folder and its Archive (under the existing rule: the user's flag and the switch); the
indexing disclosure and the opt-out confirmation carry the clause that the folder and the archived transcripts outlive an
opt-out, shown whenever the user has a mirror root."""
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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

_HTML = Path("src/web/static/cabinet.html").read_text(encoding="utf-8")


@pytest.mark.parametrize("lang", LANGS)
def test_t2_sentence_names_the_folder_and_the_archive_only_when_on(lang):
    _, dicts = _dicts()
    sentence = dicts[lang]["drive.transcribe_disclosure"]
    assert "_Transcripts" in sentence and "Archive" in sentence
    assert "Google Gemini" in sentence
    # the sentence is still gated by the user's flag (and the switch) exactly as before
    assert 'ix.transcribe_enabled ? " " + t("drive.transcribe_disclosure") : ""' in _HTML
    # and the folder is not named by the always-shown index disclosure
    assert "_Transcripts" not in dicts[lang]["drive.index_disclosure"]
    assert "_Transcripts" not in dicts[lang]["drive.index_off_confirm"]


@pytest.mark.parametrize("lang", LANGS)
def test_other_people_s_voices_are_still_said(lang):
    _, dicts = _dicts()
    markers = {"en": "other people's voices", "uk": "голоси інших людей", "fr": "voix d'autres personnes",
               "es": "voces de otras personas"}
    assert markers[lang] in dicts[lang]["drive.transcribe_disclosure"]


@pytest.mark.parametrize("lang", LANGS)
def test_optout_confirm_and_disclosure_carry_the_archive_clause(lang):
    _, dicts = _dicts()
    clause = dicts[lang]["drive.mirror_clause"]
    assert "_Transcripts" in clause and "Archive" in clause
    # shown in the disclosure and appended to the opt-out confirmation, both whenever the user has a mirror root
    assert '${ix.mirror_root ? " " + t("drive.mirror_clause") : ""}' in _HTML
    assert 'confirm(t("drive.index_off_confirm") + (driveMirrorRoot ? " " + t("drive.mirror_clause") : ""))' in _HTML
    assert "driveMirrorRoot = !!ix.mirror_root" in _HTML


def test_clauses_are_translated():
    _, dicts = _dicts()
    assert len({dicts[lang]["drive.mirror_clause"] for lang in LANGS}) == len(LANGS)
    assert len({dicts[lang]["drive.transcribe_disclosure"] for lang in LANGS}) == len(LANGS)


def test_the_clause_is_used_in_two_places_and_defined_in_four():
    assert _HTML.count("drive.mirror_clause") == 4 + 2


# -- the status flag -----------------------------------------------------------------------------------------

def _app(has_root):
    drive = MagicMock(spec=UserDrivePort)
    drive.connection = AsyncMock(return_value=DriveConnection(connected=True))
    drive.display_name = "OneDrive"
    search = AsyncMock(spec=DriveSearchService)
    search.indexing_status.return_value = DriveIndexingStatus(enabled=False, counters={})
    search.has_mirror_root.return_value = has_root
    user = MagicMock()
    user.config = UserBotConfig(drive_transcribe_enabled=False)
    user_repo = MagicMock()
    user_repo.get_user = AsyncMock(return_value=user)
    session_service = MagicMock()
    session_service.verify_access_token = MagicMock(return_value={"sub": "u1", "account_id": "a1", "role": "owner"})
    app = Quart("test_mirror_flag")
    app.register_blueprint(create_user_cabinet_blueprint(
        invite_service=MagicMock(), session_service=session_service, user_repo=user_repo,
        fact_repo=MagicMock(), embedding_service=MagicMock(), user_drive=drive,
        drive_search_service=search, drive_task_queue=AsyncMock(), drive_transcribe_switch_on=True, connectors=ConnectorRegistry()))
    return app, search


async def _status(app):
    async with app.test_client() as client:
        return await (await client.get("/api/drive/status", headers={"Authorization": "Bearer token"})).get_json()


async def test_status_flags_a_mirror_root_even_with_indexing_off_and_the_transcription_flag_off():
    app, _ = _app(True)
    body = await _status(app)
    assert body["indexing"]["mirror_root"] is True and body["indexing"]["transcribe_enabled"] is False


async def test_status_flag_is_false_without_a_root():
    app, _ = _app(False)
    assert (await _status(app))["indexing"]["mirror_root"] is False


async def test_a_failing_lookup_never_breaks_the_status():
    app, search = _app(True)
    search.has_mirror_root.side_effect = RuntimeError("boom")
    body = await _status(app)
    assert body["connected"] is True and body["indexing"]["mirror_root"] is False
