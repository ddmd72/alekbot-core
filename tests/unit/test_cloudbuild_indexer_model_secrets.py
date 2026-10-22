"""The indexer's model keys are Secret Manager secrets on its own deploy step, never env vars or Cloud Build
substitutions (USER_DRIVE_INDEXING_RFC §24.2; CLAUDE.md deploy-substitution trap)."""
from pathlib import Path

import yaml

_BUILD = Path(__file__).resolve().parents[2] / "cloudbuild-dev.yaml"


def _indexer_args():
    steps = yaml.safe_load(_BUILD.read_text())["steps"]
    hits = [s["args"] for s in steps if s.get("args", [])[:3] == ["run", "deploy", "alek-bot-indexer-dev"]]
    assert len(hits) == 1
    return hits[0]


def _flag(args, name):
    return next(a.split("=", 1)[1] for a in args if a.startswith(f"--{name}="))


def test_model_keys_are_indexer_secrets():
    secrets = dict(pair.split("=", 1) for pair in _flag(_indexer_args(), "set-secrets").split(","))
    assert secrets["ANTHROPIC_API_KEY"] == "ANTHROPIC_API_KEY:latest"
    assert secrets["OPENAI_API_KEY"] == "OPENAI_API_KEY:latest"
    assert secrets["BILLING_SLACK_WEBHOOK_URL"] == "BILLING_SLACK_WEBHOOK_URL:latest"


def test_model_keys_never_travel_as_env_vars_or_substitutions():
    env = _flag(_indexer_args(), "set-env-vars")
    assert "API_KEY" not in env and "$_ANTHROPIC" not in env and "$_OPENAI" not in env
