"""The production-shaped environment of the main service (P9 design §4).

`seal()` empties `os.environ` down to a keep-list, so the Mac (`make` exports `.env`), CI and the cloud VM see the same
process. `apply_deploy_env()` then sets what the `alek-bot-dev` step of `cloudbuild-dev.yaml` sets: its `--set-env-vars`
literals, its `--set-secrets` names with shaped fakes, the Cloud Run variables and the build substitutions.
`prod_settings()` runs the real `load_settings()` with `load_dotenv` a no-op and `get_secret` recording, so a key that is
absent in production shows up as a recorded Secret Manager fallback. The `.env` file is never read.

Residual: the seal runs per test, but `import main` runs at collection, so four reads made at import time see the
ambient process (the VM, CI, or a Mac where `make` exports `.env`) instead of the deploy's value:
`ENABLE_GROUNDING_ATTRIBUTION` (`agent_config.py`), `DEEP_RESEARCH_SECOND_PASS` (`agent_config.py`),
`DOCS_OWNER_USER_ID` (`user_cabinet_app.py`) and the logger's `K_SERVICE` (`logger.py`, which picks the handler).
No assertion of the boot test depends on them today; the RFC residual list (T6) carries them.
"""
import os
import re
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Tuple

import yaml

ROOT = Path(__file__).resolve().parents[2]
BUILD_FILE = ROOT / "cloudbuild-dev.yaml"
MAKEFILE = ROOT / "Makefile"
SERVICE = "alek-bot-dev"

KEEP_EXACT = frozenset({
    "PATH", "HOME", "TMPDIR", "LANG", "TZ", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS", "GRPC_DEFAULT_SSL_ROOTS_FILE_PATH", "RUN_SLOW_TESTS", "RUN_TIMING_TESTS",
})
KEEP_PREFIXES = ("LC_", "PYTHON", "PYTEST", "COVERAGE", "COV_CORE")

CLOUD_RUN_ENV = {"K_SERVICE": SERVICE, "K_REVISION": f"{SERVICE}-00001-test"}

_VAR = re.compile(r"\$\{?([A-Z_][A-Z0-9_]*)\}?")


class Substitution(NamedTuple):
    value: str
    source: str


def keeps(name: str) -> bool:
    return name in KEEP_EXACT or name.startswith(KEEP_PREFIXES)


def seal(monkeypatch) -> List[str]:
    """Delete every environment name that is not on the keep-list; returns the deleted names."""
    gone = sorted(name for name in os.environ if not keeps(name))
    for name in gone:
        monkeypatch.delenv(name, raising=False)
    return gone


# ----------------------------------------------------------------------------- the deploy step

def deploy_args() -> List[str]:
    for step in yaml.safe_load(BUILD_FILE.read_text())["steps"]:
        args = step.get("args", [])
        if args[:3] == ["run", "deploy", SERVICE]:
            return args
    raise AssertionError(f"no deploy step for {SERVICE} in {BUILD_FILE.name}")


def _flag(args: List[str], flag: str) -> str:
    [value] = [a[len(flag) + 1:] for a in args if a.startswith(flag + "=")]
    return value


def env_var_templates() -> Dict[str, str]:
    """`--set-env-vars` of the deploy step as written, `$…` still in place."""
    pairs = (item.split("=", 1) for item in _flag(deploy_args(), "--set-env-vars").split(","))
    return {key: value for key, value in pairs}


def secret_names() -> List[str]:
    """The environment names `--set-secrets` mounts."""
    return [item.split("=", 1)[0] for item in _flag(deploy_args(), "--set-secrets").split(",")]


def deploy_variable_names() -> List[str]:
    """Every `$…` the deploy step uses (env vars, secrets and the remaining arguments)."""
    return sorted(set(_VAR.findall("\n".join(deploy_args()))))


# ----------------------------------------------------------------------------- the substitutions

def makefile_defaults() -> Dict[str, str]:
    """`_NAME = $(or $(KEY),default)` lines of the Makefile: the value a deploy with no override passes."""
    found = {}
    for line in MAKEFILE.read_text().splitlines():
        match = re.match(r"^(_[A-Z0-9_]+)\s*=\s*\$\(or \$\([A-Z0-9_]+\),([^)]*)\)\s*$", line)
        if match:
            found[match.group(1)] = match.group(2)
    return found


_SERVICE_URL = "https://alek-bot-dev.test.example"

# A documented live value that differs from the Makefile default (docs/07_deployment/README.md), and values the Makefile takes
# from the owner's `.env`, which the tests never read: fakes shaped like the real ones.
LIVE_OVERRIDES: Dict[str, Substitution] = {
    "_DRIVE_TRANSCRIBE_ENABLED": Substitution("on", "root CLAUDE.md: transcription T1 is live (Makefile default is off)"),
    "_SANDBOX_RENDER": Substitution("inprocess", "decisions/sandbox_render_service.md: the sandbox S1 is not deployed (equals the Makefile default)"),
}
FAKE_SUBSTITUTIONS: Dict[str, Substitution] = {
    "PROJECT_ID": Substitution("p9-test-project", "Cloud Build built-in; a fake project id"),
    "_SERVICE_URL": Substitution(_SERVICE_URL, "Makefile deploy: SERVICE_URL_DEV (a fake, https)"),
    "_OAUTH_REDIRECT_URI": Substitution(f"{_SERVICE_URL}/auth/callback", "Makefile deploy: $(SERVICE_URL_DEV)/auth/callback"),
    "_VOICE_RELAY_URL": Substitution("wss://alek-voice-relay-dev.test.example", "Makefile: patsubst https:// -> wss:// of VOICE_RELAY_URL_DEV"),
    "_INDEXER_URL": Substitution("https://alek-bot-indexer-dev.test.example", "Makefile: INDEXER_URL_DEV, non-empty as `make deploy` requires"),
    "_DRIVE_TRANSCRIBE_USD_CAP": Substitution("", "Makefile: DRIVE_TRANSCRIBE_USD_CAP_DEV, empty = the $20 maximum"),
    "_SANDBOX_RENDER_URL": Substitution("", "Makefile: SANDBOX_RENDER_URL_DEV, empty while the sandbox is not deployed"),
}


def substitutions() -> Dict[str, Substitution]:
    """One row per `$…` of the deploy step, each naming its source. A variable with no row is a test failure."""
    defaults = makefile_defaults()
    table: Dict[str, Substitution] = {name: Substitution(value, "Makefile default") for name, value in defaults.items()}
    table.update(LIVE_OVERRIDES)
    table.update(FAKE_SUBSTITUTIONS)
    return table


def missing_substitutions() -> List[str]:
    table = substitutions()
    return [name for name in deploy_variable_names() if name not in table]


def _expand(template: str) -> str:
    table = substitutions()
    return _VAR.sub(lambda m: table[m.group(1)].value, template)


# ----------------------------------------------------------------------------- the secrets

_TELEGRAM_TOKEN = "123456789:" + "A" * 35
_SA_DOMAIN = "p9-test-project.iam.gserviceaccount.com"

SECRET_VALUES: Dict[str, str] = {
    "SLACK_BOT_TOKEN": "xoxb-" + "1" * 24,
    "TELEGRAM_BOT_TOKEN": _TELEGRAM_TOKEN,
    "TELEGRAM_WEBHOOK_SECRET": "w" * 40,
    "OAUTH_SESSION_SECRET": "s" * 40,
    "SERVICE_ACCOUNT_EMAIL": f"alek-invoker@{_SA_DOMAIN}",
    "DRIVE_INVOKER_SA_EMAIL": f"alek-drive-invoker@{_SA_DOMAIN}",
    "VOICE_RELAY_SA_EMAIL": f"alek-voice-relay@{_SA_DOMAIN}",
    "OIDC_TRANSITION_SA_EMAILS": "-",
    "TWILIO_ACCOUNT_SID": "AC" + "0" * 32,
    "TWILIO_VERIFY_SERVICE_SID": "VA" + "0" * 32,
    "BILLING_SLACK_WEBHOOK_URL": "https://hooks.slack.test.example/services/T000/B000/xxxxxxxx",
    "OPENAI_DEEP_RESEARCH_WEBHOOK_URL": "https://alek-bot-dev.test.example/webhook/openai-deep-research",
    "GCS_WORKSPACE_BUCKET": "alek-workspace-test",
    "GMAIL_OAUTH_REDIRECT_URI": f"{_SERVICE_URL}/auth/connect-gmail/callback",
    "GOOGLE_TASKS_OAUTH_REDIRECT_URI": f"{_SERVICE_URL}/auth/connect-google-tasks/callback",
    "MICROSOFT_TODO_REDIRECT_URI": f"{_SERVICE_URL}/auth/connect-microsoft-todo/callback",
    "ONEDRIVE_REDIRECT_URI": f"{_SERVICE_URL}/auth/connect-onedrive/callback",
    # secrets at rest (SECRETS_AT_REST_RFC §2.5-§2.6): a KMS key resource name and a hex digest key of 32 bytes, both shaped
    # as the deploy's; the KMS client itself is the SDK-shaped fake `install_offline_services` installs
    "KMS_CRYPTO_KEY": "projects/p9-test-project/locations/us-central1/keyRings/r-test/cryptoKeys/k-test",
    "SECRET_DIGEST_KEY": "5e" * 32,
}


def secret_value(name: str) -> str:
    return SECRET_VALUES.get(name, f"fake-{name.lower().replace('_', '-')}")


def deploy_env() -> Dict[str, str]:
    """Every variable the deployed container has: `--set-env-vars`, `--set-secrets` and Cloud Run's own."""
    env = {key: _expand(template) for key, template in env_var_templates().items()}
    env.update({name: secret_value(name) for name in secret_names()})
    env.update(CLOUD_RUN_ENV)
    return env


def apply_deploy_env(monkeypatch) -> Dict[str, str]:
    """Seal the process, then set the deployed container's variables. Returns what was set."""
    seal(monkeypatch)
    env = deploy_env()
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


def install_settings_patches(monkeypatch) -> List[str]:
    """`load_dotenv` a no-op and `get_secret` recording. Returns the list the Secret Manager keys asked for land in."""
    fallbacks: List[str] = []

    def recording_get_secret(project_id: str, secret_name: str):
        fallbacks.append(secret_name)
        return None

    monkeypatch.setattr("src.config.settings.load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr("src.config.settings.get_secret", recording_get_secret)
    return fallbacks


def prod_settings(monkeypatch) -> Tuple[dict, List[str]]:
    """`(config, fallbacks)`: the real `load_settings()` over the deploy's environment, and the Secret Manager keys it asked for."""
    apply_deploy_env(monkeypatch)
    fallbacks = install_settings_patches(monkeypatch)
    from src.config.settings import load_settings
    return load_settings(), fallbacks


# ----------------------------------------------------------------------------- the SDK boundaries

VALID_TOKEN = "valid-oidc-token"
EMBEDDING = [0.1] * 768
CANNED_DEEP_RESEARCH_RESPONSE = {"metadata": {}, "output_text": "", "model": "", "error": None, "incomplete_details": None}


class OfflineServices(NamedTuple):
    db: Any
    env: Dict[str, str]
    fallbacks: List[str]


def install_offline_services(monkeypatch) -> OfflineServices:
    """The deployed environment, and every SDK boundary the application graph touches replaced by a fake.

    Fakes sit at SDK modules, not at names a composition module owns, so they hold wherever the graph is built:
    Firestore (`EvalDb`), Cloud Tasks (a recording client), Gemini embeddings, OpenAI deep-research retrieval and
    Google's OIDC key check (a token equal to `VALID_TOKEN` carries the deploy's invoker identity) and Cloud KMS (the
    SDK-shaped `kms_fake`, real AES-GCM with the AAD bound).
    """
    from tests.support import kms_fake
    from tests.support.cloud_tasks_fake import FakeCloudTasksClient
    from tests.support.firestore_eval import EvalDb

    env = apply_deploy_env(monkeypatch)
    fallbacks = install_settings_patches(monkeypatch)
    db = EvalDb()
    monkeypatch.setattr("google.cloud.firestore.AsyncClient", lambda *a, **k: db)
    monkeypatch.setattr(FakeCloudTasksClient, "instances", [])
    monkeypatch.setattr("google.cloud.tasks_v2.CloudTasksClient", FakeCloudTasksClient)
    kms_fake.install(monkeypatch)

    async def embedding(self, text, task_type="RETRIEVAL_DOCUMENT"):
        return list(EMBEDDING)

    async def deep_research_response(self, job_id):
        return dict(CANNED_DEEP_RESEARCH_RESPONSE)

    from src.adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter
    from src.adapters.openai_deep_research_adapter import OpenAIDeepResearchAdapter
    monkeypatch.setattr(GeminiEmbeddingAdapter, "get_embedding", embedding)
    monkeypatch.setattr(OpenAIDeepResearchAdapter, "fetch_response", deep_research_response)

    def verify_oauth2_token(token, request, *args, **kwargs):
        if token != VALID_TOKEN:
            raise ValueError("Could not verify token signature.")
        return {"email": env["SERVICE_ACCOUNT_EMAIL"], "email_verified": True}

    monkeypatch.setattr("src.web.worker_oidc_verifier.google_id_token.verify_oauth2_token", verify_oauth2_token)

    from src.adapters.platform.factory import PlatformAdapterFactory
    monkeypatch.setattr(PlatformAdapterFactory, "_adapters", {})
    return OfflineServices(db, env, fallbacks)
