"""Every `/worker` task type something sends, and every branch `WorkerHandler._dispatch` has (P9 design §5c).

Triggers come from two places the deploy owns: the Cloud Scheduler jobs `scripts/infrastructure/cloudbuild_scheduler_jobs.sh`
creates, and the enqueue sites in `src/` and the root `*_main.py` (an `enqueue_worker_task(...)` call, or a dict literal
keyed `"task_type"`). The extractor reads the sources and fails on a form it does not understand, so a new way to
enqueue cannot hide from the check.
"""
import ast
import functools
import importlib
import json
import shlex
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Set

ROOT = Path(__file__).resolve().parents[2]
SCHEDULER_SCRIPT = ROOT / "scripts" / "infrastructure" / "cloudbuild_scheduler_jobs.sh"
LEGACY_SCHEDULER_SCRIPT = "setup-daily-email-review-scheduler.sh"
WORKER_HANDLER = ROOT / "src" / "handlers" / "worker_handler.py"

# Sent to another service's /worker; the main service has no branch for it.
OTHER_SERVICE: Dict[str, str] = {"drive_sync": "indexer_main.py /worker, delivered by the drive queue"}
# A branch nothing sends. Empty: every handler has a trigger.
MANUAL_ONLY: Dict[str, str] = {}
# Names that stand for a task type: the module attribute they are read from, checked against its runtime value.
CONSTANTS: Dict[str, str] = {"DRIVE_SYNC_TASK": "src.domain.drive_task_keys"}
# The `task_type is None` branch of `_dispatch`: a queued Slack event has no task type by design.
SLACK_EVENT_BRANCH = "<queued slack event>"


class ExtractorError(AssertionError):
    """A form of enqueue site or dispatch comparison the extractor does not understand."""


class Site(NamedTuple):
    task_type: str
    where: str


# ----------------------------------------------------------------------------- the scheduler

class SchedulerJob(NamedTuple):
    name: str
    update: Dict[str, str]
    create: Dict[str, str]


def _logical_lines(text: str) -> List[str]:
    return text.replace("\\\n", " ").splitlines()


def _flags(tokens: List[str]) -> Dict[str, str]:
    flags = {}
    for token in tokens:
        if token.startswith("--") and "=" in token:
            key, value = token[2:].split("=", 1)
            flags[key] = value
    return flags


def _commands(text: str) -> List[List[str]]:
    """The shell commands of the script as token lists: `&&` and `||` end a command, so do line ends."""
    commands: List[List[str]] = []
    for line in _logical_lines(text):
        current: List[str] = []
        for token in shlex.split(line, comments=True):
            if token in ("&&", "||"):
                commands.append(current)
                current = []
            else:
                current.append(token)
        commands.append(current)
    return [c for c in commands if c]


def scheduler_jobs(script: Path = SCHEDULER_SCRIPT) -> Dict[str, SchedulerJob]:
    """Each `gcloud scheduler jobs update|create http NAME …` of the script, by job name."""
    found: Dict[str, Dict[str, Dict[str, str]]] = {}
    for tokens in _commands(script.read_text()):
        if tokens[:3] != ["gcloud", "scheduler", "jobs"] or tokens[3] == "describe":
            continue
        if tokens[3] not in ("update", "create") or tokens[4] != "http":
            raise ExtractorError(f"unrecognised scheduler command: {' '.join(tokens[:6])}")
        found.setdefault(tokens[5], {})[tokens[3]] = _flags(tokens[6:])
    return {name: SchedulerJob(name, kinds.get("update", {}), kinds.get("create", {})) for name, kinds in found.items()}


def scheduler_task_types(jobs: Optional[Dict[str, SchedulerJob]] = None) -> Dict[str, dict]:
    """`task_type -> literal message body` of every job that posts to `/worker`."""
    types: Dict[str, dict] = {}
    for job in (jobs or scheduler_jobs()).values():
        if not job.create.get("uri", "").endswith("/worker"):
            continue
        body = json.loads(job.create["message-body"])
        types[body["task_type"]] = body
    return types


# ----------------------------------------------------------------------------- the enqueue sites

def _source_files() -> List[Path]:
    return sorted((ROOT / "src").rglob("*.py")) + sorted(ROOT.glob("*_main.py"))


def _function_params(func: Optional[ast.AST]) -> Set[str]:
    if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return set()
    args = func.args
    return {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs} | {a.arg for a in (args.vararg, args.kwarg) if a}


def _constant_value(name: str) -> str:
    return getattr(importlib.import_module(CONSTANTS[name]), name)


def _resolve(node: ast.AST, params: Set[str], where: str) -> Optional[str]:
    """The task type a node stands for; None for a parameter the enclosing function forwards."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in params:
            return None
        if node.id in CONSTANTS:
            return _constant_value(node.id)
    raise ExtractorError(f"{where}: cannot resolve the task type {ast.unparse(node)!r}")


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.stack: List[ast.AST] = []
        self.sites: List[Site] = []

    def _enclosing_params(self) -> Set[str]:
        params: Set[str] = set()
        for node in self.stack:
            params |= _function_params(node)
        return params

    def _record(self, node: ast.AST, value_node: ast.AST) -> None:
        where = f"{self.path.relative_to(ROOT)}:{node.lineno}"
        value = _resolve(value_node, self._enclosing_params(), where)
        if value is not None:
            self.sites.append(Site(value, where))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == "enqueue_worker_task":
            keyword = [k.value for k in node.keywords if k.arg == "task_type"]
            if keyword:
                self._record(node, keyword[0])
            elif node.args:
                self._record(node, node.args[0])
            else:
                raise ExtractorError(f"{self.path.relative_to(ROOT)}:{node.lineno}: enqueue_worker_task without a task type")
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == "task_type":
                self._record(node, value)
        self.generic_visit(node)


def enqueue_sites(files: Optional[List[Path]] = None) -> List[Site]:
    if files is None:
        return list(_all_sites())
    sites: List[Site] = []
    for path in files:
        visitor = _Visitor(path)
        visitor.visit(ast.parse(path.read_text()))
        sites.extend(visitor.sites)
    return sites


@functools.lru_cache(maxsize=None)
def _all_sites() -> tuple:
    sites: List[Site] = []
    for path in _source_files():
        visitor = _Visitor(path)
        visitor.visit(ast.parse(path.read_text()))
        sites.extend(visitor.sites)
    return tuple(sites)


def sites_in_source(source: str, name: str = "inline.py") -> List[Site]:
    """The extractor over one source string (its self-tests)."""
    visitor = _Visitor(ROOT / name)
    visitor.visit(ast.parse(source))
    return visitor.sites


def trigger_types() -> Set[str]:
    return set(scheduler_task_types()) | {site.task_type for site in enqueue_sites()}


# ----------------------------------------------------------------------------- the dispatch branches

def _branch_name(test: ast.AST) -> str:
    # task_type == "literal"
    if (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) and test.left.id == "task_type"
            and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)
            and isinstance(test.comparators[0], ast.Constant) and isinstance(test.comparators[0].value, str)):
        return test.comparators[0].value
    # task_type is None and "event" in payload and "session_id" in payload
    if (isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And) and isinstance(test.values[0], ast.Compare)
            and isinstance(test.values[0].left, ast.Name) and test.values[0].left.id == "task_type"
            and isinstance(test.values[0].ops[0], ast.Is)
            and isinstance(test.values[0].comparators[0], ast.Constant) and test.values[0].comparators[0].value is None):
        return SLACK_EVENT_BRANCH
    raise ExtractorError(f"worker_handler.py:{test.lineno}: unrecognised dispatch comparison {ast.unparse(test)!r}")


def branches_in_source(source: str) -> Set[str]:
    """The task types of the `if/elif` chain of the first `_dispatch` function in `source`."""
    [dispatch] = [n for n in ast.walk(ast.parse(source))
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_dispatch"]
    names: Set[str] = set()
    chain = [s for s in dispatch.body if isinstance(s, ast.If)]
    if not chain:
        raise ExtractorError("_dispatch has no if/elif chain")
    node: Optional[ast.If] = chain[0]
    while node is not None:
        names.add(_branch_name(node.test))
        node = node.orelse[0] if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If) else None
    return names


def dispatch_branches() -> Set[str]:
    """The task types `WorkerHandler._dispatch` handles (the queued Slack event is not one)."""
    return branches_in_source(WORKER_HANDLER.read_text()) - {SLACK_EVENT_BRANCH}
