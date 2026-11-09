"""The fact domain registry is the only list of life areas (fact profile RFC §3.2): outside `src/domain/fact_domains.py`
no list, tuple or set literal and no Enum class under `src/` holds three or more registry keys as string literals.
A second copy drifts from the registry silently; a reader takes keys from the registry instead.

`_UNRELATED_SETS` names the closed sets of another vocabulary whose words happen to equal registry keys; each line says
why, and a line whose set no longer trips the scan fails."""
import ast
from pathlib import Path

import pytest

from src.domain.fact_domains import all_keys

_SRC = Path(__file__).resolve().parents[2] / "src"
_REGISTRY = _SRC / "domain" / "fact_domains.py"
_KEYS = frozenset(all_keys())
_LIMIT = 3
# module:assigned name -> why it is not a copy of the registry
_UNRELATED_SETS = {
    # the drive's own life-area labels for files (USER_DRIVE_INDEXING_RFC §25.2.3), a separate closed set
    "domain/drive_labels.py:LIFE_DOMAINS": "drive search file labels, not fact domains",
    # photo scene labels (D40): "home", "sport", "travel", "food" are scenes in a picture
    "domain/drive_labels.py:SCENE_LABELS": "photo scene labels, not fact domains",
}


def _str_values(nodes: list[ast.expr]) -> set[str]:
    return {n.value for n in nodes if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def _is_enum(node: ast.ClassDef) -> bool:
    return any((isinstance(b, ast.Name) and b.id.endswith("Enum"))
               or (isinstance(b, ast.Attribute) and b.attr.endswith("Enum")) for b in node.bases)


def _enum_values(node: ast.ClassDef) -> list[ast.expr]:
    return [s.value for s in node.body if isinstance(s, (ast.Assign, ast.AnnAssign)) and s.value is not None]


def _owner(stmt: ast.stmt) -> str:
    """The name a top-level statement binds, so a hit is reported (and allowlisted) by name, not by line."""
    if isinstance(stmt, ast.Assign) and isinstance(stmt.targets[0], ast.Name):
        return stmt.targets[0].id
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        return stmt.target.id
    if isinstance(stmt, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        return stmt.name
    return "?"


def offenders(source: str, name: str) -> list[str]:
    """Each place in one module's source that holds 3+ registry keys as string literals, as `module:owner`."""
    found = []
    for stmt in ast.parse(source).body:
        for node in ast.walk(stmt):
            if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
                hits = _str_values(node.elts) & _KEYS
            elif isinstance(node, ast.ClassDef) and _is_enum(node):
                hits = _str_values(_enum_values(node)) & _KEYS
            else:
                continue
            if len(hits) >= _LIMIT:
                found.append(f"{name}:{_owner(stmt)}")
    return found


@pytest.fixture(scope="module")
def src_hits() -> list[str]:
    return [hit for path in sorted(_SRC.rglob("*.py")) if path != _REGISTRY
            for hit in offenders(path.read_text(encoding="utf-8"), str(path.relative_to(_SRC)))]


@pytest.mark.slow  # parses all of src/; the module-scoped fixture is paid by the first test that uses it
def test_no_module_outside_the_registry_lists_three_domain_keys(src_hits):
    bad = [hit for hit in src_hits if hit not in _UNRELATED_SETS]
    assert bad == [], f"domain keys listed outside src/domain/fact_domains.py: {bad}"


@pytest.mark.slow  # parses all of src/
def test_every_unrelated_set_still_trips_the_scan(src_hits):
    assert [name for name in _UNRELATED_SETS if name not in src_hits] == [], "a stale _UNRELATED_SETS line: remove it"


def test_the_registry_file_exists():
    assert _REGISTRY.exists(), "the registry moved: update this test"


@pytest.mark.parametrize("source", [
    "AREAS = ['health', 'money', 'travel']",
    "AREAS = ('health', 'money', 'travel')",
    "AREAS = {'health', 'money', 'travel'}",
    "AREAS = frozenset({'health', 'money', 'travel'})",
    "from enum import Enum\nclass Area(str, Enum):\n    A = 'health'\n    B = 'money'\n    C = 'travel'\n",
    "import enum\nclass Area(enum.Enum):\n    A = 'health'\n    B = 'money'\n    C: str = 'travel'\n",
])
def test_the_scan_catches_three_keys(source):
    assert offenders(source, "m.py") in (["m.py:AREAS"], ["m.py:Area"])


@pytest.mark.parametrize("source", [
    "AREAS = ['health', 'money', 'groceries']",                  # two keys
    "AREAS = ['health', 'health', 'money']",                     # three literals, two distinct keys
    "AREAS = {'health': 1, 'money': 2, 'travel': 3}",            # a dict is not a list of areas
    "class Plain:\n    A = 'health'\n    B = 'money'\n    C = 'travel'\n",  # a constants class, not an Enum
    "TEXT = 'health money travel'",
])
def test_the_scan_passes_fewer_than_three_keys_or_other_shapes(source):
    assert offenders(source, "m.py") == []
