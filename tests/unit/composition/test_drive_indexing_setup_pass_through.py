"""Drive indexing composition passes every collaborator through and uses the real repository (RFC §13)."""
from unittest.mock import AsyncMock, MagicMock

from src.adapters.firestore_drive_index_repo import FirestoreDriveIndexRepository
from src.composition import drive_indexing_setup as setup
from src.composition.service_container import ServiceContainer
from src.config.environment import EnvironmentConfig
from src.domain.settings import ConsolidationSettings
from src.ports.account_repository import AccountRepository
from src.ports.oauth_credentials_port import OAuthCredentialsPort
from tests.support.aes_gcm_cipher import AesGcmTestCipher


class TestSchedulerBuilderPassThrough:
    def test_notifier_alert_sink_and_ensure_agents_reach_the_service(self):
        """Dropping any of them silences the reconnect notice or the alert."""
        notifier, alert_sink = object(), object()

        async def ensure_agents(user_id: str) -> dict:
            return {}

        scheduler = setup.build_drive_sync_scheduler(
            repo=AsyncMock(), queue=AsyncMock(), connections=AsyncMock(), connection_service=AsyncMock(),
            notifier=notifier,
            alert_sink=alert_sink, ensure_agents=ensure_agents)
        assert scheduler._notifier is notifier
        assert scheduler._alert_sink is alert_sink
        assert scheduler._ensure_agents is ensure_agents


class TestContainerDriveIndexRepositoryConcrete:
    def test_configured_container_uses_the_firestore_repository(self, monkeypatch):
        """The main service reads the real index, never an in-memory stand-in."""
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.delenv("DRIVE_INDEXER_URL", raising=False)
        config = {
            "GEMINI_API_KEY": "k", "ANTHROPIC_API_KEY": "k",
            "GOOGLE_OAUTH_CLIENT_ID": "k", "GOOGLE_OAUTH_CLIENT_SECRET": "k",
            "CONSOLIDATION": ConsolidationSettings(), "GCS_MEDIA_BUCKET": "",
            "MICROSOFT_TODO_CLIENT_ID": "cid", "MICROSOFT_TODO_CLIENT_SECRET": "sec",
        }
        c = ServiceContainer(cipher=AesGcmTestCipher(), config=config, db_client=MagicMock(), env_config=EnvironmentConfig(),
                             account_repo=AsyncMock(spec=AccountRepository))
        assert isinstance(c.drive_index_repo, FirestoreDriveIndexRepository)
