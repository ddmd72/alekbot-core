"""The workflow `widget_renderer.yml` and the skip ledger's `widget-job` rows name the same test files, both ways.

A `widget-job` row promises that the workflow installs Playwright Chromium, runs the file and goes red when it skips; a file
the workflow runs with no such row is a gate the ledger does not know.
"""
from pathlib import Path

import pytest
import yaml

from tests.support import skip_ledger as sl

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "widget_renderer.yml"


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _step(workflow, name):
    return next(s for s in workflow["jobs"]["widget-renderer"]["steps"] if s.get("name") == name)


def _run_files(workflow):
    return set(_step(workflow, "Widget renderer tests")["env"]["TESTS"].split())


def _ledger_files():
    ledger = sl.parse_ledger(sl.LEDGER.read_text(encoding="utf-8"))
    return {path for (path, _kind, _condition), (_, gate, _reason) in ledger.items() if gate == "widget-job"}


def test_the_workflow_runs_exactly_the_files_of_the_widget_job_rows(workflow):
    assert _run_files(workflow) == _ledger_files() != set()


def test_a_pr_touching_a_run_file_or_the_renderer_runs_the_workflow(workflow):
    triggers = workflow.get("on", workflow.get(True))  # PyYAML reads the bare key `on` as True
    paths = set(triggers["pull_request"]["paths"])
    assert _run_files(workflow) | {"src/adapters/playwright_html_renderer.py", "requirements.txt"} <= paths


def test_a_skipped_playwright_test_fails_the_job(workflow):
    check = _step(workflow, "Fail when a widget test was skipped or a file ran nothing")
    assert check["if"] == "always()"
    assert "check_junit_ran.py junit-widget.xml" in check["run"]
    allowed = check["run"].split("--allow-skip", 1)[1]
    assert "Playwright" not in allowed and "Chromium" not in allowed


def test_the_workflow_uses_no_secrets_and_a_read_only_token(workflow):
    assert workflow["permissions"] == {"contents": "read"}
    assert "secrets." not in WORKFLOW.read_text(encoding="utf-8")
