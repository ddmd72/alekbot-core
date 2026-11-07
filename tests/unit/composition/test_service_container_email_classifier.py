"""ServiceContainer: the email indexing service's classifier is the per-batch runner, wired with what it bills and records
through (roadmap 14b). Its behaviour is in test_email_classifier_runner.py; this file pins the container's wiring."""
from unittest.mock import AsyncMock, MagicMock

from src.composition.email_classifier_runner import EmailClassifierRunner
from src.composition.service_container import ServiceContainer
from src.config.environment import EnvironmentConfig
from src.domain.settings import ConsolidationSettings
from src.ports.account_repository import AccountRepository
from src.ports.quota_service import QuotaService
from src.ports.user_repository import UserRepository
from tests.support.aes_gcm_cipher import AesGcmTestCipher


def _container(monkeypatch, **extra: object) -> ServiceContainer:
    monkeypatch.setenv("APP_ENV", "test")
    config = {
        "GEMINI_API_KEY": "dummy-gemini-key",
        "ANTHROPIC_API_KEY": "dummy-claude-key",
        "GOOGLE_OAUTH_CLIENT_ID": "dummy-google-client-id",
        "GOOGLE_OAUTH_CLIENT_SECRET": "dummy-google-client-secret",
        "CONSOLIDATION": ConsolidationSettings(),
        "GCS_MEDIA_BUCKET": "",
        "DEBUG_PROMPTS": "true",
        "BIGQUERY_PROMPT_DATASET": "observability",
    }
    return ServiceContainer(cipher=AesGcmTestCipher(), config=config, db_client=MagicMock(),
                            env_config=EnvironmentConfig(), account_repo=AsyncMock(spec=AccountRepository), **extra)


def test_the_indexing_service_classifies_through_the_runner_with_the_users_quota_and_content_store(monkeypatch):
    users = AsyncMock(spec=UserRepository)
    quota = AsyncMock(spec=QuotaService)

    container = _container(monkeypatch, user_repo=users, quota_service=quota)

    runner = container.email_indexing_service._classifier
    assert isinstance(runner, EmailClassifierRunner) and runner is container.email_classifier
    assert runner._user_repo is users
    assert runner._quota_service is quota
    assert runner._prompt_content_store is container.prompt_content_store is not None
    assert runner._context_builder is container.context_builder
    assert runner._gmail is container.gmail_provider
