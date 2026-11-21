"""The media bucket name stays out of tracked files (the private personal-data denylist guards that): both deploy
steps take GCS_MEDIA_BUCKET from the `_GCS_MEDIA_BUCKET` substitution, which `make deploy` feeds from the deploy-only
key GCS_MEDIA_BUCKET_DEV and refuses when that key is empty."""
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_BUILD = yaml.safe_load((_ROOT / "cloudbuild-dev.yaml").read_text())
_MAKEFILE = (_ROOT / "Makefile").read_text()


def _env(service: str):
    (args,) = [s["args"] for s in _BUILD["steps"] if s.get("args", [])[:3] == ["run", "deploy", service]]
    env = next(a.split("=", 1)[1] for a in args if a.startswith("--set-env-vars="))
    return dict(pair.split("=", 1) for pair in env.split(","))


def _main_service() -> str:
    deploys = [s["args"][2] for s in _BUILD["steps"] if s.get("args", [])[:2] == ["run", "deploy"]]
    return next(d for d in deploys if "indexer" not in d and "relay" not in d and "sandbox" not in d)


def test_main_service_and_research_job_set_the_bucket_from_one_substitution():
    assert _env(_main_service())["GCS_MEDIA_BUCKET"] == "$_GCS_MEDIA_BUCKET"
    (research_job,) = [s["args"] for s in _BUILD["steps"] if s.get("args", [])[:3] == ["run", "jobs", "deploy"]
                       and "--args=job_main.py" in s["args"]]
    job_env = next(a.split("=", 1)[1] for a in research_job if a.startswith("--set-env-vars="))
    assert dict(p.split("=", 1) for p in job_env.split(","))["GCS_MEDIA_BUCKET"] == "$_GCS_MEDIA_BUCKET"


def test_the_substitution_defaults_to_empty():
    assert _BUILD["substitutions"]["_GCS_MEDIA_BUCKET"] == ""


def test_make_deploy_passes_the_deploy_only_key_and_refuses_it_empty():
    assert "_GCS_MEDIA_BUCKET=$(GCS_MEDIA_BUCKET_DEV)" in _MAKEFILE
    assert 'if [ -z "$(GCS_MEDIA_BUCKET_DEV)" ]' in _MAKEFILE
    assert "❌ GCS_MEDIA_BUCKET_DEV is empty: set the media bucket name in .env (docs/07_deployment/README.md)" in _MAKEFILE
