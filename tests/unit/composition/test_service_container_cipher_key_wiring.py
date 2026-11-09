"""ServiceContainer without an injected cipher builds the KMS cipher from its own config's `KMS_CRYPTO_KEY`
(the owner-run-script path); with no key it refuses to build rather than fall back to a no-op."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.gcp_kms_cipher_adapter import GcpKmsCipherAdapter
from src.composition.service_container import ServiceContainer
from src.config.environment import EnvironmentConfig
from src.domain.settings import ConsolidationSettings
from src.ports.account_repository import AccountRepository

KEY_NAME = "projects/p-test/locations/l-test/keyRings/r-test/cryptoKeys/k-test"


def _build(monkeypatch, **extra: object) -> ServiceContainer:
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
        **extra,
    }
    return ServiceContainer(config=config, db_client=MagicMock(), env_config=EnvironmentConfig(),
                            account_repo=AsyncMock(spec=AccountRepository))


def test_without_an_injected_cipher_the_credential_store_uses_the_kms_cipher_of_the_configured_key(monkeypatch):
    container = _build(monkeypatch, KMS_CRYPTO_KEY=KEY_NAME)

    cipher = container.oauth_credentials._cipher
    assert isinstance(cipher, GcpKmsCipherAdapter)
    assert KEY_NAME in vars(cipher).values()


def test_without_an_injected_cipher_and_without_a_key_the_container_refuses_to_build(monkeypatch):
    with pytest.raises(ValueError, match="KMS_CRYPTO_KEY"):
        _build(monkeypatch)
