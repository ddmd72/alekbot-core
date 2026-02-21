"""
OAuth Provider Registry (OAuth Multi-Tenant Session 3).

Centralized registry for OAuth authentication providers.
Follows Service Locator pattern similar to ProviderRegistry for LLM services.

RFC: docs/10_rfcs/MULTI_TENANT_OAUTH_RFC.md
"""
from typing import Dict, Optional

from ..ports.auth_port import AuthPort
from ..utils.logger import logger


class AuthProviderRegistry:
    """
    OAuth provider registry.

    Receives pre-built AuthPort instances from the composition root (main.py).
    Adapters are created outside this class, keeping services → ports direction clean.

    Usage:
        from src.adapters.firebase_auth_adapter import FirebaseAuthAdapter
        firebase = FirebaseAuthAdapter(project_id=..., ...)
        registry = AuthProviderRegistry(providers={"firebase": firebase})
        provider = registry.get_provider("firebase")

    Future Enhancement (Phase 2):
        - Support multiple providers simultaneously
        - Provider-specific configuration overrides
        - Runtime provider switching
    """

    def __init__(
        self,
        providers: Dict[str, AuthPort],
        default_provider_name: str = "firebase",
    ):
        """
        Initialize auth provider registry with pre-built provider instances.

        Args:
            providers: Mapping of provider_name → AuthPort implementation.
            default_provider_name: Name used when get_provider() is called without args.
        """
        if not providers:
            raise ValueError("No OAuth providers configured")
        self._providers: Dict[str, AuthPort] = providers
        self._default_provider_name = default_provider_name
        logger.info(f"✅ OAuth providers registered: {list(self._providers.keys())}")

    def get_provider(self, provider_name: Optional[str] = None) -> AuthPort:
        """
        Get OAuth provider by name.

        Args:
            provider_name: Provider identifier ("firebase", "cognito", etc.)
                          If None, returns the default provider.

        Returns:
            AuthPort implementation for the specified provider

        Raises:
            ValueError: Provider not found or not configured
        """
        if provider_name is None:
            provider_name = self._default_provider_name

        if provider_name not in self._providers:
            available = list(self._providers.keys())
            raise ValueError(
                f"Auth provider '{provider_name}' not registered. "
                f"Available: {available}"
            )

        return self._providers[provider_name]

    def get_default_provider(self) -> AuthPort:
        """
        Get default OAuth provider.

        Returns:
            AuthPort implementation for default provider
        """
        return self.get_provider(self._default_provider_name)

    def list_available_providers(self) -> list[str]:
        """
        List all registered OAuth provider names.

        Returns:
            List of provider identifiers (e.g., ["firebase"])
        """
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
