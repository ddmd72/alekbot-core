"""P22: the Secret Manager fallback of `load_settings()` / `get_secret` (src/config/settings.py), against a fake SDK client.

Rule under test: a key the environment leaves empty is fetched from Secret Manager (only with a project set and no
emulator); a value that is present is never fetched; a failed fetch leaves the key as it was (the boot goes on, and
the required keys refuse the start where `cipher_setup` enforces them). The fake replaces the SDK's client class, never `get_secret`.
"""
import pytest

from src.composition import cipher_setup
from src.config import settings as settings_module

PROJECT = "invented-project"
SPECIAL = {"APP_ENV", "ENVIRONMENT_CONFIG"}
DIGEST_HEX = "ab" * 32
KMS_NAME = "projects/p/locations/l/keyRings/k/cryptoKeys/c"

# Every key `load_settings()` resolves that is empty with a bare environment: each one costs a Secret Manager lookup per boot.
# A new optional knob here costs a lookup on every cold start (CLAUDE.md "config.get() vs os.getenv()"): read it with os.getenv() instead.
CHASED_WITH_A_BARE_ENVIRONMENT = frozenset({
    "ANTHROPIC_API_KEY", "CLOUDFLARE_SFU_APP_ID", "CLOUDFLARE_SFU_APP_SECRET", "CLOUD_RUN_SERVICE_URL", "DRIVE_INVOKER_SA_EMAIL",
    "FIREBASE_WEB_API_KEY", "GCS_MEDIA_BUCKET", "GCS_WORKSPACE_BUCKET", "GEMINI_API_KEY", "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_SEARCH_API_KEY", "GOOGLE_SEARCH_CX", "KMS_CRYPTO_KEY", "MICROSOFT_TASKS_WEBHOOK_SECRET",
    "MICROSOFT_TODO_CLIENT_ID", "MICROSOFT_TODO_CLIENT_SECRET", "OAUTH_SESSION_SECRET", "OPENAI_API_KEY", "SANDBOX_PARSE_URL",
    "SECRET_DIGEST_KEY", "SERVICE_ACCOUNT_EMAIL", "SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET", "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER", "TWILIO_VERIFY_SERVICE_SID",
    "UNSPLASH_ACCESS_KEY", "VOICE_MCP_SIGNING_KEY", "VOICE_RELAY_SA_EMAIL", "VOICE_RELAY_STREAM_URL", "XAI_API_KEY",
    "BIGQUERY_PROMPT_DATASET",
})


class FakeSecretManager:
    """The SDK's `SecretManagerServiceClient` over a dict; `broken` names secrets whose access raises. Records every request."""
    secrets: dict = {}
    broken: set = set()
    requests: list = []
    constructed = 0

    def __init__(self):
        type(self).constructed += 1

    def access_secret_version(self, request):
        type(self).requests.append(request["name"])
        name = request["name"].split("/secrets/")[1].split("/versions/")[0]
        if name in type(self).broken:
            raise RuntimeError("permission denied")
        if name not in type(self).secrets:
            raise LookupError("not found")
        payload = type(self).secrets[name]

        class Response:
            pass

        response = Response()
        response.payload = Response()
        response.payload.data = payload.encode("utf-8")
        return response


@pytest.fixture
def fake(monkeypatch):
    FakeSecretManager.secrets, FakeSecretManager.broken, FakeSecretManager.requests, FakeSecretManager.constructed = {}, set(), [], 0
    monkeypatch.setattr(settings_module.secretmanager, "SecretManagerServiceClient", FakeSecretManager)
    return FakeSecretManager


@pytest.fixture
def bare_env(monkeypatch, fake):
    """No setting in the environment, a project set, no emulator, no dotenv file."""
    monkeypatch.setattr(settings_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.delenv("FIRESTORE_EMULATOR_HOST", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "")
    for key in settings_module.load_settings():
        if key not in SPECIAL:
            monkeypatch.delenv(key, raising=False)
    for key in ("DEBUG_PROMPTS", "ENABLE_PROMPT_V3", "ENABLE_HTML_RENDERER", "SYSTEM_DEFAULT_LANGUAGE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", PROJECT)
    fake.requests.clear()
    fake.constructed = 0
    return monkeypatch


def _names(fake):
    return [request.split("/secrets/")[1].split("/versions/")[0] for request in fake.requests]


def test_a_present_value_is_used_and_never_fetched(bare_env, fake):
    bare_env.setenv("SLACK_BOT_TOKEN", "from-env")
    fake.secrets["SLACK_BOT_TOKEN"] = "from-secret-manager"
    assert settings_module.load_settings()["SLACK_BOT_TOKEN"] == "from-env"
    assert "SLACK_BOT_TOKEN" not in _names(fake)


def test_an_empty_value_is_fetched_by_project_secret_and_latest_version(bare_env, fake):
    fake.secrets["SLACK_BOT_TOKEN"] = "from-secret-manager"
    assert settings_module.load_settings()["SLACK_BOT_TOKEN"] == "from-secret-manager"
    assert f"projects/{PROJECT}/secrets/SLACK_BOT_TOKEN/versions/latest" in fake.requests


def test_a_fetched_value_is_stripped(bare_env, fake):
    fake.secrets["SERVICE_ACCOUNT_EMAIL"] = "  sa@example.invalid\n"
    fake.secrets["SLACK_BOT_TOKEN"] = "token\n"
    config = settings_module.load_settings()
    assert config["SERVICE_ACCOUNT_EMAIL"] == "sa@example.invalid" and config["SLACK_BOT_TOKEN"] == "token"


def test_a_key_with_an_empty_string_default_is_fetched_too(bare_env, fake):
    fake.secrets["UNSPLASH_ACCESS_KEY"] = "unsplash-secret"
    assert settings_module.load_settings()["UNSPLASH_ACCESS_KEY"] == "unsplash-secret"


def test_a_failed_fetch_leaves_the_key_as_it_was_and_does_not_stop_the_others(bare_env, fake):
    fake.broken.add("SLACK_BOT_TOKEN")
    fake.secrets["SLACK_SIGNING_SECRET"] = "signing"
    config = settings_module.load_settings()
    assert config["SLACK_BOT_TOKEN"] is None
    assert config["SLACK_SIGNING_SECRET"] == "signing"
    assert config["UNSPLASH_ACCESS_KEY"] == ""      # a default-"" key that is not in Secret Manager keeps its default


def test_an_empty_secret_value_counts_as_not_found(bare_env, fake):
    fake.secrets["SLACK_BOT_TOKEN"] = "  \n"
    assert settings_module.load_settings()["SLACK_BOT_TOKEN"] is None


def test_get_secret_returns_the_stripped_value_or_none(fake):
    fake.secrets["A"] = " value \n"
    assert settings_module.get_secret(PROJECT, "A") == "value"
    assert settings_module.get_secret(PROJECT, "MISSING") is None
    fake.broken.add("B")
    assert settings_module.get_secret(PROJECT, "B") is None


def test_a_client_that_cannot_be_built_is_a_failed_fetch(bare_env, monkeypatch):
    def refuse():
        raise RuntimeError("no credentials")

    monkeypatch.setattr(settings_module.secretmanager, "SecretManagerServiceClient", refuse)
    assert settings_module.load_settings()["SLACK_BOT_TOKEN"] is None


def test_without_a_project_nothing_is_fetched(bare_env, fake):
    bare_env.setenv("GOOGLE_CLOUD_PROJECT", "")
    settings_module.load_settings()
    assert fake.constructed == 0 and fake.requests == []


def test_with_the_emulator_nothing_is_fetched(bare_env, fake):
    bare_env.setenv("FIRESTORE_EMULATOR_HOST", "localhost:8080")
    settings_module.load_settings()
    assert fake.constructed == 0 and fake.requests == []


def test_every_empty_key_is_chased_on_each_boot_and_no_other(bare_env, fake):
    settings_module.load_settings()
    fetched = set(_names(fake))
    assert fetched == CHASED_WITH_A_BARE_ENVIRONMENT
    assert len(_names(fake)) == len(fetched), "one lookup per key"


def test_a_key_with_a_non_empty_default_and_the_dev_prefix_are_never_chased(bare_env, fake):
    settings_module.load_settings()
    fetched = set(_names(fake))
    assert fetched.isdisjoint({"DEBUG_PROMPTS", "BIGQUERY_PROMPT_TABLE", "APP_ENV", "ENVIRONMENT_CONFIG"})
    assert not any(name.startswith("DEV_") for name in fetched)


# --- the required keys: what a failed or empty fetch means at the place that enforces them --------------------------

def test_required_keys_missing_everywhere_refuse_the_start(bare_env, fake):
    config = settings_module.load_settings()
    with pytest.raises(ValueError, match="KMS_CRYPTO_KEY must be set"):
        cipher_setup.build_cipher(config.get("KMS_CRYPTO_KEY"))
    with pytest.raises(ValueError, match="SECRET_DIGEST_KEY must be set"):
        cipher_setup.digest_key(config.get("SECRET_DIGEST_KEY"))


def test_required_keys_that_fail_to_fetch_refuse_the_start(bare_env, fake):
    fake.broken.update({"KMS_CRYPTO_KEY", "SECRET_DIGEST_KEY"})
    config = settings_module.load_settings()
    with pytest.raises(ValueError):
        cipher_setup.build_cipher(config.get("KMS_CRYPTO_KEY"))
    with pytest.raises(ValueError):
        cipher_setup.digest_key(config.get("SECRET_DIGEST_KEY"))


def test_required_keys_found_only_in_secret_manager_start(bare_env, fake):
    fake.secrets.update({"KMS_CRYPTO_KEY": KMS_NAME, "SECRET_DIGEST_KEY": DIGEST_HEX})
    config = settings_module.load_settings()
    assert cipher_setup.digest_key(config["SECRET_DIGEST_KEY"]) == bytes.fromhex(DIGEST_HEX)
    assert cipher_setup.build_cipher(config["KMS_CRYPTO_KEY"]) is not None
