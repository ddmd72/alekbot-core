"""IAM least-privilege Step 2a: the identity secrets the OIDC gates read are mounted where they are read —
main: drive invoker, relay identity and the transition list; indexer: drive invoker and the transition list.
`OIDC_TRANSITION_SA_EMAILS` is read with os.getenv, so without the mount it is silently absent."""
import pytest
from pathlib import Path

import yaml

pytestmark = pytest.mark.datafile

_BUILD = Path(__file__).resolve().parents[2] / "cloudbuild-dev.yaml"


def _deploy_args(service):
    for step in yaml.safe_load(_BUILD.read_text())["steps"]:
        args = step.get("args", [])
        if args[:3] == ["run", "deploy", service]:
            return args
    raise AssertionError(f"no deploy step for {service}")


def _secrets(service):
    flag = next(a for a in _deploy_args(service) if a.startswith("--set-secrets="))
    return dict(pair.split("=", 1) for pair in flag.split("=", 1)[1].split(","))


def test_main_mounts_every_identity_secret_its_gates_read():
    secrets = _secrets("alek-bot-dev")
    for name in ("SERVICE_ACCOUNT_EMAIL", "DRIVE_INVOKER_SA_EMAIL", "VOICE_RELAY_SA_EMAIL", "OIDC_TRANSITION_SA_EMAILS"):
        assert secrets[name] == f"{name}:latest"


def test_indexer_mounts_the_drive_invoker_and_the_transition_list():
    secrets = _secrets("alek-bot-indexer-dev")
    for name in ("SERVICE_ACCOUNT_EMAIL", "DRIVE_INVOKER_SA_EMAIL", "OIDC_TRANSITION_SA_EMAILS"):
        assert secrets[name] == f"{name}:latest"
    assert "VOICE_RELAY_SA_EMAIL" not in secrets   # the indexer never admits the relay
