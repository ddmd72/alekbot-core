"""AST operators: every mutant of a module, numbered in a stable order.

`enumerate_mutants(source)` lists them; `apply_mutant(source, number)` returns the mutated AST of one. Both walk the tree
in the same pre-order, so a mutant is addressed by (visit number of its node, variant number at that node).

Operators (one mutant per site, or per element where a site has several):
  order_by-flip   `.order_by(f, direction=ASCENDING)` -> DESCENDING (and back); no direction given -> DESCENDING
  order_by-drop   `q.order_by(...)` -> `q`
  where-drop      `q.where(...)` -> `q` (tagged "tenancy filter" when the arguments mention user_id/account_id/tenant)
  select-drop     one element removed from a list/tuple/set literal given to `.select(...)`
  dump-drop       one element removed from the include=/exclude= literal of `.model_dump(...)`
  doc-drop        one key removed from a dict literal passed to a write call (`.set/.update/.create/.add`), or assigned
                  to a name that is passed to one in the same function
  except-success  an `except` body replaced by `return <success value>` (rule below); handlers outside a function and
                  handlers whose last statement already returns that value after nothing but logger calls (an equivalent
                  mutant) are not mutated; any other side effect in the handler keeps the mutant
  write-skip      one statement-level `.set/.update/.create/.delete(...)` call (awaited or not) replaced by `pass`
  kwarg-drop      one keyword removed from a call when its value is a bare name equal to a parameter of the enclosing
                  function (a forwarded argument)

The success value of `except-success` follows the enclosing function's return annotation: `bool` -> `True`;
`list`/`List[..]`/`Sequence` -> `[]`; `dict`/`Dict[..]`/`Mapping` -> `{}`; `set` -> `set()`; `str` -> `""`; `int` -> `0`;
`float` -> `0.0`; anything else (no annotation, `None`, `Optional[..]`, a class) -> `None`.

A mutant that is semantically identical to the original (an `order_by` whose order nothing observes, a skipped write
to a throwaway object) is "equivalent": a reviewer rules it out in the driver's --equivalent file; the tool never does.
"""

import ast
import copy
import hashlib
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Set, Tuple

WRITE_CALLS = {"set", "update", "create", "delete"}
DOC_WRITE_CALLS = {"set", "update", "create", "add"}
_TENANCY_WORDS = ("user_id", "account_id", "tenant")
_DIRECTIONS = {"ASCENDING": "DESCENDING", "DESCENDING": "ASCENDING", "asc": "desc", "desc": "asc"}


@dataclass(frozen=True)
class Mutant:
    number: int
    operator: str
    lineno: int
    description: str
    key: str  # stable across unrelated edits: a hash of the operator, the node and the variant (+ occurrence)
    visit: int
    variant: int


@dataclass
class _Variant:
    operator: str
    description: str
    build: Callable[[], ast.AST]


def _src(node: ast.AST) -> str:
    text = ast.unparse(node)
    return text if len(text) <= 90 else text[:87] + "..."


def _own(node: ast.Call) -> str:
    """The call itself, without the chain it hangs on: `.where('user_id', '==', uid)`."""
    args = [ast.unparse(a) for a in node.args] + [f"{k.arg}={ast.unparse(k.value)}" if k.arg else f"**{ast.unparse(k.value)}" for k in node.keywords]
    name = f".{node.func.attr}" if isinstance(node.func, ast.Attribute) else ast.unparse(node.func)
    text = f"{name}({', '.join(args)})"
    return text if len(text) <= 90 else text[:87] + "..."


def _attr_call(node: ast.AST, names) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in names


def _annotation_success(annotation: Optional[ast.expr]) -> ast.expr:
    """The `return` value of the success path, from the return annotation (rule in the module docstring)."""
    text = ast.unparse(annotation) if annotation is not None else ""
    head = text.split("[", 1)[0].split(".")[-1].strip()
    table = {
        "bool": ast.Constant(True),
        "list": ast.List(elts=[], ctx=ast.Load()), "List": ast.List(elts=[], ctx=ast.Load()),
        "Sequence": ast.List(elts=[], ctx=ast.Load()),
        "dict": ast.Dict(keys=[], values=[]), "Dict": ast.Dict(keys=[], values=[]), "Mapping": ast.Dict(keys=[], values=[]),
        "set": ast.Call(func=ast.Name("set", ast.Load()), args=[], keywords=[]),
        "str": ast.Constant(""), "int": ast.Constant(0), "float": ast.Constant(0.0),
    }
    return table.get(head, ast.Constant(None))


def _fn_params(fn: ast.AST) -> Set[str]:
    a = fn.args  # type: ignore[attr-defined]
    names = {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs}
    names.update(x.arg for x in (a.vararg, a.kwarg) if x is not None)
    names.discard("self")
    names.discard("cls")
    return names


def _is_async_generator(fn: ast.AST) -> bool:
    return isinstance(fn, ast.AsyncFunctionDef) and any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(fn))


def _written_names(fn: ast.AST) -> Set[str]:
    """Names passed (positionally or as a keyword value) to a document write call anywhere in the function."""
    names: Set[str] = set()
    for node in ast.walk(fn):
        if _attr_call(node, DOC_WRITE_CALLS):
            values = list(node.args) + [k.value for k in node.keywords]  # type: ignore[attr-defined]
            names.update(v.id for v in values if isinstance(v, ast.Name))
    return names


def _without(items: list, index: int) -> list:
    return items[:index] + items[index + 1:]


def _drop_receiver(call: ast.Call) -> ast.expr:
    return call.func.value  # type: ignore[attr-defined]


def _literal_elements(node: ast.expr) -> Optional[List[ast.expr]]:
    return list(node.elts) if isinstance(node, (ast.List, ast.Tuple, ast.Set)) else None


def _with_elements(node: ast.expr, elements: List[ast.expr]) -> ast.expr:
    clone = copy.deepcopy(node)
    clone.elts = elements  # type: ignore[attr-defined]
    return clone


def _flip_direction(value: ast.expr) -> Optional[ast.expr]:
    if isinstance(value, ast.Attribute) and value.attr in _DIRECTIONS:
        return ast.Attribute(value=value.value, attr=_DIRECTIONS[value.attr], ctx=ast.Load())
    if isinstance(value, ast.Name) and value.id in _DIRECTIONS:
        return ast.Name(id=_DIRECTIONS[value.id], ctx=ast.Load())
    if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value in _DIRECTIONS:
        return ast.Constant(_DIRECTIONS[value.value])
    return None


def _call_variants(node: ast.Call, fn: Optional[ast.AST], written: Callable[[], Set[str]]) -> List[_Variant]:
    out: List[_Variant] = []
    if _attr_call(node, {"order_by"}):
        flipped = None
        for index, kw in enumerate(node.keywords):
            if kw.arg == "direction" and _flip_direction(kw.value) is not None:
                flipped = ("kw", index)
        if flipped is None and len(node.args) >= 2 and _flip_direction(node.args[1]) is not None:
            flipped = ("pos", 1)

        def flip(node=node, flipped=flipped):
            clone = copy.deepcopy(node)
            if flipped is None:  # no direction given: the default is ascending
                clone.keywords.append(ast.keyword(arg="direction", value=ast.Constant("DESCENDING")))  # type: ignore[attr-defined]
            elif flipped[0] == "kw":
                clone.keywords[flipped[1]].value = _flip_direction(clone.keywords[flipped[1]].value)  # type: ignore[attr-defined]
            else:
                clone.args[1] = _flip_direction(clone.args[1])  # type: ignore[attr-defined]
            return clone

        out.append(_Variant("order_by-flip", f"flip the direction of `{_own(node)}`", flip))
        out.append(_Variant("order_by-drop", f"drop `{_own(node)}`", lambda node=node: _drop_receiver(node)))
    if _attr_call(node, {"where"}):
        own = _own(node)
        tag = " (tenancy filter)" if any(w in own for w in _TENANCY_WORDS) else ""
        out.append(_Variant("where-drop", f"drop the filter `{own}`{tag}", lambda node=node: _drop_receiver(node)))
    if _attr_call(node, {"select"}) and node.args:
        elements = _literal_elements(node.args[0])
        for i in range(len(elements or [])):
            def drop(node=node, elements=elements, i=i):
                clone = copy.deepcopy(node)
                clone.args[0] = _with_elements(node.args[0], _without(elements, i))  # type: ignore[attr-defined]
                return clone
            out.append(_Variant("select-drop", f"drop {_src(elements[i])} from `{_own(node)}`", drop))
    if _attr_call(node, {"model_dump"}):
        for kw in node.keywords:
            elements = _literal_elements(kw.value) if kw.arg in ("include", "exclude") else None
            for i in range(len(elements or [])):
                def drop_dump(node=node, kw=kw, elements=elements, i=i):
                    clone = copy.deepcopy(node)
                    for k in clone.keywords:  # type: ignore[attr-defined]
                        if k.arg == kw.arg:
                            k.value = _with_elements(kw.value, _without(elements, i))
                    return clone
                out.append(_Variant("dump-drop", f"drop {_src(elements[i])} from {kw.arg}= in `{_own(node)}`", drop_dump))
    if _attr_call(node, DOC_WRITE_CALLS):
        positions = [("arg", i) for i, a in enumerate(node.args) if isinstance(a, ast.Dict)]
        positions += [("kw", i) for i, k in enumerate(node.keywords) if isinstance(k.value, ast.Dict)]
        for kind, pos in positions:
            holder = node.args[pos] if kind == "arg" else node.keywords[pos].value
            for j, key in enumerate(holder.keys):  # type: ignore[attr-defined]
                if key is None:
                    continue  # a **spread
                def drop_key(node=node, kind=kind, pos=pos, j=j):
                    clone = copy.deepcopy(node)
                    target = clone.args[pos] if kind == "arg" else clone.keywords[pos].value  # type: ignore[attr-defined]
                    target.keys = _without(target.keys, j)
                    target.values = _without(target.values, j)
                    return clone
                out.append(_Variant("doc-drop", f"drop the field {_src(key)} from the document written by `{_own(node)}`", drop_key))
    forwarded = [i for i, kw in enumerate(node.keywords) if kw.arg is not None and isinstance(kw.value, ast.Name)]
    if fn is not None and forwarded:
        params = _fn_params(fn)
        for i in forwarded:
            kw = node.keywords[i]
            if kw.value.id in params:
                def drop_kw(node=node, i=i):
                    clone = copy.deepcopy(node)
                    clone.keywords = _without(clone.keywords, i)  # type: ignore[attr-defined]
                    return clone
                out.append(_Variant("kwarg-drop", f"stop forwarding {kw.arg}={kw.value.id} in the call `{_own(node)}`", drop_kw))
    return out


def _assign_variants(node: ast.AST, fn: Optional[ast.AST], written: Callable[[], Set[str]]) -> List[_Variant]:
    """`doc = {...}` where doc is later passed to a write call in the same function."""
    value = getattr(node, "value", None)
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]  # type: ignore[attr-defined]
    if fn is None or not isinstance(value, ast.Dict):
        return []
    names = [t.id for t in targets if isinstance(t, ast.Name)]
    if not names or not any(n in written() for n in names):
        return []
    out = []
    for j, key in enumerate(value.keys):
        if key is None:
            continue

        def drop_key(node=node, j=j):
            clone = copy.deepcopy(node)
            clone.value.keys = _without(clone.value.keys, j)  # type: ignore[attr-defined]
            clone.value.values = _without(clone.value.values, j)  # type: ignore[attr-defined]
            return clone

        out.append(_Variant("doc-drop", f"drop the field {_src(key)} from the document `{names[0]}` that is written later", drop_key))
    return out


_LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}


def _is_log_call(stmt: ast.stmt) -> bool:
    """`logger.warning(...)` as a statement: the receiver is a name containing "log"."""
    if not (isinstance(stmt, ast.Expr) and _attr_call(stmt.value, _LOG_METHODS)):
        return False
    root = stmt.value.func.value  # type: ignore[attr-defined]
    while isinstance(root, ast.Attribute):
        root = root.value
    return isinstance(root, ast.Name) and "log" in root.id.lower()


def _already_returns(body: List[ast.stmt], success: ast.expr) -> bool:
    """The handler's last statement returns the success value and every other statement is a logger call."""
    last = body[-1]
    return (
        isinstance(last, ast.Return)
        and ast.dump(last.value or ast.Constant(None)) == ast.dump(success)
        and all(_is_log_call(stmt) for stmt in body[:-1])
    )


def _except_variants(node: ast.ExceptHandler, fn: Optional[ast.AST]) -> List[_Variant]:
    if fn is None or _is_async_generator(fn):
        return []  # an async generator cannot `return value`
    success = _annotation_success(getattr(fn, "returns", None))
    if _already_returns(node.body, success):
        return []  # equivalent: the handler already returns the success value and does nothing else observable

    def replace(node=node, success=success):
        clone = copy.deepcopy(node)
        clone.body = [ast.Return(value=copy.deepcopy(success))]
        return clone

    where = _src(node.type) if node.type is not None else "everything"
    return [_Variant("except-success", f"`except {where}` returns {ast.unparse(success)} instead of handling the failure", replace)]


def _write_skip_variants(node: ast.Expr) -> List[_Variant]:
    call = node.value.value if isinstance(node.value, ast.Await) else node.value
    if not _attr_call(call, WRITE_CALLS):
        return []
    return [_Variant("write-skip", f"skip the write `{_src(call)}`", lambda: ast.Pass())]


class _Walker(ast.NodeTransformer):
    """Pre-order walk shared by enumeration (target None) and application (target = (visit, variant))."""

    def __init__(self, target: Optional[Tuple[int, int]] = None):
        self.target = target
        self.visit_no = 0
        self.found: List[Tuple[int, int, _Variant, int]] = []  # (visit, variant, variant object, lineno)
        self._fns: List[ast.AST] = []
        self._written: Dict[int, Set[str]] = {}
        self.applied = False

    def _variants(self, node: ast.AST) -> List[_Variant]:
        fn = self._fns[-1] if self._fns else None

        def written() -> Set[str]:
            return self._written.setdefault(id(fn), _written_names(fn) if fn is not None else set())

        if isinstance(node, ast.Call):
            return _call_variants(node, fn, written)
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            return _assign_variants(node, fn, written)
        if isinstance(node, ast.ExceptHandler):
            return _except_variants(node, fn)
        if isinstance(node, ast.Expr):
            return _write_skip_variants(node)
        return []

    def visit(self, node: ast.AST):
        self.visit_no += 1
        mine = self.visit_no
        if self.target is None:
            for v, variant in enumerate(self._variants(node)):
                self.found.append((mine, v, variant, getattr(node, "lineno", 0)))
        elif self.target[0] == mine:
            self.applied = True
            replacement = self._variants(node)[self.target[1]].build()
            return ast.copy_location(replacement, node)
        is_fn = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        if is_fn:
            self._fns.append(node)
        try:
            return self.generic_visit(node)
        finally:
            if is_fn:
                self._fns.pop()


def enumerate_mutants(source: str) -> List[Mutant]:
    walker = _Walker()
    walker.visit(ast.parse(source))
    mutants, seen = [], {}
    for number, (visit, v, variant, lineno) in enumerate(walker.found, 1):
        base = f"{variant.operator}|{variant.description}"
        seen[base] = seen.get(base, 0) + 1
        key = hashlib.sha1(f"{base}|{seen[base]}".encode()).hexdigest()[:8]
        mutants.append(Mutant(number, variant.operator, lineno, variant.description, key, visit, v))
    return mutants


def apply_mutant(source: str, number: int) -> ast.Module:
    """The module AST with mutant `number` (1-based, as `enumerate_mutants` numbers them) applied."""
    mutants = enumerate_mutants(source)
    if not 1 <= number <= len(mutants):
        raise ValueError(f"mutant {number} does not exist: the module has {len(mutants)}")
    chosen = mutants[number - 1]
    walker = _Walker(target=(chosen.visit, chosen.variant))
    tree = walker.visit(ast.parse(source))
    assert walker.applied, f"mutant {number} was not applied (source changed between enumeration and application?)"
    return ast.fix_missing_locations(tree)
