"""IAM least-privilege Step 0 (C4): the scheduler step reads the invoker identity from its secret, fails fast,
pins audiences and manages all jobs (incl. the relay keep-alive); logging is Cloud Logging only."""
import re
from pathlib import Path

import yaml

_BUILD = Path(__file__).resolve().parents[2] / "cloudbuild-dev.yaml"


def _doc():
    return yaml.safe_load(_BUILD.read_text())


def _scheduler_script():
    scripts = [s["args"][-1] for s in _doc()["steps"] if "scheduler jobs" in s.get("args", [""])[-1]]
    assert len(scripts) == 1
    return scripts[0]


def test_logging_is_cloud_logging_only():
    assert _doc()["options"]["logging"] == "CLOUD_LOGGING_ONLY"


def test_script_fails_fast_and_reads_identity_from_secret():
    script = _scheduler_script()
    assert script.lstrip().splitlines()[0].strip() == "set -euo pipefail"
    assert "gcloud secrets versions access latest --secret=SERVICE_ACCOUNT_EMAIL" in script
    assert "projectNumber" not in script and "developer.gserviceaccount.com" not in script


def test_every_oidc_job_pins_an_audience():
    script = _scheduler_script()
    assert script.count("--oidc-service-account-email") == script.count("--oidc-token-audience")
    assert script.count("--oidc-token-audience") >= 10 * 2 - 2  # 10 jobs, update + create each


def test_all_ten_jobs_are_build_managed():
    names = set(re.findall(r"scheduler jobs (?:create|update) http (\S+)", _scheduler_script()))
    assert "alek-voice-relay-dev-keep-alive" in names and "alek-bot-dev-keep-alive" in names
    assert len(names) == 10


def test_renew_task_subscriptions_is_never_deleted():
    script = _scheduler_script()
    assert "jobs delete" not in script
    assert re.search(r"describe alek-bot-dev-renew-task-subscriptions[^|]*&&\s*gcloud scheduler jobs update http "
                     r"alek-bot-dev-renew-task-subscriptions", script)
