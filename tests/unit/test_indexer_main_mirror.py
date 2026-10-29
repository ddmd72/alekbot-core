"""indexer_main wiring of the transcript mirror (USER_DRIVE_INDEXING_RFC §27.9.1, §27.9.2, §27.9.5, §27.9.12; plan Task
21C): the mirror's ledger is wired whatever the kill switch (classification, marks and pins need it), the step and its
localization (the strings and the UI-language resolution, the deep-research copy's precedent) only with the switch on,
and the stage gets the ledger for its pins. Google clients are replaced; nothing leaves the process."""
from unittest.mock import MagicMock

import pytest

import indexer_main
from src.adapters.firestore_drive_mirror_repo import FirestoreDriveMirrorRepository
from src.composition import drive_indexing_setup
from src.domain.language import LanguageCode
from src.domain.ui_messages import UIMessage
from src.ports.language_service_port import LanguageServicePort
from src.services.drive_transcript_mirror import DriveTranscriptMirror
from src.services.localization_service import LocalizationService


class _Queue:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture
def wired(monkeypatch):
    captured = {}
    monkeypatch.delenv("K_SERVICE", raising=False)
    for key in ("DRIVE_TRANSCRIBE_ENABLED", "DRIVE_TRANSCRIBE_USD_CAP"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {"GOOGLE_CLOUD_PROJECT": "p", "APP_ENV": "development", "GEMINI_API_KEY": "k",
                       "DRIVE_INDEXER_URL": "https://indexer.example.invalid",
                       # the cipher's key name (SECRETS_AT_REST_RFC §2.6, I6); build_cipher calls no KMS API
                       "KMS_CRYPTO_KEY": "projects/example-project/locations/global/keyRings/ring-a/cryptoKeys/key-a",
                       "MICROSOFT_TODO_CLIENT_ID": "cid", "MICROSOFT_TODO_CLIENT_SECRET": "sec",
                       "SERVICE_ACCOUNT_EMAIL": "sa@example.invalid"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(indexer_main.firestore, "AsyncClient", MagicMock())
    monkeypatch.setattr(drive_indexing_setup, "GcpTaskQueue", _Queue)
    monkeypatch.setattr(indexer_main, "build_place_lookup", lambda: (lambda lat, lon: None))
    real = indexer_main.DriveIndexingService

    def capture(*args, **kwargs):
        captured.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(indexer_main, "DriveIndexingService", capture)
    return captured


def test_indexer_wires_the_mirror_and_localization(wired, monkeypatch):
    monkeypatch.setenv("DRIVE_TRANSCRIBE_ENABLED", "on")
    indexer_main.build_app()
    repo = wired["mirror_repo"]
    assert isinstance(repo, FirestoreDriveMirrorRepository)
    step = wired["mirror"]
    assert isinstance(step, DriveTranscriptMirror)
    assert step._mirror is repo and step._repo is wired["repo"]   # one ledger for the tick, the step and the stage
    assert step._drive is wired["drive"]                         # the port, never UserDriveService (§27.9.8)
    assert wired["transcripts"]._mirror is repo                  # the stage's orphan sweep keeps the pins
    localization, language = wired["localization"], wired["language"]
    assert isinstance(localization, LocalizationService) and isinstance(language, LanguageServicePort)
    assert "Archive/" in localization.get_ui_string(LanguageCode.UK, UIMessage.DRIVE_TRANSCRIPTS_README)


def test_switch_off_builds_the_repository_not_the_service(wired):
    indexer_main.build_app()
    assert isinstance(wired["mirror_repo"], FirestoreDriveMirrorRepository)   # classification, marks and pins
    assert wired["mirror"] is None and wired["localization"] is None and wired["language"] is None
    assert wired["transcripts"] is None and wired["transcribe_enabled"] is False


def test_switch_on_without_a_stage_builds_no_step(wired, monkeypatch):
    """The switch on but no Gemini key: no stage, so no transcription runs and no step is built either."""
    monkeypatch.setenv("DRIVE_TRANSCRIBE_ENABLED", "on")
    monkeypatch.setenv("GEMINI_API_KEY", " ")
    indexer_main.build_app()
    assert wired["transcripts"] is None and wired["mirror"] is None
    assert isinstance(wired["mirror_repo"], FirestoreDriveMirrorRepository)
