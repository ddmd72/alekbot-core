"""
OAuth Provider Registry (OAuth Multi-Tenant Session 3).

Centralized registry for OAuth authentication providers.
Follows Service Locator pattern similar to ProviderRegistry for LLM services.

RFC: docs/10_rfcs/MULTI_TENANT_OAUTH_RFC.md
"""
from typing import Dict, Optional

from ..ports.auth_port import AuthPort
from ..adapters.firebase_auth_adapter import FirebaseAuthAdapter
from ..config.auth import AuthConfig, AuthProvider
from ..utils.logger import logger


class AuthProviderRegistry:
    """
    OAuth provider registry with lazy initialization.

    Manages OAuth authentication providers (Firebase, AWS Cognito, Okta, etc.).
    Provides unified interface for application layer to access auth providers.

    Usage:
        registry = AuthProviderRegistry(auth_config)
        firebase = registry.get_provider("firebase")
        tokens = await firebase.exchange_code_for_tokens(code, redirect_uri)

    Future Enhancement (Phase 2):
        - Support multiple providers simultaneously
        - Provider-specific configuration overrides
        - Runtime provider switching
    """

    def __init__(self, auth_config: Optional[AuthConfig] = None):
        """
        Initialize auth provider registry.

        Args:
            auth_config: OAuth configuration (defaults to new AuthConfig())
        """
        self.auth_config = auth_config or AuthConfig()
        self._providers: Dict[str, AuthPort] = {}
        self._initialized = False

    def _initialize_providers(self) -> None:
        """
        Lazy initialization of OAuth providers.

        Creates provider instances based on configuration.
        Only initializes providers once on first access.
        """
        if self._initialized:
            return

        # Initialize Firebase (MVP)
        if self.auth_config.is_firebase:
            try:
                firebase_adapter = FirebaseAuthAdapter(
                    project_id=self.auth_config.firebase_project_id,
                    web_api_key=self.auth_config.firebase_web_api_key,
                    service_account_path=self.auth_config.firebase_service_account,
                    oauth_client_id=self.auth_config.google_oauth_client_id,
                    oauth_client_secret=self.auth_config.google_oauth_client_secret,
                )
                self._providers["firebase"] = firebase_adapter
                logger.info("🔐 Firebase OAuth provider registered")
            except Exception as e:
                logger.error(f"Failed to initialize Firebase auth provider: {e}")
                raise ValueError(f"Firebase auth initialization failed: {e}")

        # Future: Initialize other providers (AWS Cognito, Okta, Auth0)
        # if self.auth_config.default_provider == AuthProvider.COGNITO:
        #     self._providers["cognito"] = CognitoAuthAdapter(...)

        self._initialized = True

        if not self._providers:
            raise ValueError("No OAuth providers configured")

        logger.info(f"✅ OAuth providers initialized: {list(self._providers.keys())}")

    def get_provider(self, provider_name: Optional[str] = None) -> AuthPort:
        """
        Get OAuth provider by name.

        Args:
            provider_name: Provider identifier ("firebase", "cognito", etc.)
                          If None, returns default provider from config

        Returns:
            AuthPort implementation for the specified provider

        Raises:
            ValueError: Provider not found or not configured
        """
        # Lazy initialization
        if not self._initialized:
            self._initialize_providers()

        # Use default provider if not specified
        if provider_name is None:
            provider_name = self.auth_config.default_provider.value

        if provider_name not in self._providers:
            available = list(self._providers.keys())
            raise ValueError(
                f"Auth provider '{provider_name}' not registered. "
                f"Available: {available}"
            )

        return self._providers[provider_name]

    def get_default_provider(self) -> AuthPort:
        """
        Get default OAuth provider from configuration.

        Returns:
            AuthPort implementation for default provider
        """
        return self.get_provider(self.auth_config.default_provider.value)

    def list_available_providers(self) -> list[str]:
        """
        List all registered OAuth provider names.

        Returns:
            List of provider identifiers (e.g., ["firebase"])
        """
        if not self._initialized:
            self._initialize_providers()

        return list(self._providers.keys())

    def parse_external_user_id(self, external_user_id: str) -> tuple[str, str]:
        """
        Parse external_user_id into provider and subject.

        External user IDs are formatted as: "{provider}|{sub}"
        Example: "firebase|abc123" → ("firebase", "abc123")

        Args:
            external_user_id: OAuth identity string

        Returns:
            Tuple of (provider_name, subject_id)

        Raises:
            ValueError: Invalid format

        Note:
            Used by AuthenticationService to determine which provider
            to use for token operations.
        """
        if "|" not in external_user_id:
            raise ValueError(
                f"Invalid external_user_id format: '{external_user_id}'. "
                f"Expected: 'provider|subject'"
            )

        provider, subject = external_user_id.split("|", 1)

        if not provider or not subject:
            raise ValueError(
                f"Invalid external_user_id format: '{external_user_id}'. "
                f"Both provider and subject must be non-empty"
            )

        return provider, subject
