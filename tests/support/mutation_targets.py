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
    graph_group: bool = False  # the production-graph tests are added to the computed selection (they reach it through src/)
    extra: tuple[str, ...] = ()  # hand-added tests: one that reaches the module only through src/ or a conftest fixture

    @property
    def slug(self) -> str:
        return self.module.replace(".", "-")

    @property
    def extra_tests(self) -> tuple[str, ...]:
        """Computed at access time: the graph group (wiring targets) plus the hand-added `extra`."""
        return (*(graph_tests() if self.graph_group else ()), *self.extra)

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


def _closure(graph: _ImportGraph, seeds: Set[str]) -> Set[str]:
    reached = {name for name, (_, imports) in graph.items() if imports & seeds}
    changed = True
    while changed:
        changed = False
        for name, (_, imports) in graph.items():
            if name not in reached and imports & reached:
                reached.add(name)
                changed = True
    return reached


def _unit_tests(graph: _ImportGraph, reached: Set[str]) -> tuple[str, ...]:
    found = []
    for name in reached:
        rel = graph[name][0]
        if rel.startswith("tests/unit/") and Path(rel).name.startswith("test_"):
            found.append(rel)
    return tuple(sorted(found))


def tests_for(module: str, root: Path = REPO_ROOT) -> tuple[str, ...]:
    """Every tests/unit/**/test_*.py whose imports reach `module` through modules under tests/ only (never through src/)."""
    graph = _import_graph(root)
    return _unit_tests(graph, _closure(graph, {module}))


# What builds or boots the production graph. A test reaches the wiring modules through these (and then through src/,
# which tests_for never follows), so a test that imports any of them belongs to every wiring target.
# tests.support.worker_triggers is kept for parity with the P9 hand list (an existing test requires its file); it reads
# source from disk, so it cannot kill a wiring mutant.
GRAPH_ROOTS = frozenset({"tests.support.prod_graph", "tests.support.worker_triggers", "main", "src.composition.application_graph"})


def graph_tests(root: Path = REPO_ROOT) -> tuple[str, ...]:
    """Every tests/unit/**/test_*.py that reaches a GRAPH_ROOTS module through imports under tests/."""
    graph = _import_graph(root)
    return _unit_tests(graph, _closure(graph, set(GRAPH_ROOTS)))


_WIRING_EQUIVALENT = "tests/unit/composition/wiring_equivalent_mutants.txt"

_DRIVE_E2E = "tests/unit/agents/test_drive_agent_tool_fields_e2e.py"
_DRIVE_AGENT = "tests/unit/agents/test_drive_agent.py"
_NOTE_E2E = "tests/unit/agents/test_drive_agent_note_tool_fields_e2e.py"
_NOTE_CHAIN = "tests/unit/infrastructure/test_drive_note_chain.py"

TARGETS: tuple[MutationTarget, ...] = (
    *(
        MutationTarget(module, _WIRING_EQUIVALENT, "wiring", graph_group=True)
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
    # P15 part 2 and P16: one equivalent file each, holding a header comment the driver skips. Not None: test_mutation_targets.py counts
    # None as one shared file, so two or more None targets fail it (only the wiring file may be shared). A reviewer rules a mutant
    # equivalent by adding a line there when the weekly run reports a survivor.
    MutationTarget("src.handlers.worker_handler", "tests/unit/handlers/worker_handler_equivalent_mutants.txt",
                   extra=("tests/unit/test_worker_redelivery_route.py",)),  # reaches it only through main
    MutationTarget("src.adapters.firestore_task_claim_store", "tests/unit/adapters/firestore_task_claim_store_equivalent_mutants.txt",
                   extra=("tests/unit/test_worker_redelivery_route.py",)),  # reaches it only through main
    MutationTarget("src.services.reminders_service", "tests/unit/services/reminders_service_equivalent_mutants.txt"),
    MutationTarget("src.adapters.firestore_agent_note_adapter", "tests/unit/adapters/firestore_agent_note_adapter_equivalent_mutants.txt"),
    MutationTarget("src.services.task_setup_service", "tests/unit/services/task_setup_service_equivalent_mutants.txt"),
    MutationTarget("src.services.companion_extraction_service", "tests/unit/services/companion_extraction_service_equivalent_mutants.txt"),
    MutationTarget("src.adapters.firestore_companion_extraction_queue", "tests/unit/adapters/firestore_companion_extraction_queue_equivalent_mutants.txt"),
    MutationTarget("src.adapters.dateutil_recurrence_adapter", "tests/unit/adapters/dateutil_recurrence_adapter_equivalent_mutants.txt"),
    MutationTarget("src.adapters.firestore_account_repo", "tests/unit/adapters/firestore_account_repo_equivalent_mutants.txt"),
    MutationTarget("src.services.session_service", "tests/unit/services/session_service_equivalent_mutants.txt"),
    MutationTarget("src.domain.daily_review_window", "tests/unit/domain/daily_review_window_equivalent_mutants.txt"),
    MutationTarget("src.adapters.firestore_ephemeral_store", "tests/unit/adapters/firestore_ephemeral_store_equivalent_mutants.txt"),
    MutationTarget("src.adapters.firestore_dedup_store", "tests/unit/adapters/firestore_dedup_store_equivalent_mutants.txt"),
    MutationTarget("src.adapters.firestore_quota_service", "tests/unit/adapters/firestore_quota_service_equivalent_mutants.txt"),
    MutationTarget("src.adapters.firestore_long_turn_registry", "tests/unit/adapters/firestore_long_turn_registry_equivalent_mutants.txt"),
    MutationTarget("src.adapters.firestore_task_search_index", "tests/unit/adapters/firestore_task_search_index_equivalent_mutants.txt"),
    MutationTarget("src.adapters.firestore_task_config_repository", "tests/unit/adapters/firestore_task_config_repository_equivalent_mutants.txt"),
    MutationTarget("src.adapters.firestore_channel_binding_adapter", "tests/unit/adapters/firestore_channel_binding_adapter_equivalent_mutants.txt"),
    MutationTarget("src.adapters.firestore_skill_repository", "tests/unit/adapters/firestore_skill_repository_equivalent_mutants.txt"),
    MutationTarget("src.adapters.firestore_memory_fact_draft_repository", "tests/unit/adapters/firestore_memory_fact_draft_repository_equivalent_mutants.txt"),
    MutationTarget("src.adapters.firestore_companion_memory_repository", "tests/unit/adapters/firestore_companion_memory_repository_equivalent_mutants.txt"),
    MutationTarget("src.adapters.firestore_companion_cache_repository", "tests/unit/adapters/firestore_companion_cache_repository_equivalent_mutants.txt"),
    MutationTarget("src.adapters.firestore_email_job_repo", "tests/unit/adapters/firestore_email_job_repo_equivalent_mutants.txt"),
    MutationTarget("src.adapters.firestore_email_exclusions_adapter", "tests/unit/adapters/firestore_email_exclusions_adapter_equivalent_mutants.txt"),
    MutationTarget("src.adapters.firestore_short_link_repository", "tests/unit/adapters/firestore_short_link_repository_equivalent_mutants.txt"),
    MutationTarget("src.adapters.gcp_task_queue", "tests/unit/adapters/gcp_task_queue_equivalent_mutants.txt"),
    MutationTarget("src.adapters.gcs_workspace_adapter", "tests/unit/adapters/gcs_workspace_adapter_equivalent_mutants.txt"),
    MutationTarget("src.adapters.gcs_file_storage_adapter", "tests/unit/adapters/gcs_file_storage_adapter_equivalent_mutants.txt"),
    MutationTarget("src.adapters.gcs_media_adapter", "tests/unit/adapters/gcs_media_adapter_equivalent_mutants.txt"),
    MutationTarget("src.adapters.firestore_session_store", "tests/unit/adapters/firestore_session_store_equivalent_mutants.txt"),
    MutationTarget("src.adapters.firestore_consolidation_queue", "tests/unit/adapters/firestore_consolidation_queue_equivalent_mutants.txt"),
    MutationTarget("src.adapters.firestore_indexed_email_repo", "tests/unit/adapters/firestore_indexed_email_repo_equivalent_mutants.txt"),
    MutationTarget("src.adapters.firestore_user_repo", "tests/unit/adapters/firestore_user_repo_equivalent_mutants.txt"),
    MutationTarget("src.adapters.firestore_notification_state_adapter", "tests/unit/adapters/firestore_notification_state_adapter_equivalent_mutants.txt"),
    # P13 (service seams): one equivalent file each. video_generation_delivery also takes the seam file that exercises it through src/ (reviewer note).
    MutationTarget("src.services.user_notification_service", "tests/unit/services/user_notification_service_equivalent_mutants.txt"),
    MutationTarget("src.services.task_dispatch_service", "tests/unit/services/task_dispatch_service_equivalent_mutants.txt"),
    MutationTarget("src.services.document_delivery_service", "tests/unit/services/document_delivery_service_equivalent_mutants.txt"),
    MutationTarget("src.services.deep_research_delivery", "tests/unit/services/deep_research_delivery_equivalent_mutants.txt"),
    MutationTarget("src.services.video_generation_delivery", "tests/unit/services/video_generation_delivery_equivalent_mutants.txt",
                   extra=("tests/unit/handlers/test_seam_worker_notices.py",)),
    MutationTarget("src.infrastructure.agent_coordinator", "tests/unit/infrastructure/agent_coordinator_equivalent_mutants.txt"),
    MutationTarget("src.handlers.agent_worker_handler", "tests/unit/handlers/agent_worker_handler_equivalent_mutants.txt"),
    MutationTarget("src.handlers.conversation_handler", "tests/unit/handlers/conversation_handler_equivalent_mutants.txt"),
    MutationTarget("src.services.prompt_builder", "tests/unit/services/prompt_builder_equivalent_mutants.txt"),
    MutationTarget("src.services.search_enrichment_service", "tests/unit/services/search_enrichment_service_equivalent_mutants.txt"),
    MutationTarget("src.agents.core.router_agent", "tests/unit/agents/core/router_agent_equivalent_mutants.txt"),
    # P14 (scripted-LLM tool fields): one equivalent file each, next to the e2e file that tests the module.
    MutationTarget("src.agents.notes_agent", "tests/unit/agents/notes_agent_equivalent_mutants.txt"),
    MutationTarget("src.agents.tasks_agent", "tests/unit/agents/tasks_agent_equivalent_mutants.txt"),
    MutationTarget("src.agents.file_management_agent", "tests/unit/agents/file_management_agent_equivalent_mutants.txt"),
    MutationTarget("src.adapters.fact_management_adapter", "tests/unit/agents/fact_management_adapter_equivalent_mutants.txt"),
    MutationTarget("src.infrastructure.fact_review_scope", "tests/unit/services/fact_review_scope_equivalent_mutants.txt"),
    MutationTarget("src.infrastructure.memory_fact_tools", "tests/unit/agents/core/memory_fact_tools_equivalent_mutants.txt"),
    MutationTarget("src.infrastructure.skill_tools", "tests/unit/agents/core/skill_tools_equivalent_mutants.txt"),
    MutationTarget("src.infrastructure.workspace_tools", "tests/unit/agents/core/workspace_tools_equivalent_mutants.txt"),
    # The drive specialist (DRIVE_SPECIALIST_RFC): one equivalent file each, holding a header comment the driver skips. The
    # e2e card and the agent tests reach the tools, the answer and the commission parser only through src/ (DriveAgent).
    MutationTarget("src.infrastructure.drive_operations", "tests/unit/infrastructure/drive_operations_equivalent_mutants.txt"),
    MutationTarget("src.infrastructure.drive_tools", "tests/unit/infrastructure/drive_tools_equivalent_mutants.txt",
                   extra=(_DRIVE_E2E, _DRIVE_AGENT, "tests/unit/infrastructure/test_drive_specialist_chain.py")),
    MutationTarget("src.agents.drive_agent", "tests/unit/agents/drive_agent_equivalent_mutants.txt"),
    MutationTarget("src.domain.drive_answer", "tests/unit/domain/drive_answer_equivalent_mutants.txt",
                   extra=(_DRIVE_E2E, _DRIVE_AGENT)),
    MutationTarget("src.domain.drive_pass_through", "tests/unit/domain/drive_pass_through_equivalent_mutants.txt"),
    MutationTarget("src.domain.drive_file_contents", "tests/unit/domain/drive_file_contents_equivalent_mutants.txt",
                   extra=(_DRIVE_E2E, _DRIVE_AGENT)),
    # Drive file notes (DRIVE_FILE_NOTES design §5): header-only equivalent files; the note e2e and chain tests reach them through src/.
    MutationTarget("src.services.drive_note_service", "tests/unit/services/drive_note_service_equivalent_mutants.txt",
                   extra=(_NOTE_E2E, _NOTE_CHAIN)),
    MutationTarget("src.adapters.firestore_drive_note_repo", "tests/unit/adapters/firestore_drive_note_repo_equivalent_mutants.txt",
                   extra=(_NOTE_E2E, _NOTE_CHAIN)),
    MutationTarget("src.infrastructure.drive_note_tools", "tests/unit/infrastructure/drive_note_tools_equivalent_mutants.txt",
                   extra=(_NOTE_E2E, _NOTE_CHAIN)),
    MutationTarget("src.domain.drive_note_change", "tests/unit/domain/drive_note_change_equivalent_mutants.txt",
                   extra=(_NOTE_E2E, _NOTE_CHAIN)),
    MutationTarget("src.domain.drive_note_tool_texts", "tests/unit/domain/drive_note_tool_texts_equivalent_mutants.txt",
                   extra=(_NOTE_E2E, _NOTE_CHAIN)),
    MutationTarget("src.domain.drive_note_view", "tests/unit/domain/drive_note_view_equivalent_mutants.txt",
                   extra=(_NOTE_E2E, _NOTE_CHAIN)),
    # Answer from content (USER_DRIVE_INDEXING_RFC §28): header-only equivalent files.
    MutationTarget("src.domain.drive_excerpts", "tests/unit/domain/drive_excerpts_equivalent_mutants.txt"),
    MutationTarget("src.domain.drive_excerpt_request", "tests/unit/domain/drive_excerpt_request_equivalent_mutants.txt"),
    MutationTarget("src.domain.drive_excerpt_render", "tests/unit/domain/drive_excerpt_render_equivalent_mutants.txt"),
    MutationTarget("src.domain.drive_excerpt_ledger", "tests/unit/domain/drive_excerpt_ledger_equivalent_mutants.txt"),
    MutationTarget("src.domain.drive_content_gate", "tests/unit/domain/drive_content_gate_equivalent_mutants.txt"),
    MutationTarget("src.domain.drive_excerpt_context", "tests/unit/domain/drive_excerpt_context_equivalent_mutants.txt"),
    MutationTarget("src.domain.drive_open_bound", "tests/unit/domain/drive_open_bound_equivalent_mutants.txt"),
    MutationTarget("src.domain.drive_excerpt_page", "tests/unit/domain/drive_excerpt_page_equivalent_mutants.txt"),
    # The fan-out's cap alert (USER_DRIVE_INDEXING_RFC §5.4, §12.1): header-only equivalent file; the scheduler test
    # reaches the module only through src/ (DriveSyncSchedulerService).
    MutationTarget("src.domain.drive_cap_alert", "tests/unit/domain/drive_cap_alert_equivalent_mutants.txt",
                   extra=("tests/unit/services/test_drive_sync_scheduler_cap_alert.py",)),
    # Secrets at rest (SECRETS_AT_REST_RFC, slice S1): the KMS cipher, the keyed digest, the fail-closed setup and the MCP
    # store over them; header-only equivalent files.
    MutationTarget("src.adapters.gcp_kms_cipher_adapter", "tests/unit/adapters/gcp_kms_cipher_adapter_equivalent_mutants.txt"),
    MutationTarget("src.domain.secret_digest", "tests/unit/domain/secret_digest_equivalent_mutants.txt"),
    MutationTarget("src.composition.cipher_setup", "tests/unit/composition/cipher_setup_equivalent_mutants.txt"),
    MutationTarget("src.adapters.firestore_mcp_client_repository",
                   "tests/unit/adapters/firestore_mcp_client_repository_equivalent_mutants.txt"),
    # Secrets at rest, slice S3: the credential store; header-only equivalent file.
    MutationTarget("src.adapters.firestore_credential_store",
                   "tests/unit/adapters/firestore_credential_store_equivalent_mutants.txt"),
    # Secrets at rest, slice S4: the voice-ticket store keys; header-only equivalent file.
    MutationTarget("src.domain.voice_ticket_keys", "tests/unit/domain/voice_ticket_keys_equivalent_mutants.txt"),
    # Secrets at rest, slice S5: invite codes (normalisation, grouping, hint); header-only equivalent file.
    MutationTarget("src.domain.invite_code", "tests/unit/domain/invite_code_equivalent_mutants.txt"),
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


ANNOTATION_LIMIT = 4000  # the runner truncates a message at 4096 characters after decoding, silently
MAX_SURVIVOR_ANNOTATIONS = 9
DESCRIPTION_LIMIT = 160


def _escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _module_line(row: Dict[str, Any]) -> str:
    if row["status"] == "missing":
        return f"{row['module']}: missing"
    c = row["counts"]
    total = row.get("total")
    status = f"partial ({row['done']} of {'?' if total is None else total})" if row["status"] == "partial" else "complete"
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
