"""
Smoke test for ServiceContainer wiring.

Guards against the class of bug where a service gets silently constructed
without a required port dependency because the parameter has an `Optional`
default. Not a logic test — only checks that the composition root wires
critical collaborators through to the services that need them.

Motivated by the 2026-04-13 incident where EmailIndexingService was
instantiated without `oauth=`, causing daily `oauth not configured`
warnings and no email indexing runs until the wiring was fixed.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.composition.service_container import ServiceContainer
from src.config.environment import EnvironmentConfig
from src.domain.settings import ConsolidationSettings
from src.ports.account_repository import AccountRepository


@pytest.fixture
def fake_config():
    """
    Minimal config dict covering every key ServiceContainer reads on init.

    API keys are dummy strings — LLM/embedding adapters must accept them
    at construction time without making network calls. Optional providers
    (Grok/OpenAI/MS Todo) are intentionally omitted to exercise the
    graceful-skip branches.
    """
    return {
        "GEMINI_API_KEY": "dummy-gemini-key",
        "ANTHROPIC_API_KEY": "dummy-claude-key",
        "GOOGLE_OAUTH_CLIENT_ID": "dummy-google-client-id",
        "GOOGLE_OAUTH_CLIENT_SECRET": "dummy-google-client-secret",
        "CONSOLIDATION": ConsolidationSettings(),
        "GCS_MEDIA_BUCKET": "",
    }


@pytest.fixture
def container(monkeypatch, fake_config):
    """Construct a real ServiceContainer with fake config + mocked db_client."""
    monkeypatch.setenv("APP_ENV", "test")
    env_config = EnvironmentConfig()
    db_client = MagicMock(name="firestore_db_client")
    account_repo = AsyncMock(spec=AccountRepository)
    return ServiceContainer(
        config=fake_config,
        db_client=db_client,
        env_config=env_config,
        account_repo=account_repo,
    )


class TestServiceContainerWiring:
    """
    Each assertion protects a composition invariant that is NOT unit-testable
    at the service level: the service under test cannot tell whether its
    optional-port parameter was injected or left at the default None.
    """

    def test_email_indexing_service_has_oauth_port(self, container):
        """
        Regression guard for 2026-04-13: EmailIndexingService was constructed
        without oauth=, silently returning "oauth not configured" on every
        worker call path.
        """
        assert container.email_indexing_service is not None
        assert container.email_indexing_service._oauth is container.oauth_credentials

    def test_email_search_service_has_oauth_port(self, container):
        """Same structural guard for the sibling EmailSearchService."""
        assert container.email_search_service is not None
        assert container.email_search_service._oauth is container.oauth_credentials

    def test_email_review_service_has_oauth_port(self, container):
        """Same structural guard for EmailReviewService."""
        assert container.email_review_service is not None
        assert container.email_review_service._oauth is container.oauth_credentials

    def test_biographical_context_circular_dep_resolved(self, container):
        """
        BiographicalContextService has a deferred repository injection
        (constructed with None, then resolved via set_repository after
        FirestoreFactRepository is built). Verify the circular dep
        is actually wired at the end of ServiceContainer.__init__.
        """
        bio = container.biographical_context_service
        assert bio is not None
        assert bio._repo is container.repository

    def test_registry_contains_core_providers(self, container):
        """
        Gemini + Claude must always be registered (required keys are in
        fake_config). Grok and OpenAI are absent → registry should NOT
        contain them, which exercises the skip branches in _init_grok /
        _init_openai.
        """
        assert container.registry.get("gemini") is not None
        assert container.registry.get("claude") is not None
        # Grok/OpenAI keys not in fake_config → skipped
        assert container.grok_service is None
        assert container.openai_service is None

    def test_ms_todo_adapter_absent_when_creds_missing(self, container):
        """
        MS To Do adapter is conditional on MICROSOFT_TODO_CLIENT_ID/SECRET.
        Absent in fake_config → adapter + task_indexing both None.
        """
        assert container.ms_todo_adapter is None
        assert container.task_indexing is None

    def test_file_storage_absent_when_bucket_missing(self, container):
        """GCS_MEDIA_BUCKET="" → file_storage + file_conversion_service both None."""
        assert container.file_storage is None
        assert container.file_conversion_service is None

    def test_prompt_content_store_absent_by_default(self, container):
        """No DEBUG_PROMPTS + no dataset → capture off → store is None."""
        assert container.prompt_content_store is None


class TestPromptCaptureFlag:
    """DEBUG_PROMPTS is the global capture switch; it gates whether the store is
    wired at all. The wired adapter is BigQuery."""

    @staticmethod
    def _build(monkeypatch, fake_config, **overrides):
        monkeypatch.setenv("APP_ENV", "test")
        cfg = {**fake_config, **overrides}
        return ServiceContainer(
            config=cfg,
            db_client=MagicMock(),
            env_config=EnvironmentConfig(),
            account_repo=AsyncMock(spec=AccountRepository),
        )

    def test_off_with_dataset_means_no_store(self, monkeypatch, fake_config):
        # Flag is the decider: dataset present but flag off → still None.
        c = self._build(
            monkeypatch, fake_config,
            DEBUG_PROMPTS="false", BIGQUERY_PROMPT_DATASET="ds",
        )
        assert c.prompt_content_store is None

    def test_on_without_dataset_means_no_store(self, monkeypatch, fake_config):
        # Flag on but no dataset to write to → None (dataset is required config).
        c = self._build(
            monkeypatch, fake_config,
            DEBUG_PROMPTS="true", BIGQUERY_PROMPT_DATASET="",
        )
        assert c.prompt_content_store is None

    def test_on_with_dataset_wires_bigquery(self, monkeypatch, fake_config):
        from src.adapters.bigquery_prompt_content_adapter import BigQueryPromptContentAdapter
        c = self._build(
            monkeypatch, fake_config,
            DEBUG_PROMPTS="true", BIGQUERY_PROMPT_DATASET="ds",
            GOOGLE_CLOUD_PROJECT="proj",
        )
        assert isinstance(c.prompt_content_store, BigQueryPromptContentAdapter)
