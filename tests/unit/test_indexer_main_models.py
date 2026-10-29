"""indexer_main wiring of the content side (USER_DRIVE_INDEXING_RFC §24.2): the parser and the reader runner under the
EXTRACTOR_CHAINS ids, the triage runner, the three model providers (Claude, OpenAI, and Gemini for the fallback) from
their own env keys, the optional billing alert.
Google clients are replaced, so nothing leaves the process."""
from unittest.mock import MagicMock

import pytest

import indexer_main
from src.adapters.subprocess_content_extractor import SubprocessContentExtractor
from src.composition import drive_indexing_setup
from src.composition.drive_reader_runner import DriveReaderRunner
from src.composition.drive_triage_runner import DriveTriageRunner
from src.domain.drive_extraction import PARSER, READER


class _Queue:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture
def wired(monkeypatch):
    captured = {}
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("BILLING_SLACK_WEBHOOK_URL", raising=False)
    for key, value in {"GOOGLE_CLOUD_PROJECT": "p", "APP_ENV": "development", "GEMINI_API_KEY": "k",
                       "DRIVE_INDEXER_URL": "https://indexer.example.invalid",
                       # the cipher's key name (SECRETS_AT_REST_RFC §2.6, I6); build_cipher calls no KMS API
                       "KMS_CRYPTO_KEY": "projects/example-project/locations/global/keyRings/ring-a/cryptoKeys/key-a",
                       "MICROSOFT_TODO_CLIENT_ID": "cid", "MICROSOFT_TODO_CLIENT_SECRET": "sec",
                       "SERVICE_ACCOUNT_EMAIL": "sa@example.invalid", "ANTHROPIC_API_KEY": "ak",
                       "OPENAI_API_KEY": "ok"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(indexer_main.firestore, "AsyncClient", MagicMock())
    monkeypatch.setattr(drive_indexing_setup, "GcpTaskQueue", _Queue)
    monkeypatch.setattr(indexer_main, "build_place_lookup", lambda: (lambda lat, lon: None))
    monkeypatch.setattr(indexer_main, "DriveIndexingService", lambda *a, **kw: captured.update(kw) or MagicMock())
    return captured


def test_extractors_and_triage_reach_the_tick(wired):
    indexer_main.build_app()
    extractors, triage = wired["extractors"], wired["triage"]
    assert set(extractors) == {PARSER, READER}
    assert isinstance(extractors[PARSER], SubprocessContentExtractor)
    reader = extractors[READER]
    assert isinstance(reader, DriveReaderRunner) and reader._splitter is extractors[PARSER]
    assert isinstance(triage, DriveTriageRunner)
    assert reader._user_repo is wired["users"]
    registry = reader._context_builder.registry
    assert set(registry.list_available()) == {"claude", "openai", "gemini"}


def test_billing_alert_is_optional(wired, monkeypatch):
    indexer_main.build_app()
    assert wired["extractors"][READER]._quota_service.alert_sink is None
    monkeypatch.setenv("BILLING_SLACK_WEBHOOK_URL", "https://hooks.example.invalid/x")
    indexer_main.build_app()
    assert wired["extractors"][READER]._quota_service.alert_sink is not None


def test_without_the_openai_key_openai_is_not_registered(wired, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")
    indexer_main.build_app()
    assert set(wired["extractors"][READER]._context_builder.registry.list_available()) == {"claude", "gemini"}
