from enum import Enum
import os


class Environment(Enum):
    """Environment types for Alek-Core."""
    DEVELOPMENT = "development"
    PRODUCTION = "production"
    TEST = "test"


class SlackMode(Enum):
    """Slack integration modes."""
    HTTP = "http"
    SOCKET = "socket"


class EnvironmentConfig:
    """
    Central environment configuration manager.

    Provides environment detection and environment-specific settings
    to ensure proper isolation between development and production.
    """

    def __init__(self):
        self.env = self._detect_environment()
        self.slack_mode = self._detect_slack_mode()

    def _detect_environment(self) -> Environment:
        """
        Detect current environment from APP_ENV variable.

        Returns:
            Environment enum value
        """
        env_str = os.getenv("APP_ENV", "development").lower()

        # Map string to enum
        env_map = {e.value: e for e in Environment}
        return env_map.get(env_str, Environment.DEVELOPMENT)

    def _detect_slack_mode(self) -> SlackMode:
        """
        Detect Slack integration mode from SLACK_MODE variable.

        Returns:
            SlackMode enum value (default: socket for dev, http for prod)
        """
        # Default to socket for local development, http for production
        default_mode = "socket" if self.env == Environment.DEVELOPMENT else "http"
        mode_str = os.getenv("SLACK_MODE", default_mode).lower()
        
        # Map string to enum
        mode_map = {m.value: m for m in SlackMode}
        return mode_map.get(mode_str, SlackMode.SOCKET)

    @property
    def is_production(self) -> bool:
        """Check if running in production environment."""
        return self.env == Environment.PRODUCTION

    @property
    def is_development(self) -> bool:
        """Check if running in development environment."""
        return self.env == Environment.DEVELOPMENT

    @property
    def is_test(self) -> bool:
        """Check if running in test environment."""
        return self.env == Environment.TEST

    @property
    def is_http_mode(self) -> bool:
        """Check if using HTTP Events API mode."""
        return self.slack_mode == SlackMode.HTTP

    @property
    def is_socket_mode(self) -> bool:
        """Check if using Socket Mode."""
        return self.slack_mode == SlackMode.SOCKET

    @property
    def firestore_collection_prefix(self) -> str:
        """
        Get Firestore collection prefix for environment isolation.

        Returns:
            Empty string for production, prefixed for others (e.g., "dev_")
        """
        return "" if self.is_production else f"{self.env.value}_"

    @property
    def firestore_database_id(self) -> str:
        """
        Get Firestore database ID.

        Both dev and prod use the 'us-production' named database.
        The '(default)' database is not used.

        Returns:
            Database ID from FIRESTORE_DATABASE env var, default 'us-production'
        """
        return os.getenv("FIRESTORE_DATABASE", "us-production")

    @property
    def use_oauth_collections(self) -> bool:
        """
        Check if OAuth collections should be used (_oauth suffix).

        Set USE_OAUTH_COLLECTIONS=true to use OAuth schema collections.
        Default: False (use original collections)

        Returns:
            True if OAuth collections should be used
        """
        return os.getenv("USE_OAUTH_COLLECTIONS", "false").lower() == "true"

    # =========================================================================
    # Semantic Collection Names (ADR-006)
    # =========================================================================

    # --- Domain Collections (Versioned) ---

    @property
    def domain_users_collection(self) -> str:
        """
        Get domain users collection (v2 schema).
        Dev: development_domain_users_v2
        Prod: domain_users_v2
        """
        prefix = self.firestore_collection_prefix
        return f"{prefix}domain_users_v2"

    @property
    def domain_accounts_collection(self) -> str:
        """
        Get domain accounts collection (v2 schema).
        Dev: development_domain_accounts_v2
        Prod: domain_accounts_v2
        """
        prefix = self.firestore_collection_prefix
        return f"{prefix}domain_accounts_v2"

    @property
    def domain_facts_collection(self) -> str:
        """
        Get domain facts collection (v2 schema).
        Dev: development_domain_facts_v2
        Prod: domain_facts_v2
        """
        prefix = self.firestore_collection_prefix
        return f"{prefix}domain_facts_v2"

    @property
    def domain_prompt_tokens_collection(self) -> str:
        """
        Get domain prompt tokens collection base (v3 schema).
        Actual collections will have _system and _user suffixes.
        Dev: development_domain_prompt_tokens_v3
        Prod: domain_prompt_tokens_v3
        """
        prefix = self.firestore_collection_prefix
        return f"{prefix}domain_prompt_tokens_v3"

    @property
    def domain_prompt_blueprints_collection(self) -> str:
        """
        Get domain prompt blueprints collection (v3 schema).
        Dev: development_domain_prompt_blueprints_v3
        Prod: domain_prompt_blueprints_v3
        """
        prefix = self.firestore_collection_prefix
        return f"{prefix}domain_prompt_blueprints_v3"

    @property
    def domain_prompt_profiles_collection(self) -> str:
        """
        Get domain prompt agent profiles collection (v3 schema).
        Dev: development_domain_prompt_profiles_v3
        Prod: domain_prompt_profiles_v3
        """
        prefix = self.firestore_collection_prefix
        return f"{prefix}domain_prompt_profiles_v3"

    @property
    def domain_prompt_overrides_collection(self) -> str:
        """
        Get domain prompt user overrides collection (v3 schema).
        Dev: development_domain_prompt_overrides_v3
        Prod: domain_prompt_overrides_v3
        """
        prefix = self.firestore_collection_prefix
        return f"{prefix}domain_prompt_overrides_v3"

    @property
    def domain_invite_codes_collection(self) -> str:
        """
        Get domain invite codes collection (v1 schema).
        Dev: development_domain_invite_codes_v1
        Prod: domain_invite_codes_v1
        """
        prefix = self.firestore_collection_prefix
        return f"{prefix}domain_invite_codes_v1"

    @property
    def domain_whitelist_collection(self) -> str:
        """
        Get domain whitelist collection (v1 schema).
        Dev: development_domain_whitelist_v1
        Prod: domain_whitelist_v1
        """
        prefix = self.firestore_collection_prefix
        return f"{prefix}domain_whitelist_v1"

    # --- Infrastructure Collections (Stable) ---

    @property
    def sessions_collection(self) -> str:
        """
        Get sessions collection (infrastructure).
        Dev: development_sessions
        Prod: sessions
        """
        prefix = self.firestore_collection_prefix
        return f"{prefix}sessions"

    @property
    def consolidation_queue_collection(self) -> str:
        """
        Get consolidation queue collection (infrastructure).
        Dev: development_consolidation_queue
        Prod: consolidation_queue
        """
        prefix = self.firestore_collection_prefix
        return f"{prefix}consolidation_queue"

    @property
    def event_dedup_collection(self) -> str:
        """
        Get event deduplication collection (infrastructure).
        Dev: development_event_dedup
        Prod: event_dedup
        """
        prefix = self.firestore_collection_prefix
        return f"{prefix}event_dedup"

    @property
    def user_context_collection(self) -> str:
        """
        Get user context cache collection (infrastructure).
        Dev: development_user_context
        Prod: user_context
        """
        prefix = self.firestore_collection_prefix
        return f"{prefix}user_context"

    # --- Legacy Compatibility (Deprecated) ---
    # TODO: Remove after full migration to Semantic Names

    @property
    def user_collection_name(self) -> str:
        return self.domain_users_collection

    @property
    def account_collection_name(self) -> str:
        return self.domain_accounts_collection

    @property
    def fact_collection_name(self) -> str:
        return self.domain_facts_collection

    @property
    def use_emulator(self) -> bool:
        """
        Check if Firestore emulator should be used.

        Returns:
            True if FIRESTORE_EMULATOR_HOST is set
        """
        return os.getenv("FIRESTORE_EMULATOR_HOST") is not None

    def get_emulator_host(self) -> str:
        """
        Get Firestore emulator host if emulator is enabled.

        Returns:
            Emulator host string or empty string
        """
        return os.getenv("FIRESTORE_EMULATOR_HOST", "")

    def __str__(self) -> str:
        """String representation for logging."""
        parts = [self.env.value]
        if self.use_emulator:
            parts.append("emulator")
        parts.append(f"slack:{self.slack_mode.value}")
        return f"{parts[0]} ({', '.join(parts[1:])})" if len(parts) > 1 else parts[0]

    def __repr__(self) -> str:
        """Detailed representation."""
        return f"EnvironmentConfig(env={self.env.value}, slack_mode={self.slack_mode.value}, emulator={self.use_emulator})"


def validate_telegram_config():
    """
    Validate Telegram configuration at startup.
    
    Returns:
        Dict with token and webhook_secret if valid, None if not configured
        
    Raises:
        ValueError: If configuration is invalid
    """
    import re
    
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return None  # Telegram not configured
    
    # Validate token format: {bot_id}:{secret}
    # Example: 1234567890:ABCdefGHIjklMNOpqrsTUVwxyz1234567
    if not re.match(r'^\d+:[A-Za-z0-9_-]{35}$', token):
        raise ValueError(
            f"Invalid TELEGRAM_BOT_TOKEN format. "
            f"Expected format: <bot_id>:<secret> (e.g., 1234567890:ABCdefGHIjklMNOpqrsTUVwxyz1234567)"
        )
    
    webhook_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET")
    if not webhook_secret:
        raise ValueError("TELEGRAM_WEBHOOK_SECRET is required when TELEGRAM_BOT_TOKEN is set")
    
    if len(webhook_secret) < 32:
        raise ValueError(
            f"TELEGRAM_WEBHOOK_SECRET must be at least 32 characters (current: {len(webhook_secret)})"
        )
    
    return {
        "token": token,
        "webhook_secret": webhook_secret
    }
