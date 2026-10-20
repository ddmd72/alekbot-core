"""IAM least-privilege Step 3: the deep-research job runs as its own identity, named through the build's
`$PROJECT_ID` substitution; the other runtimes have not moved yet (Steps 4-6)."""
from pathlib import Path

import yaml

_BUILD = Path(__file__).resolve().parents[2] / "cloudbuild-dev.yaml"


def _steps():
    return yaml.safe_load(_BUILD.read_text())["steps"]


def _job_args():
    for step in _steps():
        args = step.get("args", [])
        if "--args=job_main.py" in args:
            return args
    raise AssertionError("no research job deploy step")


def test_the_research_job_runs_as_its_own_identity():
    assert "--service-account=alek-research-job@$PROJECT_ID.iam.gserviceaccount.com" in _job_args()


def test_exactly_one_runtime_names_its_identity_so_far():
    named = [a for s in _steps() for a in s.get("args", []) if a.startswith("--service-account=")]
    assert named == ["--service-account=alek-research-job@$PROJECT_ID.iam.gserviceaccount.com"]
