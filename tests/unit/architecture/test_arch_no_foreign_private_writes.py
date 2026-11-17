"""No code in `src/` writes another object's private attribute (composition-root assessment D4: `x._attr = ...` from
outside bypasses the constructor, is invisible to the type checker, and breaks silently when the class renames it).

An offence is an assignment, annotated assignment or augmented assignment whose target is `<expr>._<name>` (not a
dunder) where `<expr>` is anything but the bare name `self` or `cls`; `self.helper._x = ...` is foreign too.

Allowlist `foreign_private_writes_allowlist.txt`: `<path>::<function>::<target>  # <count>  # <reason>`, where
`<function>` is the dotted qualified name of the enclosing function (`<module>` at module level), `<target>` the target
as written, and the count the number of such writes in that function (exact). A line whose offence is gone fails:
delete it.
"""
import ast
import re
from pathlib import Path
from typing import Dict, Iterator, List

import pytest

from tests.support import arch_ratchet

ALLOWLIST = Path(__file__).with_name("foreign_private_writes_allowlist.txt")
_OWN = re.compile(r"\b(?:self|cls)\._")
_TARGET = re.compile(r"\._\w+[ \t]*(?:=(?!=)|[-+*/%&|^@]=|//=|\*\*=|<<=|>>=|[,:)\]])")
FIX = ("Do not write another object's private attribute: pass the value to its constructor, or give the class a public "
       "method that takes it.")


def _targets(node: ast.AST) -> Iterator[ast.AST]:
    targets: List[ast.AST] = list(node.targets) if isinstance(node, ast.Assign) else [node.target]
    while targets:
        target = targets.pop()
        if isinstance(target, (ast.Tuple, ast.List)):
            targets.extend(target.elts)
        elif isinstance(target, ast.Starred):
            targets.append(target.value)
        else:
            yield target


def _foreign_private(target: ast.AST) -> bool:
    if not isinstance(target, ast.Attribute):
        return False
    attr = target.attr
    private = attr.startswith("_") and not (attr.startswith("__") and attr.endswith("__"))
    own = isinstance(target.value, ast.Name) and target.value.id in ("self", "cls")
    return private and not own


def writes(module: ast.Module, path: str) -> Dict[str, int]:
    found: Dict[str, int] = {}
    for stmt, scope, _ in arch_ratchet.statements(module):
        if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for target in _targets(stmt):
                if _foreign_private(target):
                    key = f"{path}::{scope}::{ast.unparse(target)}"
                    found[key] = found.get(key, 0) + 1
    return found


def may_write(text: str) -> bool:
    """False when, once `self._`/`cls._` are taken out, no `._<name>` is followed by what can follow an assignment
    target (`=`, an augmented operator, `,`, `:`, `)`, `]`): the module cannot hold an offence and is not parsed
    (parsing all of src/ takes seconds). A target split across lines around the dot is not seen."""
    return _TARGET.search(_OWN.sub("", text)) is not None


def scan() -> Dict[str, int]:
    found: Dict[str, int] = {}
    for path in arch_ratchet.sources("src"):
        if may_write((arch_ratchet.ROOT / path).read_text()):
            found.update(writes(arch_ratchet.tree(path), path))
    return found


@pytest.mark.slow  # reads every module of src/: over 0.5 s with coverage on
def test_no_code_writes_another_objects_private_attribute():
    listed = arch_ratchet.load(ALLOWLIST, counted=True)

    problems = arch_ratchet.compare(scan(), listed, allowlist=ALLOWLIST.name, unit="writes", fix=FIX)

    assert not problems, "\n".join(problems)


# ----------------------------------------------------------------------------- the check, on inline sources

def test_foreign_writes_are_keyed_by_function_and_target_and_counted():
    source = (
        "x._a = 1\n"
        "class F:\n"
        "    def build(self, agent):\n"
        "        agent._store = s\n"
        "        agent._store = t\n"
        "        self.helper._n += 1\n"
        "        a, other._b = 1, 2\n"
        "        agent._q: int = 3\n"
    )

    assert writes(ast.parse(source), "p.py") == {
        "p.py::<module>::x._a": 1, "p.py::F.build::agent._store": 2, "p.py::F.build::self.helper._n": 1,
        "p.py::F.build::other._b": 1, "p.py::F.build::agent._q": 1}


def test_own_public_dunder_and_read_accesses_are_not_writes():
    source = (
        "class F:\n"
        "    def __init__(self, agent):\n"
        "        self._a = agent._b\n"
        "        agent.public = 1\n"
        "        agent.__dict__ = {}\n"
        "    @classmethod\n"
        "    def make(cls):\n"
        "        cls._cache = None\n"
    )

    assert writes(ast.parse(source), "p.py") == {}


def test_the_text_filter_keeps_every_shape_of_a_foreign_write():
    for source in ("self.helper._n += 1", "myself._x = 1", "f()._x=1", "a._b, c = 1, 2", "a, x._b = 1, 2",
                   "(a, x._b) = t", "[x._b] = t", "x._b: int = 1", "x._b //= 2", "x._b **= 2", "x._b >>= 1"):
        assert may_write(source), source
    for source in ("self._a = cls._b + 1", "y = x._b", "if x._b == 1: pass", "x._b >= 1", "x._b.c = 1"):
        assert not may_write(source), source
