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
PROP = "duration_budget_exempt"
PHASES = ("setup", "call", "teardown")


class DurationBudget:
    def __init__(self, budget_s: float, is_worker: bool) -> None:
        self.budget_s = budget_s
        self.is_worker = is_worker
        self._phases: Dict[str, Dict[str, float]] = {}
        self._exempt: set = set()
        self.over: List[Tuple[str, float, Dict[str, float]]] = []

    def pytest_runtest_setup(self, item: pytest.Item) -> None:
        if any(item.get_closest_marker(m) is not None for m in EXEMPT_MARKERS):
            item.user_properties.append((PROP, True))

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if self.is_worker:
            return
        phases = self._phases.setdefault(report.nodeid, {})
        phases[report.when] = phases.get(report.when, 0.0) + report.duration
        if any(key == PROP and value for key, value in report.user_properties):
            self._exempt.add(report.nodeid)

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        if self.is_worker:
            return
        totals = ((n, sum(p.values()), p) for n, p in self._phases.items())
        self.over = sorted(
            ((n, total, p) for n, total, p in totals if total > self.budget_s and n not in self._exempt),
            key=lambda item: -item[1],
        )
        if self.over and session.exitstatus == pytest.ExitCode.OK:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

    def pytest_terminal_summary(self, terminalreporter) -> None:
        if not self.over:
            return
        terminalreporter.section(f"tests over the {self.budget_s:g} s budget ({len(self.over)})", red=True)
        for nodeid, total, phases in self.over:
            split = " ".join(f"{name} {phases[name]:.2f}" for name in PHASES if name in phases)
            terminalreporter.write_line(f"{total:8.2f}s  {nodeid}  ({split})")
        terminalreporter.write_line(
            "Make it faster, or mark it @pytest.mark.slow (runs in `make ci` only) -- tests/CLAUDE.md. "
            "A large setup is usually a first-use cost paid by whichever test of the worker runs first."
        )


def pytest_configure(config: pytest.Config) -> None:
    raw = os.environ.get(ENV_BUDGET, "").strip()
    if not raw:
        return
    try:
        budget_s = float(raw)
    except ValueError:
        raise pytest.UsageError(f"{ENV_BUDGET}={raw!r} is not a number of seconds") from None
    config.pluginmanager.register(DurationBudget(budget_s, hasattr(config, "workerinput")), "duration-budget")
