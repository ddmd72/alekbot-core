"""indexer_main builds the Google Drive adapter only when its OAuth client is mounted (GOOGLE_DRIVE_RFC GE-31)."""
import logging
from unittest.mock import MagicMock

import pytest

import indexer_main
from src.adapters.google.google_drive_adapter import GoogleDriveAdapter
from src.composition import drive_indexing_setup


class _Queue:
    def __init__(self, **kwargs):
        pass


@pytest.fixture
def built(monkeypatch):
    seen = {}
    monkeypatch.delenv("K_SERVICE", raising=False)
    for key in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {"GOOGLE_CLOUD_PROJECT": "p", "APP_ENV": "development", "GEMINI_API_KEY": "k",
                       "DRIVE_INDEXER_URL": "https://indexer.example.invalid",
                       "KMS_CRYPTO_KEY": "projects/example-project/locations/global/keyRings/ring-a/cryptoKeys/key-a",
                       "MICROSOFT_TODO_CLIENT_ID": "cid", "MICROSOFT_TODO_CLIENT_SECRET": "sec",
                       "SERVICE_ACCOUNT_EMAIL": "sa@example.invalid"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(indexer_main.firestore, "AsyncClient", MagicMock())
    monkeypatch.setattr(drive_indexing_setup, "GcpTaskQueue", _Queue)
    monkeypatch.setattr(indexer_main, "build_place_lookup", lambda: (lambda lat, lon: None))
    real = indexer_main.DriveProviderRouter

    def capture(adapters):
        router = real(adapters)
        seen["router"] = router
        return router

    monkeypatch.setattr(indexer_main, "DriveProviderRouter", capture)
    return seen


def test_with_the_google_pair_the_kind_is_registered(built, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "gid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "gsecret")
    indexer_main.build_app()
    assert sorted(built["router"].kinds()) == ["google_drive", "microsoft_onedrive"]
    assert isinstance(built["router"]._route(_handle("google_drive")), GoogleDriveAdapter)


def test_without_it_the_indexer_starts_with_onedrive_only(built, caplog):
    with caplog.at_level(logging.DEBUG):
        indexer_main.build_app()
    assert built["router"].kinds() == ["microsoft_onedrive"]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_a_blank_secret_counts_as_absent(built, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "gid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", " ")
    indexer_main.build_app()
    assert built["router"].kinds() == ["microsoft_onedrive"]


def _handle(kind):
    from tests.support.drive_handles import drive_handle
    return drive_handle(kind=kind)
