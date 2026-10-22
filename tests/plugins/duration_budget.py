"""Per-test duration budget, enforced on CI only (TEST_COVERAGE_SYSTEMIC_RFC §4b).

With env `PYTEST_DURATION_BUDGET_S=<seconds>` the run fails at the end when a test took longer than the budget
(setup + call + teardown) and is neither `slow` nor `timing`. Unset: this plugin does nothing. A loaded laptop gives
the same false failures the `timing` marker removed, so only the CI job sets it (`make ci`).

Summed on the controller from the phase reports, so it works under xdist. Tests marked `slow` run in `make ci` only
(RUN_SLOW_TESTS=1) and are exempt here.
"""

import os
from typing import Dict, List, Tuple

import pytest

ENV_BUDGET = "PYTEST_DURATION_BUDGET_S"
EXEMPT_MARKERS = ("slow", "timing")


class DurationBudget:
    def __init__(self, budget_s: float) -> None:
        self.budget_s = budget_s
        self._total: Dict[str, float] = {}
        self._exempt: set = set()
        self.over: List[Tuple[str, float]] = []

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        self._total[report.nodeid] = self._total.get(report.nodeid, 0.0) + report.duration
        if any(m in report.keywords for m in EXEMPT_MARKERS):
            self._exempt.add(report.nodeid)

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        self.over = sorted(
            ((n, d) for n, d in self._total.items() if d > self.budget_s and n not in self._exempt),
            key=lambda item: -item[1],
        )
        if self.over and session.exitstatus == pytest.ExitCode.OK:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

    def pytest_terminal_summary(self, terminalreporter) -> None:
        if not self.over:
            return
        terminalreporter.section(f"tests over the {self.budget_s:g} s budget ({len(self.over)})", red=True)
        for nodeid, duration in self.over:
            terminalreporter.write_line(f"{duration:8.2f}s  {nodeid}")
        terminalreporter.write_line(
            "Make it faster, or mark it @pytest.mark.slow (runs in `make ci` only) -- tests/CLAUDE.md."
        )


def pytest_configure(config: pytest.Config) -> None:
    raw = os.environ.get(ENV_BUDGET, "").strip()
    if not raw or hasattr(config, "workerinput"):
        return
    try:
        budget_s = float(raw)
    except ValueError:
        raise pytest.UsageError(f"{ENV_BUDGET}={raw!r} is not a number of seconds") from None
    config.pluginmanager.register(DurationBudget(budget_s), "duration-budget")
