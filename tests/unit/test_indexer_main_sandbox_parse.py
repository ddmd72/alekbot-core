"""The indexer parses only through the sandbox light service (UNTRUSTED_CONTENT_SANDBOX_RFC §4.10, §5 O14): no
`SANDBOX_PARSE_URL` refuses the start before any client is built, locally and on Cloud Run alike; with it the parser
and the reader's splitter are the sandbox's one adapter, and no in-process parser child is built."""
from unittest.mock import MagicMock

import pytest

import indexer_main
from src.adapters.sandbox_client.sandbox_content_extractor import SandboxContentExtractor
from src.adapters.subprocess_content_extractor import PARSER_USER_ENV, SubprocessContentExtractor
from src.composition import drive_indexing_setup
from src.domain.document_extraction import PARSER, READER


class _Queue:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture
def wired(monkeypatch):
    captured = {}
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv(PARSER_USER_ENV, raising=False)
    monkeypatch.delenv("DRIVE_TRANSCRIBE_ENABLED", raising=False)
    for key, value in {"GOOGLE_CLOUD_PROJECT": "p", "APP_ENV": "development", "GEMINI_API_KEY": "k",
                       "DRIVE_INDEXER_URL": "https://indexer.example.invalid",
                       "SANDBOX_PARSE_URL": "https://alek-sandbox-light.test.example",
                       "KMS_CRYPTO_KEY": "projects/example-project/locations/global/keyRings/ring-a/cryptoKeys/key-a",
                       "MICROSOFT_TODO_CLIENT_ID": "cid", "MICROSOFT_TODO_CLIENT_SECRET": "sec",
                       "SERVICE_ACCOUNT_EMAIL": "sa@example.invalid", "ANTHROPIC_API_KEY": "ak"}.items():
        monkeypatch.setenv(key, value)
    client = MagicMock()
    monkeypatch.setattr(indexer_main.firestore, "AsyncClient", client)
    monkeypatch.setattr(drive_indexing_setup, "GcpTaskQueue", _Queue)
    monkeypatch.setattr(indexer_main, "build_place_lookup", lambda: (lambda lat, lon: None))
    monkeypatch.setattr(indexer_main, "DriveIndexingService", lambda *a, **kw: captured.update(kw) or MagicMock())
    captured["_client"] = client
    return captured


@pytest.mark.parametrize("cloud_run", [False, True], ids=["local", "cloud-run"])
@pytest.mark.parametrize("value", [None, "", "  "], ids=["unset", "empty", "blank"])
def test_no_url_refuses_the_start_before_any_client(wired, monkeypatch, caplog, cloud_run, value):
    if value is None:
        monkeypatch.delenv("SANDBOX_PARSE_URL")
    else:
        monkeypatch.setenv("SANDBOX_PARSE_URL", value)
    if cloud_run:
        monkeypatch.setenv("K_SERVICE", "alek-bot-indexer-dev")

    with pytest.raises(ValueError, match="light service URL"):
        indexer_main.build_app()

    wired["_client"].assert_not_called()
    assert any(r.levelname == "ERROR" and "light service URL" in r.getMessage() for r in caplog.records)


def test_the_parser_and_the_readers_splitter_are_the_sandboxs(wired):
    indexer_main.build_app()
    parser = wired["extractors"][PARSER]
    assert isinstance(parser, SandboxContentExtractor)
    assert wired["extractors"][READER]._splitter is parser


def test_no_in_process_parser_child_is_built(wired, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("the indexer built an in-process parser child")

    monkeypatch.setattr(SubprocessContentExtractor, "__init__", fail)
    indexer_main.build_app()


def test_the_media_extractor_still_gets_the_parser_user(wired, monkeypatch):
    seen = {}
    monkeypatch.setenv("DRIVE_TRANSCRIBE_ENABLED", "on")
    monkeypatch.setenv(PARSER_USER_ENV, "driveparser")

    def spy(**kwargs):
        seen.update(kwargs)
        return None          # switch on but no stage: only the user handed to the media wiring is observed

    monkeypatch.setattr(indexer_main, "build_drive_transcription", spy)
    indexer_main.build_app()
    assert seen["parser_user"] == "driveparser"
