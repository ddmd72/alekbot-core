"""Phase B of the light service: both runtimes get `SANDBOX_PARSE_URL` from `_SANDBOX_LIGHT_URL`, and the deploy target
refuses an empty `SANDBOX_LIGHT_URL_DEV` after the transcription guard (docs/07_deployment/README.md § Sandbox light)."""
import subprocess
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_BUILD = yaml.safe_load((_ROOT / "cloudbuild-dev.yaml").read_text())
_BASE = ["make", "-C", str(_ROOT), "SERVICE_URL_DEV=https://svc.invalid", "INDEXER_URL_DEV=https://idx.invalid",
         "DRIVE_TRANSCRIBE_ENABLED_DEV=off"]


def _env(service):
    (args,) = [s["args"] for s in _BUILD["steps"] if s.get("args", [])[:3] == ["run", "deploy", service]]
    env = next(a.split("=", 1)[1] for a in args if a.startswith("--set-env-vars="))
    return dict(pair.split("=", 1) for pair in env.split(","))


def test_the_main_service_and_the_indexer_get_the_light_service_url():
    assert _env("alek-bot-dev")["SANDBOX_PARSE_URL"] == "$_SANDBOX_LIGHT_URL"
    assert _env("alek-bot-indexer-dev")["SANDBOX_PARSE_URL"] == "$_SANDBOX_LIGHT_URL"


def test_the_target_refuses_an_empty_light_url_naming_the_key():
    # the guard exits before gcloud is reached, so a real (non-dry) run is safe
    done = subprocess.run(_BASE + ["SANDBOX_LIGHT_URL_DEV=", "deploy"], capture_output=True, text=True)
    out = done.stdout + done.stderr
    assert done.returncode != 0
    assert "SANDBOX_LIGHT_URL_DEV" in out and "docs/07_deployment/README.md" in out


def test_the_target_with_the_url_passes_it_to_the_build():
    done = subprocess.run(_BASE + ["SANDBOX_LIGHT_URL_DEV=https://light.test.example", "-n", "deploy"],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert "_SANDBOX_LIGHT_URL=https://light.test.example" in done.stdout
