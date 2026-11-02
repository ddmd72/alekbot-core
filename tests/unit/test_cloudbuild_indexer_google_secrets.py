"""The indexer's deploy step mounts the Google OAuth client as secrets (GOOGLE_DRIVE_RFC GE-31, Δ11), never as env
vars or substitutions."""
from pathlib import Path

import yaml

_BUILD = Path(__file__).resolve().parents[2] / "cloudbuild-dev.yaml"


def _indexer_args():
    steps = yaml.safe_load(_BUILD.read_text())["steps"]
    hits = [s["args"] for s in steps if any("indexer_main.py" in str(a) for a in s.get("args", []))]
    assert len(hits) == 1
    return hits[0]


def _flag(args, name):
    return next(a.split("=", 1)[1] for a in args if a.startswith(f"--{name}="))


def test_both_google_secrets_are_mounted():
    secrets = dict(pair.split("=", 1) for pair in _flag(_indexer_args(), "set-secrets").split(","))
    assert secrets["GOOGLE_OAUTH_CLIENT_ID"] == "GOOGLE_OAUTH_CLIENT_ID:latest"
    assert secrets["GOOGLE_OAUTH_CLIENT_SECRET"] == "GOOGLE_OAUTH_CLIENT_SECRET:latest"


def test_they_do_not_travel_as_env_vars():
    assert "GOOGLE_OAUTH" not in _flag(_indexer_args(), "set-env-vars")
