"""IAM least-privilege Step 1: every build runs as the deployer identity, named through the build's own
`$PROJECT_ID` substitution (no `.env` dependency, so a second checkout cannot silently build as the default compute
identity)."""
import pytest
from pathlib import Path

import yaml

pytestmark = pytest.mark.datafile

_BUILD = Path(__file__).resolve().parents[2] / "cloudbuild-dev.yaml"


def _doc():
    return yaml.safe_load(_BUILD.read_text())


def test_build_runs_as_the_deployer_identity():
    assert _doc()["serviceAccount"] == (
        "projects/$PROJECT_ID/serviceAccounts/alek-deployer@$PROJECT_ID.iam.gserviceaccount.com"
    )


def test_a_user_specified_build_identity_logs_to_cloud_logging_only():
    assert _doc()["options"]["logging"] == "CLOUD_LOGGING_ONLY"


def test_no_build_runs_as_the_default_compute_identity():
    assert "developer.gserviceaccount.com" not in _BUILD.read_text()
