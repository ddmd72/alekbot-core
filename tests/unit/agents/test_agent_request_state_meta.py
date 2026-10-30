"""No agent writes shared state on the request path unless the write is listed with a reason (P15 card J3).

Agent instances are per-user singletons that serve concurrent executions (`DelegationEngine` gathers a tool batch), so a
write to `self.*` outside the constructor is state shared by every request the instance is serving at that moment. The
3.6x billing inflation was such a write: a per-instance token accumulator on `BaseAgent`, read by each concurrent
execution as its own total (fixed 2026-07-28, `docs/04_solution_strategy/decisions/billing_execution_scoped_ledger.md`). This check reads
the source of every agent class, present and future, and fails on a write nobody has explained.

Which classes: `BaseAgent` from `src/agents/base_agent.py` (scanned too, the accumulator lived there) and every class
whose bases name an agent class, transitively (a base is an `ast.Name.id` or the `.attr` of an `ast.Attribute`). Any
other base of an agent class must be `ABC`, `Generic[...]`, `Protocol`, `object` or a class defined under `src/ports/`;
anything else is an error, because a mixin's methods would write agent state this check does not read. A base name
defined twice with different agent-ness is an error too.

What is a write, in every method but `__init__` (nested functions and lambdas included), and in every function or
lambda defined inside `__init__` (it runs later, on the request path; its writes are keyed under `__init__`):
- an assignment, augmented or annotated assignment, `del`, or `for`/`with`/comprehension target rooted at `self`
  (`self.a = …`, `self.a += …`, `self.a[k] = …`, `self.a.b = …`, also inside tuple or list unpacking), at `cls` or at
  `type(self)` (class state, key `<cls>.a`), or at a module-level name of the agent's module that the function does not
  bind locally (`_CACHE[k] = …`, `_CACHE.a = …`, key `<module>._CACHE`); a plain `NAME = …` only under `global NAME`; a
  name bound only by a plain `import` is a module, not state, so it counts only with an attribute after it
  (`os.environ[k] = …`, key `<module>.os.environ`);
- a call `<root>.<path>.<m>(…)` on the same roots with `m` a mutator name (`MUTATORS`; the path may hold subscripts, not
  calls; for `self`, `cls` and an imported module it names at least one attribute, so `os.remove(p)` is not a write
  and `os.environ.pop(k)` is; any other module-level name may be called directly: `_CACHE.append(x)`);
- a `global NAME` statement (key `<module>.NAME`);
- `setattr`/`delattr(self, …)` and `object.__setattr__`/`__delattr__(self, …)`.
The key of a write is the dotted attribute path from the root up to the first subscript or call.

The allowlist (`agent_instance_writes_allowlist.txt`) has one line per (file, class, method, key) with a reason and at
least two words of evidence. Reasons: `constructor-only` (checked: every `*.<method>(` call in `src/` sits in a function
named `__init__`), `lazy-cache` (checked: every write of the key in the method sits in the body of
`if <root>.<key> is None:` in the same function), `cross-request-by-design`, `port-call`, `dead-write`, `defect:<id>`.
A lock is never a reason alone: it stops a torn write, not state that spans requests (a lock around the accumulators
was the rejected fix for the 3.6x class, `decisions/billing_execution_scoped_ledger.md`), so `lock-guarded` comes only
as `cross-request-by-design+lock-guarded` (the evidence names the consumer that outlives the request) or
`lazy-cache+lock-guarded`; then every write of the key in the method must also sit lexically inside
`async with self.<name>:` in the same function, where `lock` is a whole `_`-separated part of `<name>` (`lock`,
`_lock`, `_system_lock`; `_session`, `_clock` and `_blocker` are not locks). The check fails both ways: a write with no line, and a line whose write
is gone.

Known limits: a write through a local alias (`buf = self.buffer; buf.append(x)`) is not seen; a bound mutator handed
out as a callback (`task.add_done_callback(self._tasks.discard)`) is not seen; a mutator call on the result of a call
(`self._cache.get(k).append(x)`) is not seen; a base imported under another name (`import X as Y`) is not resolved; a
call of a `constructor-only` method through `getattr` is not seen; a module imported inside the function
(`import x; x.CACHE[k] = v`) is a local name, so its state is not seen; a local named `cls` in an ordinary method reads
as class state (loud, not silent). To stay fast, a file is parsed only when a `class` header in it (read up to its colon,
so `class X(make_base(), BaseAgent):` is seen) defines an agent name found so far or names one before that colon
(subclassing needs the name; a superset, a match in a docstring only costs a parse), or defines a non-agent base of an
agent class (to tell whether it is a port), or its text has a `.<method>(` call of a `constructor-only` method.
"""
import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, Iterator, List, Optional, Set, Tuple

REPO = Path(__file__).resolve().parents[3]
ALLOWLIST = Path(__file__).with_name("agent_instance_writes_allowlist.txt")
ALLOWLIST_NAME = "tests/unit/agents/agent_instance_writes_allowlist.txt"
BASE_AGENT_PATH = "src/agents/base_agent.py"
PORTS_PREFIX = "src/ports/"

MUTATORS = frozenset({"append", "extend", "insert", "add", "update", "setdefault", "pop", "popitem", "remove", "discard",
                      "clear"})
OTHER_BASES = frozenset({"ABC", "Generic", "Protocol", "object"})
SCOPE_REASONS = frozenset({"constructor-only", "lazy-cache", "cross-request-by-design", "port-call", "dead-write"})
LOCKABLE = frozenset({"lazy-cache", "cross-request-by-design"})
LOCK = "lock-guarded"
REASONS = sorted(SCOPE_REASONS | {f"{reason}+{LOCK}" for reason in LOCKABLE})
DEFECT_REASON = re.compile(r"^defect:[A-Z][A-Z0-9]*-D\d+$")
CLASS_HEADER = re.compile(r"^[ \t]*class[ \t]+(\w+)([^:]*):", re.M)
CLS = "<cls>"
MODULE = "<module>"


@dataclass(frozen=True)
class ClassDef:
    path: str
    name: str
    line: int
    node: ast.ClassDef


@dataclass(frozen=True)
class Write:
    path: str
    cls: str
    method: str
    key: str
    line: int
    locked: bool
    lazy: bool

    @property
    def entry(self) -> str:
        return f"{self.path}::{self.cls}.{self.method}::{self.key}"

    @property
    def expr(self) -> str:
        """The written expression's root and path as code: `self.a`, `cls.a`, `_CACHE`."""
        if self.key.startswith(MODULE + "."):
            return self.key[len(MODULE) + 1:]
        if self.key.startswith(CLS + "."):
            return "cls." + self.key[len(CLS) + 1:]
        return f"self.{self.key}"

    @property
    def shown(self) -> str:
        if self.key.startswith(MODULE + "."):
            return f"the module-level name {self.key[len(MODULE) + 1:]}"
        if self.key.startswith(CLS + "."):
            return f"class state {self.key[len(CLS) + 1:]}"
        return f"self.{self.key}"


@dataclass(frozen=True)
class Listed:
    reason: str
    line: int

    @property
    def scope(self) -> str:
        return self.reason.split("+")[0]

    @property
    def lock_guarded(self) -> bool:
        return self.reason.endswith("+" + LOCK)


# ----------------------------------------------------------------------------- reading an expression


def is_self(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == "self"


def _is_type_of_self(node: ast.expr) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "type" \
        and len(node.args) == 1 and is_self(node.args[0])


def base_name(node: ast.expr) -> Optional[str]:
    """`Foo` -> Foo, `mod.Foo` -> Foo, `Generic[T]` -> Generic; None for anything else."""
    if isinstance(node, ast.Subscript):
        node = node.value
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _local_names(function: ast.AST) -> Tuple[Set[str], Set[str]]:
    """(names the function binds locally, names it declares `global`); nested scopes are not entered."""
    args = function.args
    names = {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs}
    names |= {a.arg for a in (args.vararg, args.kwarg) if a is not None}
    declared: Set[str] = set()
    outer: Set[str] = set()
    stack: List[ast.AST] = [function.body] if isinstance(function, ast.Lambda) else list(function.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            continue
        if isinstance(node, ast.Lambda):
            continue
        if isinstance(node, ast.Global):
            declared |= set(node.names)
        elif isinstance(node, ast.Nonlocal):
            outer |= set(node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names |= {alias.asname or alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        stack.extend(ast.iter_child_nodes(node))
    return names - declared - outer, declared


def module_names(tree: ast.Module) -> Tuple[FrozenSet[str], FrozenSet[str]]:
    """(names bound at module level: assignments, imports, classes, functions, not inside a function or a class;
    the subset bound only by a plain `import`, which name a module rather than state)."""
    names: Set[str] = set()
    imported: Set[str] = set()
    stack: List[ast.stmt] = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            continue
        if isinstance(node, ast.Import):
            imported |= {alias.asname or alias.name.split(".")[0] for alias in node.names}
            continue
        if isinstance(node, ast.ImportFrom):
            names |= {alias.asname or alias.name.split(".")[0] for alias in node.names}
            continue
        targets = node.targets if isinstance(node, ast.Assign) else \
            [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else []
        for target in targets:
            names |= {n.id for n in ast.walk(target) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        for field in ("body", "orelse", "finalbody"):
            stack.extend(getattr(node, field, None) or [])
        for handler in getattr(node, "handlers", None) or []:
            stack.extend(handler.body)
    return frozenset(names | imported), frozenset(imported - names)


# ----------------------------------------------------------------------------- writes inside one method


@dataclass
class _Scope:
    node: ast.AST
    locks: int = 0
    lazy: Tuple[str, ...] = ()
    _bound: Optional[Tuple[Set[str], Set[str]]] = None

    def _names(self) -> Tuple[Set[str], Set[str]]:
        if self._bound is None:  # computed on demand: most functions never touch a module-level name
            self._bound = _local_names(self.node)
        return self._bound

    @property
    def locals(self) -> Set[str]:
        return self._names()[0]

    @property
    def globals(self) -> Set[str]:
        return self._names()[1]


class _WriteFinder(ast.NodeVisitor):
    """Collects `(key, line, locked, lazy)` for every write in one method: `locked` = inside `async with self.<name>:`,
    `lazy` = inside `if <root>.<key> is None:`, both in the innermost enclosing function."""

    def __init__(self, module_level: FrozenSet[str], imported: FrozenSet[str] = frozenset(),
                 enclosing: Optional[ast.AST] = None) -> None:
        self.found: List[Tuple[str, int, bool, bool]] = []
        self._module = module_level
        self._imported = imported
        # `enclosing` is the `__init__` whose nested functions are visited: its locals shadow module names
        self._scopes: List[_Scope] = [_Scope(enclosing)] if enclosing is not None else []

    # --- keys

    def _root(self, node: ast.expr) -> Optional[str]:
        """The key prefix of a chain's root: "" for self, `<cls>.` for cls / type(self), `<module>.` for a module-level
        name no enclosing function binds locally; None for anything else."""
        if is_self(node):
            return ""
        if _is_type_of_self(node) or (isinstance(node, ast.Name) and node.id == "cls"):
            return CLS + "."
        if isinstance(node, ast.Name) and node.id in self._module \
                and not any(node.id in scope.locals for scope in self._scopes):
            return MODULE + "."
        return None

    def key(self, node: ast.expr, allow_calls: bool = True) -> Optional[str]:
        """The key of a store or mutator target: the root's prefix plus the dotted path up to the first subscript or
        call. None when the chain has no known root, holds a call with `allow_calls=False`, or names no attribute of
        `self` / `cls`."""
        parts: List[Optional[str]] = []
        while True:
            prefix = self._root(node)
            if prefix is not None:
                break
            if isinstance(node, ast.Attribute):
                parts.append(node.attr)
                node = node.value
            elif isinstance(node, ast.Subscript):
                parts.append(None)
                node = node.value
            elif isinstance(node, ast.Call) and allow_calls:
                parts.append(None)
                node = node.func
            else:
                return None
        path: List[str] = []
        for part in reversed(parts):
            if part is None:
                break
            path.append(part)
        if prefix == MODULE + ".":
            if node.id in self._imported and not path:
                return None  # `os.remove(p)`: a call on a module, not on state the module holds
            return f"{prefix}{'.'.join([node.id] + path)}"
        return f"{prefix}{'.'.join(path)}" if path else None

    # --- recording

    def _record(self, key: str, line: int) -> None:
        scope = self._scopes[-1] if self._scopes else None
        self.found.append((key, line, bool(scope and scope.locks), bool(scope and key in scope.lazy)))

    def _scope(self, node: ast.AST) -> None:
        self._scopes.append(_Scope(node))
        self.generic_visit(node)
        self._scopes.pop()

    visit_FunctionDef = _scope
    visit_AsyncFunctionDef = _scope
    visit_Lambda = _scope

    def visit_Global(self, node: ast.Global) -> None:
        for name in node.names:
            self._record(f"{MODULE}.{name}", node.lineno)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return  # a class defined inside a method has its own `self`

    def _target(self, target: ast.expr) -> None:
        if isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._target(element)
            return
        if isinstance(target, ast.Starred):
            self._target(target.value)
            return
        if isinstance(target, ast.Name):
            if target.id in self._module and self._scopes and target.id in self._scopes[-1].globals:
                self._record(f"{MODULE}.{target.id}", target.lineno)
            return
        key = self.key(target)
        if key is not None:
            self._record(key, target.lineno)
        self.generic_visit(target)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._target(target)
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._target(node.target)
        if node.value is not None:
            self.visit(node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._target(node.target)
        self.visit(node.value)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self._target(target)

    def _loop(self, node: ast.AST) -> None:
        self._target(node.target)
        for child in ast.iter_child_nodes(node):
            if child is not node.target:
                self.visit(child)

    visit_For = _loop
    visit_AsyncFor = _loop
    visit_comprehension = _loop

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        test = node.test
        guard = None
        if isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Is) \
                and isinstance(test.comparators[0], ast.Constant) and test.comparators[0].value is None:
            guard = self.key(test.left)
        scope = self._scopes[-1] if self._scopes else None
        if guard is not None and scope is not None:
            scope.lazy += (guard,)
        for statement in node.body:
            self.visit(statement)
        if guard is not None and scope is not None:
            scope.lazy = scope.lazy[:-1]
        for statement in node.orelse:
            self.visit(statement)

    def _with(self, node: ast.AST, is_async: bool) -> None:
        guarded = False
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._target(item.optional_vars)
            context = item.context_expr
            guarded |= is_async and isinstance(context, ast.Attribute) and is_self(context.value) \
                and "lock" in context.attr.lower().split("_")
        scope = self._scopes[-1] if self._scopes else None
        if guarded and scope is not None:
            scope.locks += 1
        for statement in node.body:
            self.visit(statement)
        if guarded and scope is not None:
            scope.locks -= 1

    def visit_With(self, node: ast.With) -> None:
        self._with(node, is_async=False)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self._with(node, is_async=True)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in MUTATORS:
            key = self.key(func.value, allow_calls=False)
            if key is not None:
                self._record(key, node.lineno)
        reflective = (isinstance(func, ast.Name) and func.id in ("setattr", "delattr")) or (
            isinstance(func, ast.Attribute) and func.attr in ("__setattr__", "__delattr__")
            and isinstance(func.value, ast.Name) and func.value.id == "object")
        if reflective and node.args and is_self(node.args[0]):
            name = node.args[1] if len(node.args) > 1 else None
            key = name.value if isinstance(name, ast.Constant) and isinstance(name.value, str) else "<dynamic>"
            self._record(key, node.lineno)
        self.generic_visit(node)


def _functions_inside(function: ast.AST) -> List[ast.AST]:
    """The outermost functions and lambdas defined inside `function` (their own nested ones are visited with them)."""
    found: List[ast.AST] = []
    stack = list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            found.append(node)
        elif not isinstance(node, ast.ClassDef):
            stack.extend(ast.iter_child_nodes(node))
    return sorted(found, key=lambda n: (n.lineno, n.col_offset))


def writes_of(cls: ClassDef, module_level: FrozenSet[str], imported: FrozenSet[str] = frozenset()) -> List[Write]:
    found: List[Write] = []
    for method in cls.node.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if method.name == "__init__":
            finders = []
            for nested in _functions_inside(method):  # defined in the constructor, run later on the request path
                finder = _WriteFinder(module_level, imported, enclosing=method)
                finder.visit(nested)
                finders.append(finder)
        else:
            finder = _WriteFinder(module_level, imported)
            finder.visit(method)
            finders = [finder]
        found += [Write(cls.path, cls.name, method.name, key, line, locked, lazy)
                  for finder in finders for key, line, locked, lazy in finder.found]
    return found

# ----------------------------------------------------------------------------- the source tree


class Scan:
    """The agent classes and their writes in `sources` (`{repo-relative path: source}`)."""

    def __init__(self, sources: Dict[str, str]) -> None:
        self.sources = sources
        self.trees: Dict[str, ast.Module] = {}
        self._classes: Dict[str, List[ClassDef]] = {}
        self._headers: Dict[str, Tuple[Set[str], Set[str]]] = {}
        for path, text in sources.items():
            headers = CLASS_HEADER.findall(text)
            if headers:
                self._headers[path] = ({name for name, _ in headers},
                                       {word for _, bases in headers for word in re.findall(r"\w+", bases)})
        self.errors: List[str] = []
        self.agents: List[ClassDef] = []
        self._resolve()

    def tree(self, path: str) -> ast.Module:
        if path not in self.trees:
            self.trees[path] = ast.parse(self.sources[path], path)
            self._classes[path] = [ClassDef(path, node.name, node.lineno, node)
                                   for node in _statements(self.trees[path].body) if isinstance(node, ast.ClassDef)]
        return self.trees[path]

    def classes(self) -> Iterator[ClassDef]:
        for path in sorted(self._classes):
            yield from self._classes[path]

    def _resolve(self) -> None:
        if BASE_AGENT_PATH not in self.sources:
            self.errors.append(f"{BASE_AGENT_PATH} is missing: the scan has no root")
            return
        self.tree(BASE_AGENT_PATH)
        known: Set[str] = set()
        frontier = {"BaseAgent"}
        while frontier:
            for path, (defined, named) in self._headers.items():
                if path not in self.trees and (defined & frontier or named & frontier):
                    self.tree(path)
            agents = self._agent_classes(list(self.classes()))
            names = {c.name for c in agents}
            frontier = names - known
            known |= names
        self.agents = agents
        self._check_bases(list(self.classes()), agents)

    @staticmethod
    def _agent_classes(classes: List[ClassDef]) -> List[ClassDef]:
        by_name: Dict[str, List[ClassDef]] = {}
        for c in classes:
            by_name.setdefault(c.name, []).append(c)
        agents = {id(c.node): c for c in classes if c.name == "BaseAgent" and c.path == BASE_AGENT_PATH}
        changed = True
        while changed:
            changed = False
            for c in classes:
                if id(c.node) in agents:
                    continue
                if any(id(d.node) in agents for b in c.node.bases for d in by_name.get(base_name(b) or "", [])):
                    agents[id(c.node)] = c
                    changed = True
        return sorted(agents.values(), key=lambda c: (c.path, c.line))

    def _check_bases(self, classes: List[ClassDef], agents: List[ClassDef]) -> None:
        agent_ids = {id(c.node) for c in agents}
        by_name: Dict[str, List[ClassDef]] = {}
        for c in classes:
            by_name.setdefault(c.name, []).append(c)
        used = {base_name(b) for c in classes for b in c.node.bases} - {None}
        for name in sorted(n for n in used if n is not None):
            defs = by_name.get(name, [])
            if len({id(d.node) in agent_ids for d in defs}) > 1:
                where = ", ".join(f"{d.path}:{d.line} ({'agent' if id(d.node) in agent_ids else 'not an agent'})"
                                  for d in defs)
                self.errors.append(f"base name {name} is defined {len(defs)} times in src/ with different agent-ness: "
                                   f"{where}; rename one so the scan can tell which class a subclass extends")
        agent_names = {c.name for c in agents}
        for c in agents:
            for b in c.node.bases:
                name = base_name(b)
                if name in agent_names or name in OTHER_BASES:
                    continue
                defs = self._definitions(name) if name else []
                if defs and all(d.path.startswith(PORTS_PREFIX) for d in defs):
                    continue
                self.errors.append(f"{c.path}:{c.line}: {c.name} has the base {ast.unparse(b)}, which is not an agent "
                                   f"class, ABC, Generic, Protocol, object or a class under src/ports/: its methods would "
                                   f"write agent state this check does not read")

    def _definitions(self, name: str) -> List[ClassDef]:
        """Every class named `name` in the sources (files with a matching `class` header are parsed on demand)."""
        paths = [path for path, (defined, _) in sorted(self._headers.items()) if name in defined]
        for path in paths:
            self.tree(path)
        return [c for path in paths for c in self._classes[path] if c.name == name]

    def writes(self) -> List[Write]:
        modules = {path: module_names(self.trees[path]) for path in {c.path for c in self.agents}}
        return [w for c in self.agents for w in writes_of(c, *modules[c.path])]

    def calls_outside_init(self, method: str) -> List[str]:
        """`path:line` of every `*.<method>(` call whose innermost enclosing function is not named `__init__`."""
        outside: List[str] = []
        called = re.compile(rf"\.[ \t]*{re.escape(method)}[ \t]*\(")
        for path, text in sorted(self.sources.items()):
            if not called.search(text):
                continue
            for call, function in _calls_with_function(self.tree(path)):
                if isinstance(call.func, ast.Attribute) and call.func.attr == method and function != "__init__":
                    outside.append(f"{path}:{call.lineno}")
        return outside


def _statements(body: List[ast.stmt]) -> Iterator[ast.stmt]:
    """Every statement, nested ones included (bodies of classes, functions, branches, loops, `try` and `with`), without
    walking into expressions: a class can only be defined by a statement."""
    for statement in body:
        yield statement
        for field in ("body", "orelse", "finalbody"):
            yield from _statements(getattr(statement, field, None) or [])
        for handler in getattr(statement, "handlers", None) or []:
            yield from _statements(handler.body)
        for case in getattr(statement, "cases", None) or []:
            yield from _statements(case.body)


def _calls_with_function(tree: ast.AST, function: Optional[str] = None) -> Iterator[Tuple[ast.Call, Optional[str]]]:
    for child in ast.iter_child_nodes(tree):
        inner = function
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            inner = child.name
        elif isinstance(child, ast.Lambda):
            inner = "<lambda>"
        if isinstance(child, ast.Call):
            yield child, function
        yield from _calls_with_function(child, inner)


# ----------------------------------------------------------------------------- the allowlist


def _reason_error(reason: str) -> Optional[str]:
    if reason in SCOPE_REASONS or DEFECT_REASON.match(reason):
        return None
    scope, plus, lock = reason.partition("+")
    if plus and lock == LOCK and scope in LOCKABLE:
        return None
    if LOCK in reason:
        allowed = " or ".join(f"{scope}+{LOCK}" for scope in sorted(LOCKABLE))
        return (f"{reason!r}: a lock is never a reason alone, it stops a torn write, not state shared across requests "
                f"(decisions/billing_execution_scoped_ledger.md); use {allowed}")
    return f"unknown reason {reason!r}; use one of {', '.join(REASONS)} or defect:<id>"


def parse_allowlist(text: str) -> Tuple[Dict[str, Listed], List[str]]:
    entries: Dict[str, Listed] = {}
    errors: List[str] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        where = f"{ALLOWLIST_NAME}:{number}"
        key, _, comment = line.partition("#")
        key = key.strip()
        words = comment.split()
        if key.count("::") != 2 or "." not in key.split("::")[1]:
            errors.append(f"{where}: '{key}' is not path::Class.method::key")
            continue
        refused = _reason_error(words[0]) if words else "no reason"
        if refused:
            errors.append(f"{where}: {refused}")
            continue
        if len(words) < 3:
            errors.append(f"{where}: {words[0]} needs evidence of at least two words")
            continue
        if key in entries:
            errors.append(f"{where}: {key} is listed twice (first at line {entries[key].line})")
            continue
        entries[key] = Listed(words[0], number)
    return entries, errors


def problems(sources: Dict[str, str], allowlist_text: str) -> List[str]:
    scan = Scan(sources)
    entries, result = parse_allowlist(allowlist_text)
    result = scan.errors + result
    by_entry: Dict[str, List[Write]] = {}
    for w in scan.writes():
        by_entry.setdefault(w.entry, []).append(w)
    for entry, writes in sorted(by_entry.items()):
        first = writes[0]
        if entry not in entries:
            where = ("(closure) writes {} on the request path" if first.method == "__init__"
                     else "writes {} outside __init__").format(first.shown)
            result.append(f"{first.path}:{first.line}: {first.cls}.{first.method} {where}, "
                          f"state shared by every request the agent instance serves; keep it per execution "
                          f"(a local or a ContextVar) or add '{entry}  # <reason> <evidence>' to {ALLOWLIST_NAME}")
            continue
        listed = entries[entry]
        at = f"({ALLOWLIST_NAME}:{listed.line})"
        if listed.lock_guarded:
            result += [f"{w.path}:{w.line}: {entry} is listed {listed.reason} {at} but this write is outside "
                       f"'async with self.<lock>:' in its function" for w in writes if not w.locked]
        if listed.scope == "lazy-cache":
            result += [f"{w.path}:{w.line}: {entry} is listed {listed.reason} {at} but this write is not inside "
                       f"'if {w.expr} is None:' in its function" for w in writes if not w.lazy]
        if listed.scope == "constructor-only":
            result += [f"{site}: {first.method} is called outside __init__, but {entry} is listed constructor-only "
                       f"{at}" for site in scan.calls_outside_init(first.method)]
    result += [f"{ALLOWLIST_NAME}:{listed.line}: stale line, no agent write matches {entry}; remove it"
               for entry, listed in sorted(entries.items(), key=lambda kv: kv[1].line) if entry not in by_entry]
    return result


def src_sources() -> Dict[str, str]:
    return {p.relative_to(REPO).as_posix(): p.read_text(encoding="utf-8") for p in sorted((REPO / "src").rglob("*.py"))}


# ----------------------------------------------------------------------------- the real tree


def test_no_agent_writes_shared_state_on_the_request_path_without_a_listed_reason():
    sources = src_sources()
    scan = Scan(sources)
    names = {c.name for c in scan.agents}

    assert len(scan.agents) >= 30 and {"BaseAgent", "EmailClassificationAgent", "SmartResponseAgent"} <= names, \
        f"the scan found only {sorted(names)}: the discovery is broken"
    found = problems(sources, ALLOWLIST.read_text(encoding="utf-8"))
    assert not found, "\n".join(found)


# ----------------------------------------------------------------------------- the check, on inline sources

BASE = """
from abc import ABC

class BaseAgent(ABC):
    def __init__(self, config):
        self.config = config
        self._agent_execution_context = None
"""
SETTER = """
    def _set_execution_context(self, context):
        self._agent_execution_context = context
"""


def _sources(**agents: str) -> Dict[str, str]:
    files = {BASE_AGENT_PATH: BASE}
    files.update({f"src/agents/{name}.py": text for name, text in agents.items()})
    return files


def _keys(sources: Dict[str, str]) -> Set[str]:
    return {w.key for w in Scan(sources).writes()}


def _agent(body: str, head: str = "") -> str:
    """An agent module: `head` at module level, then `class SampleAgent(BaseAgent):` with `body` as its methods."""
    return f"from src.agents.base_agent import BaseAgent\n{head}\nclass SampleAgent(BaseAgent):\n{body}"


ACCUMULATOR = """
from src.agents.base_agent import BaseAgent

class SpendAgent(BaseAgent):
    def __init__(self, config):
        super().__init__(config)
        self._total_tokens = 0

    async def execute(self, message):
        n = await self._call_llm(message)
        self._total_tokens += n
        return n
"""


def test_a_per_instance_token_accumulator_in_a_subclass_is_flagged():
    found = problems(_sources(spend_agent=ACCUMULATOR), "")

    assert len(found) == 1
    assert found[0].startswith("src/agents/spend_agent.py:11: SpendAgent.execute writes self._total_tokens outside __init__")


def test_the_same_accumulator_in_base_agent_itself_is_flagged():
    base = BASE + """
    async def _call_llm(self, request):
        self._total_tokens += 1
"""
    found = problems({BASE_AGENT_PATH: base}, "")

    assert len(found) == 1
    assert found[0].startswith("src/agents/base_agent.py:10: BaseAgent._call_llm writes self._total_tokens outside __init__")


def test_writes_in_init_are_not_flagged():
    agent = _agent("""
    def __init__(self, config):
        super().__init__(config)
        self.a = {}
        self.a["k"] = 1
        self.b = []
        self.b.append(1)
""")
    assert Scan(_sources(sample_agent=agent)).writes() == []


SHAPES = """
from src.agents.base_agent import BaseAgent

class ShapesAgent(BaseAgent):
    def subscript(self, k, v):
        self.a[k] = v

    def call(self, x):
        self.b.append(x)

    def subscripted_call(self, k, x):
        self.c[k].append(x)

    def reflective(self, v):
        setattr(self, "d", v)
        object.__setattr__(self, "e", v)
        delattr(self, "f")

    def nested(self):
        def inner():
            self.g = 1
        return lambda: self.h.clear()

    def unpacking(self, pair):
        self.i, [self.j, *self.k] = pair
        del self.m

    def dotted(self):
        self.config.metadata["user_tone"] = "warm"
        self.n.o += 1

    def loops(self, items):
        for self.p in items:
            pass
        with open("x") as self.q:
            pass
        return [0 for self.r in items]

    async def async_loop(self, source):
        async for self.s in source:
            pass

    def reflective_delete(self):
        object.__delattr__(self, "t")
"""


def test_every_write_shape_on_self_is_flagged_with_its_key():
    keys = _keys(_sources(shapes_agent=SHAPES))

    assert keys == {"a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "m", "config.metadata", "n.o", "p", "q", "r", "s",
                    "t"}


def test_reads_and_calls_that_are_not_mutators_are_not_flagged():
    agent = _agent("""
    async def execute(self, message):
        local = []
        local.append(self.a)
        self.update(message)
        self._port.get(message).append(1)
        value = self.b[0]
        await self._repo.save(value)
        return [x for x in self.c]
""")
    assert Scan(_sources(sample_agent=agent)).writes() == []


def test_a_cls_store_in_a_classmethod_is_class_state():
    agent = _agent("""
    @classmethod
    def count(cls):
        cls.calls += 1
""")
    found = problems(_sources(sample_agent=agent), "")

    assert _keys(_sources(sample_agent=agent)) == {"<cls>.calls"}
    assert found[0].startswith("src/agents/sample_agent.py:7: SampleAgent.count writes class state calls outside __init__")


def test_a_store_and_a_mutator_call_through_type_of_self_are_class_state():
    agent = _agent("""
    def execute(self, record):
        type(self).last = record
        type(self).seen.append(record)
""")
    assert _keys(_sources(sample_agent=agent)) == {"<cls>.last", "<cls>.seen"}


def test_a_subscript_or_attribute_store_on_a_module_level_name_is_flagged():
    agent = _agent("""
    def execute(self, key, value):
        _CACHE[key] = value
        _STATS.hits += 1
""", head="_CACHE = {}\n_STATS = object()")
    found = problems(_sources(sample_agent=agent), "")

    assert _keys(_sources(sample_agent=agent)) == {"<module>._CACHE", "<module>._STATS.hits"}
    assert found[0].startswith("src/agents/sample_agent.py:7: SampleAgent.execute writes the module-level name _CACHE outside")


def test_a_mutator_call_on_a_module_level_name_is_flagged():
    agent = _agent("""
    def execute(self, record):
        _PENDING.append(record)
        _BY_USER[record].add(1)
""", head="_PENDING = []\n_BY_USER = {}")
    assert _keys(_sources(sample_agent=agent)) == {"<module>._PENDING", "<module>._BY_USER"}


def test_a_global_statement_and_its_rebinding_are_flagged():
    agent = _agent("""
    def execute(self, n):
        global _TOTAL
        _TOTAL += n
""", head="_TOTAL = 0")
    scan = Scan(_sources(sample_agent=agent))

    assert {(w.key, w.line) for w in scan.writes()} == {("<module>._TOTAL", 6), ("<module>._TOTAL", 7)}


def test_a_call_on_a_plainly_imported_module_is_not_a_write():
    agent = _agent("""
    def cleanup(self, path):
        os.remove(path)
        os.path.join(path, "x")
""", head="import os")
    assert Scan(_sources(sample_agent=agent)).writes() == []


def test_state_held_by_a_plainly_imported_module_is_a_write():
    agent = _agent("""
    def configure(self, key, value):
        os.environ.pop(key)
        os.environ[key] = value
""", head="import os")
    found = problems(_sources(sample_agent=agent), "")

    assert _keys(_sources(sample_agent=agent)) == {"<module>.os.environ"}
    assert found[0].startswith("src/agents/sample_agent.py:6: SampleAgent.configure writes the module-level name os.environ")


def test_functions_and_lambdas_defined_in_init_are_scanned_under_init():
    agent = _agent("""
    def __init__(self, config, pool=None):
        super().__init__(config)
        self.total = 0
        self._pending = set()
        def on_done(n):
            self.total += n
        self._cb = lambda t: self._pending.discard(t)
        self._hooks = [on_done]
""")
    scan = Scan(_sources(sample_agent=agent))

    assert {(w.method, w.key, w.line) for w in scan.writes()} == {("__init__", "total", 10), ("__init__", "_pending", 11)}
    assert problems(_sources(sample_agent=agent), "")[1].startswith(
        "src/agents/sample_agent.py:10: SampleAgent.__init__ (closure) writes self.total on the request path, state shared")


def test_a_local_of_init_shadows_a_module_level_name_for_its_nested_functions():
    agent = _agent("""
    def __init__(self, config):
        super().__init__(config)
        _CACHE = {}
        def remember(k, v):
            _CACHE[k] = v
        self._remember = remember
""", head="_CACHE = {}")
    assert Scan(_sources(sample_agent=agent)).writes() == []


def test_a_local_that_shadows_a_module_level_name_is_not_flagged():
    agent = _agent("""
    def execute(self, records, _CACHE=None):
        _PENDING = []
        _PENDING.append(records)
        _CACHE = _CACHE or {}
        _CACHE["k"] = 1
        def inner():
            _PENDING.append(2)
        return [r for r in records]
""", head="_PENDING = []\n_CACHE = {}")
    assert Scan(_sources(sample_agent=agent)).writes() == []


def test_a_class_that_does_not_reach_base_agent_is_ignored():
    registry = """
from typing import Dict

class AgentRegistry(Dict[str, BaseAgent]):
    def put(self, key, agent):
        self.count += 1
"""
    sources = _sources()
    sources["src/services/agent_registry.py"] = registry
    scan = Scan(sources)

    assert "src/services/agent_registry.py" in scan.trees  # read, because its header names BaseAgent, and found not an agent
    assert scan.writes() == [] and [c.name for c in scan.agents] == ["BaseAgent"]


def test_a_class_header_is_read_up_to_its_colon():
    agent = """
from src.agents.base_agent import BaseAgent

class CallAgent(make_base(), BaseAgent):
    def execute(self):
        self.seen = True
"""
    scan = Scan(_sources(call_agent=agent))

    assert "CallAgent" in {c.name for c in scan.agents}
    assert {w.entry for w in scan.writes()} == {"src/agents/call_agent.py::CallAgent.execute::seen"}


def test_a_two_level_subclass_is_scanned():
    middle = """
from src.agents.base_agent import BaseAgent

class MiddleAgent(BaseAgent):
    pass
"""
    leaf = """
from src.agents.middle_agent import MiddleAgent

class LeafAgent(MiddleAgent):
    def execute(self):
        self.seen = True
"""
    scan = Scan(_sources(middle_agent=middle, leaf_agent=leaf))

    assert {c.name for c in scan.agents} == {"BaseAgent", "MiddleAgent", "LeafAgent"}
    assert {w.entry for w in scan.writes()} == {"src/agents/leaf_agent.py::LeafAgent.execute::seen"}


def test_a_mixin_base_defined_in_src_is_an_error_and_a_port_base_is_not():
    mixin = """
class CachingMixin:
    def remember(self, value):
        self.cached = value
"""
    port = """
from abc import ABC

class ClassifierPort(ABC):
    pass
"""
    agent = """
from src.agents.base_agent import BaseAgent

class MixedAgent(BaseAgent, CachingMixin):
    pass

class PortedAgent(BaseAgent, ClassifierPort):
    pass
"""
    sources = _sources(mixed_agent=agent)
    sources["src/utils/caching_mixin.py"] = mixin
    sources["src/ports/classifier_port.py"] = port

    errors = Scan(sources).errors

    assert len(errors) == 1
    assert errors[0].startswith("src/agents/mixed_agent.py:4: MixedAgent has the base CachingMixin")


def test_a_base_name_defined_twice_with_different_agent_ness_is_an_error():
    agent = """
from src.agents.base_agent import BaseAgent

class Helper(BaseAgent):
    pass

class LeafAgent(Helper):
    pass
"""
    other = """
class Helper:
    pass
"""
    sources = _sources(helper_agent=agent)
    sources["src/utils/helper.py"] = other

    errors = Scan(sources).errors

    assert len(errors) == 1 and errors[0].startswith("base name Helper is defined 2 times in src/ with different agent-ness")
    assert "src/agents/helper_agent.py:4 (agent)" in errors[0] and "src/utils/helper.py:2 (not an agent)" in errors[0]


# --- constructor-only

CONTEXT_SETTER = """
from src.agents.base_agent import BaseAgent

class ContextAgent(BaseAgent):
    def __init__(self, config, context):
        super().__init__(config)
        self._set_execution_context(context)
"""
CONSTRUCTOR_ONLY = ("src/agents/base_agent.py::BaseAgent._set_execution_context::_agent_execution_context  "
                    "# constructor-only every caller is a constructor")


def test_a_constructor_only_line_holds_while_every_call_is_in_init():
    sources = _sources(context_agent=CONTEXT_SETTER)
    sources[BASE_AGENT_PATH] = BASE + SETTER

    assert problems(sources, CONSTRUCTOR_ONLY) == []


def test_a_constructor_only_line_fails_when_the_method_is_called_outside_init():
    request_path = CONTEXT_SETTER + """
    async def execute(self, message, context):
        self._set_execution_context(context)
"""
    sources = _sources(context_agent=request_path)
    sources[BASE_AGENT_PATH] = BASE + SETTER

    found = problems(sources, CONSTRUCTOR_ONLY)

    assert found == ["src/agents/context_agent.py:10: _set_execution_context is called outside __init__, but "
                     "src/agents/base_agent.py::BaseAgent._set_execution_context::_agent_execution_context is listed "
                     f"constructor-only ({ALLOWLIST_NAME}:1)"]


# --- the lock: never alone, checked on every write of the key

BUFFER = """
from src.agents.base_agent import BaseAgent

class BufferAgent(BaseAgent):
    async def execute(self, record):
        async with self._lock:
            self.buffer.append(record)
        return True
"""
BUFFER_ENTRY = "src/agents/buffer_agent.py::BufferAgent.execute::buffer"
LOCK_GUARDED = f"{BUFFER_ENTRY}  # cross-request-by-design+lock-guarded drained by the periodic flush task"


def test_a_lock_guarded_line_holds_while_the_write_is_inside_the_lock():
    assert problems(_sources(buffer_agent=BUFFER), LOCK_GUARDED) == []


def test_lock_guarded_alone_is_refused_because_a_lock_does_not_scope_state_to_a_request():
    found = problems(_sources(buffer_agent=BUFFER), f"{BUFFER_ENTRY}  # lock-guarded async with self._lock")

    assert found[0] == (f"{ALLOWLIST_NAME}:1: 'lock-guarded': a lock is never a reason alone, it stops a torn write, not "
                        f"state shared across requests (decisions/billing_execution_scoped_ledger.md); use "
                        f"cross-request-by-design+lock-guarded or lazy-cache+lock-guarded")
    assert "a lock is never a reason alone" in problems(
        _sources(buffer_agent=BUFFER), f"{BUFFER_ENTRY}  # dead-write+lock-guarded nothing reads it")[0]


def test_a_lock_guarded_line_fails_when_the_write_moves_out_of_the_lock():
    moved = BUFFER.replace("""        async with self._lock:
            self.buffer.append(record)
""", """        async with self._lock:
            pass
        self.buffer.append(record)
""")
    found = problems(_sources(buffer_agent=moved), LOCK_GUARDED)

    assert found == [f"src/agents/buffer_agent.py:8: {BUFFER_ENTRY} is listed cross-request-by-design+lock-guarded "
                     f"({ALLOWLIST_NAME}:1) but this write is outside 'async with self.<lock>:' in its function"]


def test_the_lock_check_applies_to_every_write_of_the_key_in_the_method():
    second = BUFFER.replace("        return True\n", "        self.buffer.clear()\n        return True\n")

    found = problems(_sources(buffer_agent=second), LOCK_GUARDED)

    assert len(found) == 1 and found[0].startswith(f"src/agents/buffer_agent.py:8: {BUFFER_ENTRY} is listed")


def test_a_write_in_a_nested_function_under_the_lock_is_not_guarded():
    nested = BUFFER.replace("            self.buffer.append(record)\n",
                            "            def later():\n                self.buffer.append(record)\n")

    assert len(problems(_sources(buffer_agent=nested), LOCK_GUARDED)) == 1


def test_lock_is_a_whole_part_of_the_attribute_name():
    for lock in ("async with self.lock:", "async with self._system_lock:", "async with self._LOCK:"):
        assert problems(_sources(buffer_agent=BUFFER.replace("async with self._lock:", lock)), LOCK_GUARDED) == [], lock
    for not_a_lock in ("async with self._clock:", "async with self._blocker:", "async with self._unlocked:"):
        assert len(problems(_sources(buffer_agent=BUFFER.replace("async with self._lock:", not_a_lock)),
                            LOCK_GUARDED)) == 1, not_a_lock


def test_a_sync_with_a_non_lock_attribute_or_a_lock_that_is_not_an_attribute_of_self_does_not_guard():
    for lock in ("with self._lock:", "async with lock:", "async with self._locks[record]:", "async with self._session:"):
        unguarded = BUFFER.replace("async with self._lock:", lock)
        assert len(problems(_sources(buffer_agent=unguarded), LOCK_GUARDED)) == 1, lock


# --- lazy-cache: every write under `if <key> is None:`

LAZY = """
from src.agents.base_agent import BaseAgent

class LazyAgent(BaseAgent):
    async def prompt(self):
        if self._prompt is None:
            self._prompt = await self._build()
        return self._prompt
"""
LAZY_ENTRY = "src/agents/lazy_agent.py::LazyAgent.prompt::_prompt"


def test_a_lazy_cache_line_holds_while_every_write_is_under_its_none_check():
    assert problems(_sources(lazy_agent=LAZY), f"{LAZY_ENTRY}  # lazy-cache built once per user") == []


def test_a_lazy_cache_line_fails_for_an_unguarded_write():
    unguarded = LAZY.replace("        return self._prompt\n", "        self._prompt = None\n        return self._prompt\n")

    found = problems(_sources(lazy_agent=unguarded), f"{LAZY_ENTRY}  # lazy-cache built once per user")

    assert found == [f"src/agents/lazy_agent.py:8: {LAZY_ENTRY} is listed lazy-cache ({ALLOWLIST_NAME}:1) but this write "
                     f"is not inside 'if self._prompt is None:' in its function"]


def test_a_write_in_the_else_branch_or_under_another_keys_check_is_not_lazy():
    other_key = LAZY.replace("if self._prompt is None:", "if self._other is None:")
    else_branch = LAZY.replace("            self._prompt = await self._build()\n",
                               "            pass\n        else:\n            self._prompt = await self._build()\n")

    for source in (other_key, else_branch):
        assert len(problems(_sources(lazy_agent=source), f"{LAZY_ENTRY}  # lazy-cache built once per user")) == 1


def test_lazy_cache_plus_lock_checks_both_the_none_check_and_the_lock():
    entry = f"{LAZY_ENTRY}  # lazy-cache+lock-guarded built once under the lock"
    locked = LAZY.replace("        if self._prompt is None:\n            self._prompt = await self._build()\n",
                          "        async with self._lock:\n            if self._prompt is None:\n"
                          "                self._prompt = await self._build()\n")

    assert problems(_sources(lazy_agent=locked), entry) == []
    assert len(problems(_sources(lazy_agent=LAZY), entry)) == 1  # under the None check, outside the lock


# --- the allowlist's own form

def test_an_unknown_reason_a_missing_evidence_and_a_malformed_key_fail():
    sources = _sources(buffer_agent=BUFFER)

    unknown = problems(sources, f"{BUFFER_ENTRY}  # harmless it is only a buffer")
    no_evidence = problems(sources, f"{BUFFER_ENTRY}  # cross-request-by-design+lock-guarded")
    malformed = problems(sources, "src/agents/buffer_agent.py::execute::buffer  # dead-write nothing reads")

    assert unknown[0] == f"{ALLOWLIST_NAME}:1: unknown reason 'harmless'; use one of {', '.join(REASONS)} or defect:<id>"
    assert no_evidence[0] == (f"{ALLOWLIST_NAME}:1: cross-request-by-design+lock-guarded needs evidence of at least two "
                              f"words")
    assert malformed[0].startswith(f"{ALLOWLIST_NAME}:1: 'src/agents/buffer_agent.py::execute::buffer' is not")


def test_a_defect_reason_is_accepted_with_its_id():
    assert problems(_sources(buffer_agent=BUFFER), f"{BUFFER_ENTRY}  # defect:P15-D9 owner owns the fix") == []
    assert "unknown reason 'defect:soon'" in problems(_sources(buffer_agent=BUFFER), f"{BUFFER_ENTRY}  # defect:soon a fix")[0]


def test_a_stale_line_fails_with_its_line_number():
    text = f"# header\n{LOCK_GUARDED}\nsrc/agents/buffer_agent.py::BufferAgent.execute::gone  # dead-write nothing reads it\n"

    found = problems(_sources(buffer_agent=BUFFER), text)

    assert found == [f"{ALLOWLIST_NAME}:3: stale line, no agent write matches "
                     f"src/agents/buffer_agent.py::BufferAgent.execute::gone; remove it"]


def test_a_line_listed_twice_fails():
    found = problems(_sources(buffer_agent=BUFFER), f"{LOCK_GUARDED}\n{LOCK_GUARDED}\n")

    assert found == [f"{ALLOWLIST_NAME}:2: {BUFFER_ENTRY} is listed twice (first at line 1)"]
