"""The CLI: run chosen tests once per mutant of one module, in a child pytest each.

    python -m tests.support.semantic_mutants --module src.adapters.firestore_repo \\
        --tests tests/unit/adapters/test_fact_store_roundtrip.py [more test paths or node ids]

Options:
  --equivalent FILE   mutants a reviewer ruled equivalent: one `key  # reason` per line (the 8-hex key the report
                      prints; keys survive unrelated edits, numbers do not). A listed key that matches no mutant is
                      reported as stale. The tool never rules a mutant equivalent by itself.
  --list              print the mutants and exit
  --only 3,7,12       run only these mutant numbers
  --operators a,b     run only these operators (order_by-flip, where-drop, ...)
  --json FILE         write the full result as JSON (the backlog list for the next package)
  --timeout SECONDS   per-mutant limit (default: 20 x the baseline run, at least 60); a mutant that hangs is "killed"
  --jobs N            mutants run N at a time (default 1)
  --cwd DIR           directory of the child runs (default: the repository root)

Per mutant: killed (the selected tests failed), survived (they passed: the tests do not notice the defect), error (the
run could not tell: the mutated module was never loaded, or a collection/usage error). A baseline run with no mutation
must pass first. Exit code: 1 if any mutant survived, 2 if the baseline failed or any mutant errored (and none
survived), else 0.
"""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .operators import Mutant, enumerate_mutants

REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGIN = "tests.support.semantic_mutants.activate"


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
    entries: Dict[str, str] = {}
    if not path:
        return entries
    for number, raw in enumerate(Path(path).read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, reason = line.partition("#")
        key = key.strip()
        if len(key) != 8 or not all(c in "0123456789abcdef" for c in key):
            raise SystemExit(f"{path}:{number}: {key!r} is not an 8-hex mutant key")
        if not reason.strip():
            raise SystemExit(f"{path}:{number}: {key} has no reason (`key  # why the mutant cannot change behaviour`)")
        entries[key] = reason.strip()
    return entries


def _child_env(cwd: Path, extra: Dict[str, str]) -> Dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT), str(cwd), env.get("PYTHONPATH", "")]))
    # a mutant run is not coverage data: the outer run's subprocess-coverage switches must not reach it
    for name in ("SEMANTIC_MUTANT_MODULE", "SEMANTIC_MUTANT_NUMBER", "SEMANTIC_MUTANT_REPORT", "COVERAGE_PROCESS_CONFIG", "COVERAGE_PROCESS_START"):
        env.pop(name, None)
    env.update(extra)
    return env


def run_pytest(tests: List[str], cwd: Path, env_extra: Dict[str, str], timeout: Optional[float]) -> subprocess.CompletedProcess:
    command = [sys.executable, "-m", "pytest", *tests, "-x", "-q", "-p", "no:testmon", "-p", "no:cacheprovider", "-p", PLUGIN]
    return subprocess.run(
        command, cwd=cwd, env=_child_env(cwd, env_extra), capture_output=True, text=True, timeout=timeout, check=False
    )


def run_mutant(mutant: Mutant, module: str, tests: List[str], cwd: Path, timeout: float) -> Outcome:
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.json"
        extra = {
            "SEMANTIC_MUTANT_MODULE": module,
            "SEMANTIC_MUTANT_NUMBER": str(mutant.number),
            "SEMANTIC_MUTANT_REPORT": str(report),
        }
        try:
            done = run_pytest(tests, cwd, extra, timeout)
        except subprocess.TimeoutExpired:
            return Outcome(mutant, "killed", f"timeout after {timeout:.0f}s")
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
    ap.add_argument("--equivalent")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--operators")
    ap.add_argument("--json")
    ap.add_argument("--timeout", type=float)
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--cwd", default=str(REPO_ROOT))
    args = ap.parse_args(argv)

    cwd = Path(args.cwd).resolve()
    source_file = module_path(args.module, cwd)
    mutants = enumerate_mutants(source_file.read_text(encoding="utf-8"))
    if args.only:
        wanted = {int(x) for x in args.only.split(",")}
        mutants = [m for m in mutants if m.number in wanted]
    if args.operators:
        ops = set(args.operators.split(","))
        mutants = [m for m in mutants if m.operator in ops]
    equivalent = read_equivalent(args.equivalent)
    stale = sorted(set(equivalent) - {m.key for m in enumerate_mutants(source_file.read_text(encoding="utf-8"))})

    print(f"semantic mutants of {args.module} ({source_file.relative_to(cwd) if source_file.is_relative_to(cwd) else source_file}): {len(mutants)}")
    if args.list:
        for m in mutants:
            print(f"  #{m.number:<4}L{m.lineno:<6}{m.operator:<15}{m.description}  key={m.key}")
        return 0

    started = time.monotonic()
    base = run_pytest(args.tests, cwd, {}, None)
    baseline_s = time.monotonic() - started
    if base.returncode != 0:
        print("baseline run (no mutation) did not pass: fix the tests before mutating.\n" + base.stdout[-2000:] + base.stderr[-1000:])
        return 2
    timeout = args.timeout or max(60.0, 20 * baseline_s)

    todo = [m for m in mutants if m.key not in equivalent]
    outcomes: Dict[int, Outcome] = {m.number: Outcome(m, "equivalent", equivalent[m.key]) for m in mutants if m.key in equivalent}
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        for outcome in pool.map(lambda m: run_mutant(m, args.module, args.tests, cwd, timeout), todo):
            outcomes[outcome.mutant.number] = outcome
            print(format_outcome(outcome), flush=True)
    ordered = [outcomes[m.number] for m in mutants]
    for o in ordered:
        if o.status == "equivalent":
            print(format_outcome(o) + f"  ({o.detail})")

    counts = {s: sum(o.status == s for o in ordered) for s in ("killed", "survived", "equivalent", "error")}
    print(f"\n{len(ordered)} mutants: {counts['killed']} killed, {counts['survived']} survived, "
          f"{counts['equivalent']} ruled equivalent, {counts['error']} error  ({time.monotonic() - started:.0f}s)")
    for key in stale:
        print(f"stale: equivalent key {key} matches no mutant of the current source")
    survivors = [o for o in ordered if o.status == "survived"]
    if survivors:
        print("SURVIVORS (the tests do not notice these defects; kill them or have a reviewer rule them equivalent):")
        for o in survivors:
            print(format_outcome(o))
    if args.json:
        Path(args.json).write_text(json.dumps({"module": args.module, "tests": args.tests, "counts": counts,
                                               "mutants": [dict(asdict(o.mutant), status=o.status, detail=o.detail) for o in ordered]}, indent=1))
    if survivors:
        return 1
    return 2 if counts["error"] else 0
