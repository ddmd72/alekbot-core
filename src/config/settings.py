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

    # Secrets and required configuration ONLY. Every key here that resolves empty is chased
    # into Secret Manager on each boot (see the loop below), so an optional knob costs a
    # pointless lookup and a warning every cold start. Optional overrides — model pins,
    # rollback switches — are read with os.getenv() at their call site instead. A key read
    # via config.get() but missing here silently resolves to None: that is how the
    # deep-research model overrides were dead from the day they were written.
    settings = {
        "APP_ENV": env_config.env.value,
        "ENVIRONMENT_CONFIG": env_config,
        "SLACK_BOT_TOKEN": os.getenv("SLACK_BOT_TOKEN"),
        "SLACK_SIGNING_SECRET": os.getenv("SLACK_SIGNING_SECRET"),
        "CLOUD_RUN_SERVICE_URL": os.getenv("CLOUD_RUN_SERVICE_URL"),
        "SERVICE_ACCOUNT_EMAIL": os.getenv("SERVICE_ACCOUNT_EMAIL"),
        # OIDC identities (IAM least-privilege): the drive queue's invoker and the voice relay's runtime SA.
        # Absent today -> consumers fall back to SERVICE_ACCOUNT_EMAIL / accept nothing extra.
        "DRIVE_INVOKER_SA_EMAIL": os.getenv("DRIVE_INVOKER_SA_EMAIL"),
        "VOICE_RELAY_SA_EMAIL": os.getenv("VOICE_RELAY_SA_EMAIL"),
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
        # Secrets at rest (SECRETS_AT_REST_RFC §2.5-§2.6): the Cloud KMS key that encrypts stored secrets, and the hex
        # key of the verify-only digests. Required: the composition root refuses to build without either (I6), locally
        # too (local dev uses the real key, §2.7).
        "KMS_CRYPTO_KEY": os.getenv("KMS_CRYPTO_KEY"),
        "SECRET_DIGEST_KEY": os.getenv("SECRET_DIGEST_KEY"),
        # Telegram secrets (Session 2026-02-09 Phase 3)
        "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN"),
        "TELEGRAM_WEBHOOK_SECRET": os.getenv("TELEGRAM_WEBHOOK_SECRET"),
        # Media storage
        "GCS_MEDIA_BUCKET": os.getenv("GCS_MEDIA_BUCKET", ""),
        # Agent workspace (AGENT_WORKSPACE_RFC §5.5): required configuration for the feature. On Cloud Run a Secret
        # Manager secret in the main service's --set-secrets, never a Cloud Build substitution. Empty → no
        # workspace (no tool, workspace: refs and workspace_file refused). Locally an empty value is fetched from
        # Secret Manager when GOOGLE_CLOUD_PROJECT is set, so local dev shares the live bucket (D9).
        "GCS_WORKSPACE_BUCKET": os.getenv("GCS_WORKSPACE_BUCKET", ""),
        # LLM prompt/response content store (BigQuery, 30-day TTL). Empty dataset → disabled.
        # DEBUG_PROMPTS is the global capture on/off switch (write / don't write).
        "DEBUG_PROMPTS": os.getenv("DEBUG_PROMPTS", "false"),
        "BIGQUERY_PROMPT_DATASET": os.getenv("BIGQUERY_PROMPT_DATASET", ""),
        "BIGQUERY_PROMPT_TABLE": os.getenv("BIGQUERY_PROMPT_TABLE", "prompt_content"),
        # Microsoft To Do OAuth + webhook (TASKS_LOCAL_FIRST_RFC.md §11)
        "MICROSOFT_TODO_CLIENT_ID": os.getenv("MICROSOFT_TODO_CLIENT_ID", ""),
        "MICROSOFT_TODO_CLIENT_SECRET": os.getenv("MICROSOFT_TODO_CLIENT_SECRET", ""),
        # The drive (OneDrive App Folder, USER_DRIVE_RFC §4.1) uses the same app registration; both connect through
        # the one callback OAUTH_CONNECT_REDIRECT_URI (read by AuthConfig, MULTI_DRIVE_RFC §5.2).
        "MICROSOFT_TASKS_WEBHOOK_SECRET": os.getenv("MICROSOFT_TASKS_WEBHOOK_SECRET", ""),
        # Unsplash image search (HtmlPageGeneratorAgent)
        "UNSPLASH_ACCESS_KEY": os.getenv("UNSPLASH_ACCESS_KEY", ""),
        # Twilio (Voice Companion RFC §4.13) — outbound call origination. MVP,
        # dev-only feature (same status as the Remote MCP Server): empty-string
        # default, like MICROSOFT_TODO_*/UNSPLASH_ACCESS_KEY above, not the
        # bare-None default used by secrets the whole app depends on
        # (ANTHROPIC_API_KEY etc.) — UserAgentFactory._build_lelik skips
        # LelikAgent construction gracefully when these are absent.
        "TWILIO_ACCOUNT_SID": os.getenv("TWILIO_ACCOUNT_SID", ""),
        "TWILIO_AUTH_TOKEN": os.getenv("TWILIO_AUTH_TOKEN", ""),
        "TWILIO_PHONE_NUMBER": os.getenv("TWILIO_PHONE_NUMBER", ""),
        # Twilio Verify service — phone-binding OTP (Slice 1 Task 11). Same
        # graceful-absence status as the TWILIO_* keys above: the Cabinet's
        # phone-binding routes 501 when this (or the client) isn't configured.
        "TWILIO_VERIFY_SERVICE_SID": os.getenv("TWILIO_VERIFY_SERVICE_SID", ""),
        # Voice relay's own wss:// media-stream URL (Task 14 deploys the relay itself) —
        # same empty-string-default, gracefully-absent-for-now status as the TWILIO_*
        # keys above, not a bare-None secret the whole app depends on.
        "VOICE_RELAY_STREAM_URL": os.getenv("VOICE_RELAY_STREAM_URL", ""),
        # Cloudflare Calls SFU (VOICE_WEB_TRANSPORT_RFC.md) — mints the browser's SFU
        # session token for web calls. Same graceful-absence status as the TWILIO_*/
        # VOICE_RELAY_STREAM_URL keys above: main.py skips registering the web-call
        # blueprint when either is missing, so the app boots exactly as before.
        "CLOUDFLARE_SFU_APP_ID": os.getenv("CLOUDFLARE_SFU_APP_ID", ""),
        "CLOUDFLARE_SFU_APP_SECRET": os.getenv("CLOUDFLARE_SFU_APP_SECRET", ""),
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

    # Identity emails are compared for equality against token claims: a mounted secret may carry a
    # trailing newline (only the Secret Manager fallback path strips, get_secret above).
    for key in ("SERVICE_ACCOUNT_EMAIL", "DRIVE_INVOKER_SA_EMAIL", "VOICE_RELAY_SA_EMAIL"):
        settings[key] = (settings[key] or "").strip() or None

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
