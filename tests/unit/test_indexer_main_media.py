"""indexer_main decodes recordings only through the sandbox light service (UNTRUSTED_CONTENT_SANDBOX_RFC §7 S5; S5
design §2.7, §2.9): the transcript stage's media port is a `SandboxMediaAdapter` signed for the same required
`SANDBOX_PARSE_URL` the parser uses; no knob, no in-process fallback, and `DRIVE_PARSER_USER` no longer matters to the
indexer. Google clients are replaced; nothing leaves the process."""
from unittest.mock import MagicMock

import pytest

import indexer_main
from src.adapters.sandbox_client.sandbox_media_adapter import SandboxMediaAdapter
from src.composition import drive_indexing_setup

LIGHT = "https://alek-sandbox-light.test.example"


class _Queue:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture
def wired(monkeypatch):
    captured = {}
    for key in ("K_SERVICE", "DRIVE_TRANSCRIBE_USD_CAP", "DRIVE_PARSER_USER", "SANDBOX_MEDIA"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {"GOOGLE_CLOUD_PROJECT": "p", "APP_ENV": "development", "GEMINI_API_KEY": "k",
                       "DRIVE_INDEXER_URL": "https://indexer.example.invalid", "SANDBOX_PARSE_URL": LIGHT + "/",
                       "KMS_CRYPTO_KEY": "projects/example-project/locations/global/keyRings/ring-a/cryptoKeys/key-a",
                       "MICROSOFT_TODO_CLIENT_ID": "cid", "MICROSOFT_TODO_CLIENT_SECRET": "sec",
                       "SERVICE_ACCOUNT_EMAIL": "sa@example.invalid", "DRIVE_TRANSCRIBE_ENABLED": "on"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(indexer_main.firestore, "AsyncClient", MagicMock())
    monkeypatch.setattr(drive_indexing_setup, "GcpTaskQueue", _Queue)
    monkeypatch.setattr(indexer_main, "build_place_lookup", lambda: (lambda lat, lon: None))
    monkeypatch.setattr(indexer_main, "DriveIndexingService", lambda *a, **kw: captured.update(kw) or MagicMock())
    return captured


def test_the_stage_decodes_through_the_light_service_at_the_parse_url(wired):
    indexer_main.build_app()
    media = wired["transcripts"]._media
    assert isinstance(media, SandboxMediaAdapter)
    assert media._client.base_url == LIGHT and media._client._token_provider.audience == LIGHT
    assert wired["extractors"]["parser"]._client._base_url == LIGHT       # one light service for both


def test_a_parser_user_that_does_not_exist_does_not_matter_on_cloud_run(wired, monkeypatch):
    """The in-process media child resolved DRIVE_PARSER_USER and refused a bad one on Cloud Run (G13); the indexer
    starts no media child any more, so the variable is not read for media."""
    monkeypatch.setenv("K_SERVICE", "alek-indexer-test")
    monkeypatch.setenv("DRIVE_PARSER_USER", "no-such-parser-user-xyz")
    indexer_main.build_app()
    assert isinstance(wired["transcripts"]._media, SandboxMediaAdapter)


def test_a_media_knob_is_ignored(wired, monkeypatch):
    """No SANDBOX_MEDIA switch exists (design §2.7, Δ3): setting one changes nothing."""
    monkeypatch.setenv("SANDBOX_MEDIA", "inprocess")
    indexer_main.build_app()
    assert isinstance(wired["transcripts"]._media, SandboxMediaAdapter)
