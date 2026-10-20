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


def test_only_the_migrated_runtimes_name_their_identity_so_far():
    """Steps 3 and 4 have moved: exactly the research job and the indexer name an identity, each its own.
    Any extra or misplaced --service-account (e.g. on main or the relay before Steps 5-6) fails here."""
    named = {}
    for step in _steps():
        args = step.get("args", [])
        flags = [a for a in args if a.startswith("--service-account=")]
        if not flags:
            continue
        runtime = "research-job" if "--args=job_main.py" in args else (
            args[2] if args[:2] == ["run", "deploy"] else "unknown")
        named.setdefault(runtime, []).extend(flags)
    assert named == {
        "research-job": ["--service-account=alek-research-job@$PROJECT_ID.iam.gserviceaccount.com"],
        "alek-bot-indexer-dev": ["--service-account=alek-indexer@$PROJECT_ID.iam.gserviceaccount.com"],
    }
