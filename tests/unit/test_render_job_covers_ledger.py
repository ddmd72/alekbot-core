"""The post-merge workflow `render.yml` and the skip ledger's `render-job` rows name the same test files, both ways.

A `render-job` row promises that the workflow runs the file (job `render`: node modules and a browser; job `sandbox-image`:
the built images and the `docker` marker). A file the workflow runs with no such row is a gate the ledger does not know. The
helper `scripts/ci/check_junit_ran.py` that makes a skipped test fail the job is tested here too.
"""
import re
from pathlib import Path

import pytest
import yaml

from scripts.ci import check_junit_ran
from tests.support import skip_ledger as sl

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "render.yml"


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _render_job_files(workflow):
    step = next(s for s in workflow["jobs"]["render"]["steps"] if s.get("name") == "Real-render tests")
    return set(step["env"]["TESTS"].split())


def _image_job_files(workflow):
    return {f for entry in workflow["jobs"]["sandbox-image"]["strategy"]["matrix"]["include"] for f in entry["tests"].split()}


def _ledger_files():
    """{path} of the render-job rows, split into image rows (the `docker` marker, SANDBOX_TEST_*IMAGE) and node/browser rows."""
    ledger = sl.parse_ledger(sl.LEDGER.read_text(encoding="utf-8"))
    image, render = set(), set()
    for (path, kind, condition), (_, gate, _reason) in ledger.items():
        if gate != "render-job":
            continue
        (image if kind == "marker" or "IMAGE" in condition else render).add(path)
    return render, image


def test_the_render_job_runs_exactly_the_files_of_the_node_and_browser_rows(workflow):
    render_rows, _ = _ledger_files()
    assert _render_job_files(workflow) == render_rows


def test_the_image_job_runs_exactly_the_files_of_the_image_rows(workflow):
    _, image_rows = _ledger_files()
    assert _image_job_files(workflow) == image_rows


def test_every_listed_file_exists(workflow):
    root = WORKFLOW.parents[2]
    assert [f for f in _render_job_files(workflow) | _image_job_files(workflow) if not (root / f).is_file()] == []


def test_the_render_job_leaves_the_docker_tests_to_the_image_job(workflow):
    run = next(s for s in workflow["jobs"]["render"]["steps"] if s.get("name") == "Real-render tests")["run"]
    assert '-m "not docker"' in run


def test_the_workflow_uses_no_secrets_and_a_read_only_token(workflow):
    assert workflow["permissions"] == {"contents": "read"}
    assert "secrets." not in WORKFLOW.read_text(encoding="utf-8")


def _junit(tmp_path, cases):
    body = "".join(f'<testcase classname="{c}" name="t{i}">{extra}</testcase>' for i, (c, extra) in enumerate(cases))
    path = tmp_path / "junit.xml"
    path.write_text(f"<testsuites><testsuite>{body}</testsuite></testsuites>", encoding="utf-8")
    return str(path)


FILE = "tests/unit/x/test_a.py"
MODULE = "tests.unit.x.test_a"


def test_junit_check_passes_when_every_file_has_a_pass(tmp_path):
    assert check_junit_ran.check(_junit(tmp_path, [(MODULE, "")]), [FILE], None) == []


def test_junit_check_fails_on_a_skip_unless_its_reason_is_allowed(tmp_path):
    path = _junit(tmp_path, [(MODULE, ""), (MODULE, '<skipped message="needs Playwright Chromium"/>')])
    assert check_junit_ran.check(path, [FILE], r"Playwright Chromium") == []
    assert len(check_junit_ran.check(path, [FILE], None)) == 1


def test_junit_check_fails_on_a_file_with_no_pass_and_on_a_missing_report(tmp_path):
    assert check_junit_ran.check(_junit(tmp_path, [(MODULE, '<failure message="x"/>')]), [FILE], None) == [f"no test passed in {FILE}"]
    assert re.match(r"no readable junit report", check_junit_ran.check(str(tmp_path / "none.xml"), [FILE], None)[0])
