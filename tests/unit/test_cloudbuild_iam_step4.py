"""IAM least-privilege Step 4: the drive indexer runs as its own identity, named through `$PROJECT_ID`."""
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
def test_the_indexer_runs_as_its_own_identity():
    assert "--service-account=alek-indexer@$PROJECT_ID.iam.gserviceaccount.com" in _deploy_args("alek-bot-indexer-dev")
