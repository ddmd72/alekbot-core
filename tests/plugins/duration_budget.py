"""Per-test duration budget: a CI warning, never a gate (owner decision 2026-10-22; TEST_COVERAGE_SYSTEMIC_RFC §4b).

With env `PYTEST_DURATION_BUDGET_S=<seconds>` the terminal summary lists every test that took longer than the budget
(setup + call + teardown) and is neither `slow` nor `timing`, slowest first with the setup/call/teardown split. The exit
status is never changed: wall-clock limits flake on a shared runner (PR #133 failed on exactly this). When
`GITHUB_ACTIONS=true` the list is also emitted as one `::warning` workflow command, so it shows as an annotation on the
run. A test that keeps showing up gets `@pytest.mark.slow`. Unset: this plugin does nothing.

Summed on the controller from the phase reports, so it works under xdist. Tests marked `slow` run in `make ci` only
(RUN_SLOW_TESTS=1) and are exempt here. The exemption is decided where the item lives (a worker, under xdist) with
`get_closest_marker` and shipped in `user_properties`: the report's `keywords` also hold node names and parametrize ids,
so a case called "slow" would otherwise be exempt.
"""

import os
from typing import Dict, List, Tuple

import pytest

ENV_BUDGET = "PYTEST_DURATION_BUDGET_S"
EXEMPT_MARKERS = ("slow", "timing")
PROP = "duration_budget_exempt"
PHASES = ("setup", "call", "teardown")
MAX_ANNOTATED = 50  # annotations are for a human to read; the terminal summary has the full list


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

    def pytest_terminal_summary(self, terminalreporter) -> None:
        if not self.over:
            terminalreporter.write_line(
                f"duration budget {self.budget_s:g} s: 0 over ({len(self._phases)} tests)"
            )
            return
        terminalreporter.section(f"tests over the {self.budget_s:g} s budget ({len(self.over)}) -- warning only", yellow=True)
        lines = []
        for nodeid, total, phases in self.over:
            split = " ".join(f"{name} {phases[name]:.2f}" for name in PHASES if name in phases)
            lines.append(f"{total:.2f}s {nodeid} ({split})")
            terminalreporter.write_line(f"{total:8.2f}s  {nodeid}  ({split})")
        terminalreporter.write_line(
            "Not a failure. If a test keeps showing up, mark it @pytest.mark.slow (runs in `make ci` only) -- "
            "tests/CLAUDE.md. A large setup is usually a first-use cost paid by whichever test of the worker runs first."
        )
        if os.environ.get("GITHUB_ACTIONS") == "true":
            terminalreporter.write_line(github_warning(self.budget_s, lines))


def github_warning(budget_s: float, lines: List[str]) -> str:
    """One `::warning` workflow command: `%` is escaped first, then the line breaks that the command syntax reserves."""
    shown = lines[:MAX_ANNOTATED]
    if len(lines) > len(shown):
        shown.append(f"... and {len(lines) - len(shown)} more")
    message = "\n".join(shown).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::warning title=Tests over the {budget_s:g} s budget::{message}"


def pytest_configure(config: pytest.Config) -> None:
    raw = os.environ.get(ENV_BUDGET, "").strip()
    if not raw:
        return
    try:
        budget_s = float(raw)
    except ValueError:
        raise pytest.UsageError(f"{ENV_BUDGET}={raw!r} is not a number of seconds") from None
    config.pluginmanager.register(DurationBudget(budget_s, hasattr(config, "workerinput")), "duration-budget")
