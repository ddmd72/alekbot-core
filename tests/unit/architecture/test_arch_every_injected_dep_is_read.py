"""Every dependency a constructor stores is read by its class (composition-root assessment §2.2: a dependency stored and
never read is dead wiring that every caller and every test still has to build and pass).

Checked: classes in `src/handlers/`, `src/services/`, `src/composition/` and `src/agents/`. For each `__init__`
parameter stored as `self.<a> = <param>` (or `self.<a>: T = <param>`), `self.<a>` must be read (Load) somewhere in the
same class body, or as `getattr(self, "<a>")`. A read in a subclass does not count; a read in a base class that is
defined in the same layers does (the base consumes what the subclass stores). An exception class (a base named
`*Error` or `*Exception`) is not checked: its fields are read by the code that catches it. A public attribute that a
collaborator reads is listed with that reason.

Allowlist `unread_injected_deps_allowlist.txt`: `<path>::<Class>::<attr>  # <reason>`. A line whose offence is gone
(the attribute is read again, or no longer stored) fails: delete it.
"""
import ast
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Set

import pytest

from tests.support import arch_ratchet

ALLOWLIST = Path(__file__).with_name("unread_injected_deps_allowlist.txt")
LAYERS = ("src/handlers", "src/services", "src/composition", "src/agents")
FIX = ("Do not store a dependency the class never reads: drop the parameter and stop passing it (callers and tests "
       "included).")


def _init(cls: ast.ClassDef):
    return next((n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "__init__"),
                None)


def stored(cls: ast.ClassDef) -> List[str]:
    """Attributes `__init__` sets straight from one of its parameters, in order."""
    init = _init(cls)
    if init is None:
        return []
    a = init.args
    params = {p.arg for p in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg] if p is not None}
    found = []
    for node in ast.walk(init):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self"
                and isinstance(value, ast.Name) and value.id in params and target.attr not in found):
            found.append(target.attr)
    return found


def read(cls: ast.ClassDef) -> Set[str]:
    """Attributes the class body reads as `self.<a>` or `getattr(self, "<a>"[, default])`."""
    names = set()
    for node in ast.walk(cls):
        if (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) and isinstance(node.value, ast.Name)
                and node.value.id == "self"):
            names.add(node.attr)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr"
              and len(node.args) >= 2 and isinstance(node.args[0], ast.Name) and node.args[0].id == "self"
              and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)):
            names.add(node.args[1].value)
    return names


def _bases(cls: ast.ClassDef) -> List[str]:
    return [b.id if isinstance(b, ast.Name) else b.attr for b in cls.bases if isinstance(b, (ast.Name, ast.Attribute))]


def unread(modules: Mapping[str, ast.AST]) -> Dict[str, int]:
    """`{<path>::<Class>::<attr>: 1}` for every stored and unread attribute. A base class is looked up by its simple
    name among the classes of `modules`; a name defined twice is not followed."""
    by_name: Dict[str, List[ast.ClassDef]] = {}
    for module in modules.values():
        for _, cls in arch_ratchet.classes(module):
            by_name.setdefault(cls.name, []).append(cls)

    def reads_with_bases(cls: ast.ClassDef, seen: Set[str]) -> Set[str]:
        names = read(cls)
        for base in _bases(cls):
            known = by_name.get(base, [])
            if len(known) == 1 and base not in seen:
                names |= reads_with_bases(known[0], seen | {base})
        return names

    found: Dict[str, int] = {}
    for path, module in modules.items():
        for qualname, cls in arch_ratchet.classes(module):
            if any(b.endswith(("Error", "Exception")) for b in _bases(cls)):
                continue
            attrs = stored(cls)
            if attrs:
                names = reads_with_bases(cls, {cls.name})
                found.update({f"{path}::{qualname}::{a}": 1 for a in attrs if a not in names})
    return found


def scan(paths: Iterable[str] = ()) -> Dict[str, int]:
    return unread({p: arch_ratchet.tree(p) for p in (paths or arch_ratchet.sources(*LAYERS))})


@pytest.mark.slow  # parses a whole layer of src/: over 0.5 s with coverage on
def test_every_stored_constructor_dependency_is_read():
    listed = arch_ratchet.load(ALLOWLIST, counted=False)

    problems = arch_ratchet.compare(scan(), listed, allowlist=ALLOWLIST.name, unit="unread attribute", fix=FIX)

    assert not problems, "\n".join(problems)


# ----------------------------------------------------------------------------- the check, on inline sources

def _unread(source: str) -> Dict[str, int]:
    return unread({"p.py": ast.parse(source)})


def test_a_stored_parameter_nobody_reads_is_reported_and_reads_of_every_form_count():
    source = (
        "class H:\n"
        "    def __init__(self, a, b, c, d, e: int, *, f):\n"
        "        self._a = a\n"
        "        self._b = b\n"
        "        self.c = c\n"
        "        self._d = d\n"
        "        self._e: int = e\n"
        "        self._f = f or None\n"
        "        self._g = self._b\n"
        "    def run(self):\n"
        "        lambda: self.c\n"
        "        return getattr(self, '_d', None)\n"
    )

    assert _unread(source) == {"p.py::H::_a": 1, "p.py::H::_e": 1}


def test_a_read_in_the_base_class_counts_and_a_read_in_a_subclass_does_not():
    source = (
        "class Base:\n"
        "    def go(self):\n"
        "        return self._llm\n"
        "class Agent(Base):\n"
        "    def __init__(self, llm, store):\n"
        "        self._llm = llm\n"
        "        self._store = store\n"
        "class Child(Agent):\n"
        "    def go(self):\n"
        "        return self._store\n"
    )

    assert _unread(source) == {"p.py::Agent::_store": 1}


def test_an_exception_class_is_not_checked():
    source = "class Taken(ValueError):\n    def __init__(self, name):\n        self.name = name\n"

    assert _unread(source) == {}
