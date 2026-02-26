import os
from dotenv import load_dotenv
from google.cloud import secretmanager
from typing import Optional
from dataclasses import dataclass
from .environment import EnvironmentConfig

@dataclass
class ConsolidationSettings:
    """Centralized settings for sliding window consolidation."""
    threshold: int = 50  # dev default
    batch_size: int = 40  # dev default
    max_queue_messages: int = 600
    max_retry_attempts: int = 3
    prompt_version: str = "v3"  # "v3" (multi-turn deliberate) or "v2" (legacy single-shot)


@dataclass
class SearchConfig:
    """
    Centralized settings for semantic search (multi-vector).
    
    Session: 2026-02-07 Multi-Vector Semantic Search
    Plan: docs/SESSION_2026_02_07_MULTI_VECTOR_SEMANTIC_SEARCH.md
    Purpose: System-wide defaults for search context limits
    """
    # Semantic search (SearchEnrichmentService) defaults
    DEFAULT_SEMANTIC_SEARCH_LIMIT: int = 30
    DEFAULT_KEYWORD_LIMIT: int = 10
    DEFAULT_PHRASE_ONE_LIMIT: int = 10
    DEFAULT_PHRASE_TWO_LIMIT: int = 10
    
    # Memory search (MemorySearchAgent) - future use
    DEFAULT_MEMORY_SEARCH_LIMIT: int = 50
    
    # Biographical cache (BiographicalContextService) defaults
    # Session: 2026-02-07 Biographical Cache Optimization
    # Plan: docs/SESSION_2026_02_07_BIOGRAPHICAL_CACHE_OPTIMIZATION.md
    # RFC: docs/10_rfcs/BIOGRAPHICAL_CACHE_MULTI_VECTOR_RFC.md
    DEFAULT_BIOGRAPHICAL_CACHE_LIMIT: int = 65
    DEFAULT_PRINCIPLES_CACHE_LIMIT: int = 20

    # History optimization (2026-02-18): Tiered history loading
    DEFAULT_HISTORY_RECENT_FULL_TURNS: int = 5

    # Default queries for biographical cache multi-vector search
    DEFAULT_BIOGRAPHICAL_QUERIES: list = None
    
    # ========================================================================
    # NEW Biographical Keywords (2026-02-07): Configurable query keywords
    # Plan: docs/SESSION_2026_02_07_BIOGRAPHICAL_CACHE_REFACTORING.md
    # Purpose: 3 separate keyword sets for multi-vector biographical search
    # ========================================================================
    DEFAULT_BIO_KEYWORDS_QUERY1: list = None  # Query 1: tags + metadata
    DEFAULT_BIO_KEYWORDS_QUERY2: list = None  # Query 2: vector + tags
    DEFAULT_BIO_KEYWORDS_QUERY3: list = None  # Query 3: vector + metadata
    
    # Tiered defaults (can be overridden at account level)
    # These are optional defaults - account owners can set custom limits
    TIERED_SEMANTIC_LIMITS: dict = None
    TIERED_BIOGRAPHICAL_LIMITS: dict = None
    TIERED_PRINCIPLES_LIMITS: dict = None
    
    def __post_init__(self):
        """Initialize tiered limits and default queries if not provided."""
        # Import here to avoid circular dependency
        from ..domain.billing import AccountTier
        
        if self.TIERED_SEMANTIC_LIMITS is None:
            self.TIERED_SEMANTIC_LIMITS = {
                AccountTier.FREE: 20,       # Budget-conscious
                AccountTier.FAMILY: 30,     # Standard quality
                AccountTier.PRO: 50,        # Higher quality
                AccountTier.ENTERPRISE: 100 # Maximum recall
            }
        
        if self.TIERED_BIOGRAPHICAL_LIMITS is None:
            self.TIERED_BIOGRAPHICAL_LIMITS = {
                AccountTier.FREE: 30,       # Budget-conscious
                AccountTier.FAMILY: 50,     # Standard quality
                AccountTier.PRO: 70,        # Higher quality
                AccountTier.ENTERPRISE: 100 # Maximum recall
            }
        
        if self.TIERED_PRINCIPLES_LIMITS is None:
            self.TIERED_PRINCIPLES_LIMITS = {
                AccountTier.FREE: 10,       # Budget-conscious
                AccountTier.FAMILY: 15,     # Standard quality
                AccountTier.PRO: 20,        # Higher quality
                AccountTier.ENTERPRISE: 25  # Maximum recall
            }
        
        if self.DEFAULT_BIOGRAPHICAL_QUERIES is None:
            self.DEFAULT_BIOGRAPHICAL_QUERIES = [
                "identity name bio family relationships",  # Personal identity
                "medical health conditions diagnoses",     # Health facts
                "assets possessions vehicles property",    # Material facts
            ]
        
        # ========================================================================
        # NEW Biographical Keywords (2026-02-07): Initialize keyword sets
        # ========================================================================
        if self.DEFAULT_BIO_KEYWORDS_QUERY1 is None:
            self.DEFAULT_BIO_KEYWORDS_QUERY1 = [
                "identity", "name", "bio", "family", "relationships"
            ]
        
        if self.DEFAULT_BIO_KEYWORDS_QUERY2 is None:
            self.DEFAULT_BIO_KEYWORDS_QUERY2 = [
                "medical", "health", "conditions", "diagnoses", "treatments"
            ]
        
        if self.DEFAULT_BIO_KEYWORDS_QUERY3 is None:
            self.DEFAULT_BIO_KEYWORDS_QUERY3 = [
                "assets", "possessions", "vehicles", "property", "finances"
            ]

def get_secret(project_id, secret_name):
    """Fetch a secret from Google Cloud Secret Manager."""
    try:
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8").strip()
    except Exception as e:
        print(f"⚠️ Failed to fetch secret '{secret_name}': {e}")
        return None

def load_settings():
    """Load all required settings from .env and Secret Manager with environment awareness."""
    load_dotenv()

    # Initialize environment config FIRST
    env_config = EnvironmentConfig()

    # Environment-aware logging
    if env_config.is_production:
        print("🔴 PRODUCTION MODE ACTIVE - Be careful!")
    elif env_config.use_emulator:
        print(f"🟢 {env_config.env.value.upper()} MODE (emulator)")
    else:
        print(f"🟢 {env_config.env.value.upper()} MODE")

    settings = {
        "APP_ENV": env_config.env.value,
        "ENVIRONMENT_CONFIG": env_config,
        "SLACK_BOT_TOKEN": os.getenv("SLACK_BOT_TOKEN"),
        "SLACK_APP_TOKEN": os.getenv("SLACK_APP_TOKEN"),
        "DEV_SLACK_BOT_TOKEN": os.getenv("DEV_SLACK_BOT_TOKEN"),
        "DEV_SLACK_APP_TOKEN": os.getenv("DEV_SLACK_APP_TOKEN"),
        "SLACK_SIGNING_SECRET": os.getenv("SLACK_SIGNING_SECRET"),
        "CLOUD_RUN_SERVICE_URL": os.getenv("CLOUD_RUN_SERVICE_URL"),
        "SERVICE_ACCOUNT_EMAIL": os.getenv("SERVICE_ACCOUNT_EMAIL"),
        "GOOGLE_CLOUD_PROJECT": os.getenv("GOOGLE_CLOUD_PROJECT"),
        "GEMINI_API_KEY": os.getenv("GEMINI_API_KEY"),
        "ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY"),
        "XAI_API_KEY": os.getenv("XAI_API_KEY"),  # xAI Grok (Session 2026-02-12)
        "GOOGLE_SEARCH_API_KEY": os.getenv("GOOGLE_SEARCH_API_KEY"),
        "GOOGLE_SEARCH_CX": os.getenv("GOOGLE_SEARCH_CX"),
        # OAuth secrets (Session 2026-02-05)
        "FIREBASE_WEB_API_KEY": os.getenv("FIREBASE_WEB_API_KEY"),
        "GOOGLE_OAUTH_CLIENT_ID": os.getenv("GOOGLE_OAUTH_CLIENT_ID"),
        "GOOGLE_OAUTH_CLIENT_SECRET": os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"),
        "OAUTH_SESSION_SECRET": os.getenv("OAUTH_SESSION_SECRET"),
        # Telegram secrets (Session 2026-02-09 Phase 3)
        "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN"),
        "TELEGRAM_WEBHOOK_SECRET": os.getenv("TELEGRAM_WEBHOOK_SECRET"),
        # Media storage
        "GCS_MEDIA_BUCKET": os.getenv("GCS_MEDIA_BUCKET", ""),
    }

    if settings["GOOGLE_CLOUD_PROJECT"] and not env_config.use_emulator:
        for key in list(settings.keys()):
            # Skip environment config keys
            if key in ["APP_ENV", "ENVIRONMENT_CONFIG"]:
                continue
            
            # Skip DEV tokens in production/cloud fetch
            if key.startswith("DEV_"):
                continue

            if not settings[key]:
                print(f"🤫 {key} not found in .env, attempting to fetch from Secret Manager...")
                secret_value = get_secret(settings["GOOGLE_CLOUD_PROJECT"], key)
                if secret_value:
                    settings[key] = secret_value
                    print(f"✅ Successfully fetched {key}.")
                else:
                    print(f"⚠️ Could not fetch {key} from Secret Manager.")

    missing_keys = [key for key, value in settings.items()
                   if value is None and key not in ["APP_ENV", "ENVIRONMENT_CONFIG"]]
    if missing_keys:
        # We allow some keys to be missing if they are not core to boot
        # but critical ones must be checked later or here.
        pass

    # Add consolidation settings based on environment
    consolidation = ConsolidationSettings()

    # Load overrides from env
    if os.getenv("CONSOLIDATION__PROMPT_VERSION"):
        consolidation.prompt_version = os.getenv("CONSOLIDATION__PROMPT_VERSION")

    if env_config.is_production:
        consolidation.threshold = 50
        consolidation.batch_size = 30

    settings["CONSOLIDATION"] = consolidation

    # Add Prompt Design System v3 feature flag (Phase 5.6 - Rollback Plan)
    # Default: False (disabled) - must be explicitly enabled via environment variable
    # See: docs/10_rfcs/PROMPT_V3_ROLLBACK_PLAN.md
    settings["ENABLE_PROMPT_V3"] = os.getenv("ENABLE_PROMPT_V3", "false").lower() == "true"
    settings["ENABLE_HTML_RENDERER"] = os.getenv("ENABLE_HTML_RENDERER", "false").lower() == "true"

    return settings
