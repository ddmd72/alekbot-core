"""IAM least-privilege Step 5: the voice relay runs as its own identity, named through `$PROJECT_ID`.
The main service already admits it on /voice/* through VOICE_RELAY_SA_EMAIL."""
import pytest
from pathlib import Path

import yaml

_BUILD = Path(__file__).resolve().parents[2] / "cloudbuild-dev.yaml"


def _deploy_args(service):
    for step in yaml.safe_load(_BUILD.read_text())["steps"]:
        args = step.get("args", [])
        if args[:3] == ["run", "deploy", service]:
            return args
    raise AssertionError(f"no deploy step for {service}")


@pytest.mark.datafile
def test_the_relay_runs_as_its_own_identity():
    assert "--service-account=alek-relay@$PROJECT_ID.iam.gserviceaccount.com" in _deploy_args("alek-voice-relay-dev")
