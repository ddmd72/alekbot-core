"""The drive search wiring gives the search service a user repository (USER_DRIVE_INDEXING_RFC §10, §13).

The search service reads the opt-in flag uncached through its user repository on every search (§10.1 step 0).
Without one, every search raises in production."""
from unittest.mock import AsyncMock, MagicMock

from src.adapters.firestore_user_repo import FirestoreUserRepository
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
    def test_search_gets_a_firestore_user_repository(self, monkeypatch):
        monkeypatch.setattr(service_container, "OneDriveAdapter", lambda **kw: MagicMock(spec=UserDrivePort))
        c = _container(monkeypatch, MICROSOFT_TODO_CLIENT_ID="cid", MICROSOFT_TODO_CLIENT_SECRET="sec")
        assert c.drive_search_service is not None
        assert isinstance(c.drive_search_service._users, FirestoreUserRepository)
