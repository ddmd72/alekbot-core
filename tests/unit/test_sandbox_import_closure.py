"""The sandbox image holds nothing (RFC §4.3, I1): the static import closure of `sandbox_main.py` reaches no agent
stack, no credential-holding cloud client and no model SDK. Runs in CI without docker (plan G15)."""
import sys
from pathlib import Path

from tests.unit.sandbox_closure import ROOT, closure

_FORBIDDEN_SRC_PREFIXES = ("src/agents/", "src/handlers/", "src/services/", "src/infrastructure/")
# RFC §4.3 set + the plan's additions (model SDKs, chat SDK, BigQuery).
_FORBIDDEN_EXTERNAL = (
    "google.cloud.firestore", "google.cloud.secretmanager", "google.cloud.storage",
    "anthropic", "openai", "google.genai", "slack_sdk", "google.cloud.bigquery",
)
# Allowed by name (plan G13): `src.utils.logger` pulls them; they hold no credential. `logfire` is imported only inside
# `telemetry._init_logfire`, which the sandbox never calls, and is not installed in the image.
_ALLOWED_THIRD_PARTY = {"quart", "hypercorn", "werkzeug", "opentelemetry", "logfire", "google"}


def test_closure_reaches_no_agent_service_handler_infrastructure_or_firestore_repo():
    files, _ = closure()
    bad = sorted(f for f in files
                 if f.startswith(_FORBIDDEN_SRC_PREFIXES) or Path(f).name.startswith("firestore_"))
    assert not bad, bad


def test_closure_adapters_are_only_the_sandbox_engine():
    files, _ = closure()
    bad = sorted(f for f in files if f.startswith("src/adapters/") and not f.startswith("src/adapters/sandbox_engine/"))
    assert not bad, bad


def test_closure_imports_no_credential_holding_client_or_model_sdk():
    _, external = closure()
    bad = sorted(n for n in external for f in _FORBIDDEN_EXTERNAL if n == f or n.startswith(f + "."))
    assert not bad, bad


def test_closure_third_party_imports_are_only_the_documented_ones():
    _, external = closure()
    tops = {n.split(".")[0] for n in external} - set(sys.stdlib_module_names) - {"src"}
    assert tops <= _ALLOWED_THIRD_PARTY, sorted(tops - _ALLOWED_THIRD_PARTY)


def test_closure_google_imports_are_only_auth_and_logging():
    _, external = closure()
    google = sorted(n for n in external if n == "google" or n.startswith("google."))
    bad = [n for n in google if n != "google"
           and not n.startswith(("google.auth", "google.oauth2", "google.cloud.logging", "google.cloud.trace"))
           and n != "google.cloud"]
    assert not bad, bad


def test_closure_is_computed_not_empty():
    files, _ = closure()
    assert "sandbox_main.py" in files and "src/utils/logger.py" in files
    assert "src/adapters/sandbox_engine/render_docx_child.py" in files


def test_requirements_sandbox_pins_match_requirements_txt():
    def parse(path):
        out = {}
        for line in (ROOT / path).read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                name = line
                for sep in ("==", ">=", "<=", "~=", ">", "<"):
                    name = name.split(sep, 1)[0]
                out[name.strip().lower()] = line
        return out

    main, sandbox = parse("requirements.txt"), parse("requirements-sandbox.txt")
    assert sandbox, "requirements-sandbox.txt is empty"
    drift = {n: (line, main[n]) for n, line in sandbox.items() if n in main and line != main[n]}
    assert not drift, drift
    # Packages the main file only gets transitively: the sandbox pins them exactly (no range, no drift via a resolver).
    transitive = {n: line for n, line in sandbox.items() if n not in main}
    assert all("==" in line for line in transitive.values()), transitive


def test_requirements_sandbox_excludes_what_the_image_must_not_hold():
    names = " ".join(ln.split("#", 1)[0] for ln in (ROOT / "requirements-sandbox.txt").read_text().lower().splitlines())
    for banned in ("logfire", "firestore", "secret-manager", "storage", "anthropic", "openai", "genai", "slack",
                   "bigquery", "playwright", "pydub"):
        assert banned not in names, banned
