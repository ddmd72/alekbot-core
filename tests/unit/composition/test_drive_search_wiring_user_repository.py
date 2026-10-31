"""The drive search wiring gives the search service the drive connection service (USER_DRIVE_INDEXING_RFC §10, §13;
MULTI_DRIVE_RFC §3, §6).

The search service reads the opt-in flag fresh on every search (§10.1 step 0); the flag now lives on the connection
(MULTI_DRIVE_RFC §3 "Removed"), read through the connection service. Without one, every search raises in production."""
from unittest.mock import AsyncMock, MagicMock

from src.composition import service_container
from src.composition.service_container import ServiceContainer
from src.config.environment import EnvironmentConfig
from src.domain.settings import ConsolidationSettings
from src.ports.account_repository import AccountRepository
from src.ports.user_drive_port import UserDrivePort
from tests.support.aes_gcm_cipher import AesGcmTestCipher


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


class TestSearchUserRepository:
    def test_search_gets_the_drive_connection_service(self, monkeypatch):
        monkeypatch.setattr(service_container, "OneDriveAdapter", lambda **kw: MagicMock(spec=UserDrivePort))
        c = _container(monkeypatch, MICROSOFT_TODO_CLIENT_ID="cid", MICROSOFT_TODO_CLIENT_SECRET="sec")
        assert c.drive_search_service is not None
        assert c.drive_connection_service is not None
        assert c.drive_search_service._connections is c.drive_connection_service
