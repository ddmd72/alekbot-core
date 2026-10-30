"""cloudbuild-dev.yaml step graph: every step has an id, every waitFor names an existing earlier step, and nothing the
main service deploy depends on is a drive, relay, job or scheduler step (a failure there never blocks a bot hotfix)."""
from pathlib import Path

import yaml

_STEPS = yaml.safe_load((Path(__file__).resolve().parents[2] / "cloudbuild-dev.yaml").read_text())["steps"]
_BY_ID = {s["id"]: s for s in _STEPS}


def _ancestors(step_id):
    seen, todo = set(), list(_BY_ID[step_id].get("waitFor", []))
    while todo:
        cur = todo.pop()
        if cur not in seen:
            seen.add(cur)
            todo.extend(_BY_ID[cur].get("waitFor", []))
    return seen


def test_every_step_has_a_unique_id_and_waitfor_names_an_earlier_step():
    ids = [s.get("id") for s in _STEPS]
    assert all(ids) and len(set(ids)) == len(ids)
    for pos, step in enumerate(_STEPS):
        for dep in step.get("waitFor", []):
            assert dep in ids[:pos], (step["id"], dep)


def test_every_step_after_the_first_declares_its_dependencies():
    assert [s["id"] for s in _STEPS if "waitFor" not in s] == ["pull-cache"]


def test_main_deploy_depends_only_on_the_image_steps():
    assert _ancestors("deploy-main") == {"pull-cache", "build-image", "push-image"}


def test_independent_tail_waits_for_the_main_service_being_public():
    for sid in ("drive-queue", "deploy-research-job", "deploy-relay"):
        assert "main-public-iam" in _ancestors(sid), sid
    assert "drive-queue" in _ancestors("scheduler-jobs")
