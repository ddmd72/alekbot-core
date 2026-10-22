"""T-R34: `make deploy` refuses unless DRIVE_TRANSCRIBE_ENABLED_DEV is explicitly on/off, echoes the effective switch and
cap, and the switch reaches the main service so the Cabinet disclosure follows it."""
import subprocess
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_BUILD = yaml.safe_load((_ROOT / "cloudbuild-dev.yaml").read_text())
_BASE = ["make", "-C", str(_ROOT), "SERVICE_URL_DEV=https://svc.invalid", "INDEXER_URL_DEV=https://idx.invalid"]


def _env(service):
    (args,) = [s["args"] for s in _BUILD["steps"] if s.get("args", [])[:3] == ["run", "deploy", service]]
    env = next(a.split("=", 1)[1] for a in args if a.startswith("--set-env-vars="))
    return dict(pair.split("=", 1) for pair in env.split(","))


def test_deploy_refuses_when_the_switch_key_is_absent_or_not_on_off():
    # the guard exits before gcloud is reached, so a real (non-dry) run is safe
    for value in ("", "yes", "ON"):
        done = subprocess.run(_BASE + [f"DRIVE_TRANSCRIBE_ENABLED_DEV={value}", "deploy"], capture_output=True, text=True)
        assert done.returncode != 0, value
        assert "DRIVE_TRANSCRIBE_ENABLED_DEV" in done.stdout + done.stderr
        assert "docs/07_deployment/README.md" in done.stdout + done.stderr


def test_deploy_accepts_on_and_off_and_echoes_the_effective_switch_and_cap():
    for value in ("on", "off"):
        done = subprocess.run(_BASE + [f"DRIVE_TRANSCRIBE_ENABLED_DEV={value}", "DRIVE_TRANSCRIBE_USD_CAP_DEV=10",
                                       "-n", "deploy"], capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
        assert f"_DRIVE_TRANSCRIBE_ENABLED={value}, cap: _DRIVE_TRANSCRIBE_USD_CAP=10" in done.stdout


def test_the_echo_comes_before_the_submit():
    done = subprocess.run(_BASE + ["DRIVE_TRANSCRIBE_ENABLED_DEV=off", "-n", "deploy"], capture_output=True, text=True)
    assert done.stdout.index("Transcription switch") < done.stdout.index("gcloud builds submit")


def test_the_main_service_gets_the_switch_for_the_cabinet():
    assert _env("alek-bot-dev")["DRIVE_TRANSCRIBE_ENABLED"] == "$_DRIVE_TRANSCRIBE_ENABLED"
    assert _env("alek-bot-indexer-dev")["DRIVE_TRANSCRIBE_ENABLED"] == "$_DRIVE_TRANSCRIBE_ENABLED"
