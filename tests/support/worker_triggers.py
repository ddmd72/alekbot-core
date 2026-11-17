"""Every `/worker` task type something sends, and every task type `WorkerHandler` registers (P9 design §5c).

Triggers come from two places the deploy owns: the Cloud Scheduler jobs `scripts/infrastructure/cloudbuild_scheduler_jobs.sh`
creates, and the enqueue sites in `src/` and the root `*_main.py` (an `enqueue_worker_task(...)` call, or a dict literal
keyed `"task_type"`). The extractor reads the sources and fails on a form it does not understand: a task type it cannot
resolve, `dict(task_type=...)` and `payload["task_type"] = ...`. A payload built any other way (`update(...)`,
`setdefault(...)`) is not seen.
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

# Sent to another service's /worker; the main service registers no task for it.
OTHER_SERVICE: Dict[str, str] = {"drive_sync": "indexer_main.py /worker, delivered by the drive queue"}
# A registered task type nothing sends. Empty: every task has a trigger.
MANUAL_ONLY: Dict[str, str] = {}
# Names that stand for a task type: the module attribute they are read from, checked against its runtime value.
CONSTANTS: Dict[str, str] = {"DRIVE_SYNC_TASK": "src.domain.drive_task_keys", "FACT_REVIEW_TASK": "src.domain.fact_review_task_keys",
                            "INTERACTIVE_CHOICE_TASK": "src.domain.interactive_choice"}
# The registry entry `_dispatch` runs for a payload without a task type: a queued Slack event has none by design.
SLACK_EVENT_BRANCH = "<queued slack event>"


class ExtractorError(AssertionError):
    """A form of enqueue site or task registry entry the extractor does not understand."""


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
        if name == "dict" and any(k.arg == "task_type" for k in node.keywords):
            raise ExtractorError(f"{self.path.relative_to(ROOT)}:{node.lineno}: dict(task_type=...) builds a payload")
        if name == "enqueue_worker_task":
            keyword = [k.value for k in node.keywords if k.arg == "task_type"]
            if keyword:
                self._record(node, keyword[0])
            elif node.args:
                self._record(node, node.args[0])
            else:
                raise ExtractorError(f"{self.path.relative_to(ROOT)}:{node.lineno}: enqueue_worker_task without a task type")
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.ctx, ast.Store) and isinstance(node.slice, ast.Constant) and node.slice.value == "task_type":
            raise ExtractorError(f"{self.path.relative_to(ROOT)}:{node.lineno}: a payload gets its task_type by item assignment")
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == "task_type":
                self._record(node, value)
        self.generic_visit(node)


def enqueue_sites() -> List[Site]:
    return list(_all_sites())


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


# ----------------------------------------------------------------------------- the dispatch registry

# The name of the registry key of the queued Slack event in worker_handler.py; the extractor maps it to SLACK_EVENT_BRANCH.
SLACK_EVENT_KEY = "_QUEUED_SLACK_EVENT"


class Entry(NamedTuple):
    method: str                 # the WorkerHandler method the task runs
    payload_at: Optional[int]   # the payload's positional index in its call; None when the method gets no payload


def _registry_key(node: ast.AST) -> str:
    # "literal" | KNOWN_CONSTANT | _QUEUED_SLACK_EVENT
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in CONSTANTS:
        return _constant_value(node.id)
    if isinstance(node, ast.Name) and node.id == SLACK_EVENT_KEY:
        return SLACK_EVENT_BRANCH
    raise ExtractorError(f"worker_handler.py:{node.lineno}: unrecognised registry key {ast.unparse(node)!r}")


def _self_method(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
        return node.attr
    return None


def _wrapper_payload_at(tree: ast.Module, name: str) -> Optional[int]:
    """Where the module-level wrapper `name(method)` puts the task's payload when it calls `method`, read from its body:
    `async def task(payload, headers): return await method(payload)` -> 0; `... method()` -> None."""
    found = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    if len(found) != 1 or len(found[0].args.args) != 1:
        raise ExtractorError(f"worker_handler.py: the registry wrapper {name} is not one module function of one parameter")
    method = found[0].args.args[0].arg
    inner = [n for n in found[0].body if isinstance(n, ast.AsyncFunctionDef)]
    if len(inner) != 1:
        raise ExtractorError(f"worker_handler.py: the registry wrapper {name} defines {len(inner)} async tasks")
    params = [a.arg for a in inner[0].args.args]
    calls = [n for n in ast.walk(inner[0]) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == method]
    if len(calls) != 1 or calls[0].keywords or not all(isinstance(a, ast.Name) and a.id in params for a in calls[0].args):
        raise ExtractorError(f"worker_handler.py: the registry wrapper {name} does not call {method} once with its own parameters")
    passed = [a.id for a in calls[0].args if isinstance(a, ast.Name)]
    return passed.index(params[0]) if params and params[0] in passed else None


def _registry_value(node: ast.AST, tree: ast.Module) -> Entry:
    # wrapper(self.<method>) | self.<method> (a method of the WorkerTask shape itself: payload first)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and len(node.args) == 1 and not node.keywords
            and _self_method(node.args[0])):
        return Entry(str(_self_method(node.args[0])), _wrapper_payload_at(tree, node.func.id))
    if _self_method(node):
        return Entry(str(_self_method(node)), 0)
    raise ExtractorError(f"worker_handler.py:{node.lineno}: unrecognised registry value {ast.unparse(node)!r}")


def registry_in_source(source: str) -> Dict[str, Entry]:
    """`branch -> Entry` of the one `self._tasks = {...}` literal in the `__init__` of `source`."""
    tree = ast.parse(source)
    inits = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "__init__"]
    literals = [n.value for init in inits for n in ast.walk(init) if isinstance(n, (ast.Assign, ast.AnnAssign))
                and ast.unparse(n.targets[0] if isinstance(n, ast.Assign) else n.target) == "self._tasks"]
    if len(literals) != 1 or not isinstance(literals[0], ast.Dict):
        raise ExtractorError("worker_handler.py: no single `self._tasks = {...}` literal in __init__")
    entries: Dict[str, Entry] = {}
    for key, value in zip(literals[0].keys, literals[0].values):
        if key is None:
            raise ExtractorError(f"worker_handler.py:{value.lineno}: a ** spread in the task registry")
        name = _registry_key(key)
        if name in entries:
            raise ExtractorError(f"worker_handler.py:{key.lineno}: the task type {name!r} is registered twice")
        entries[name] = _registry_value(value, tree)
    return entries


def branches_in_source(source: str) -> Set[str]:
    """The task types of the task registry in `source` (the queued Slack event included)."""
    return set(registry_in_source(source))


def dispatch_branches() -> Set[str]:
    """The task types `WorkerHandler` dispatches (the queued Slack event is not one)."""
    return branches_in_source(WORKER_HANDLER.read_text()) - {SLACK_EVENT_BRANCH}
