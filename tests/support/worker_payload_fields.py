"""The payload fields every `/worker` producer writes and every consumer reads (P15 card I; the P9 residual "producer and
consumer payload fields that do not match").

Producers are the sites `worker_triggers.enqueue_sites()` finds (each re-parsed at its `path:line`), the Cloud Scheduler
bodies (`worker_triggers.scheduler_task_types()`) and `GcpTaskQueue.enqueue_slack_event`, the producer of the queued Slack
event. Consumers are the branches of `WorkerHandler._dispatch`: the statements before the chain (read by every branch), the
branch test, the branch body, and every function the payload is handed to (a `self.<method>(payload)` of the same class, one
of `DELEGATES`, or a model of `MODELS`). Both sides fail loudly (`ExtractorError`, naming `path:line`) on a form they do
not understand, so a new payload shape is a test failure, not a silent pass.

A key is a field name, or `outer.inner` for a field of a nested dict (`payload.get("context", {}).get("user_id")` reads
`context.user_id`). A consumer key is **required** when it is read as `payload["k"]`, or when a name bound from the
payload is falsity-tested (`not x`, `x is None`, `not (x and y)`) in an operand of an `if` whose body refuses: returns a
status `>= 400` (an int literal or `HTTPStatus.<NAME>`) or raises. A model's fields are all required; everything else is
read and optional. Three checks use these sets: required keys are written by every producer, read keys are written by every
producer (`UNWRITTEN_BY_DESIGN` lists the exceptions), written keys are read (`UNREAD_BY_DESIGN`).

Known limit: the nested `context` of `agent_execution` is not compared. Its producers' context is opaque here: the typed
queue method takes it as a parameter (`gcp_task_queue.py:208`), the coordinator forwards the runtime delegation context
(`agent_coordinator.py:558`, not a literal) and only `deep_research_delivery.py:287` writes a literal (with conditional
`**` spreads). The consumer hands the whole dict to the agent (`agent_worker_handler.py:117`, `context.items()`), so every
written key is trivially read, and its other reads (`_deliver_*`, `_notify_*`) depend on the `intent`, not on the task
type, so a per-type "read is written" set would be wrong. A required `context.<k>` counts as written by an opaque
`context`.

Delta from the card ("static test, not slow"): the three tests that need the producer list are `slow`, because
`worker_triggers.enqueue_sites()` parses all of `src/` (about 2.5 s), the same as in `test_worker_triggers.py`.
"""
import ast
import functools
from http import HTTPStatus
from pathlib import Path
from typing import Callable, Dict, FrozenSet, List, NamedTuple, Optional, Set, Tuple, Union

from pydantic import BaseModel

from src.domain.drive_index import Nudge
from src.domain.connection_scope import ConnectionScope
from src.domain.drive_task_keys import scope_payload
from src.domain.fact_review_task_keys import review_payload
from src.domain.interactive_choice import INTERACTIVE_CHOICE_TASK, InteractiveChoice
from tests.support import worker_triggers as wt
from tests.support.worker_triggers import ROOT, SLACK_EVENT_BRANCH, WORKER_HANDLER, ExtractorError

GCP_TASK_QUEUE = ROOT / "src" / "adapters" / "gcp_task_queue.py"
PAYLOAD = "payload"


class Fn(NamedTuple):
    path: Path
    qualname: str   # "Class.method"


# Calls in `WorkerHandler._dispatch` that hand the payload to a consumer outside worker_handler.py: call text -> (consumer, reason).
DELEGATES: Dict[str, Tuple[Fn, str]] = {
    "self._agent_worker.handle_task": (
        Fn(ROOT / "src" / "handlers" / "agent_worker_handler.py", "AgentWorkerHandler.handle_task"),
        "WorkerHandler.__init__ binds agent_worker_handler: AgentWorkerHandler to self._agent_worker (worker_handler.py:116,161)"),
    "self._telegram_worker": (
        Fn(ROOT / "src" / "adapters" / "telegram" / "webhook_adapter.py", "TelegramWebhookAdapter.handle_queued_update"),
        "application_graph attaches telegram_adapter.handle_queued_update via attach_telegram_worker"),
    "self._slack_worker": (
        Fn(ROOT / "src" / "adapters" / "slack" / "http_adapter.py", "HTTPModeAdapter.handle_queued_event"),
        "application_graph attaches slack_adapter.handle_queued_event via attach_slack_worker"),
}
# A call that validates the whole payload into a model: its fields are the keys, all required.
MODELS: Dict[str, "type[BaseModel]"] = {"InteractiveChoice.from_payload": InteractiveChoice}
# `<variable>.to_payload()` as an enqueued payload: the model it serialises, by task type.
TO_PAYLOAD: Dict[str, "type[BaseModel]"] = {INTERACTIVE_CHOICE_TASK: InteractiveChoice}


def _review_payload_keys(call: ast.Call, where: str) -> Dict[str, object]:
    if len(call.args) + len(call.keywords) != 3:
        raise ExtractorError(f"{where}: review_payload with {len(call.args) + len(call.keywords)} arguments")
    return review_payload("u1", "a1", "daily")


def _scope_payload_keys(call: ast.Call, where: str) -> Dict[str, object]:
    count = len(call.args) + len(call.keywords)
    if count == 1:
        return scope_payload(ConnectionScope("u1", "c1"))
    if count == 2:
        return scope_payload(ConnectionScope("u1", "c1"), Nudge(mutation_id="m1", item_ids=("i1",)))
    raise ExtractorError(f"{where}: scope_payload with {count} arguments")


# Payload builders evaluated with dummy arguments: their keys do not depend on the values.
EVALUATED: Dict[str, Callable[[ast.Call, str], Dict[str, object]]] = {
    "review_payload": _review_payload_keys, "scope_payload": _scope_payload_keys,
}


class Keys(NamedTuple):
    keys: FrozenSet[str]
    opaque: FrozenSet[str]   # written with a non-literal value: any `key.inner` may be inside


class Producer(NamedTuple):
    task_type: str
    where: str
    keys: FrozenSet[str]
    opaque: FrozenSet[str]


class Consumer(NamedTuple):
    read: FrozenSet[str]
    required: FrozenSet[str]
    functions: Tuple[str, ...]   # every function the payload reached, "path::Class.method"


# ----------------------------------------------------------------------------- parsing

@functools.lru_cache(maxsize=None)
def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text())


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


FunctionNode = Union[ast.FunctionDef, ast.AsyncFunctionDef]


def _find_function(tree: ast.Module, qualname: str, where: str) -> FunctionNode:
    owner, _, name = qualname.rpartition(".")
    scopes: List[ast.AST] = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == owner] if owner else [tree]
    found = [n for s in scopes for n in getattr(s, "body", [])
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    if len(found) != 1:
        raise ExtractorError(f"{where}: {qualname} found {len(found)} times")
    return found[0]


# ----------------------------------------------------------------------------- producers

def _dict_keys(node: ast.Dict, where: str, prefix: str = "") -> Keys:
    keys: Set[str] = set()
    opaque: Set[str] = set()
    for key, value in zip(node.keys, node.values):
        if key is None:
            raise ExtractorError(f"{where}: a payload literal spreads {ast.unparse(value)!r}")
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            raise ExtractorError(f"{where}: a payload literal has the non-constant key {ast.unparse(key)!r}")
        name = prefix + key.value
        keys.add(name)
        if isinstance(value, ast.Dict):
            inner = _dict_keys(value, where, name + ".")
            keys |= inner.keys
            opaque |= inner.opaque
        else:
            opaque.add(name)
    return Keys(frozenset(keys), frozenset(opaque))


def _value_keys(value: Dict[str, object], prefix: str = "") -> Set[str]:
    keys: Set[str] = set()
    for key, item in value.items():
        keys.add(prefix + key)
        if isinstance(item, dict):
            keys |= _value_keys(item, prefix + key + ".")
    return keys


def payload_keys(node: ast.AST, task_type: str, where: str) -> Keys:
    """The keys of the payload expression handed to `enqueue_worker_task` (the queue adds `task_type` itself)."""
    if isinstance(node, ast.Dict):
        return _dict_keys(node, where)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in EVALUATED:
        return Keys(frozenset(_value_keys(EVALUATED[node.func.id](node, where))), frozenset())
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "to_payload"
            and not node.args and not node.keywords and task_type in TO_PAYLOAD):
        return Keys(frozenset(TO_PAYLOAD[task_type].model_fields), frozenset())
    raise ExtractorError(f"{where}: unrecognised payload form {ast.unparse(node)!r}")


def _call_name(node: ast.Call) -> str:
    return node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")


@functools.lru_cache(maxsize=None)
def generic_envelope() -> FrozenSet[str]:
    """The keys `GcpTaskQueue.enqueue_worker_task` adds around the caller's payload: `{"task_type": task_type, **payload}`."""
    func = _find_function(_tree(GCP_TASK_QUEUE), "GcpTaskQueue.enqueue_worker_task", _rel(GCP_TASK_QUEUE))
    for node in ast.walk(func):
        if (isinstance(node, ast.Dict) and None in node.keys
                and any(isinstance(v, ast.Name) and v.id == PAYLOAD for k, v in zip(node.keys, node.values) if k is None)):
            return _dict_keys(ast.Dict(keys=[k for k in node.keys if k is not None],
                                       values=[v for k, v in zip(node.keys, node.values) if k is not None]),
                              f"{_rel(GCP_TASK_QUEUE)}:{node.lineno}").keys
    raise ExtractorError(f"{_rel(GCP_TASK_QUEUE)}: enqueue_worker_task builds no {{..., **payload}} envelope")


def _site_nodes(tree: ast.Module) -> Dict[int, List[ast.AST]]:
    """Line -> the dict literals with a `task_type` key and the `enqueue_worker_task` calls that start on it."""
    by_line: Dict[int, List[ast.AST]] = {}
    for n in ast.walk(tree):
        if ((isinstance(n, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == "task_type" for k in n.keys))
                or (isinstance(n, ast.Call) and _call_name(n) == "enqueue_worker_task")):
            by_line.setdefault(n.lineno, []).append(n)
    return by_line


@functools.lru_cache(maxsize=None)
def _file_site_nodes(path: Path) -> Dict[int, List[ast.AST]]:
    return _site_nodes(_tree(path))


def site_keys(tree: ast.Module, line: int, task_type: str, where: str,
              nodes: Optional[Dict[int, List[ast.AST]]] = None) -> Keys:
    """The keys written at one enqueue site: a dict literal with a `task_type` key, or an `enqueue_worker_task` call."""
    found = (nodes if nodes is not None else _site_nodes(tree)).get(line, [])
    if len(found) != 1:
        raise ExtractorError(f"{where}: {len(found)} payload literals or enqueue calls on the line, expected one")
    if isinstance(found[0], ast.Dict):
        return _dict_keys(found[0], where)
    call = found[0]
    assert isinstance(call, ast.Call)
    keyword = [k.value for k in call.keywords if k.arg == PAYLOAD]
    if keyword:
        node = keyword[0]
    elif len(call.args) >= 2:
        node = call.args[1]
    else:
        raise ExtractorError(f"{where}: enqueue_worker_task without a payload")
    inner = payload_keys(node, task_type, where)
    return Keys(inner.keys | generic_envelope(), inner.opaque)


def slack_event_producer() -> Producer:
    func = _find_function(_tree(GCP_TASK_QUEUE), "GcpTaskQueue.enqueue_slack_event", _rel(GCP_TASK_QUEUE))
    found = [n for n in ast.walk(func) if isinstance(n, ast.Assign) and len(n.targets) == 1
             and isinstance(n.targets[0], ast.Name) and n.targets[0].id == PAYLOAD and isinstance(n.value, ast.Dict)]
    if len(found) != 1:
        raise ExtractorError(f"{_rel(GCP_TASK_QUEUE)}: enqueue_slack_event builds {len(found)} payload literals")
    where = f"{_rel(GCP_TASK_QUEUE)}:{found[0].lineno}"
    keys = _dict_keys(found[0].value, where)
    return Producer(SLACK_EVENT_BRANCH, where, keys.keys, keys.opaque)


def producers() -> List[Producer]:
    out: List[Producer] = []
    for site in wt.enqueue_sites():
        path, line = site.where.rsplit(":", 1)
        keys = site_keys(_tree(ROOT / path), int(line), site.task_type, site.where, _file_site_nodes(ROOT / path))
        out.append(Producer(site.task_type, site.where, keys.keys, keys.opaque))
    for task_type, body in wt.scheduler_task_types().items():
        out.append(Producer(task_type, _rel(wt.SCHEDULER_SCRIPT), frozenset(_value_keys(body)), frozenset()))
    out.append(slack_event_producer())
    return out


# ----------------------------------------------------------------------------- consumers

def _is_p(node: ast.AST, param: str) -> bool:
    return isinstance(node, ast.Name) and node.id == param


def _const_str(node: ast.AST) -> Optional[str]:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _direct_get(node: ast.AST, param: str) -> Optional[str]:
    """`payload.get("k"[, default])` -> "k"."""
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
            and _is_p(node.func.value, param) and node.args):
        key = _const_str(node.args[0])
        if key is None:
            raise ExtractorError(f"line {node.lineno}: payload.get with the key {ast.unparse(node.args[0])!r}")
        return key
    return None


def _nested_get(node: ast.AST, param: str) -> Optional[str]:
    """`payload.get("k", {}).get("j"[, default])` -> "k.j"."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" and node.args:
        outer = _direct_get(node.func.value, param)
        inner = _const_str(node.args[0])
        if outer is not None and inner is not None:
            return f"{outer}.{inner}"
    return None


def _subscript(node: ast.AST, param: str) -> Optional[str]:
    if isinstance(node, ast.Subscript) and _is_p(node.value, param) and isinstance(node.ctx, ast.Load):
        key = _const_str(node.slice)
        if key is None:
            raise ExtractorError(f"line {node.lineno}: payload[{ast.unparse(node.slice)}]")
        return key
    return None


def _read_expr(node: ast.AST, param: str) -> Optional[str]:
    """The key a payload-reading expression stands for (`get`, nested `get`, `[]`, or one of them `or` a default)."""
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        return _read_expr(node.values[0], param)
    return _nested_get(node, param) or _direct_get(node, param) or _subscript(node, param)


class _Scan:
    """The reads and escapes of one function body for one payload parameter."""

    def __init__(self, param: str, where: str) -> None:
        self.param = param
        self.where = where
        self.read: Set[str] = set()
        self.subscripted: Set[str] = set()   # read as `payload["k"]`: a KeyError when absent, so required
        self.escapes: List[Tuple[ast.Call, object]] = []   # (call, positional index | keyword name)

    def scan(self, node: ast.AST) -> None:
        p = self.param
        if isinstance(node, ast.Call):
            key = _nested_get(node, p)
            if key is not None:
                self.read.add(key)
                inner_call = node.func.value if isinstance(node.func, ast.Attribute) else None
                for extra in node.args[1:] + (inner_call.args[1:] if isinstance(inner_call, ast.Call) else []):
                    self.scan(extra)
                return
            key = _direct_get(node, p)
            if key is not None:
                self.read.add(key)
                for extra in node.args[1:]:
                    self.scan(extra)
                return
            for index, arg in enumerate(node.args):
                if isinstance(arg, ast.Starred) and _is_p(arg.value, p):
                    raise ExtractorError(f"{self.where}:{node.lineno}: *payload in {ast.unparse(node.func)}(...)")
                if _is_p(arg, p):
                    self.escapes.append((node, index))
                else:
                    self.scan(arg)
            for kw in node.keywords:
                if _is_p(kw.value, p):
                    if kw.arg is None:
                        raise ExtractorError(f"{self.where}:{node.lineno}: **payload in {ast.unparse(node.func)}(...)")
                    self.escapes.append((node, kw.arg))
                else:
                    self.scan(kw.value)
            self.scan(node.func)
            return
        if isinstance(node, ast.Attribute) and _is_p(node.value, p):
            raise ExtractorError(f"{self.where}:{node.lineno}: payload.{node.attr} is not a read the extractor knows")
        key = _subscript(node, p)
        if key is not None:
            self.read.add(key)
            self.subscripted.add(key)
            return
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)) and node.value is not None \
                and _aliases(node.value, p):
            raise ExtractorError(f"{self.where}:{node.lineno}: an alias of the payload ({ast.unparse(node)!r}) hides its reads")
        if isinstance(node, ast.Subscript) and _is_p(node.value, p):
            return   # a store into the payload is not a read
        if (isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.In, ast.NotIn))
                and _is_p(node.comparators[0], p) and _const_str(node.left) is not None):
            self.read.add(str(_const_str(node.left)))
            return
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if k is None and _is_p(v, p):
                    raise ExtractorError(f"{self.where}:{node.lineno}: {{**payload}} copies every field")
        for child in ast.iter_child_nodes(node):
            self.scan(child)


def _aliases(value: ast.AST, param: str) -> bool:
    """`body = payload`, `body = payload or {}`, `body = payload if c else {}`: another name for the payload itself."""
    if _is_p(value, param):
        return True
    if isinstance(value, ast.BoolOp):
        return any(_aliases(v, param) for v in value.values)
    if isinstance(value, ast.IfExp):
        return _aliases(value.body, param) or _aliases(value.orelse, param)
    return False


def _status(ret: ast.Return, where: str) -> Optional[int]:
    """The status of `return body, status`: an int literal or `HTTPStatus.<NAME>`; None for a return that is no pair.
    Any other status expression is a form the extractor does not understand."""
    if not (isinstance(ret.value, ast.Tuple) and len(ret.value.elts) == 2):
        return None
    last = ret.value.elts[-1]
    if isinstance(last, ast.Constant) and isinstance(last.value, int) and not isinstance(last.value, bool):
        return last.value
    if isinstance(last, ast.Attribute) and ast.unparse(last.value) in ("HTTPStatus", "http.HTTPStatus") \
            and last.attr in HTTPStatus.__members__:
        return int(HTTPStatus[last.attr])
    raise ExtractorError(f"{where}:{ret.lineno}: the status {ast.unparse(last)!r} is not a literal the extractor reads")


def _refuses(branch: ast.If, where: str) -> bool:
    """The body of `branch` returns a status >= 400 or raises."""
    for stmt in branch.body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Raise):
                return True
            if isinstance(node, ast.Return) and (_status(node, where) or 0) >= 400:
                return True
    return False


def _falsity_names(operand: ast.AST) -> Set[str]:
    """Names an operand tests for falsity: `not x`, `x is None`, `not (x and y)`."""
    if isinstance(operand, ast.UnaryOp) and isinstance(operand.op, ast.Not):
        inner = operand.operand
        if isinstance(inner, ast.Name):
            return {inner.id}
        if isinstance(inner, ast.BoolOp) and isinstance(inner.op, ast.And):
            return {v.id for v in inner.values if isinstance(v, ast.Name)}
    if (isinstance(operand, ast.Compare) and isinstance(operand.left, ast.Name) and len(operand.ops) == 1
            and isinstance(operand.ops[0], ast.Is) and isinstance(operand.comparators[0], ast.Constant)
            and operand.comparators[0].value is None):
        return {operand.left.id}
    return set()


def required_keys(statements: List[ast.stmt], param: str, where: str = "inline.py") -> Set[str]:
    """The keys bound from the payload and falsity-tested in an `if` whose body refuses (`_refuses`)."""
    holder = ast.Module(body=list(statements), type_ignores=[])
    bound: Dict[str, str] = {}
    for node in ast.walk(holder):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            key = _read_expr(node.value, param)
            if key is not None:
                bound[node.targets[0].id] = key
    required: Set[str] = set()
    for node in ast.walk(holder):
        if not isinstance(node, ast.If):
            continue
        if not _refuses(node, where):
            continue
        operands = node.test.values if isinstance(node.test, ast.BoolOp) and isinstance(node.test.op, ast.Or) else [node.test]
        for operand in operands:
            required |= {bound[name] for name in _falsity_names(operand) if name in bound}
    return required


def _param_at(func: FunctionNode, position: object, where: str) -> str:
    args = func.args
    names = [a.arg for a in args.posonlyargs + args.args]
    if names and names[0] in ("self", "cls"):
        names = names[1:]
    if isinstance(position, str):
        if position not in names + [a.arg for a in args.kwonlyargs]:
            raise ExtractorError(f"{where}: {func.name} has no parameter {position!r}")
        return position
    if not isinstance(position, int) or position >= len(names):
        raise ExtractorError(f"{where}: {func.name} has no positional parameter {position}")
    return names[position]


class _Walk:
    """A consumer: every function the payload reaches from one branch, with their reads and required keys."""

    def __init__(self, delegates: Dict[str, Tuple[Fn, str]], sources: Optional[Dict[Path, ast.Module]] = None) -> None:
        self.delegates = delegates
        self.sources = sources or {}
        self.read: Set[str] = set()
        self.required: Set[str] = set()
        self.functions: List[str] = []

    def statements(self, statements: List[ast.stmt], param: str, path: Path, owner: str, extra: Tuple[ast.AST, ...] = ()) -> None:
        where = _rel(path)
        scan = _Scan(param, where)
        for node in list(statements) + list(extra):
            scan.scan(node)
        self.read |= scan.read
        self.required |= scan.subscripted | required_keys(statements, param, where)
        for call, position in scan.escapes:
            self._follow(call, position, path, owner, where)

    def function(self, fn: Fn, position: object = 0) -> None:
        where = _rel(fn.path)
        func = _find_function(self.sources.get(fn.path) or _tree(fn.path), fn.qualname, where)
        label = f"{where}::{fn.qualname}"
        if label in self.functions:
            return
        self.functions.append(label)
        self.statements(func.body, _param_at(func, position, where), fn.path, fn.qualname.rpartition(".")[0])

    def _follow(self, call: ast.Call, position: object, path: Path, owner: str, where: str) -> None:
        text = ast.unparse(call.func)
        if text in MODELS:
            fields = frozenset(MODELS[text].model_fields)
            self.read |= fields
            self.required |= fields
            return
        if text in self.delegates:
            self.function(self.delegates[text][0], position)
            return
        if text.startswith("self.") and text.count(".") == 1 and owner:
            self.function(Fn(path, f"{owner}.{text[5:]}"), position)
            return
        raise ExtractorError(f"{where}:{call.lineno}: the payload goes into {text}(...), which the extractor does not follow")


def _dispatch_chain(tree: ast.Module, where: str) -> Tuple[FunctionNode, List[ast.stmt], List[ast.If]]:
    """`_dispatch`, the statements before its `if/elif` chain (read by every branch), and the chain's `if` nodes."""
    func = _find_function(tree, "WorkerHandler._dispatch", where)
    body = func.body
    first = next((i for i, s in enumerate(body) if isinstance(s, ast.If)), None)
    if first is None:
        raise ExtractorError(f"{where}: _dispatch has no if/elif chain")
    chain: List[ast.If] = []
    node: Optional[ast.If] = body[first]
    while node is not None:
        chain.append(node)
        node = node.orelse[0] if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If) else None
    return func, body[:first], chain


def _calls_a_handler(statements: List[ast.stmt], delegates: Dict[str, Tuple[Fn, str]]) -> bool:
    for node in ast.walk(ast.Module(body=list(statements), type_ignores=[])):
        if isinstance(node, ast.Call):
            text = ast.unparse(node.func)
            if text in delegates or text.startswith("self._handle_"):
                return True
    return False


def consumers(source_path: Path = WORKER_HANDLER, delegates: Optional[Dict[str, Tuple[Fn, str]]] = None,
              source: Optional[str] = None) -> Dict[str, Consumer]:
    """`branch -> Consumer` for every branch of `WorkerHandler._dispatch` (the queued Slack event included). `source`
    replaces the file's text (the self-tests); `delegates` replaces `DELEGATES`."""
    delegates = DELEGATES if delegates is None else delegates
    tree = ast.parse(source) if source is not None else _tree(source_path)
    where = _rel(source_path)
    func, preamble, chain = _dispatch_chain(tree, where)
    param = _param_at(func, 0, where)
    out: Dict[str, Consumer] = {}
    for branch in chain:
        name = wt._branch_name(branch.test)
        if not _calls_a_handler(branch.body, delegates):
            raise ExtractorError(f"{where}:{branch.lineno}: the {name} branch calls no handler")
        walk = _Walk(delegates, {source_path: tree})
        walk.statements(list(preamble) + list(branch.body), param, source_path, "WorkerHandler", extra=(branch.test,))
        out[name] = Consumer(frozenset(walk.read), frozenset(walk.required), tuple(walk.functions))
    return out


def delegates_used() -> Set[str]:
    """The `DELEGATES` call texts the real `_dispatch` contains."""
    _, _, chain = _dispatch_chain(_tree(WORKER_HANDLER), _rel(WORKER_HANDLER))
    return {ast.unparse(n.func) for branch in chain for stmt in branch.body for n in ast.walk(stmt)
            if isinstance(n, ast.Call) and ast.unparse(n.func) in DELEGATES}


def function_consumer(source: str, qualname: str, position: object = 0, name: str = "inline.py") -> Consumer:
    """The consumer of one function in a source string (the self-tests); a followed `self.<method>` resolves in it too."""
    path = ROOT / name
    walk = _Walk({}, {path: ast.parse(source)})
    walk.function(Fn(path, qualname), position)
    return Consumer(frozenset(walk.read), frozenset(walk.required), tuple(walk.functions))


# ----------------------------------------------------------------------------- the comparison

def _covers(written: FrozenSet[str], opaque: FrozenSet[str], key: str) -> bool:
    """A required key is written when it is written itself or sits inside a field written with a non-literal value."""
    if key in written:
        return True
    parts = key.split(".")
    return any(".".join(parts[:i]) in opaque for i in range(1, len(parts)))


def _is_read(read: FrozenSet[str], key: str) -> bool:
    """A written key is read when the consumer reads it, a field inside it, or the field it sits in."""
    if key in read:
        return True
    if any(r.startswith(key + ".") for r in read):
        return True
    parts = key.split(".")
    return any(".".join(parts[:i]) in read for i in range(1, len(parts)))


def missing_required(producer_list: List[Producer], consumer_map: Dict[str, Consumer]) -> Set[Tuple[str, str, str]]:
    """(task type, producer site, key) for every required key a producer does not write."""
    out: Set[Tuple[str, str, str]] = set()
    for prod in producer_list:
        consumer = consumer_map.get(prod.task_type)
        if consumer is None:
            continue
        for key in consumer.required:
            if not _covers(prod.keys, prod.opaque, key):
                out.add((prod.task_type, prod.where, key))
    return out


def unread_written(producer_list: List[Producer], consumer_map: Dict[str, Consumer]) -> Dict[Tuple[str, str], Set[str]]:
    """(task type, key) -> the producer sites that write a key the consumer never reads."""
    out: Dict[Tuple[str, str], Set[str]] = {}
    for prod in producer_list:
        consumer = consumer_map.get(prod.task_type)
        if consumer is None:
            continue
        for key in prod.keys:
            if not _is_read(consumer.read, key):
                out.setdefault((prod.task_type, key), set()).add(prod.where)
    return out


def unwritten_read(producer_list: List[Producer], consumer_map: Dict[str, Consumer]) -> Dict[Tuple[str, str], Set[str]]:
    """(task type, key) -> the producer sites that do not write a key the consumer reads (required or optional)."""
    out: Dict[Tuple[str, str], Set[str]] = {}
    for prod in producer_list:
        consumer = consumer_map.get(prod.task_type)
        if consumer is None:
            continue
        for key in consumer.read:
            if not _covers(prod.keys, prod.opaque, key):
                out.setdefault((prod.task_type, key), set()).add(prod.where)
    return out
