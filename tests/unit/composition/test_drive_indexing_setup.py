"""Drive indexing wiring (USER_DRIVE_INDEXING_RFC §4, §12.5, §13; RFC Δ1)."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter
from src.composition import drive_indexing_setup as setup
from src.composition.service_container import ServiceContainer
from src.config.environment import EnvironmentConfig
from src.domain.drive_connection import DriveConnection
from src.domain.place_lookup import PlaceRow
from src.domain.settings import ConsolidationSettings
from src.ports.account_repository import AccountRepository
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.drive_connection_repo import FakeDriveConnectionRepository


class _RecordingQueue:
    instances: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.created = False
        _RecordingQueue.instances.append(self)

    async def create_queue_if_not_exists(self):
        self.created = True


@pytest.fixture
def env_config(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    return EnvironmentConfig()


@pytest.fixture
def recording_queue(monkeypatch):
    _RecordingQueue.instances = []
    monkeypatch.setattr(setup, "GcpTaskQueue", _RecordingQueue)
    return _RecordingQueue


def test_embedding_space_is_the_adapter_model_and_dimension():
    assert GeminiEmbeddingAdapter.EMBEDDING_SPACE == "gemini-embedding-2/768"


class TestDriveTaskQueue:
    def test_none_without_indexer_url(self, monkeypatch, env_config, recording_queue):
        monkeypatch.delenv("DRIVE_INDEXER_URL", raising=False)
        assert setup.build_drive_task_queue({"GOOGLE_CLOUD_PROJECT": "p"}, env_config) is None
        assert recording_queue.instances == []

    def test_none_without_project(self, monkeypatch, env_config, recording_queue):
        monkeypatch.setenv("DRIVE_INDEXER_URL", "https://indexer.example.invalid")
        assert setup.build_drive_task_queue({}, env_config) is None

    def test_bound_to_drive_queue_and_indexer(self, monkeypatch, env_config, recording_queue):
        monkeypatch.setenv("DRIVE_INDEXER_URL", " https://indexer.example.invalid/ ")
        queue = setup.build_drive_task_queue(
            {"GOOGLE_CLOUD_PROJECT": "p", "SERVICE_ACCOUNT_EMAIL": "sa@example.invalid"}, env_config)
        assert queue.kwargs == {"project_id": "p", "location": "us-central1", "queue_name": "drive-sync-dev",
                                "service_url": "https://indexer.example.invalid",
                                "service_account_email": "sa@example.invalid"}

    def test_never_creates_the_queue(self, monkeypatch, env_config, recording_queue):
        """create_queue_if_not_exists would create it with concurrency 5 (§4)."""
        monkeypatch.setenv("DRIVE_INDEXER_URL", "https://indexer.example.invalid")
        queue = setup.build_drive_task_queue({"GOOGLE_CLOUD_PROJECT": "p"}, env_config)
        assert queue.created is False


def test_place_lookup_uses_the_bundled_table(monkeypatch):
    rows = [PlaceRow(41.9028, 12.4964, "Rome", "Roma", "IT")]
    monkeypatch.setattr(setup, "load_place_rows", lambda: rows)
    place_of = setup.build_place_lookup()
    # geo table v2 (§25.4.3): the display name and its `place_forms`, written together with the geo stamp
    found = place_of(41.90, 12.50)
    assert found.name == "Rome (Roma), IT" and {"Rome", "Roma"} <= set(found.forms)
    assert place_of(0.0, 0.0) is None


async def test_scheduler_lists_connections_from_the_connection_repository():
    """MULTI_DRIVE_RFC §8: the fan-out lists ACTIVE connections with indexing on from the connection repository (it
    no longer lists users by the drive provider's credentials)."""
    connections = FakeDriveConnectionRepository()
    await connections.create(DriveConnection(
        connection_id="conn-1", user_id="usr-1", account_id="acc-1", credential_id="cred-1",
        kind="microsoft_onedrive", drive_identity="microsoft_onedrive:drv-1", ref_tag="a1",
        indexing_enabled=True, created_at=datetime(2026, 10, 30, tzinfo=timezone.utc)))
    repo = AsyncMock()
    repo.list_sync_scopes.return_value = []
    repo.get_sync_state.return_value = None
    scheduler = setup.build_drive_sync_scheduler(repo=repo, queue=AsyncMock(), connections=connections,
                                                 connection_service=None, notifier=None, alert_sink=None)
    summary = await scheduler.fan_out()
    assert summary["candidates"] == 1


def _container(monkeypatch, **extra):
    monkeypatch.setenv("APP_ENV", "test")
    config = {
        "GEMINI_API_KEY": "k", "ANTHROPIC_API_KEY": "k",
        "GOOGLE_OAUTH_CLIENT_ID": "k", "GOOGLE_OAUTH_CLIENT_SECRET": "k",
        "CONSOLIDATION": ConsolidationSettings(), "GCS_MEDIA_BUCKET": "",
        **extra,
    }
    return ServiceContainer(cipher=AesGcmTestCipher(), config=config, db_client=MagicMock(), env_config=EnvironmentConfig(),
                            account_repo=AsyncMock(spec=AccountRepository))


class TestContainerDriveIndexWiring:
    def test_absent_without_microsoft_app_registration(self, monkeypatch):
        c = _container(monkeypatch)
        assert c.drive_index_repo is None and c.drive_task_queue is None

    def test_repo_without_queue_when_indexer_url_unset(self, monkeypatch):
        monkeypatch.delenv("DRIVE_INDEXER_URL", raising=False)
        c = _container(monkeypatch, MICROSOFT_TODO_CLIENT_ID="cid", MICROSOFT_TODO_CLIENT_SECRET="sec")
        assert c.drive_index_repo is not None and c.drive_task_queue is None
        assert c.user_drive_service._nudge_queue is None

    def test_nudges_go_to_the_drive_queue(self, monkeypatch, recording_queue):
        monkeypatch.setenv("DRIVE_INDEXER_URL", "https://indexer.example.invalid")
        c = _container(monkeypatch, MICROSOFT_TODO_CLIENT_ID="cid", MICROSOFT_TODO_CLIENT_SECRET="sec",
                       GOOGLE_CLOUD_PROJECT="p")
        assert c.drive_task_queue is recording_queue.instances[0]
        assert c.user_drive_service._nudge_queue is c.drive_task_queue
