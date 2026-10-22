"""The CLI: run the chosen tests once per mutant of one module, in a child pytest each.

    python -m tests.support.semantic_mutants --module src.adapters.firestore_repo \\
        --tests tests/unit/adapters/test_fact_store_roundtrip.py [more test paths or node ids]
        [--equivalent FILE]  [--cwd DIR]

--equivalent FILE  mutants a reviewer ruled equivalent, one `key  # reason` per line (the 8-hex key the report prints;
                   keys survive unrelated edits, numbers do not). The tool never rules a mutant equivalent itself.
--cwd DIR          directory of the child runs (default: the repository root); the self-tests use it for a toy module.

Per mutant: killed (the selected tests failed or the run hung), survived (they passed: the tests do not notice the
defect), error (the run could not tell: the mutated module was never loaded, or a collection/usage error). A baseline
run with no mutation must pass first. Exit code: 1 if any mutant survived, 2 if the baseline failed or any mutant
errored (and none survived), else 0.
"""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .operators import Mutant, enumerate_mutants

REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGIN = "tests.support.semantic_mutants.activate"
MUTANT_TIMEOUT_S = 300  # a mutant that hangs (a dropped loop guard) counts as killed


@dataclass
class Outcome:
    mutant: Mutant
    status: str  # killed | survived | equivalent | error
    detail: str = ""


def module_path(module: str, cwd: Path) -> Path:
    sys.path.insert(0, str(cwd))
    try:
        spec = importlib.util.find_spec(module)
    finally:
        sys.path.remove(str(cwd))
    if spec is None or not spec.origin or not spec.origin.endswith(".py"):
        raise SystemExit(f"cannot find the source of module {module!r} from {cwd}")
    return Path(spec.origin)


def read_equivalent(path: Optional[str]) -> Dict[str, str]:
    """`key  # reason` per line; blank lines and lines starting with # are skipped."""
    entries: Dict[str, str] = {}
    for raw in Path(path).read_text().splitlines() if path else []:
        line = raw.strip()
        if line and not line.startswith("#"):
            key, _, reason = line.partition("#")
            entries[key.strip()] = reason.strip()
    return entries


def _child_env(cwd: Path, extra: Dict[str, str]) -> Dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT), str(cwd), env.get("PYTHONPATH", "")]))
    # a mutant run is not coverage data: the outer run's subprocess-coverage switches must not reach it
    for name in ("SEMANTIC_MUTANT_MODULE", "SEMANTIC_MUTANT_NUMBER", "SEMANTIC_MUTANT_REPORT",
                 "COVERAGE_PROCESS_CONFIG", "COVERAGE_PROCESS_START"):
        env.pop(name, None)
    env.setdefault("RUN_SLOW_TESTS", "1")  # a slow-marked test in --tests must run in the child, else false survivors
    env.update(extra)
    return env


def run_pytest(tests: List[str], cwd: Path, env_extra: Dict[str, str], timeout: Optional[float]) -> subprocess.CompletedProcess:
    command = [sys.executable, "-m", "pytest", *tests, "-x", "-q", "-p", "no:testmon", "-p", "no:cacheprovider", "-p", PLUGIN]
    return subprocess.run(
        command, cwd=cwd, env=_child_env(cwd, env_extra), capture_output=True, text=True, timeout=timeout, check=False
    )


def run_mutant(mutant: Mutant, module: str, tests: List[str], cwd: Path) -> Outcome:
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.json"
        extra = {
            "SEMANTIC_MUTANT_MODULE": module,
            "SEMANTIC_MUTANT_NUMBER": str(mutant.number),
            "SEMANTIC_MUTANT_REPORT": str(report),
        }
        try:
            done = run_pytest(tests, cwd, extra, MUTANT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return Outcome(mutant, "killed", f"timeout after {MUTANT_TIMEOUT_S}s")
        loaded = report.exists() and json.loads(report.read_text()).get("loaded")
    tail = (done.stdout.strip().splitlines() or [""])[-1]
    if not loaded:
        return Outcome(mutant, "error", f"the mutated module was never loaded by the selected tests (pytest exit {done.returncode}: {tail})")
    if done.returncode == 1:
        return Outcome(mutant, "killed", tail)
    if done.returncode == 0:
        return Outcome(mutant, "survived", tail)
    return Outcome(mutant, "error", f"pytest exit {done.returncode}: {tail}")


def format_outcome(o: Outcome) -> str:
    m = o.mutant
    line = f"  #{m.number:<4}{'[' + o.status + ']':<13}L{m.lineno:<6}{m.operator:<15}{m.description}  key={m.key}"
    return line + (f"\n        {o.detail}" if o.status == "error" else "")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tests.support.semantic_mutants", description=__doc__.split("\n\n")[0])
    ap.add_argument("--module", required=True, help="dotted module to mutate, e.g. src.adapters.firestore_repo")
    ap.add_argument("--tests", nargs="+", required=True, help="pytest paths / node ids that must kill every mutant")
    ap.add_argument("--equivalent", help="file of `key  # reason` lines: mutants a reviewer ruled equivalent")
    ap.add_argument("--cwd", default=str(REPO_ROOT))
    args = ap.parse_args(argv)

    cwd = Path(args.cwd).resolve()
    source_file = module_path(args.module, cwd)
    mutants = enumerate_mutants(source_file.read_text(encoding="utf-8"))
    equivalent = read_equivalent(args.equivalent)
    print(f"semantic mutants of {args.module}: {len(mutants)}")

    started = time.monotonic()
    base = run_pytest(args.tests, cwd, {}, None)
    if base.returncode != 0:
        print("baseline run (no mutation) did not pass: fix the tests before mutating.\n" + base.stdout[-2000:] + base.stderr[-1000:])
        return 2

    outcomes: List[Outcome] = []
    for m in mutants:
        o = Outcome(m, "equivalent", equivalent[m.key]) if m.key in equivalent else run_mutant(m, args.module, args.tests, cwd)
        outcomes.append(o)
        print(format_outcome(o) + (f"  ({o.detail})" if o.status == "equivalent" else ""), flush=True)

    counts = {s: sum(o.status == s for o in outcomes) for s in ("killed", "survived", "equivalent", "error")}
    print(f"\n{len(outcomes)} mutants: {counts['killed']} killed, {counts['survived']} survived, "
          f"{counts['equivalent']} ruled equivalent, {counts['error']} error  ({time.monotonic() - started:.0f}s)")
    survivors = [o for o in outcomes if o.status == "survived"]
    if survivors:
        print("SURVIVORS (the tests do not notice these defects; kill them or have a reviewer rule them equivalent):")
        for o in survivors:
            print(format_outcome(o))
        return 1
    return 2 if counts["error"] else 0
