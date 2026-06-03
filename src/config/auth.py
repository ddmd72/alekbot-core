"""
OAuth Authentication Configuration (OAuth Multi-Tenant Session 3).

Runtime configuration for OAuth providers, loaded from environment variables.
Follows the same pattern as environment.py for consistency.

RFC: docs/10_rfcs/MULTI_TENANT_OAUTH_RFC.md
"""
import os
from typing import Optional
from enum import Enum


# Placeholder used ONLY for local development when OAUTH_SESSION_SECRET is unset.
# It signs Cabinet JWT access/refresh tokens, so in any deployed environment it
# MUST be overridden by a real secret (Secret Manager) — see validate().
_DEV_DEFAULT_SESSION_SECRET = "dev-secret-change-in-production-must-be-32-chars-long"


class AuthProvider(Enum):
    """Supported OAuth authentication providers."""
    FIREBASE = "firebase"
    COGNITO = "cognito"  # AWS Cognito (Phase 2)
    OKTA = "okta"  # Okta (Phase 3)
    AUTH0 = "auth0"  # Auth0 (Phase 3)


class AuthConfig:
    """
    OAuth authentication configuration manager.

    Loads OAuth provider settings from environment variables or config dict.
    Supports multiple providers (MVP: Firebase only).

    Environment Variables:
        # Firebase (MVP)
        FIREBASE_PROJECT_ID: GCP project ID
        FIREBASE_WEB_API_KEY: Firebase Web API key
        GOOGLE_APPLICATION_CREDENTIALS: Service account path (optional)

        # OAuth Flow
        OAUTH_REDIRECT_URI: OAuth callback URL
        OAUTH_SESSION_SECRET: CSRF protection secret
        DEFAULT_AUTH_PROVIDER: Provider name (default: firebase)

        # Future: AWS Cognito, Okta, Auth0
    """

    def __init__(self, config: Optional[dict] = None):
        """
        Initialize AuthConfig from config dict or environment variables.
        
        Args:
            config: Configuration dictionary from load_settings() (optional)
        """
        self.default_provider = self._detect_default_provider()

        # Firebase configuration (MVP)
        self.firebase_project_id = self._get("FIREBASE_PROJECT_ID", "alek-core-dev", config)
        self.firebase_web_api_key = self._get("FIREBASE_WEB_API_KEY", "", config)
        self.firebase_service_account = self._get("GOOGLE_APPLICATION_CREDENTIALS", None, config)

        # Google OAuth credentials
        self.google_oauth_client_id = self._get("GOOGLE_OAUTH_CLIENT_ID", "", config)
        self.google_oauth_client_secret = self._get("GOOGLE_OAUTH_CLIENT_SECRET", "", config)

        # OAuth flow configuration
        self.oauth_redirect_uri = self._get(
            "OAUTH_REDIRECT_URI",
            "http://localhost:8080/auth/callback",  # Dev default
            config
        )
        self.gmail_oauth_redirect_uri = self._get(
            "GMAIL_OAUTH_REDIRECT_URI",
            "http://localhost:5001/auth/connect-gmail/callback",  # Dev default
            config
        )
        self.google_tasks_oauth_redirect_uri = self._get(
            "GOOGLE_TASKS_OAUTH_REDIRECT_URI",
            "http://localhost:5001/auth/connect-google-tasks/callback",  # Dev default
            config
        )
        # CRITICAL: Load from config (Secret Manager) or fall back to the local
        # dev placeholder. validate() rejects the placeholder in deployed envs.
        self.oauth_session_secret = self._get(
            "OAUTH_SESSION_SECRET",
            _DEV_DEFAULT_SESSION_SECRET,
            config
        )

        # Token TTLs (seconds)
        self.access_token_ttl = int(self._get("ACCESS_TOKEN_TTL", "3600", config))  # 1 hour
        self.refresh_token_ttl = int(self._get("REFRESH_TOKEN_TTL", "2592000", config))  # 30 days

        # MCP (remote MCP server exposed to claude.ai custom connectors)
        # Canonical resource URI (RFC 8707) — must match public URL where /mcp is mounted.
        # Issued access tokens carry aud=<mcp_resource_uri>; SDK validates on every request.
        self.mcp_resource_uri = self._get(
            "MCP_RESOURCE_URI",
            "http://localhost:8080/mcp",  # Dev default
            config,
        )
        self.mcp_access_token_ttl = int(self._get("MCP_ACCESS_TOKEN_TTL", "3600", config))       # 1h
        self.mcp_refresh_token_ttl = int(self._get("MCP_REFRESH_TOKEN_TTL", "2592000", config))  # 30d
        self.mcp_auth_code_ttl = int(self._get("MCP_AUTH_CODE_TTL", "600", config))              # 10min
        self.mcp_consent_request_ttl = int(self._get("MCP_CONSENT_REQUEST_TTL", "600", config))  # 10min

    def _get(self, key: str, default: any, config: Optional[dict] = None) -> any:
        """Get value from config dict or environment variable."""
        if config and key in config:
            return config[key]
        return os.getenv(key, default)

    def _detect_default_provider(self) -> AuthProvider:
        """
        Detect default authentication provider from environment.

        Returns:
            AuthProvider enum value (default: FIREBASE for MVP)
        """
        provider_str = os.getenv("DEFAULT_AUTH_PROVIDER", "firebase").lower()

        # Map string to enum
        provider_map = {p.value: p for p in AuthProvider}
        return provider_map.get(provider_str, AuthProvider.FIREBASE)

    @property
    def is_firebase(self) -> bool:
        """Check if Firebase is the default provider."""
        return self.default_provider == AuthProvider.FIREBASE

    def validate(self) -> None:
        """
        Validate required configuration is present.

        Raises:
            ValueError: Missing required environment variables
        """
        if self.is_firebase:
            if not self.firebase_project_id:
                raise ValueError("FIREBASE_PROJECT_ID environment variable required")
            if not self.firebase_web_api_key:
                raise ValueError("FIREBASE_WEB_API_KEY environment variable required")
            if not self.google_oauth_client_id:
                raise ValueError("GOOGLE_OAUTH_CLIENT_ID environment variable required")
            if not self.google_oauth_client_secret:
                raise ValueError("GOOGLE_OAUTH_CLIENT_SECRET environment variable required")

        if not self.oauth_redirect_uri:
            raise ValueError("OAUTH_REDIRECT_URI environment variable required")

        if not self.oauth_session_secret or len(self.oauth_session_secret) < 32:
            raise ValueError(
                "OAUTH_SESSION_SECRET must be set and at least 32 characters"
            )

        # Fail closed: the dev placeholder is public (it lives in source control),
        # so it must never sign real tokens. K_SERVICE is injected by Cloud Run and
        # reliably distinguishes a deployed environment from a local laptop run.
        if (
            self.oauth_session_secret == _DEV_DEFAULT_SESSION_SECRET
            and os.getenv("K_SERVICE")
        ):
            raise ValueError(
                "OAUTH_SESSION_SECRET is the built-in dev placeholder in a deployed "
                "environment — set a real secret (Secret Manager). Refusing to start."
            )
