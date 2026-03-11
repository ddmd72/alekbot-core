"""
Unit tests for AuthProviderRegistry (OAuth Multi-Tenant Session 3).

Tests OAuth provider registry with pre-built provider instances.
The registry no longer creates adapters internally — they are injected
from the composition root (main.py), keeping services → ports direction clean.

RFC: docs/10_rfcs/MULTI_TENANT_OAUTH_RFC.md
"""
import pytest
from unittest.mock import Mock

from src.services.auth_provider_registry import AuthProviderRegistry


def _make_registry(**extra):
    """Helper: create registry with a mock firebase provider."""
    mock_firebase = Mock()
    providers = {"firebase": mock_firebase, **extra}
    return AuthProviderRegistry(providers=providers), mock_firebase


# ============================================================================
# AuthProviderRegistry Basic Tests
# ============================================================================
def test_auth_provider_registry_initialization():
    """Test registry initializes with injected providers."""
    registry, _ = _make_registry()
    assert registry.list_available_providers() == ["firebase"]


def test_auth_provider_registry_custom_providers():
    """Test registry stores exactly the providers that were passed."""
    mock_a = Mock()
    mock_b = Mock()
    registry = AuthProviderRegistry(providers={"firebase": mock_a, "cognito": mock_b})
    assert set(registry.list_available_providers()) == {"firebase", "cognito"}


def test_auth_provider_registry_requires_providers():
    """Test that empty providers dict raises ValueError."""
    with pytest.raises(ValueError):
        AuthProviderRegistry(providers={})


# ============================================================================
# Provider lookup tests
# ============================================================================
def test_auth_provider_registry_get_default_provider():
    """Test get_default_provider returns the firebase provider."""
    registry, mock_firebase = _make_registry()
    provider = registry.get_default_provider()
    assert provider is mock_firebase


def test_auth_provider_registry_get_provider_by_name():
    """Test get_provider with explicit provider name."""
    registry, mock_firebase = _make_registry()
    provider = registry.get_provider("firebase")
    assert provider is mock_firebase


def test_auth_provider_registry_get_provider_invalid_name():
    """Test get_provider raises error for invalid provider."""
    registry, _ = _make_registry()
    with pytest.raises(ValueError, match="Auth provider 'invalid' not registered"):
        registry.get_provider("invalid")


def test_auth_provider_registry_list_available_providers():
    """Test list_available_providers returns registered provider names."""
    registry, _ = _make_registry()
    providers = registry.list_available_providers()
    assert providers == ["firebase"]


# ============================================================================
# External User ID Parsing Tests
# ============================================================================
def test_auth_provider_registry_parse_external_user_id_valid():
    """Test parse_external_user_id with valid format."""
    registry, _ = _make_registry()
    provider, subject = registry.parse_external_user_id("firebase|abc123")
    assert provider == "firebase"
    assert subject == "abc123"


def test_auth_provider_registry_parse_external_user_id_cognito():
    """Test parse_external_user_id with future provider (Cognito)."""
    registry, _ = _make_registry()
    provider, subject = registry.parse_external_user_id("cognito|xyz789")
    assert provider == "cognito"
    assert subject == "xyz789"


def test_auth_provider_registry_parse_external_user_id_invalid_no_pipe():
    """Test parse_external_user_id raises error for missing pipe."""
    registry, _ = _make_registry()
    with pytest.raises(ValueError, match="Invalid external_user_id format"):
        registry.parse_external_user_id("firebase-abc123")


def test_auth_provider_registry_parse_external_user_id_invalid_empty_provider():
    """Test parse_external_user_id raises error for empty provider."""
    registry, _ = _make_registry()
    with pytest.raises(ValueError, match="Both provider and subject must be non-empty"):
        registry.parse_external_user_id("|abc123")


def test_auth_provider_registry_parse_external_user_id_invalid_empty_subject():
    """Test parse_external_user_id raises error for empty subject."""
    registry, _ = _make_registry()
    with pytest.raises(ValueError, match="Both provider and subject must be non-empty"):
        registry.parse_external_user_id("firebase|")


# ============================================================================
# Custom default provider tests
# ============================================================================
def test_auth_provider_registry_custom_default_provider():
    """Test registry with non-firebase default provider name."""
    mock_a = Mock()
    mock_b = Mock()
    registry = AuthProviderRegistry(
        providers={"firebase": mock_a, "cognito": mock_b},
        default_provider_name="cognito"
    )
    assert registry.get_default_provider() is mock_b
