"""indexer_main wires the owner-note store into the tick (DRIVE_FILE_NOTES_RFC §3.6, §5.1): built by default, none with
the DRIVE_NOTES_ENABLED kill switch off; and the main service's factory hands the open header its readers. Google
clients are replaced, so nothing leaves the process."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import indexer_main
from src.adapters.firestore_drive_note_repo import FirestoreDriveNoteRepository
from src.composition import drive_indexing_setup
from src.composition.drive_note_setup import drive_notes_switch_on

from .test_indexer_main import _Queue


@pytest.fixture
def wired(monkeypatch):
    captured = {}
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("DRIVE_NOTES_ENABLED", raising=False)
    for key, value in {"GOOGLE_CLOUD_PROJECT": "p", "APP_ENV": "development", "GEMINI_API_KEY": "k",
                       "DRIVE_INDEXER_URL": "https://indexer.example.invalid",
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


def test_the_note_store_is_wired_by_default(wired):
    indexer_main.build_app()
    assert isinstance(wired["notes"], FirestoreDriveNoteRepository)


@pytest.mark.parametrize("value", ["off", "0", "false", "NO"])
def test_the_kill_switch_builds_no_store(wired, monkeypatch, value):
    monkeypatch.setenv("DRIVE_NOTES_ENABLED", value)
    indexer_main.build_app()
    assert wired["notes"] is None


@pytest.mark.parametrize("value,expected", [(None, True), ("", True), ("on", True), ("1", True), ("off", False)])
def test_drive_notes_switch_on(value, expected):
    assert drive_notes_switch_on(value) is expected


def test_the_factory_hands_the_open_header_its_readers():
    from src.composition.user_agent_factory import UserAgentFactory
    factory = UserAgentFactory.__new__(UserAgentFactory)
    factory.file_conversion_service, factory.file_storage = MagicMock(), MagicMock()
    factory.notification_service = factory.user_drive_service = factory.localization = None
    factory.language_service = factory.drive_search_service = None
    factory.drive_note_service, factory.user_drive = MagicMock(name="notes"), MagicMock(name="port")
    agent = factory._build_file_management("u", MagicMock())
    assert agent._drive_notes is factory.drive_note_service and agent._drive_port is factory.user_drive
