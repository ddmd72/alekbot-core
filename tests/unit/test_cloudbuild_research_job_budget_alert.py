"""The research job mounts the ops Slack webhook as a secret, so a day's budget crossing caused by a research run is
announced (job_main.py: no BILLING_SLACK_WEBHOOK_URL means usage is counted but no alert posts)."""
from pathlib import Path

import yaml

_BUILD = Path(__file__).resolve().parents[2] / "cloudbuild-dev.yaml"


def _job_args():
    for step in yaml.safe_load(_BUILD.read_text())["steps"]:
        args = step.get("args", [])
        if "--args=job_main.py" in args:
            return args
    raise AssertionError("no research job deploy step")


def _flag(args, name):
    return next(a.split("=", 1)[1] for a in args if a.startswith(f"--{name}="))


def test_the_research_job_mounts_the_budget_alert_webhook_as_a_secret():
    secrets = dict(pair.split("=", 1) for pair in _flag(_job_args(), "set-secrets").split(","))
    assert secrets["BILLING_SLACK_WEBHOOK_URL"] == "BILLING_SLACK_WEBHOOK_URL:latest"


def test_the_webhook_never_travels_as_an_env_var():
    assert "BILLING_SLACK_WEBHOOK_URL" not in _flag(_job_args(), "set-env-vars")
