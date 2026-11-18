"""Every Firestore query of `src/adapters/` that needs a composite index has one in `config/firestore.indexes.json`
(TEST_COVERAGE_SYSTEMIC_RFC P12, H1's "refuse undeclared composite queries" turned into a tree-wide check).

Why a static walk: the per-collection tests (`test_firestore_indexes_*.py`) cover the collections somebody remembered;
a query written later, on any other collection, fails at query time in production, never in `make check`. This test reads
the adapters' source, rebuilds each query a method sends (a chain of `where` / `order_by` / `find_nearest`, possibly
built over several statements, branches and helper methods), resolves its collection through `EnvironmentConfig`, and
asks the H1 fake's index model (`EvalDb(indexes=...)._check_index`, the rule set of `tests/support/firestore_eval.py`)
whether the declared indexes serve it. Nothing here is a second rule set.

How the walk reads a method: a small abstract interpreter over the AST. A name bound to a query holds its filters and
orderings; `if` / loops / `try` join the values of both paths (a query that may carry an extra filter is checked both
ways); a call of a method or function of the tree is inlined with the arguments' values, so a query built in a helper
and consumed in its caller is one query. A query counts when it is consumed (`.stream()`, `.get()`, `.count()`, ...) or
handed to a call the walk cannot enter. Every `where` / `order_by` / `find_nearest` call must end up in some consumed
query: one that does not is a shape the walk cannot follow, and must sit in UNRESOLVED_CALLS with a reason (the list
holds nothing that the walk can follow, so it only shrinks). A consumed query that needs an index but whose fields or
collection name are not literals is in UNRESOLVED_SITES, with a reason.

Outcomes of a query that needs a composite: served (the normal case); DOCUMENTED_UNINDEXED (no index by design, with the
line of the doc that says so; the test pins that it is still unindexed); or a missing index with no such line, which is
a strict xfail `P12-D<n>` (the index deploys are the owner's: this test never edits `config/firestore.indexes.json`).
"""
import ast
import importlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import pytest
from google.api_core.exceptions import FailedPrecondition

from src.config.environment import Environment, EnvironmentConfig
from src.adapters.firestore_repo import _NEAREST_VECTOR_FIELDS
from src.adapters.firestore_repo import _VECTOR_FIELDS as _FACT_VECTOR_FIELDS
from src.adapters.firestore_task_search_index import _VECTOR_FIELDS
from src.domain.drive_enrichment import EnrichJob
from tests.support.firestore_eval import EvalDb, _ARRAY_OPS, _EQUALITY_OPS, _RANGE_OPS, _required_index, _serves

_ENV = EnvironmentConfig()
_ENV.env = Environment.DEVELOPMENT

ROOT = Path(__file__).resolve().parents[2]
ADAPTERS = ROOT / "src" / "adapters"
INDEXES = ROOT / "config" / "firestore.indexes.json"

# --------------------------------------------------------------------------------------- the explicit lists

# (file, function, call text) -> why the walk cannot follow that `where` / `order_by` / `find_nearest` call.
UNRESOLVED_CALLS: Dict[Tuple[str, str, str], str] = {}

# (file, function) -> why a consumed query that needs a composite cannot be matched (a field or collection name that is
# not a literal on any path the walk takes).
UNRESOLVED_SITES: Dict[Tuple[str, str], str] = {
    ("src/adapters/firestore_drive_index_repo.py", "FirestoreDriveIndexRepository.unread_count_queries"):
        "the filters come from data tables of (field, operator, value) triples; a test pins that every operator in "
        "them is `==`, so the queries are equality-only plus `kind in`, which single-field indexes serve",
    ("src/adapters/firestore_drive_index_repo.py", "FirestoreDriveIndexRepository._subdoc_refs"):
        "a collection-group purge helper: the group and the equality field are arguments chosen by the caller",
}

# (file, function, filter field) -> the doc line that says the queries filtering on that field have no index on purpose;
# they must still be unindexed.
DOCUMENTED_UNINDEXED: Dict[Tuple[str, str, str], str] = {
    ("src/adapters/firestore_task_search_index.py", "FirestoreTaskSearchIndex.find_nearest._query_one", "list_id"):
        "src/adapters/firestore_task_search_index.py:36-39 ('none serves list_id, so a query with it is refused as a "
        "missing index (the field query then fails and is logged)')",
}

# (file, constructor argument) -> the value the composition passes, where the collection name is an argument.
CONSTRUCTOR_ARGUMENTS: Dict[Tuple[str, str], str] = {
    # `FirestoreSessionStore(db_client, env_config.sessions_collection, ...)`: src/composition/service_container.py:412-414
    ("src/adapters/firestore_session_store.py", "collection_prefix"): _ENV.sessions_collection,
}

# (file, function) -> the names a filter field written as an f-string can take: each is checked.
FIELD_CHOICES: Dict[Tuple[str, str], Tuple[str, ...]] = {
    ("src/adapters/firestore_drive_index_repo.py", "FirestoreDriveIndexRepository.enrich_queue"):
        tuple(f"{job.value}_state" for job in EnrichJob),
}

# (file, function) -> why a query is handed to a call the walk cannot enter (a transaction read, an SDK helper): such a
# query is recorded and checked, but nobody followed what the callee does with it.
OPAQUE_CONSUMPTIONS: Dict[Tuple[str, str], str] = {}

# (file, function) -> the vector fields a `find_nearest` whose field is a runtime value can take: each is checked.
VECTOR_FIELD_CHOICES: Dict[Tuple[str, str], Tuple[str, ...]] = {
    ("src/adapters/firestore_repo.py", "FirestoreFactRepository.search_facts"): tuple(_FACT_VECTOR_FIELDS),   # the adapter's own write-side list
    ("src/adapters/firestore_repo.py", "FirestoreFactRepository.nearest_fact_ids"): tuple(_NEAREST_VECTOR_FIELDS),
    ("src/adapters/firestore_task_search_index.py", "FirestoreTaskSearchIndex.find_nearest._query_one"): tuple(_VECTOR_FIELDS),
}

# (file, function, shape) -> the defect label and its place: a query that needs an index nobody declared.
KNOWN_MISSING: Dict[Tuple[str, str, str], str] = {
    ("src/adapters/firestore_drive_note_repo.py", "FirestoreDriveNoteRepository.find_nearest",
     "development_drive_notes: where(user_id ==) where(state ==) where(embedding_space ==) find_nearest(note_vector)"):
        "P12-D1: src/adapters/firestore_drive_note_repo.py:269 the vector index of the owner notes (user_id, state, "
        "embedding_space, note_vector) is created by an owner-run script, not declared in config/firestore.indexes.json "
        "(only a `note_vector` field override with no indexes is), so the test database never gets it",
}

# ----------------------------------------------------------------------------------------------- the walk

_BUILDERS = {"where", "order_by", "find_nearest"}
_SHAPING = {"select", "limit", "limit_to_last", "offset", "start_at", "start_after", "end_at", "end_before"}
_CONSUMING = {"stream", "get", "count", "sum", "avg", "explain", "on_snapshot"}
_ENV_BASE = re.compile(r"(^|\.)_?(env_config|env|environment)$")
_MAX_DEPTH = 8
_TRANSPARENT = {"zip", "enumerate", "list", "tuple", "sorted", "reversed", "len", "set", "frozenset", "bool", "any", "all", "iter"}


class Unresolved(str):
    """A field or collection name the walk cannot read as a literal: its source text."""


@dataclass(frozen=True)
class Op:
    kind: str                       # where | order_by | find_nearest
    field: str
    arg: str                        # the operator, the direction, or ""
    node: Tuple[str, int, int]      # file, line, column of the call


@dataclass(frozen=True)
class Coll:
    name: str
    group: bool = False


@dataclass(frozen=True)
class Q:
    coll: Coll
    ops: Tuple[Op, ...]


@dataclass(frozen=True)
class FF:
    field: str
    op: str


class Alt(tuple):
    """The values one name may hold after a join."""


class Seq(list):
    """A list or tuple literal / comprehension result."""


@dataclass
class Fn:
    node: Any
    module: "Module"
    cls: Optional[str]
    closure: Optional[Dict[str, Any]] = None


@dataclass
class Frame:
    module: "Module"
    cls: Optional[str]
    env: Dict[str, Any]
    qualname: str
    depth: int = 0
    returns: Optional[List[Any]] = None
    capture: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class Site:
    file: str
    function: str
    line: int
    coll: Coll
    ops: Tuple[Op, ...]

    @property
    def shape(self) -> str:
        parts = [f"{o.kind}({o.field}{' ' + o.arg if o.arg else ''})" for o in self.ops]
        return f"{self.coll.name}: " + " ".join(parts)


def _alt(values: Iterable[Any]) -> Any:
    flat: List[Any] = []
    for value in values:
        for item in (value if isinstance(value, Alt) else (value,)):
            if item not in flat:
                flat.append(item)
    known = [v for v in flat if v is not None]
    if not known:
        return None
    return known[0] if len(known) == 1 else Alt(known)


def _alts(value: Any) -> List[Any]:
    return list(value) if isinstance(value, Alt) else [value]


class Module:
    def __init__(self, project: "Project", rel: str, tree: ast.Module, namespace: Dict[str, Any]) -> None:
        self.project, self.rel, self.tree, self.ns = project, rel, tree, namespace
        self.classes: Dict[str, ast.ClassDef] = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
        self.funcs: Dict[str, Any] = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self._attrs: Dict[str, Dict[str, Any]] = {}
        self._busy: Set[str] = set()

    def method(self, cls: Optional[str], name: str) -> Optional[Fn]:
        seen: Set[str] = set()
        queue = [(self, cls)] if cls else []
        while queue:
            module, cname = queue.pop(0)
            if cname is None or (module.rel, cname) in seen:
                continue
            seen.add((module.rel, cname))
            node = module.classes.get(cname)
            if node is None:
                continue
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name:
                    return Fn(item, module, cname)
            for base in node.bases:
                base_name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", None)
                found = self.project.find_class(base_name, prefer=module)
                if found:
                    queue.append(found)
        return None

    def attrs(self, cls: Optional[str]) -> Dict[str, Any]:
        """`self.X = <expr>` of the class: `__init__` is walked (so a local it names resolves), then any other method's
        assignment is read on its own. Kept where the value is a collection, a query, a string or a sequence."""
        if not cls or cls not in self.classes:
            return {}
        if cls in self._attrs:
            return self._attrs[cls]
        found: Dict[str, Any] = {}
        self._attrs[cls] = found        # visible while it fills: `self.collection = db.collection(self.collection_name)`
        init = self.method(cls, "__init__")
        if init is not None:
            frame = Frame(init.module, cls, {}, f"{cls}.__init__", 4, [])
            frame.capture = found
            walker = self.project.walker
            saved, walker.stack = walker.stack, [id(init.node)]
            try:
                walker.block(init.node.body, frame)
            finally:
                walker.stack = saved
        for node in ast.walk(self.classes[cls]):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Attribute) \
                    and isinstance(node.targets[0].value, ast.Name) and node.targets[0].value.id == "self" \
                    and node.targets[0].attr not in found:
                value = self.project.walker.ev(node.value, Frame(self, cls, {}, f"{cls}.__attrs__", 99))
                if isinstance(value, (Coll, Q, str, Seq)):
                    found[node.targets[0].attr] = value
        return found


class Project:
    def __init__(self) -> None:
        self.modules: List[Module] = []
        self.sites: Dict[Tuple[str, str, Tuple[Any, ...]], Site] = {}
        self.covered: Set[Tuple[str, int, int]] = set()
        self.calls: Dict[Tuple[str, int, int], str] = {}     # every builder call -> its function
        self.call_text: Dict[Tuple[str, int, int], str] = {}
        self.filter_calls: Dict[Tuple[str, int, int], str] = {}   # every FieldFilter(...) call -> its function
        self.opaque: Set[Tuple[str, str]] = set()
        self.walker = Walker(self)

    def add(self, rel: str, source: str, namespace: Dict[str, Any]) -> Module:
        tree = ast.parse(source)
        module = Module(self, rel, tree, namespace)
        self.modules.append(module)
        self._index_calls(module)
        return module

    def _index_calls(self, module: Module) -> None:
        def visit(node: ast.AST, scope: List[str]) -> None:
            for child in ast.iter_child_nodes(node):
                inner = scope
                if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    inner = scope + [child.name]
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr in _BUILDERS:
                    self.calls[(module.rel, child.lineno, child.col_offset)] = ".".join(scope) or "<module>"
                    parts = [ast.unparse(a) for a in child.args] + [f"{k.arg}={ast.unparse(k.value)}" for k in child.keywords]
                    self.call_text[(module.rel, child.lineno, child.col_offset)] = f"{child.func.attr}({', '.join(parts)})"
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == "FieldFilter":
                    self.filter_calls[(module.rel, child.lineno, child.col_offset)] = ".".join(scope) or "<module>"
                visit(child, inner)
        visit(module.tree, [])

    def find_class(self, name: Optional[str], prefer: Optional[Module] = None) -> Optional[Tuple[Module, str]]:
        for module in ([prefer] if prefer else []) + self.modules:
            if name in module.classes:
                return module, name
        return None

    def run(self) -> None:
        for module in self.modules:
            for name, node in module.funcs.items():
                self.walker.top(Fn(node, module, None), name)
            for cname, cnode in module.classes.items():
                for item in cnode.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        self.walker.top(Fn(item, module, cname), f"{cname}.{item.name}")

    def record(self, query: Q, function: str) -> None:
        if not query.ops:
            return
        last = query.ops[-1]
        key = (last.node[0], function, (query.coll, tuple((o.kind, o.field, o.arg) for o in query.ops)))
        if key not in self.sites:
            self.sites[key] = Site(last.node[0], function, last.node[1], query.coll, query.ops)
        for op in query.ops:
            self.covered.add(op.node)


class Walker:
    def __init__(self, project: Project) -> None:
        self.project = project
        self.stack: List[int] = []

    # ---------------------------------------------------------------- functions

    def top(self, fn: Fn, qualname: str) -> None:
        frame = Frame(fn.module, fn.cls, {}, qualname, 0, [])
        self.stack = [id(fn.node)]
        self.block(fn.node.body, frame)

    def inline(self, fn: Fn, call: ast.Call, frame: Frame, skip_self: bool) -> Any:
        args = [self.ev(a.value if isinstance(a, ast.Starred) else a, frame) for a in call.args]
        kwargs = {k.arg: self.ev(k.value, frame) for k in call.keywords if k.arg}
        starred = any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords)
        if frame.depth >= _MAX_DEPTH or id(fn.node) in self.stack:
            for value in [*args, *kwargs.values()]:
                self.consume(value, frame)
            return None
        params = [a.arg for a in fn.node.args.posonlyargs + fn.node.args.args]
        if skip_self and params:
            params = params[1:]
        env: Dict[str, Any] = dict(fn.closure or {})
        if not starred:
            for name, value in zip(params, args):
                env[name] = value
            for name, value in kwargs.items():
                env[name] = value
        inner = Frame(fn.module, fn.cls, env, frame.qualname, frame.depth + 1, [])
        self.stack.append(id(fn.node))
        try:
            self.block(fn.node.body, inner)
        finally:
            self.stack.pop()
        return _alt(inner.returns or [])

    def consume(self, value: Any, frame: Frame) -> None:
        for item in _alts(value):
            if isinstance(item, Q):
                owner = self._owner(item, frame)
                self.project.record(item, owner)
                if item.ops:
                    self.project.opaque.add((item.ops[-1].node[0], owner))
            elif isinstance(item, (Seq, tuple)) and not isinstance(item, Alt):
                for sub in item:
                    self.consume(sub, frame)

    def _owner(self, query: Q, frame: Frame) -> str:
        return self.project.calls.get(query.ops[-1].node, frame.qualname) if query.ops else frame.qualname

    # --------------------------------------------------------------- statements

    def block(self, stmts: List[ast.stmt], frame: Frame) -> None:
        for stmt in stmts:
            self.stmt(stmt, frame)

    def bind(self, target: ast.AST, value: Any, frame: Frame) -> None:
        if isinstance(target, ast.Name):
            frame.env[target.id] = value
        elif isinstance(target, (ast.Tuple, ast.List)):
            options = [v for v in _alts(value) if isinstance(v, (tuple, Seq)) and not isinstance(v, Alt)]
            for i, sub in enumerate(target.elts):
                part = _alt(o[i] for o in options if i < len(o)) if options else None
                self.bind(sub.value if isinstance(sub, ast.Starred) else sub, part, frame)

    def join(self, frame: Frame, branches: List[Dict[str, Any]]) -> None:
        keys = set().union(*branches)
        frame.env = {k: _alt(b[k] for b in branches if k in b) for k in keys}

    def stmt(self, node: ast.stmt, frame: Frame) -> None:
        if isinstance(node, ast.Assign):
            value = self.ev(node.value, frame)
            for target in node.targets:
                if frame.capture is not None and isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) \
                        and target.value.id == "self" and isinstance(value, (Coll, Q, str, Seq)):
                    frame.capture[target.attr] = value
                self.bind(target, value, frame)
        elif isinstance(node, ast.AnnAssign):
            if node.value is not None:
                self.bind(node.target, self.ev(node.value, frame), frame)
        elif isinstance(node, ast.AugAssign):
            self.ev(node.value, frame)
        elif isinstance(node, ast.Expr):
            self.ev(node.value, frame)
        elif isinstance(node, ast.Return):
            if node.value is not None and frame.returns is not None:
                frame.returns.append(self.ev(node.value, frame))
            elif node.value is not None:
                self.ev(node.value, frame)
        elif isinstance(node, ast.If):
            truth = self.static_truth(node.test, frame)
            self.ev(node.test, frame)
            if truth is not None:
                self.block(node.body if truth else node.orelse, frame)
                return
            before = dict(frame.env)
            self.block(node.body, frame)
            then_env = frame.env
            frame.env = dict(before)
            self.block(node.orelse, frame)
            self.join(frame, [then_env, frame.env])
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            items = self.ev(node.iter, frame)
            before = dict(frame.env)
            seqs = [s for s in _alts(items) if isinstance(s, (Seq, tuple)) and not isinstance(s, Alt)]
            self.bind(node.target, _alt(x for s in seqs for x in s) if seqs else None, frame)
            self.block(node.body, frame)
            self.join(frame, [before, frame.env])
            self.block(node.orelse, frame)
        elif isinstance(node, ast.While):
            self.ev(node.test, frame)
            before = dict(frame.env)
            self.block(node.body, frame)
            self.join(frame, [before, frame.env])
        elif isinstance(node, ast.Try):
            before = dict(frame.env)
            self.block(node.body, frame)
            branches = [frame.env]
            for handler in node.handlers:
                frame.env = dict(before)
                self.block(handler.body, frame)
                branches.append(frame.env)
            self.join(frame, branches)
            self.block(node.orelse, frame)
            self.block(node.finalbody, frame)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                value = self.ev(item.context_expr, frame)
                if item.optional_vars is not None:
                    self.bind(item.optional_vars, value, frame)
            self.block(node.body, frame)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            frame.env[node.name] = Fn(node, frame.module, frame.cls, dict(frame.env))
        elif isinstance(node, (ast.Raise, ast.Assert, ast.Delete)):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.expr):
                    self.ev(child, frame)

    # -------------------------------------------------------------- expressions

    def ev(self, node: Optional[ast.AST], frame: Frame) -> Any:
        if node is None:
            return None
        handler = getattr(self, "e_" + type(node).__name__, None)
        if handler:
            return handler(node, frame)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.expr):
                self.ev(child, frame)
        return None

    def e_Constant(self, node: ast.Constant, frame: Frame) -> Any:
        return node.value

    def e_Name(self, node: ast.Name, frame: Frame) -> Any:
        if node.id in frame.env:
            return frame.env[node.id]
        if (frame.module.rel, node.id) in CONSTRUCTOR_ARGUMENTS:
            return CONSTRUCTOR_ARGUMENTS[(frame.module.rel, node.id)]
        value = frame.module.ns.get(node.id)
        if isinstance(value, (tuple, list)) and value and all(isinstance(v, str) for v in value):
            return Seq(value)
        return value if isinstance(value, (str, int, float, bool)) else None

    def e_Attribute(self, node: ast.Attribute, frame: Frame) -> Any:
        if node.attr in ("ASCENDING", "DESCENDING"):
            return node.attr
        if isinstance(node.value, ast.Name) and node.value.id in ("self", "cls"):
            found = frame.module.attrs(frame.cls).get(node.attr)
            if found is None:
                prop = frame.module.method(frame.cls, node.attr)
                if prop is not None and any(isinstance(d, ast.Name) and d.id in ("property", "cached_property")
                                            for d in prop.node.decorator_list) and id(prop.node) not in self.stack:
                    inner = Frame(prop.module, prop.cls, {}, frame.qualname, frame.depth + 1, [])
                    self.stack.append(id(prop.node))
                    try:
                        self.block(prop.node.body, inner)
                    finally:
                        self.stack.pop()
                    return _alt(inner.returns or [])
            return found
        if _ENV_BASE.search(ast.unparse(node.value)):
            value = getattr(_ENV, node.attr, None)
            return value if isinstance(value, str) else Unresolved(ast.unparse(node))
        return None

    def e_Await(self, node: ast.Await, frame: Frame) -> Any:
        return self.ev(node.value, frame)

    def e_Starred(self, node: ast.Starred, frame: Frame) -> Any:
        return self.ev(node.value, frame)

    def static_truth(self, test: ast.AST, frame: Frame) -> Optional[bool]:
        """A test the table of constructor arguments settles: a name holding a string, `not` of one, `x.endswith(y)`."""
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            inner = self.static_truth(test.operand, frame)
            return None if inner is None else not inner
        if isinstance(test, ast.Name):
            value = self.ev(test, frame)
            return bool(value) if isinstance(value, str) else None
        if isinstance(test, ast.Call) and isinstance(test.func, ast.Attribute) and test.func.attr == "endswith" \
                and len(test.args) == 1:
            subject, suffix = self.ev(test.func.value, frame), self.ev(test.args[0], frame)
            if isinstance(subject, str) and isinstance(suffix, str) and not isinstance(subject, Unresolved):
                return subject.endswith(suffix)
        return None

    def e_IfExp(self, node: ast.IfExp, frame: Frame) -> Any:
        truth = self.static_truth(node.test, frame)
        if truth is not None:
            return self.ev(node.body if truth else node.orelse, frame)
        self.ev(node.test, frame)
        return _alt([self.ev(node.body, frame), self.ev(node.orelse, frame)])

    def e_BoolOp(self, node: ast.BoolOp, frame: Frame) -> Any:
        return _alt(self.ev(v, frame) for v in node.values)

    def e_JoinedStr(self, node: ast.JoinedStr, frame: Frame) -> Any:
        parts = [v.value if isinstance(v, ast.Constant) else self.ev(v.value, frame) for v in node.values]
        if all(isinstance(p, str) for p in parts):
            return "".join(parts)
        return Unresolved(ast.unparse(node))

    def e_Lambda(self, node: ast.Lambda, frame: Frame) -> Any:
        return None

    def e_List(self, node: Any, frame: Frame) -> Any:
        out = Seq()
        for elt in node.elts:
            value = self.ev(elt, frame)
            if isinstance(elt, ast.Starred) and isinstance(value, Seq):
                out.extend(value)
            else:
                out.append(value)
        return out

    def e_Tuple(self, node: ast.Tuple, frame: Frame) -> Any:
        return tuple(self.e_List(node, frame))

    e_Set = e_List

    def e_comp(self, node: Any, frame: Frame) -> Any:
        inner = Frame(frame.module, frame.cls, dict(frame.env), frame.qualname, frame.depth, frame.returns)
        for gen in node.generators:
            items = self.ev(gen.iter, inner)
            seqs = [s for s in _alts(items) if isinstance(s, (Seq, tuple)) and not isinstance(s, Alt)]
            self.bind(gen.target, _alt(x for s in seqs for x in s) if seqs else None, inner)
            for cond in gen.ifs:
                self.ev(cond, inner)
        return Seq([self.ev(node.elt, inner)])

    e_ListComp = e_GeneratorExp = e_SetComp = e_comp

    def e_Call(self, node: ast.Call, frame: Frame) -> Any:
        func = node.func
        if isinstance(func, ast.Attribute):
            name = func.attr
            if name == "document_id":
                return "__name__"
            if name in ("collection", "collection_group"):
                self.ev(func.value, frame)
                label = self.ev(node.args[0], frame) if node.args else None
                for extra in node.args[1:]:
                    self.ev(extra, frame)
                return Coll(label if isinstance(label, str) else Unresolved(ast.unparse(node.args[0]) if node.args else ""),
                            name == "collection_group")
            if isinstance(func.value, ast.Name) and func.value.id in ("self", "cls"):
                target = frame.module.method(frame.cls, name)
                if target:
                    static = any(isinstance(d, ast.Name) and d.id == "staticmethod" for d in target.node.decorator_list)
                    return self.inline(target, node, frame, skip_self=not static)
            base = self.ev(func.value, frame)
            bases = [b for b in _alts(base) if isinstance(b, (Coll, Q))]
            if bases:
                return self.query_call(name, node, bases, frame)
            if name in ("append", "extend", "insert") and isinstance(base, Seq):
                for arg in node.args:
                    value = self.ev(arg, frame)
                    if name == "extend" and isinstance(value, Seq):
                        base.extend(value)
                    else:
                        base.append(value)
                return None
            return self.generic(node, frame)
        if isinstance(func, ast.Name):
            if func.id == "FieldFilter":
                return self.field_filter(node, frame)
            local = frame.env.get(func.id)
            if isinstance(local, Fn):
                return self.inline(local, node, frame, skip_self=False)
            if func.id in frame.module.funcs:
                return self.inline(Fn(frame.module.funcs[func.id], frame.module, None), node, frame, skip_self=False)
            if func.id in _TRANSPARENT:     # a builtin that only reads its arguments: a query in it is not consumed by it
                for arg in [*node.args, *[k.value for k in node.keywords]]:
                    self.ev(arg, frame)
                return None
        else:
            self.ev(func, frame)
        return self.generic(node, frame)

    def generic(self, node: ast.Call, frame: Frame) -> Any:
        for arg in [*node.args, *[k.value for k in node.keywords]]:
            self.consume(self.ev(arg, frame), frame)
        return None

    def field_filter(self, node: ast.Call, frame: Frame) -> Any:
        args = [self.ev(a, frame) for a in node.args]
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        field = args[0] if args else self.ev(kw.get("field_path"), frame)
        op = args[1] if len(args) > 1 else self.ev(kw.get("op_string"), frame)
        value_node = node.args[2] if len(node.args) > 2 else kw.get("value")
        choices: Tuple[str, ...] = ()
        if not isinstance(field, str) or isinstance(field, Unresolved):
            where = (frame.module.rel, getattr(node, "lineno", -1), getattr(node, "col_offset", -1))
            choices = FIELD_CHOICES.get((frame.module.rel, self.project.filter_calls.get(where, "")), ())
            field = Unresolved("<" + ast.unparse(node.args[0] if node.args else kw.get("field_path", node)) + ">")
        if not isinstance(op, str):
            op = Unresolved("?")
        none_value = isinstance(value_node, ast.Constant) and value_node.value is None
        if none_value and op in ("==", "!="):
            op = "IS_NULL" if op == "==" else "IS_NOT_NULL"
        if choices:
            return _alt(FF(choice, op) for choice in choices)
        return FF(field, op)

    def query_call(self, name: str, node: ast.Call, bases: List[Any], frame: Frame) -> Any:
        where = (frame.module.rel, node.lineno, node.col_offset)
        queries = [b if isinstance(b, Q) else Q(b, ()) for b in bases]
        if name in _CONSUMING:
            for arg in [*node.args, *[k.value for k in node.keywords]]:
                self.ev(arg, frame)
            if name in ("sum", "avg") and node.args:
                field = self.ev(node.args[0], frame)
                queries = [Q(q.coll, q.ops + (Op("aggregate", field if isinstance(field, str) else Unresolved("<field>"), "", where),))
                           for q in queries]
            for query in queries:
                self.project.record(query, self._owner(query, frame))
            return None
        if name in _SHAPING:
            for arg in [*node.args, *[k.value for k in node.keywords]]:
                self.ev(arg, frame)
            return _alt(queries)
        if name == "where":
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            if "filter" in kw:
                filters = _alts(self.ev(kw["filter"], frame))
            elif len(node.args) >= 3:
                field = self.ev(node.args[0], frame)
                filters = [self.field_filter(ast.Call(func=ast.Name("FieldFilter"), args=node.args[:3], keywords=[]), frame)]
                if not isinstance(field, str):
                    filters = [FF(Unresolved("<" + ast.unparse(node.args[0]) + ">"), filters[0].op)]
            else:
                filters = [None]
            ops = [Op("where", f.field, f.op, where) if isinstance(f, FF) else
                   Op("where", Unresolved("<" + ast.unparse(node) + ">"), Unresolved("?"), where) for f in filters]
            return _alt(Q(q.coll, q.ops + (op,)) for q in queries for op in ops)
        if name == "order_by":
            field = self.ev(node.args[0], frame) if node.args else None
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            direction = self.ev(kw["direction"], frame) if "direction" in kw else \
                (self.ev(node.args[1], frame) if len(node.args) > 1 else "ASCENDING")
            field = field if isinstance(field, str) else Unresolved("<" + ast.unparse(node.args[0] if node.args else node) + ">")
            direction = direction if isinstance(direction, str) else Unresolved("?")
            op = Op("order_by", field, direction, where)
            return _alt(Q(q.coll, q.ops + (op,)) for q in queries)
        if name == "find_nearest":
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            for arg in [*node.args, *kw.values()]:
                self.ev(arg, frame)
            field = self.ev(kw.get("vector_field"), frame)
            if isinstance(field, str):
                fields: Tuple[str, ...] = (field,)
            else:
                fields = VECTOR_FIELD_CHOICES.get((frame.module.rel, self.project.calls.get(where, "")), ())
                fields = fields or (Unresolved("<" + ast.unparse(kw.get("vector_field", node)) + ">"),)
            return _alt(Q(q.coll, q.ops + (Op("find_nearest", f, "", where),)) for q in queries for f in fields)
        for arg in [*node.args, *[k.value for k in node.keywords]]:
            self.ev(arg, frame)
        return None


def _load_project(sources: Dict[str, str], namespaces: Optional[Dict[str, Dict[str, Any]]] = None) -> Project:
    project = Project()
    for rel, text in sources.items():
        project.add(rel, text, (namespaces or {}).get(rel, {}))
    project.run()
    return project


def _real_sources() -> Tuple[Dict[str, str], Dict[str, Dict[str, Any]]]:
    sources, namespaces = {}, {}
    for path in sorted(ADAPTERS.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if path.name.startswith("in_memory_"):
            continue
        sources[rel] = path.read_text()
        namespaces[rel] = vars(importlib.import_module(rel[:-3].replace("/", ".")))
    return sources, namespaces


# --------------------------------------------------------------------------------------------- the verdict

def _tuples(site: Site) -> List[tuple]:
    """The query as H1 records it, with the one rule H1 does not know, pinned to its evidence
    (`_single_field_served` in `test_firestore_indexes_mirror.py`, Δ237): when every other filter is an equality, an `in`
    or an array filter and no field is ordered, the built-in single-field indexes serve a range filter on the document id
    (`__name__`), so that filter is left out. An ascending `order_by(__name__)` that is the last ordering is the implicit
    tie-break and is left out. Anything else (another range, another ordering) goes to H1 with the `__name__` in it."""
    others = [o for o in site.ops if o.kind == "where" and o.field != "__name__"]
    orders = [o for o in site.ops if o.kind == "order_by"]
    drop_filter = all(o.arg in _EQUALITY_OPS + _ARRAY_OPS for o in others) and all(o.field == "__name__" for o in orders)
    last_order = orders[-1] if orders else None
    out: List[tuple] = []
    for op in site.ops:
        if op.kind == "where":
            if op.field == "__name__" and drop_filter:
                continue
            out.append(("where", op.field, op.arg, None))
        elif op.kind == "order_by":
            if op is last_order and op.field == "__name__" and op.arg == "ASCENDING":
                continue
            out.append(("order_by", op.field, op.arg))
        elif op.kind == "find_nearest":
            out.append(("find_nearest", {"vector_field": op.field}))
    return out


def _exempt_fields() -> Set[Tuple[str, str]]:
    """(collection group, field) with a field override `"indexes": []`: the automatic single-field indexes are off, so a
    query on that field is served by a composite or not at all."""
    return {(i["collectionGroup"], i["fieldPath"]) for i in json.loads(INDEXES.read_text())
            if "fields" not in i and i.get("indexes") == []}


EXEMPT = _exempt_fields()


def _exempt_served(site: Site, tuples: List[tuple]) -> Optional[bool]:
    """None when no filtered or ordered field of the site is exempt; otherwise whether a declared composite carries all
    its fields: equality / array fields as the head, the orderings and range fields as the tail."""
    group = site.coll.name.rsplit("/", 1)[-1]
    if not any((group, t[1]) in EXEMPT for t in tuples if t[0] in ("where", "order_by")):
        return None
    wheres = [t for t in tuples if t[0] == "where"]
    orders = [(t[1], t[2]) for t in tuples if t[0] == "order_by"]
    head = {(f, "CONTAINS" if op in _ARRAY_OPS else "EQ") for _, f, op, _ in wheres if op in _EQUALITY_OPS + _ARRAY_OPS}
    ranges = sorted({f for _, f, op, _ in wheres if op in _RANGE_OPS} - {f for f, _ in orders})
    tail = orders + [(f, "ASCENDING") for f in ranges]
    declared = EvalDb(indexes=INDEXES).declared or []
    return any(g == group and sc == "COLLECTION" and _serves(fields, head, tail) for g, sc, fields in declared)


def _aggregate_served(site: Site, tuples: List[tuple]) -> bool:
    """A sum / avg reads index entries only, so its composite carries the aggregated field last (the drive index's
    `readable_chunk_total`, §12.5): the query's own head and tail, then the field."""
    field = next(op.field for op in site.ops if op.kind == "aggregate")
    required = _required_index(tuples)
    head, tail = required if required is not None else (
        {(op.field, "EQ") for op in site.ops if op.kind == "where" and op.field != "__name__"}, [])
    db = EvalDb(indexes=INDEXES)
    return any(g == site.coll.name.rsplit("/", 1)[-1] and sc == "COLLECTION" and _serves(fields, head, [*tail, (field, "ASCENDING")])
               for g, sc, fields in db.declared or [])


def classify(site: Site) -> str:
    """none (single-field indexes serve it) | unresolved | served | missing."""
    tuples = _tuples(site)
    aggregate = any(op.kind == "aggregate" for op in site.ops)
    required = _required_index(tuples)
    if any(op.kind == "where" and op.arg == "?" for op in site.ops):
        return "unresolved"       # a filter the walk could not read (a composite Or / And filter, a filter passed in)
    exempt = _exempt_served(site, tuples) if required is None and not site.coll.group and not aggregate else None
    if required is None and not site.coll.group and not aggregate and exempt is None:
        return "none"
    names = [op.field for op in site.ops] + [op.arg for op in site.ops if op.kind == "where" or op.arg in ("?",)]
    if isinstance(site.coll.name, Unresolved) or any(isinstance(n, Unresolved) for n in names):
        return "unresolved"
    if aggregate:
        return "served" if _aggregate_served(site, tuples) else "missing"
    if exempt is not None:
        return "served" if exempt else "missing"
    try:
        EvalDb(indexes=INDEXES)._check_index(site.coll.name, site.coll.group, tuples)
    except FailedPrecondition:
        return "missing"
    return "served"


_REAL = _real_sources()
PROJECT = _load_project(*_REAL)
SITES = sorted(PROJECT.sites.values(), key=lambda s: (s.file, s.line, s.shape))
VERDICTS = {(s.file, s.function, s.shape): classify(s) for s in SITES}
UNCOVERED = {loc: fn for loc, fn in PROJECT.calls.items() if loc not in PROJECT.covered}


def _by(verdict: str) -> List[Site]:
    return [s for s in SITES if VERDICTS[(s.file, s.function, s.shape)] == verdict]


def _label(site: Site) -> str:
    return f"{site.file}:{site.line} {site.function} [{site.shape}]"


# ------------------------------------------------------------------------------------------------- the tests

def test_the_walk_finds_the_queries_of_the_adapters():
    """A floor, so a broken walk cannot pass by finding nothing: the adapters send well over a hundred distinct queries,
    dozens of them composite."""
    assert len(SITES) > 100
    assert len(_by("served")) > 40
    assert any(op.kind == "find_nearest" for s in SITES for op in s.ops)


def test_every_builder_call_ends_in_a_consumed_query_or_is_listed():
    """A `where` / `order_by` / `find_nearest` the walk could not follow is a query nobody checked: list it with a reason."""
    seen = {(loc[0], fn, PROJECT.call_text[loc]) for loc, fn in UNCOVERED.items()}
    assert seen == set(UNRESOLVED_CALLS), (
        f"not followed and not listed: {sorted(seen - set(UNRESOLVED_CALLS))}; listed but followed (delete the line): "
        f"{sorted(set(UNRESOLVED_CALLS) - seen)}")


def test_queries_handed_to_calls_the_walk_cannot_enter_are_listed():
    """A query passed to a call the walk does not follow (a transaction read, an SDK helper) is checked as it stands, but
    what the callee adds to it is not seen: each such function is listed with a reason, and the list has no stale line."""
    assert PROJECT.opaque == set(OPAQUE_CONSUMPTIONS), (
        f"handed to an opaque call, not listed: {sorted(PROJECT.opaque - set(OPAQUE_CONSUMPTIONS))}; "
        f"listed but not opaque (delete the line): {sorted(set(OPAQUE_CONSUMPTIONS) - PROJECT.opaque)}")


def test_the_filter_tables_of_unread_count_queries_are_equality_only():
    """`unread_count_queries` builds its filters from (field, operator, value) tables the walk cannot read; the queries
    are equality-only (plus `kind in`) exactly while every operator in those tables is `==`."""
    module = next(m for m in PROJECT.modules if m.rel == "src/adapters/firestore_drive_index_repo.py")
    node = module.method("FirestoreDriveIndexRepository", "unread_count_queries").node
    triples = [t for t in ast.walk(node) if isinstance(t, ast.Tuple) and len(t.elts) == 3
               and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in t.elts[:2])]
    assert triples and {t.elts[1].value for t in triples} == {"=="}


def test_the_enrich_queue_is_checked_for_every_job():
    fields = {op.field for s in SITES if s.function.endswith("enrich_queue") for op in s.ops if op.kind == "where"}
    assert {f"{job.value}_state" for job in EnrichJob} <= fields


def test_every_listed_reason_is_a_sentence():
    for table in (UNRESOLVED_CALLS, UNRESOLVED_SITES, DOCUMENTED_UNINDEXED, KNOWN_MISSING, OPAQUE_CONSUMPTIONS):
        assert all(len(reason) > 20 for reason in table.values())


def test_unresolvable_composites_are_listed_and_the_list_has_no_stale_line():
    seen = {(s.file, s.function) for s in _by("unresolved")}
    assert seen == set(UNRESOLVED_SITES), (
        f"a composite query with a name the walk cannot read, not listed: {sorted(seen - set(UNRESOLVED_SITES))}; "
        f"listed but resolved (delete the line): {sorted(set(UNRESOLVED_SITES) - seen)}")


def test_the_documented_unindexed_queries_are_still_unindexed():
    """A query documented as having no index on purpose: when an index appears the doc line is stale and goes."""
    missing = {(s.file, s.function, op.field) for s in _by("missing") for op in s.ops if op.kind == "where"}
    assert set(DOCUMENTED_UNINDEXED) <= missing, (
        f"documented as unindexed but an index serves it now: {sorted(set(DOCUMENTED_UNINDEXED) - missing)}")


def _documented(site: Site) -> bool:
    return VERDICTS[(site.file, site.function, site.shape)] == "missing" and any(
        (site.file, site.function, op.field) in DOCUMENTED_UNINDEXED for op in site.ops if op.kind == "where")


def _param(site: Site) -> Any:
    key = (site.file, site.function, site.shape)
    mark = []
    if key in KNOWN_MISSING:
        mark = [pytest.mark.xfail(strict=True, reason=KNOWN_MISSING[key])]
    return pytest.param(site, marks=mark, id=_label(site))


@pytest.mark.parametrize("site", [_param(s) for s in SITES
                                  if VERDICTS[(s.file, s.function, s.shape)] in ("served", "missing")
                                  and not _documented(s)])
def test_a_query_that_needs_a_composite_index_has_one(site):
    """Equality fields first, then the range / ordering field; a vector query its vector index (H1's rule set)."""
    assert classify(site) == "served", (
        f"{_label(site)}: no index in config/firestore.indexes.json serves it. Declare the index (the owner deploys it) "
        f"or, if it is missing on purpose, document that in the adapter and list it in DOCUMENTED_UNINDEXED.")


def test_known_missing_entries_name_a_live_defect():
    live = {(s.file, s.function, s.shape) for s in _by("missing")}
    assert set(KNOWN_MISSING) <= live, f"stale KNOWN_MISSING lines: {sorted(set(KNOWN_MISSING) - live)}"
    assert all(re.match(r"P12-D\d+: \S+\.py:\d+", reason) for reason in KNOWN_MISSING.values())


# ------------------------------------------------------------------------- the walk itself, on small sources

_SNIPPET_HEAD = '''
class Repo:
    def __init__(self, db, env_config):
        self._col = db.collection("{name}")
'''


def _snippet(body: str, name: str = "development_things") -> Project:
    return _load_project({"src/adapters/snippet.py": _SNIPPET_HEAD.format(name=name) + body})


def _verdicts(project: Project) -> Dict[str, str]:
    return {s.shape: classify(s) for s in project.sites.values()}


def test_walk_flags_equality_plus_ordering_without_an_index():
    project = _snippet('''
    async def read(self, user_id):
        return await self._col.where(filter=FieldFilter("user_id", "==", user_id)).order_by("nothing_declared").get()
''')
    assert list(_verdicts(project).values()) == ["missing"]


def test_walk_joins_branches_and_follows_helpers_and_loops():
    project = _snippet('''
    def _base(self, user_id):
        return self._col.where(filter=FieldFilter("user_id", "==", user_id))

    async def read(self, user_id, kind):
        query = self._base(user_id)
        if kind:
            query = query.where(filter=FieldFilter("kind", "==", kind))
        queries = []
        for part in (1, 2):
            queries.append(query.order_by("nothing_declared_either"))
        for q in queries:
            await q.get()
''')
    shapes = _verdicts(project)
    assert len(shapes) == 2 and set(shapes.values()) == {"missing"}
    assert not [loc for loc in project.calls if loc not in project.covered]


def test_walk_needs_no_index_for_equality_only_and_for_a_single_range():
    project = _snippet('''
    async def read(self, user_id):
        await self._col.where(filter=FieldFilter("a", "==", 1)).where(filter=FieldFilter("b", "==", 2)).get()
        await self._col.where(filter=FieldFilter("a", ">", 1)).get()
        await self._col.order_by("a").get()
''')
    assert set(_verdicts(project).values()) == {"none"}


def test_walk_reports_a_name_it_cannot_read_instead_of_guessing():
    project = _snippet('''
    async def read(self, field):
        await self._col.where(filter=FieldFilter("a", "==", 1)).where(filter=FieldFilter("b", ">", 1)).order_by(field).get()
''')
    assert list(_verdicts(project).values()) == ["unresolved"]


def test_walk_leaves_a_builder_of_an_unreachable_query_uncovered():
    project = _snippet('''
    async def read(self, query):
        return query.where(filter=FieldFilter("a", "==", 1))
''')
    assert [loc for loc in project.calls if loc not in project.covered]


def test_walk_requires_a_composite_for_a_field_with_its_single_field_index_off():
    """`development_domain_email_facts_v1.name_keys` carries a field override `"indexes": []`: equality + array-contains
    would merge single-field indexes on any other field, but not on that one."""
    assert ("development_domain_email_facts_v1", "name_keys") in EXEMPT
    project = _snippet('''
    async def read(self, user_id, key):
        await self._col.where(filter=FieldFilter("user_id", "==", user_id)).where(filter=FieldFilter("name_keys", "array_contains", key)).get()
''', name="development_domain_email_facts_v1")
    assert list(_verdicts(project).values()) == ["missing"]
    control = _snippet('''
    async def read(self, user_id, key):
        await self._col.where(filter=FieldFilter("user_id", "==", user_id)).where(filter=FieldFilter("source", "==", key)).get()
''', name="development_domain_email_facts_v1")
    assert list(_verdicts(control).values()) == ["none"]


def test_walk_keeps_a_document_id_filter_unless_every_other_filter_is_an_equality():
    served = _snippet('''
    async def read(self, user_id, after):
        await self._col.where(filter=FieldFilter("user_id", "==", user_id)).where(filter=FieldFilter(FieldPath.document_id(), ">", after)).get()
''')
    assert _verdicts(served) and set(_verdicts(served).values()) == {"none"}
    for body in (
        'await self._col.where(filter=FieldFilter("size", ">", 1)).where(filter=FieldFilter(FieldPath.document_id(), ">", a)).get()',
        'await self._col.where(filter=FieldFilter(FieldPath.document_id(), ">", a)).order_by("created_at").get()',
    ):
        project = _snippet("    async def read(self, a):\n        " + body + "\n")
        site = next(iter(project.sites.values()))
        assert any(t[1] == "__name__" for t in _tuples(site)), body
        assert classify(site) == "missing", body


def test_walk_keeps_an_ascending_document_id_ordering_unless_it_is_the_last():
    project = _snippet('''
    async def read(self, a):
        await self._col.where(filter=FieldFilter("a", "==", 1)).order_by(FieldPath.document_id()).order_by("created_at").get()
''')
    site = next(iter(project.sites.values()))
    assert ("order_by", "__name__", "ASCENDING") in _tuples(site)


def test_walk_records_a_query_handed_to_a_call_it_cannot_enter():
    project = _snippet('''
    async def read(self, tx):
        await tx.get(self._col.where(filter=FieldFilter("a", "==", 1)).order_by("b"))
''')
    assert project.opaque == {("src/adapters/snippet.py", "Repo.read")}


def test_an_unfollowed_call_is_listed_by_its_text():
    project = _snippet('''
    async def read(self, query):
        return query.where(filter=FieldFilter("a", "==", 1))
''')
    loc = next(l for l in project.calls if l not in project.covered)
    assert project.call_text[loc] == "where(filter=FieldFilter('a', '==', 1))"
