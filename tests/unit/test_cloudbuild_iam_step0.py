"""IAM least-privilege Step 0 (C4): the scheduler step reads the invoker identity from its secret, fails fast,
pins audiences and manages all jobs (incl. the relay keep-alive); logging is Cloud Logging only."""
import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_BUILD = _ROOT / "cloudbuild-dev.yaml"


def _doc():
    return yaml.safe_load(_BUILD.read_text())


def _step_script(step):
    """The step's script text: its last arg, or the file it runs when that arg is a script path."""
    last = step.get("args", [""])[-1]
    return (_ROOT / last).read_text() if last.endswith(".sh") else last


def _scheduler_script():
    scripts = [_step_script(s) for s in _doc()["steps"] if "scheduler jobs" in _step_script(s)]
    assert len(scripts) == 1
    return scripts[0]


def test_logging_is_cloud_logging_only():
    assert _doc()["options"]["logging"] == "CLOUD_LOGGING_ONLY"


def test_script_fails_fast_and_reads_identity_from_secret():
    script = _scheduler_script()
    code = [ln.strip() for ln in script.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert code[0] == "set -euo pipefail"
    assert "gcloud secrets versions access latest --secret=SERVICE_ACCOUNT_EMAIL" in script
    assert "projectNumber" not in script and "developer.gserviceaccount.com" not in script


def test_every_oidc_job_pins_an_audience():
    script = _scheduler_script()
    assert script.count("--oidc-service-account-email") == script.count("--oidc-token-audience")
    # 14 jobs, update + create each; the /health keep-alive is the one job without OIDC
    assert script.count("--oidc-token-audience") >= 14 * 2 - 2


def test_all_fourteen_jobs_are_build_managed():
    names = set(re.findall(r"scheduler jobs (?:create|update) http (\S+)", _scheduler_script()))
    assert "alek-voice-relay-dev-keep-alive" in names and "alek-bot-dev-keep-alive" in names
    # S1b F1: the email indexing watchdog and the companion consolidation sweep are build-managed too
    assert "alek-bot-dev-email-indexing-watchdog" in names
    assert "alek-bot-dev-sweep-companion-consolidation" in names
    # Facts S3 T10: the fact janitor's daily job
    assert "alek-bot-dev-fact-lifecycle-sweep" in names
    # SECRETS_AT_REST_RFC §6.2 R1, plan D-P4: the hourly Gmail / To Do reconnect notice sweep
    assert "alek-bot-dev-reconnect-notice-sweep" in names
    assert len(names) == 14


def test_renew_task_subscriptions_is_never_deleted():
    script = _scheduler_script()
    assert "jobs delete" not in script
    assert re.search(r"describe alek-bot-dev-renew-task-subscriptions[^|]*&&\s*gcloud scheduler jobs update http "
                     r"alek-bot-dev-renew-task-subscriptions", script)
