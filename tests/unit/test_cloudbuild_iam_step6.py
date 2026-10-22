"""IAM least-privilege Step 6: the main service runs as its own identity, named through `$PROJECT_ID`.
After this step no runtime runs as the default compute identity."""
from pathlib import Path

import yaml

_BUILD = Path(__file__).resolve().parents[2] / "cloudbuild-dev.yaml"


def _deploy_args(service):
    for step in yaml.safe_load(_BUILD.read_text())["steps"]:
        args = step.get("args", [])
        if args[:3] == ["run", "deploy", service]:
            return args
    raise AssertionError(f"no deploy step for {service}")


def test_the_main_service_runs_as_its_own_identity():
    assert "--service-account=alek-main@$PROJECT_ID.iam.gserviceaccount.com" in _deploy_args("alek-bot-dev")


def test_every_runtime_names_its_own_identity():
    for service in ("alek-bot-dev", "alek-bot-indexer-dev", "alek-voice-relay-dev"):
        assert any(a.startswith("--service-account=") for a in _deploy_args(service)), service
