"""indexer_main wiring smoke test: the app builds from env alone, binds the tick to the drive queue,
and never creates the queue (USER_DRIVE_INDEXING_RFC §4, §12.5). Google clients are replaced, so
nothing leaves the process."""
from unittest.mock import MagicMock

import pytest

import indexer_main
from src.adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter
from src.composition import drive_indexing_setup
from src.web import indexer_worker_app


class _Queue:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def create_queue_if_not_exists(self):
        raise AssertionError("the drive queue is provisioned by cloudbuild, never by the app")


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
    monkeypatch.setattr(indexer_main.firestore, "AsyncClient", MagicMock())
    monkeypatch.setattr(drive_indexing_setup, "GcpTaskQueue", _Queue)
    monkeypatch.setattr(indexer_main, "build_place_lookup", lambda: (lambda lat, lon: None))
    real = indexer_main.DriveIndexingService

    def capture(*args, **kwargs):
        captured.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(indexer_main, "DriveIndexingService", capture)
    return captured


async def test_app_builds_and_serves_health(wired):
    app = indexer_main.build_app()
    assert {r.rule for r in app.url_map.iter_rules()} >= {"/worker", "/health"}
    assert (await app.test_client().get("/health")).status_code == 200
    assert wired["embedding_space"] == GeminiEmbeddingAdapter.EMBEDDING_SPACE
    assert wired["queue"].kwargs["queue_name"] == "drive-sync-dev"
    assert wired["queue"].kwargs["service_url"] == "https://indexer.example.invalid"
    assert wired["queue"].kwargs["service_account_email"] == "sa@example.invalid"


async def test_missing_indexer_url_starts_with_worker_disabled(wired, monkeypatch):
    """First deploy pass: the revision must become healthy, but no tick can run without a queue."""
    monkeypatch.delenv("DRIVE_INDEXER_URL")
    monkeypatch.delenv("SERVICE_ACCOUNT_EMAIL")   # gate off: the request reaches the route
    app = indexer_main.build_app()
    client = app.test_client()
    assert (await client.get("/health")).status_code == 200
    resp = await client.post("/worker", json={"task_type": "drive_sync", "user_id": "u"})
    assert resp.status_code == 503
    assert wired == {}   # no DriveIndexingService was built


async def test_gate_pins_the_audience_to_this_service(wired, monkeypatch):
    seen = []

    def fake_verify(header, sa, audiences):
        seen.append(audiences)
        return False

    monkeypatch.setattr(indexer_worker_app, "verify_worker_oidc", fake_verify)
    app = indexer_main.build_app()
    resp = await app.test_client().post("/worker", json={"task_type": "drive_sync", "user_id": "u"},
                                        headers={"Authorization": "Bearer t"})
    assert resp.status_code == 401
    assert seen == [("https://indexer.example.invalid", "https://indexer.example.invalid/worker")]


async def test_missing_service_account_on_cloud_run_fails_closed(wired, monkeypatch):
    monkeypatch.delenv("SERVICE_ACCOUNT_EMAIL")
    monkeypatch.setenv("K_SERVICE", "indexer")
    app = indexer_main.build_app()
    resp = await app.test_client().post("/worker", json={"task_type": "drive_sync", "user_id": "u"})
    assert resp.status_code == 401


def test_graceful_timeout_is_below_cloud_runs_sigterm_window():
    assert 0 < indexer_main._GRACEFUL_TIMEOUT_S < 10
