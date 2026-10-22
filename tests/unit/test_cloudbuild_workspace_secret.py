"""The workspace bucket name reaches the main service only as a Secret Manager secret — never an env var or a Cloud
Build substitution (AGENT_WORKSPACE_RFC §5.5; CLAUDE.md deploy-substitution trap). Only the main service gets it."""
import pytest
from pathlib import Path

import yaml

pytestmark = pytest.mark.datafile

_BUILD = Path(__file__).resolve().parents[2] / "cloudbuild-dev.yaml"


def _deploy_args(service):
    steps = yaml.safe_load(_BUILD.read_text())["steps"]
    hits = [s["args"] for s in steps if s.get("args", [])[:3] == ["run", "deploy", service]]
    assert len(hits) == 1
    return hits[0]


def _flag(args, name):
    return next(a.split("=", 1)[1] for a in args if a.startswith(f"--{name}="))


def test_the_main_service_reads_it_from_secret_manager():
    secrets = dict(pair.split("=", 1) for pair in _flag(_deploy_args("alek-bot-dev"), "set-secrets").split(","))
    assert secrets["GCS_WORKSPACE_BUCKET"] == "GCS_WORKSPACE_BUCKET:latest"


def test_never_an_env_var_or_a_substitution():
    env = _flag(_deploy_args("alek-bot-dev"), "set-env-vars")
    assert "GCS_WORKSPACE_BUCKET" not in env and "_WORKSPACE" not in env


def test_only_the_main_service_gets_it():
    assert _BUILD.read_text().count("GCS_WORKSPACE_BUCKET") == 2  # the secret mapping: name=secret:latest
