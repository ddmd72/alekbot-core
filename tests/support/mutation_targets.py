"""Registry of the modules whose tests are held to the semantic-mutation bar (one entry per done package).

The weekly workflow (.github/workflows/mutation.yml) runs the driver once per entry and keeps the reports as artifacts.
A new `*_equivalent_mutants.txt` under tests/unit must be registered here (a unit test enforces it).

    python -m tests.support.mutation_targets --matrix [--module X]
    python -m tests.support.mutation_targets --summarize DIR --out-md PATH --out-json PATH
"""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class MutationTarget:
    module: str
    tests: tuple[str, ...]
    equivalent: Optional[str]
    operators: str = "default"

    @property
    def slug(self) -> str:
        return self.module.replace(".", "-")


_WIRING_TESTS = (
    "tests/unit/test_main_boots_offline.py",
    "tests/unit/test_worker_triggers.py",
    "tests/unit/composition/test_application_graph_wiring.py",
    "tests/unit/composition/test_application_graph_values.py",
    "tests/unit/test_main_wiring_values.py",
)
_WIRING_EQUIVALENT = "tests/unit/composition/wiring_equivalent_mutants.txt"

TARGETS: tuple[MutationTarget, ...] = (
    *(
        MutationTarget(module, _WIRING_TESTS, _WIRING_EQUIVALENT, "wiring")
        for module in (
            "src.composition.application_graph",
            "main",
            "src.composition.service_container",
            "src.composition.user_agent_factory",
            "src.composition.slack_adapter_factory",
            "src.composition.telegram_adapter_factory",
        )
    ),
    MutationTarget(
        "src.adapters.firestore_repo",
        (
            "tests/unit/adapters/test_fact_store_roundtrip.py",
            "tests/unit/adapters/test_fact_repository_kit.py",
            "tests/unit/adapters/test_fact_repository_versions_kit.py",
            "tests/unit/adapters/test_fact_management_kit.py",
            "tests/unit/adapters/test_fact_write_kit.py",
        ),
        "tests/unit/adapters/fact_store_kit_equivalent_mutants.txt",
    ),
    MutationTarget(
        "src.domain.fact_review_question",
        (
            "tests/unit/domain/test_fact_review_question.py",
            "tests/unit/domain/test_fact_review_question_sets.py",
        ),
        "tests/unit/domain/fact_review_question_equivalent_mutants.txt",
    ),
    MutationTarget(
        "src.services.fact_answer_service",
        (
            "tests/unit/services/test_fact_answer_service.py",
            "tests/unit/services/test_fact_answer_reply.py",
            "tests/unit/services/test_fact_answer_queue_and_counters.py",
            "tests/unit/services/test_fact_question_choice.py",
        ),
        "tests/unit/services/fact_answer_service_equivalent_mutants.txt",
    ),
    MutationTarget(
        "src.services.fact_review_service",
        (
            "tests/unit/services/test_fact_review_service_run.py",
            "tests/unit/services/test_fact_review_selection.py",
            "tests/unit/services/test_fact_review_questions.py",
        ),
        "tests/unit/services/fact_review_service_equivalent_mutants.txt",
    ),
    MutationTarget(
        "src.agents.consolidation_agent",
        (
            "tests/unit/test_consolidation_agent.py",
            "tests/unit/agents/test_consolidation_agent.py",
            "tests/unit/agents/test_consolidation_review_pass.py",
            "tests/unit/agents/test_consolidation_review_pass_stats.py",
            "tests/unit/agents/test_consolidation_review_report.py",
            "tests/unit/agents/test_consolidation_loop_alerts.py",
            "tests/unit/agents/test_consolidation_pass_lines.py",
            "tests/unit/agents/test_consolidation_error_turns.py",
            "tests/unit/agents/test_consolidation_versions_offline.py",
            "tests/unit/agents/test_consolidation_write_context.py",
            "tests/unit/agents/test_consolidation_update_fact_account.py",
            "tests/unit/agents/test_consolidation_context_forwarding.py",
        ),
        "tests/unit/agents/consolidation_agent_equivalent_mutants.txt",
    ),
)


def matrix(module: Optional[str] = None) -> Dict[str, List[Dict[str, str]]]:
    targets = [t for t in TARGETS if not module or t.module == module]
    return {
        "include": [
            {"module": t.module, "slug": t.slug, "tests": " ".join(t.tests), "equivalent": t.equivalent or "", "operators": t.operators}
            for t in targets
        ]
    }


def summarize(reports_dir: Path) -> tuple[str, Dict[str, Any]]:
    reports = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(reports_dir.rglob("*.json"))]
    reports.sort(key=lambda r: r["module"])
    lines = ["# Mutation survivors", "", "| module | killed | survived | equivalent | error |", "|---|---|---|---|---|"]
    for r in reports:
        c = r["counts"]
        lines.append(f"| {r['module']} | {c['killed']} | {c['survived']} | {c['equivalent']} | {c['error']} |")
    findings: List[Dict[str, Any]] = []
    for r in reports:
        bad = [o for o in r["outcomes"] if o["status"] in ("survived", "error")]
        if not bad:
            lines += ["", f"{r['module']}: clean"]
            continue
        lines += ["", f"## {r['module']}", ""]
        for o in bad:
            line = f"- [{o['status']}] L{o['lineno']} {o['operator']}: {o['description']} (key {o['key']})"
            lines.append(line + (f" - {o['detail']}" if o["status"] == "error" and o.get("detail") else ""))
            findings.append({"module": r["module"], **o})
    combined = {"modules": [{"module": r["module"], "counts": r["counts"]} for r in reports], "findings": findings}
    return "\n".join(lines) + "\n", combined


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tests.support.mutation_targets")
    ap.add_argument("--matrix", action="store_true", help="print the GitHub matrix JSON")
    ap.add_argument("--module", default="", help="restrict --matrix to one module")
    ap.add_argument("--summarize", metavar="DIR", help="summarize every *.json report under DIR")
    ap.add_argument("--out-md")
    ap.add_argument("--out-json")
    args = ap.parse_args(argv)
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
        md, combined = summarize(Path(args.summarize))
        Path(args.out_md).write_text(md, encoding="utf-8")
        Path(args.out_json).write_text(json.dumps(combined, indent=2), encoding="utf-8")
        return 0
    ap.error("one of --matrix or --summarize is required")
    return 2


if __name__ == "__main__":
    sys.exit(main())
