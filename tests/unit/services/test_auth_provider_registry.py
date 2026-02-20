"""
Unit tests for AuthProviderRegistry (OAuth Multi-Tenant Session 3).

Tests OAuth provider registry without initializing actual Firebase connections.

RFC: docs/10_rfcs/MULTI_TENANT_OAUTH_RFC.md
"""
import pytest
from unittest.mock import Mock, patch

from src.services.auth_provider_registry import AuthProviderRegistry
from src.config.auth import AuthConfig, AuthProvider


# ============================================================================
# OAuth Multi-Tenant Session 3: AuthProviderRegistry Basic Tests
# ============================================================================
def test_auth_provider_registry_initialization():
    """Test registry initializes with default config."""
    registry = AuthProviderRegistry()
    assert registry.auth_config is not None
    assert registry.auth_config.default_provider == AuthProvider.FIREBASE


def test_auth_provider_registry_custom_config():
    """Test registry initializes with custom config."""
    config = AuthConfig()
    config.firebase_project_id = "custom-project"

    registry = AuthProviderRegistry(auth_config=config)
    assert registry.auth_config.firebase_project_id == "custom-project"


# ============================================================================
# OAuth Multi-Tenant Session 3: Provider Registration Tests (Mocked)
# ============================================================================
@patch("src.services.auth_provider_registry.FirebaseAuthAdapter")
def test_auth_provider_registry_get_default_provider(mock_firebase_adapter):
    """Test get_default_provider returns Firebase adapter."""
    # Mock Firebase adapter initialization
    mock_adapter_instance = Mock()
    mock_firebase_adapter.return_value = mock_adapter_instance

    registry = AuthProviderRegistry()
    provider = registry.get_default_provider()

    assert provider == mock_adapter_instance
    mock_firebase_adapter.assert_called_once()


@patch("src.services.auth_provider_registry.FirebaseAuthAdapter")
def test_auth_provider_registry_get_provider_by_name(mock_firebase_adapter):
    """Test get_provider with explicit provider name."""
    mock_adapter_instance = Mock()
    mock_firebase_adapter.return_value = mock_adapter_instance

    registry = AuthProviderRegistry()
    provider = registry.get_provider("firebase")

    assert provider == mock_adapter_instance


@patch("src.services.auth_provider_registry.FirebaseAuthAdapter")
def test_auth_provider_registry_get_provider_invalid_name(mock_firebase_adapter):
    """Test get_provider raises error for invalid provider."""
    mock_adapter_instance = Mock()
    mock_firebase_adapter.return_value = mock_adapter_instance

    registry = AuthProviderRegistry()

    with pytest.raises(ValueError, match="Auth provider 'invalid' not registered"):
        registry.get_provider("invalid")


@patch("src.services.auth_provider_registry.FirebaseAuthAdapter")
def test_auth_provider_registry_list_available_providers(mock_firebase_adapter):
    """Test list_available_providers returns Firebase."""
    mock_adapter_instance = Mock()
    mock_firebase_adapter.return_value = mock_adapter_instance

    registry = AuthProviderRegistry()
    providers = registry.list_available_providers()

    assert providers == ["firebase"]


# ============================================================================
# OAuth Multi-Tenant Session 3: External User ID Parsing Tests
# ============================================================================
@patch("src.services.auth_provider_registry.FirebaseAuthAdapter")
def test_auth_provider_registry_parse_external_user_id_valid(mock_firebase_adapter):
    """Test parse_external_user_id with valid format."""
    mock_adapter_instance = Mock()
    mock_firebase_adapter.return_value = mock_adapter_instance

    registry = AuthProviderRegistry()
    provider, subject = registry.parse_external_user_id("firebase|abc123")

    assert provider == "firebase"
    assert subject == "abc123"


@patch("src.services.auth_provider_registry.FirebaseAuthAdapter")
def test_auth_provider_registry_parse_external_user_id_cognito(mock_firebase_adapter):
    """Test parse_external_user_id with future provider (Cognito)."""
    mock_adapter_instance = Mock()
    mock_firebase_adapter.return_value = mock_adapter_instance

    registry = AuthProviderRegistry()
    provider, subject = registry.parse_external_user_id("cognito|xyz789")

    assert provider == "cognito"
    assert subject == "xyz789"


@patch("src.services.auth_provider_registry.FirebaseAuthAdapter")
def test_auth_provider_registry_parse_external_user_id_invalid_no_pipe(mock_firebase_adapter):
    """Test parse_external_user_id raises error for missing pipe."""
    mock_adapter_instance = Mock()
    mock_firebase_adapter.return_value = mock_adapter_instance

    registry = AuthProviderRegistry()

    with pytest.raises(ValueError, match="Invalid external_user_id format"):
        registry.parse_external_user_id("firebase-abc123")


@patch("src.services.auth_provider_registry.FirebaseAuthAdapter")
def test_auth_provider_registry_parse_external_user_id_invalid_empty_provider(mock_firebase_adapter):
    """Test parse_external_user_id raises error for empty provider."""
    mock_adapter_instance = Mock()
    mock_firebase_adapter.return_value = mock_adapter_instance

    registry = AuthProviderRegistry()

    with pytest.raises(ValueError, match="Both provider and subject must be non-empty"):
        registry.parse_external_user_id("|abc123")


@patch("src.services.auth_provider_registry.FirebaseAuthAdapter")
def test_auth_provider_registry_parse_external_user_id_invalid_empty_subject(mock_firebase_adapter):
    """Test parse_external_user_id raises error for empty subject."""
    mock_adapter_instance = Mock()
    mock_firebase_adapter.return_value = mock_adapter_instance

    registry = AuthProviderRegistry()

    with pytest.raises(ValueError, match="Both provider and subject must be non-empty"):
        registry.parse_external_user_id("firebase|")


# ============================================================================
# OAuth Multi-Tenant Session 3: Lazy Initialization Tests
# ============================================================================
@patch("src.services.auth_provider_registry.FirebaseAuthAdapter")
def test_auth_provider_registry_lazy_initialization(mock_firebase_adapter):
    """Test providers are only initialized on first access."""
    mock_adapter_instance = Mock()
    mock_firebase_adapter.return_value = mock_adapter_instance

    registry = AuthProviderRegistry()

    # No initialization yet
    assert not registry._initialized

    # First access triggers initialization
    registry.get_default_provider()
    assert registry._initialized

    # Second access doesn't re-initialize
    mock_firebase_adapter.reset_mock()
    registry.get_default_provider()
    mock_firebase_adapter.assert_not_called()
