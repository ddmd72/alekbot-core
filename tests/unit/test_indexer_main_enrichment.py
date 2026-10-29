"""indexer_main wiring of Search v2's enrichment (USER_DRIVE_INDEXING_RFC §25.4.4): the profiler and passage-hint
runners reach the tick over the reader's context builder, prompt builder and quota service; the place lookup returns
the geo v2 place with its forms (§25.4.3). Google clients are replaced, so nothing leaves the process."""
from unittest.mock import MagicMock

import pytest

import indexer_main
from src.composition import drive_indexing_setup
from src.composition.drive_passage_runner import DrivePassageRunner
from src.composition.drive_profiler_runner import DriveProfilerRunner
from src.domain.drive_extraction import READER
from src.domain.place_lookup import Place, PlaceRow


class _Queue:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture
def wired(monkeypatch):
    captured = {}
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("BILLING_SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DRIVE_UPGRADE_PASS", raising=False)
    for key, value in {"GOOGLE_CLOUD_PROJECT": "p", "APP_ENV": "development", "GEMINI_API_KEY": "k",
                       "DRIVE_INDEXER_URL": "https://indexer.example.invalid",
                       # the cipher's key name (SECRETS_AT_REST_RFC §2.6, I6); build_cipher calls no KMS API
                       "KMS_CRYPTO_KEY": "projects/example-project/locations/global/keyRings/ring-a/cryptoKeys/key-a",
                       "MICROSOFT_TODO_CLIENT_ID": "cid", "MICROSOFT_TODO_CLIENT_SECRET": "sec",
                       "SERVICE_ACCOUNT_EMAIL": "sa@example.invalid", "OPENAI_API_KEY": "ok"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(indexer_main.firestore, "AsyncClient", MagicMock())
    monkeypatch.setattr(drive_indexing_setup, "GcpTaskQueue", _Queue)
    monkeypatch.setattr(indexer_main, "build_place_lookup", lambda: (lambda lat, lon: None))
    monkeypatch.setattr(indexer_main, "DriveIndexingService", lambda *a, **kw: captured.update(kw) or MagicMock())
    return captured


def test_the_profiler_and_passage_runners_reach_the_tick(wired):
    indexer_main.build_app()
    profiler, passages, reader = wired["profiler"], wired["passages"], wired["extractors"][READER]
    assert isinstance(profiler, DriveProfilerRunner) and isinstance(passages, DrivePassageRunner)
    assert profiler._context_builder is reader._context_builder is passages._context_builder
    assert profiler._quota_service is reader._quota_service is passages._quota_service
    assert profiler._user_repo is wired["users"]


def test_the_place_lookup_gives_the_place_forms(monkeypatch):
    rows = [PlaceRow(41.9028, 12.4964, "Rome", "Roma", "IT", alt_names=("Рим",), country_names=("Italy",))]
    monkeypatch.setattr(drive_indexing_setup, "load_place_rows", lambda: rows)
    place = drive_indexing_setup.build_place_lookup()(41.90, 12.50)
    assert isinstance(place, Place) and place.name == "Rome (Roma), IT"
    assert {"Rome", "Roma", "Рим", "Italy"} <= set(place.forms)


def test_the_upgrade_pass_switch_is_on_unless_turned_off(wired, monkeypatch):
    indexer_main.build_app()
    assert wired["upgrade_pass"] is True
    monkeypatch.setenv("DRIVE_UPGRADE_PASS", "off")
    indexer_main.build_app()
    assert wired["upgrade_pass"] is False
    assert indexer_main.upgrade_pass_enabled("0") is False and indexer_main.upgrade_pass_enabled("1") is True
