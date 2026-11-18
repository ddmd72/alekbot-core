"""Writes which tests ran and which were skipped, and why, to the path in SKIP_REPORT_PATH (P22: "CI reports which ran").

A skip ledger says which gate is meant to run a test; this report says what the run did. Enabled by `-p tests.plugins.skip_report`
(the CI step that passes it and uploads the file is the workflow's, not this module's). Without SKIP_REPORT_PATH it does nothing.
Under xdist the controller process writes the one file (workers report through the same terminal-summary hook on the controller).
"""
import json
import os
from collections import Counter
from typing import Dict, List


def pytest_configure(config):
    config._skip_report = {"outcomes": Counter(), "skips": []}


def _state(config) -> Dict:
    return config._skip_report


def pytest_report_teststatus(report, config):
    if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
        state = _state(config)
        outcome = "skipped" if report.skipped else report.outcome
        state["outcomes"][outcome] += 1
        if report.skipped:
            longrepr = report.longrepr
            reason = longrepr[2] if isinstance(longrepr, tuple) else str(longrepr)
            state["skips"].append({"test": report.nodeid, "reason": reason.removeprefix("Skipped: ")})
    return None


def build_summary(state: Dict) -> Dict:
    by_reason: Dict[str, List[str]] = {}
    for skip in state["skips"]:
        by_reason.setdefault(skip["reason"], []).append(skip["test"])
    return {
        "outcomes": dict(sorted(state["outcomes"].items())),
        "skipped_by_reason": {reason: {"count": len(tests), "tests": sorted(tests)} for reason, tests in sorted(by_reason.items())},
    }


def pytest_sessionfinish(session):
    path = os.environ.get("SKIP_REPORT_PATH")
    if not path or hasattr(session.config, "workerinput"):
        return
    with open(path, "w", encoding="utf-8") as out:
        json.dump(build_summary(_state(session.config)), out, indent=2, sort_keys=True)
        out.write("\n")
