"""Registry of the modules whose tests are held to the semantic-mutation bar (one entry per done package).

The weekly workflow (.github/workflows/mutation.yml) runs the driver once per entry and keeps the reports as artifacts.
The tests of a module are computed (tests_for), never listed by hand: the unit tests whose imports reach it through
tests/ helpers. A new `*_equivalent_mutants.txt` under tests/unit must be registered here (a unit test enforces it).

    python -m tests.support.mutation_targets --matrix [--module X]
    python -m tests.support.mutation_targets --tests-for MODULE   # the computed test list, space-separated
    python -m tests.support.mutation_targets --summarize DIR --out-md PATH --out-json PATH
    python -m tests.support.mutation_targets --annotations JSON_PATH   # GitHub annotations from the --out-json file
"""

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple


REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class MutationTarget:
    module: str
    equivalent: Optional[str]
    operators: str = "default"
    extra_tests: tuple[str, ...] = ()  # added to the computed selection, in case the import closure misses one

    @property
    def slug(self) -> str:
        return self.module.replace(".", "-")

    @property
    def tests(self) -> tuple[str, ...]:
        return tuple(sorted(set(tests_for(self.module)) | set(self.extra_tests)))


_ImportGraph = Dict[str, Tuple[str, FrozenSet[str]]]  # tests module -> (repo-relative path, names it may import)
_GRAPHS: Dict[str, _ImportGraph] = {}


def _dotted(rel: Path) -> str:
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imported_names(tree: ast.AST, package: str) -> FrozenSet[str]:
    """Every dotted name a file may import: `import a.b` -> a.b; `from a import b` -> a and a.b (b may be a module)."""
    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                up = package.split(".")[: len(package.split(".")) - (node.level - 1)] if package else []
                base = ".".join(filter(None, [*up, node.module or ""]))
            else:
                base = node.module or ""
            if not base:
                continue
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
    return frozenset(names)


def _import_graph(root: Path) -> _ImportGraph:
    key = str(root)
    if key not in _GRAPHS:
        graph: _ImportGraph = {}
        for path in sorted((root / "tests").rglob("*.py")):
            if path.name == "conftest.py":
                continue  # a conftest is never followed
            rel = path.relative_to(root)
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            package = ".".join(rel.parent.parts)
            graph[_dotted(rel)] = (rel.as_posix(), _imported_names(tree, package))
        _GRAPHS[key] = graph
    return _GRAPHS[key]


def tests_for(module: str, root: Path = REPO_ROOT) -> tuple[str, ...]:
    """Every tests/unit/**/test_*.py whose imports reach `module` through modules under tests/ only (never through src/)."""
    graph = _import_graph(root)
    reached = {name for name, (_, imports) in graph.items() if module in imports}
    changed = True
    while changed:
        changed = False
        for name, (_, imports) in graph.items():
            if name not in reached and imports & reached:
                reached.add(name)
                changed = True
    found = []
    for name in reached:
        rel = graph[name][0]
        if rel.startswith("tests/unit/") and Path(rel).name.startswith("test_"):
            found.append(rel)
    return tuple(sorted(found))


_WIRING_EXTRA = (
    "tests/unit/test_main_boots_offline.py",
    "tests/unit/test_worker_triggers.py",
    "tests/unit/composition/test_application_graph_wiring.py",
    "tests/unit/composition/test_application_graph_values.py",
    "tests/unit/test_main_wiring_values.py",
)
_WIRING_EQUIVALENT = "tests/unit/composition/wiring_equivalent_mutants.txt"

TARGETS: tuple[MutationTarget, ...] = (
    *(
        MutationTarget(module, _WIRING_EQUIVALENT, "wiring", _WIRING_EXTRA)
        for module in (
            "src.composition.application_graph",
            "main",
            "src.composition.service_container",
            "src.composition.user_agent_factory",
            "src.composition.slack_adapter_factory",
            "src.composition.telegram_adapter_factory",
        )
    ),
    MutationTarget("src.adapters.firestore_repo", "tests/unit/adapters/fact_store_kit_equivalent_mutants.txt"),
    MutationTarget("src.domain.fact_review_question", "tests/unit/domain/fact_review_question_equivalent_mutants.txt"),
    MutationTarget("src.services.fact_answer_service", "tests/unit/services/fact_answer_service_equivalent_mutants.txt"),
    MutationTarget("src.services.fact_review_service", "tests/unit/services/fact_review_service_equivalent_mutants.txt"),
    MutationTarget("src.agents.consolidation_agent", "tests/unit/agents/consolidation_agent_equivalent_mutants.txt"),
)


def matrix(module: Optional[str] = None) -> Dict[str, List[Dict[str, str]]]:
    targets = [t for t in TARGETS if not module or t.module == module]
    return {
        "include": [
            {"module": t.module, "slug": t.slug, "tests": " ".join(t.tests), "equivalent": t.equivalent or "", "operators": t.operators}
            for t in targets
        ]
    }


MISSING = "missing (no report: baseline failed, timed out or not run)"


def summarize(reports_dir: Path, expected: Optional[List[str]] = None) -> tuple[str, Dict[str, Any]]:
    reports = {}
    for p in sorted(reports_dir.rglob("*.json")):
        r = json.loads(p.read_text(encoding="utf-8"))
        reports[r["module"]] = r
    modules = sorted(set(reports) | set(expected or []))
    lines = ["# Mutation survivors", "", "| module | killed | survived | equivalent | error | status |", "|---|---|---|---|---|---|"]
    findings: List[Dict[str, Any]] = []
    summary: List[Dict[str, Any]] = []
    detail: List[str] = []
    for module in modules:
        r = reports.get(module)
        if r is None:
            lines.append(f"| {module} | {MISSING} |")
            summary.append({"module": module, "status": "missing"})
            continue
        c = r["counts"]
        done = len(r["outcomes"])
        partial = r.get("complete", True) is False
        status = f"partial ({done} of {r.get('total', '?')} mutants)" if partial else "complete"
        lines.append(f"| {module} | {c['killed']} | {c['survived']} | {c['equivalent']} | {c['error']} | {status} |")
        summary.append({"module": module, "status": "partial" if partial else "complete", "counts": c, "done": done, "total": r.get("total")})
        bad = [o for o in r["outcomes"] if o["status"] in ("survived", "error")]
        if not bad:
            detail += ["", f"{module}: clean" + (" so far" if partial else "")]
            continue
        detail += ["", f"## {module}", ""]
        for o in bad:
            line = f"- [{o['status']}] L{o['lineno']} {o['operator']}: {o['description']} (key {o['key']})"
            detail.append(line + (f" - {o['detail']}" if o["status"] == "error" and o.get("detail") else ""))
            findings.append({"module": module, **o})
    combined = {"modules": summary, "findings": findings}
    return "\n".join(lines + detail) + "\n", combined


ANNOTATION_LIMIT = 6000  # characters of one escaped annotation message
MAX_SURVIVOR_ANNOTATIONS = 9
DESCRIPTION_LIMIT = 160


def _escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _module_line(row: Dict[str, Any]) -> str:
    if row["status"] == "missing":
        return f"{row['module']}: missing"
    c = row["counts"]
    status = f"partial ({row['done']} of {row.get('total', '?')})" if row["status"] == "partial" else "complete"
    return f"{row['module']}: {c['killed']}/{c['survived']}/{c['equivalent']}/{c['error']}, {status}"


def _finding_line(f: Dict[str, Any]) -> str:
    description = " ".join(str(f.get("description", "")).split())[:DESCRIPTION_LIMIT]
    return f"{f['module']} L{f['lineno']} {f['operator']} key={f['key']} {f['status']}: {description}"


def annotations(combined: Dict[str, Any]) -> List[str]:
    """GitHub workflow commands for the verdict of a --summarize run (the weekly session reads annotations, not artifacts)."""
    out = ["::notice title=Mutation summary::" + "%0A".join(_escape(_module_line(r)) for r in combined["modules"])]
    lines = [_escape(_finding_line(f)) for f in combined["findings"]]
    chunks: List[List[str]] = []
    size = 0
    index = 0
    while index < len(lines):
        last = len(chunks) == MAX_SURVIVOR_ANNOTATIONS - 1
        limit = ANNOTATION_LIMIT - (80 if last else 0)  # room for the overflow line
        chunk: List[str] = []
        size = 0
        while index < len(lines):
            add = len(lines[index]) + (3 if chunk else 0)
            if chunk and size + add > limit:
                break
            chunk.append(lines[index])
            size += add
            index += 1
        chunks.append(chunk)
        if last:
            break
    if index < len(lines):
        chunks[-1].append(_escape(f"… {len(lines) - index} more: see the survivors artifact"))
    for i, chunk in enumerate(chunks, 1):
        out.append(f"::warning title=Mutation survivors ({i} of {len(chunks)})::" + "%0A".join(chunk))
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m tests.support.mutation_targets",
        usage="%(prog)s (--matrix [--module X] | --summarize DIR [--module X] --out-md PATH --out-json PATH | --annotations JSON | --tests-for MODULE)",
    )
    ap.add_argument("--matrix", action="store_true", help="print the GitHub matrix JSON")
    ap.add_argument("--module", default="", help="restrict --matrix to one module")
    ap.add_argument("--summarize", metavar="DIR", help="summarize every *.json report under DIR")
    ap.add_argument("--tests-for", metavar="MODULE", help="print the computed test list of MODULE, space-separated")
    ap.add_argument("--annotations", metavar="JSON_PATH", help="print GitHub annotations for the --out-json file of --summarize")
    ap.add_argument("--out-md")
    ap.add_argument("--out-json")
    args = ap.parse_args(argv)
    if args.tests_for:
        target = next((t for t in TARGETS if t.module == args.tests_for), None)
        found = target.tests if target else tests_for(args.tests_for)
        if not found:
            print(f"no unit test reaches {args.tests_for}", file=sys.stderr)
            return 2
        print(" ".join(found))
        return 0
    if args.matrix:
        data = matrix(args.module)
        if args.module and not data["include"]:
            print(f"unknown module {args.module!r}", file=sys.stderr)
            return 2
        print(json.dumps(data))
        return 0
    if args.summarize:
        if not (args.out_md and args.out_json):
            ap.error("--summarize needs --out-md and --out-json")
        expected = [row["module"] for row in matrix(args.module)["include"]]
        md, combined = summarize(Path(args.summarize), expected)
        Path(args.out_md).write_text(md, encoding="utf-8")
        Path(args.out_json).write_text(json.dumps(combined, indent=2), encoding="utf-8")
        return 0
    if args.annotations:
        for line in annotations(json.loads(Path(args.annotations).read_text(encoding="utf-8"))):
            print(line)
        return 0
    ap.error("one of --matrix, --summarize, --annotations or --tests-for is required")
    return 2


if __name__ == "__main__":
    sys.exit(main())
