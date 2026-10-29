"""indexer_main wiring: the Firestore database, the place lookup, the first-pass log level (USER_DRIVE_INDEXING_RFC
§5.3, §12.5).

Google clients are replaced the same way as in `test_indexer_main.py`, so nothing leaves the process."""
import logging
from unittest.mock import MagicMock

import pytest

import indexer_main
from src.composition import drive_indexing_setup


class _Queue:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def create_queue_if_not_exists(self):
        raise AssertionError("the drive queue is provisioned by cloudbuild, never by the app")


def _place_of(lat, lon):
    return None


@pytest.fixture
def wired(monkeypatch):
    captured = {}
    monkeypatch.delenv("K_SERVICE", raising=False)
    for key, value in {"GOOGLE_CLOUD_PROJECT": "p", "APP_ENV": "development", "GEMINI_API_KEY": "k",
                       "DRIVE_INDEXER_URL": "https://indexer.example.invalid",
                       # the cipher's key name (SECRETS_AT_REST_RFC §2.6, I6); build_cipher calls no KMS API
                       "KMS_CRYPTO_KEY": "projects/example-project/locations/global/keyRings/ring-a/cryptoKeys/key-a",
                       "MICROSOFT_TODO_CLIENT_ID": "cid", "MICROSOFT_TODO_CLIENT_SECRET": "sec",
                       "SERVICE_ACCOUNT_EMAIL": "sa@example.invalid"}.items():
        monkeypatch.setenv(key, value)
    client = MagicMock()
    monkeypatch.setattr(indexer_main.firestore, "AsyncClient", client)
    monkeypatch.setattr(drive_indexing_setup, "GcpTaskQueue", _Queue)
    monkeypatch.setattr(indexer_main, "build_place_lookup", lambda: _place_of)
    real = indexer_main.DriveIndexingService

    def capture(*args, **kwargs):
        captured.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(indexer_main, "DriveIndexingService", capture)
    captured["_client"] = client
    return captured


class TestIndexerWiring:
    def test_firestore_is_the_us_production_database(self, wired):
        """A wrong database would index into `(default)`, where the main service never searches."""
        indexer_main.build_app()
        wired["_client"].assert_called_once_with(project="p", database="us-production")

    def test_indexing_gets_the_place_lookup(self, wired):
        """Photo cards are named by place (§5.3, RFC Δ7)."""
        indexer_main.build_app()
        assert wired["place_of"] is _place_of

    def test_first_deploy_pass_logs_a_warning_not_an_error(self, wired, monkeypatch, caplog):
        """An ERROR would page the Errors policy on every first-pass deploy."""
        monkeypatch.delenv("DRIVE_INDEXER_URL")
        with caplog.at_level(logging.DEBUG):
            indexer_main.build_app()
        first_pass = [r for r in caplog.records if r.getMessage().startswith("[Indexer] DRIVE_INDEXER_URL")]
        assert [r.levelno for r in first_pass] == [logging.WARNING]
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
