#!/usr/bin/env python3
"""
Per-layer coverage floors (TEST_COVERAGE_SYSTEMIC_RFC §2 / P2), run after the per-file gate.

Reads ``coverage.json`` (``pytest --cov --cov-report=json``, branch coverage on) and checks one floor per layer of
the code base. The metric is the branch-aware percentage coverage.py itself reports for a file,
``(covered_lines + covered_branches) / (num_statements + num_branches)``, aggregated over the layer's files from
their ``summary`` numbers (never an average of per-file percentages).

Why layers and not only the global number: a global ``--cov-fail-under`` lets a 98 % domain hide a 60 % web layer,
and a file no test imports is absent from a plain report. ``.coveragerc`` lists every ``src/`` file (including
the ones nobody imported, at 0 %) and the root entry points; an entry point that no test imports (none today;
``main.py`` was one until P9) is not in the data at all, so it is counted here as 0 % from its source with
coverage.py's own parser.

THE RULE: a floor rises, or it may be lowered in the
same PR that needs it, with a one-line reason comment next to the value and the reviewer's OK; the owner reads the
lowering in the diff (it is never silent). Each floor is the lowest value observed minus NOISE_PP, rounded DOWN to one
decimal (run-to-run coverage noise is ~0.1 pp). When measured − NOISE_PP is 0.5 points or more above a floor the
script prints "floor can rise to X": raise it in the same change that earned it.

Exit codes: 0 — all floors met; 1 — a floor missed, or coverage.json missing.
"""
from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

ROOT_ENTRY_POINTS = ("main.py", "relay_main.py", "indexer_main.py", "sandbox_main.py", "job_main.py")

# (layer name, path prefixes). The first matching prefix wins; "src (other)" takes the rest of src/
# (config, utils, locales, skills, ...), so every measured file is in exactly one layer.
LAYERS: List[Tuple[str, Tuple[str, ...]]] = [
    ("src/domain", ("src/domain/",)),
    ("src/ports", ("src/ports/",)),
    ("src/infrastructure", ("src/infrastructure/",)),
    ("src/agents", ("src/agents/",)),
    ("src/services", ("src/services/",)),
    ("src/handlers", ("src/handlers/",)),
    ("src/adapters", ("src/adapters/",)),
    ("src/web", ("src/web/",)),
    ("src/composition", ("src/composition/",)),
    ("src (other)", ("src/",)),
    ("root entry points", ROOT_ENTRY_POINTS),
]

# floor = min observed (CI runner and VM, 2026-10-22) − NOISE_PP, rounded down to 0.1; run-to-run coverage noise is ~0.1 pp
# (owner decision 2026-10-22). A floor rises; it may be lowered in the PR that needs it, with a one-line reason comment and the reviewer's OK (R9). Example (2026-10-22, P7 F1): `.coveragerc` stopped
# hiding whole functions whose signature holds `...`, so domain, ports and the root entry points were re-set from the
# honest measurement (VM run; the CI runner's numbers re-adjust them to min(VM, CI) − NOISE_PP).
NOISE_PP = 0.3
FLOORS: Dict[str, float] = {
    "src/domain": 98.0,
    "src/ports": 92.6,
    "src/infrastructure": 96.3,
    "src/agents": 95.0,
    "src/services": 91.9,
    "src/handlers": 93.1,
    "src/adapters": 88.0,
    "src/web": 77.3,
    "src/composition": 94.1,
    "src (other)": 75.8,
    "root entry points": 76.0,
}

RISE_MARGIN = 0.5

COVERAGE_JSON = os.environ.get("COVERAGE_JSON_PATH", "coverage.json")
COVERAGERC = os.environ.get("COVERAGE_RCFILE", ".coveragerc")


@dataclass
class LayerTotals:
    statements: int = 0
    branches: int = 0
    covered_lines: int = 0
    covered_branches: int = 0

    def add(self, other: "LayerTotals") -> None:
        self.statements += other.statements
        self.branches += other.branches
        self.covered_lines += other.covered_lines
        self.covered_branches += other.covered_branches

    @property
    def line_percent(self) -> float:
        return 100.0 * self.covered_lines / self.statements if self.statements else 100.0

    @property
    def percent(self) -> float:
        """Branch-aware, as coverage.py computes it for one file."""
        total = self.statements + self.branches
        return 100.0 * (self.covered_lines + self.covered_branches) / total if total else 100.0


def floor_to_tenth(value: float) -> float:
    return math.floor(value * 10 + 1e-9) / 10


def layer_of(path: str) -> Optional[str]:
    for name, prefixes in LAYERS:
        if any(path == p or (p.endswith("/") and path.startswith(p)) for p in prefixes):
            return name
    return None


def _normalise_path(p: str) -> str:
    cwd = os.getcwd().rstrip(os.sep) + os.sep
    return p[len(cwd):] if p.startswith(cwd) else p


def totals_of(summary: dict) -> LayerTotals:
    return LayerTotals(
        statements=int(summary.get("num_statements", 0)),
        branches=int(summary.get("num_branches", 0)),
        covered_lines=int(summary.get("covered_lines", 0)),
        covered_branches=int(summary.get("covered_branches", 0)),
    )


def aggregate(files: Dict[str, dict]) -> Dict[str, LayerTotals]:
    """{layer: totals} over coverage.json's ``files`` (keys are repo-relative or absolute paths)."""
    layers = {name: LayerTotals() for name, _ in LAYERS}
    for raw_path, data in files.items():
        name = layer_of(_normalise_path(raw_path))
        if name is not None:
            layers[name].add(totals_of(data.get("summary", {})))
    return layers


def static_zero_totals(path: str) -> LayerTotals:
    """A source file no test imported: every statement and branch counts as missed (coverage.py's own parser)."""
    from coverage.config import read_coverage_config
    from coverage.python import PythonParser

    exclude = None
    if os.path.isfile(COVERAGERC):
        lines = read_coverage_config(COVERAGERC, warn=lambda *a, **k: None).exclude_list
        exclude = "|".join(f"(?:{line})" for line in lines) or None
    parser = PythonParser(filename=path, exclude=exclude)
    parser.parse_source()
    branches = sum(count for count in parser.exit_counts().values() if count > 1)
    return LayerTotals(statements=len(parser.statements), branches=branches)


def missing_entry_points(files: Dict[str, dict], names: Iterable[str] = ROOT_ENTRY_POINTS) -> List[str]:
    measured = {_normalise_path(p) for p in files}
    return [n for n in names if n not in measured and os.path.isfile(n)]


def check(layers: Dict[str, LayerTotals], floors: Dict[str, float]) -> Tuple[List[str], List[str], List[str]]:
    """(violations, rises, unfloored) as printable lines."""
    violations, rises, unfloored = [], [], []
    for name, _ in LAYERS:
        measured = layers[name].percent
        floor = floors.get(name)
        if floor is None:
            unfloored.append(f"{name}: measured {measured:.2f}% has no floor in FLOORS")
            continue
        if measured + 1e-6 < floor:
            violations.append(f"{name}: {measured:.2f}% < floor {floor:.1f}%")
        elif floor_to_tenth(measured - NOISE_PP) - floor >= RISE_MARGIN:
            rises.append(
                f"{name}: measured {measured:.2f}%, floor {floor:.1f}% — floor can rise to {floor_to_tenth(measured - NOISE_PP):.1f}"
            )
    return violations, rises, unfloored


def table(layers: Dict[str, LayerTotals], floors: Dict[str, float]) -> str:
    head = f"{'layer':<20}{'statements':>11}{'branches':>10}{'line %':>9}{'branch-aware %':>16}{'floor %':>9}"
    rows = [head, "-" * len(head)]
    for name, _ in LAYERS:
        t = layers[name]
        floor = floors.get(name)
        rows.append(
            f"{name:<20}{t.statements:>11}{t.branches:>10}{t.line_percent:>9.1f}{t.percent:>16.2f}"
            f"{('-' if floor is None else format(floor, '.1f')):>9}"
        )
    return "\n".join(rows)


def load_files() -> Dict[str, dict]:
    if not os.path.isfile(COVERAGE_JSON):
        print(f"❌ {COVERAGE_JSON} not found. Run pytest with `--cov --cov-report=json` first.", file=sys.stderr)
        sys.exit(1)
    with open(COVERAGE_JSON) as f:
        return json.load(f).get("files", {})


def main() -> int:
    files = load_files()
    layers = aggregate(files)
    for name in missing_entry_points(files):
        layers[layer_of(name) or "root entry points"].add(static_zero_totals(name))
        print(f"ℹ️  {name} is imported by no test: counted as 0 % from its source.")
    print(table(layers, FLOORS))
    violations, rises, unfloored = check(layers, FLOORS)
    for line in rises:
        print(f"⬆️  {line}")
    if violations or unfloored:
        for line in violations:
            print(f"❌ {line}", file=sys.stderr)
        for line in unfloored:
            print(f"❌ {line}", file=sys.stderr)
        return 1
    print(f"✅ Layer gate: all {len(LAYERS)} floors met.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
