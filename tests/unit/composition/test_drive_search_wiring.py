"""Drive search reaches FileManagementAgent and the Cabinet only through composition
(USER_DRIVE_INDEXING_RFC §10, §13)."""
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from src.agents.file_management_agent import FileManagementAgent
from src.composition import service_container, user_agent_factory
from src.composition.service_container import ServiceContainer
from src.config.environment import EnvironmentConfig
from src.domain.settings import ConsolidationSettings
from src.ports.account_repository import AccountRepository
from src.ports.user_drive_port import UserDrivePort
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from tests.support.aes_gcm_cipher import AesGcmTestCipher

_MAIN = Path(__file__).resolve().parents[3] / "main.py"


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


class TestContainer:
    def test_absent_without_a_drive(self, monkeypatch):
        c = _container(monkeypatch)
        assert c.drive_search_service is None
        assert c.agent_services()["drive_search_service"] is None

    def test_follows_the_drive_and_the_index_repository(self, monkeypatch):
        # The wiring is under test, not the adapter: any UserDrivePort will do.
        monkeypatch.setattr(service_container, "OneDriveAdapter", lambda **kw: MagicMock(spec=UserDrivePort))
        c = _container(monkeypatch, MICROSOFT_TODO_CLIENT_ID="cid", MICROSOFT_TODO_CLIENT_SECRET="sec")
        assert c.user_drive is not None and c.drive_index_repo is not None   # drive indexing wires the repository
        assert c.drive_search_service is not None
        assert c.drive_search_service._repo is c.drive_index_repo
        assert c.drive_search_service._drive is c.user_drive
        assert c.drive_search_service._embeddings is c.embedding_service
        assert c.agent_services()["drive_search_service"] is c.drive_search_service


def test_factory_hands_search_to_the_agent():
    assert "drive_search_service" in inspect.signature(user_agent_factory.UserAgentFactory.__init__).parameters
    src = inspect.getsource(user_agent_factory.UserAgentFactory._build_file_management)
    assert "drive_search=self.drive_search_service" in src


def test_agent_accepts_search():
    assert "drive_search" in inspect.signature(FileManagementAgent.__init__).parameters


def test_cabinet_gets_search_and_the_drive_queue():
    params = inspect.signature(create_user_cabinet_blueprint).parameters
    assert {"drive_search_service", "drive_task_queue"} <= set(params)
    # main() still calls the Cabinet factory; the container now reaches it through the application graph (P9 T2).
    main = _MAIN.read_text(encoding="utf-8")
    assert "drive_search_service=app.container.drive_search_service" in main
    assert "drive_task_queue=app.container.drive_task_queue" in main
