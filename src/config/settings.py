import os
from dotenv import load_dotenv
from google.cloud import secretmanager
from .environment import EnvironmentConfig
from ..domain.language import LanguageCode

# ARCHITECTURE FIX: ConsolidationSettings moved to src/domain/settings.py.
# It is a pure value object with no infrastructure deps — same as SearchConfig.
# Import from src.domain.settings instead of here.
from ..domain.settings import ConsolidationSettings  # noqa: F401 — re-export

# ARCHITECTURE FIX: SearchConfig moved to src/domain/settings.py.
# It contains domain-level constants, not infrastructure config.
# Import from src.domain.settings instead of here.
from ..domain.settings import SearchConfig  # noqa: F401 — re-export for scripts

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
    """Load all required settings from .env and Secret Manager with environment awareness.

    NOTE: print() is used intentionally here — logger is not yet initialized
    when load_settings() runs (it's called during bootstrap before any logging
    infrastructure is set up). Do NOT replace with logger.
    """
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
        "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY"),  # OpenAI (Session 2026-03-03)
        "OPENAI_DEEP_RESEARCH_WEBHOOK_URL": os.getenv("OPENAI_DEEP_RESEARCH_WEBHOOK_URL"),
        "OPENAI_DEEP_RESEARCH_WEBHOOK_SECRET": os.getenv("OPENAI_DEEP_RESEARCH_WEBHOOK_SECRET"),
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
        # LLM prompt/response content store (BigQuery, 30-day TTL). Empty dataset → disabled.
        # DEBUG_PROMPTS is the global capture on/off switch (write / don't write).
        "DEBUG_PROMPTS": os.getenv("DEBUG_PROMPTS", "false"),
        "BIGQUERY_PROMPT_DATASET": os.getenv("BIGQUERY_PROMPT_DATASET", ""),
        "BIGQUERY_PROMPT_TABLE": os.getenv("BIGQUERY_PROMPT_TABLE", "prompt_content"),
        # Microsoft To Do OAuth + webhook (TASKS_LOCAL_FIRST_RFC.md §11)
        "MICROSOFT_TODO_CLIENT_ID": os.getenv("MICROSOFT_TODO_CLIENT_ID", ""),
        "MICROSOFT_TODO_CLIENT_SECRET": os.getenv("MICROSOFT_TODO_CLIENT_SECRET", ""),
        "MICROSOFT_TODO_REDIRECT_URI": os.getenv("MICROSOFT_TODO_REDIRECT_URI", ""),
        "MICROSOFT_TASKS_WEBHOOK_SECRET": os.getenv("MICROSOFT_TASKS_WEBHOOK_SECRET", ""),
        # Unsplash image search (HtmlPageGeneratorAgent)
        "UNSPLASH_ACCESS_KEY": os.getenv("UNSPLASH_ACCESS_KEY", ""),
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

    # System default language (RFC: MULTILINGUAL_SUPPORT_RFC.md §5.1)
    # Loaded from SYSTEM_DEFAULT_LANGUAGE env var. EN is the neutral baseline.
    lang_str = os.getenv("SYSTEM_DEFAULT_LANGUAGE", "en").lower()
    settings["SYSTEM_DEFAULT_LANGUAGE"] = LanguageCode.from_str(lang_str, default=LanguageCode.EN)

    return settings
