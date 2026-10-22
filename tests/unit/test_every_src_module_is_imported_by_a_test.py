"""A source module no test imports is invisible to coverage: coverage.py omits a file nobody imported (it is not in the
report at all), and a file imported only at collection looks covered at the import lines. This static check closes
that hole (TEST_COVERAGE_SYSTEMIC_RFC §1.2, P2): every module under src/ and every root *_main.py is imported by some
file under tests/, or sits in tests/unit/untested_modules_allowlist.txt with a reason.

The allowlist only shrinks. The test fails when
  (a) a module is imported by no test and is not listed: a new untested module, write its test;
  (b) a listed module is now imported: delete its line;
  (c) a listed module no longer exists: delete its line.

What counts as an import: `import m`, `from m import x` (x counts too when it is a module), `import_module("m")`,
`importorskip("m")`, `patch("m.Name")`, `monkeypatch.setattr("m.Name", ...)`; importing `a.b.c` also imports `a` and
`a.b`. Importing a module is the floor, not the proof: it says a test exists, not that the test is any good.
"""

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ALLOWLIST = Path(__file__).with_name("untested_modules_allowlist.txt")
ROOT_ENTRY_POINTS = ("main", "relay_main", "indexer_main", "sandbox_main", "job_main")
# Calls whose first string argument names a module (or an attribute path inside one).
STRING_TARGET_CALLS = {"patch", "import_module", "importorskip", "setattr", "delattr", "__import__", "reload", "run_module"}
_DOTTED = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$")


def source_modules(repo: Path = REPO) -> set[str]:
    modules = set()
    for path in (repo / "src").rglob("*.py"):
        parts = path.relative_to(repo).with_suffix("").parts
        if "__pycache__" in parts:
            continue
        modules.add(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
    modules.update(name for name in ROOT_ENTRY_POINTS if (repo / f"{name}.py").exists())
    return modules


def _call_name(node: ast.Call) -> str:
    func = node.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def imported_names(source: str) -> set[str]:
    """Every dotted name a test file imports or names as a patch / import target (not yet matched to src modules)."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names if alias.name != "*")
        elif isinstance(node, ast.Call) and _call_name(node) in STRING_TARGET_CALLS and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str) and _DOTTED.match(first.value):
                names.add(first.value)
    return names


def imported_modules(tests_dir: Path, modules: set[str]) -> set[str]:
    """The src modules some file under tests_dir imports. A dotted name counts through its longest module prefix and
    through every package above it (Python imports the parents first)."""
    hit: set[str] = set()
    for path in tests_dir.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        for name in imported_names(path.read_text(encoding="utf-8")):
            parts = name.split(".")
            for end in range(len(parts), 0, -1):
                prefix = ".".join(parts[:end])
                if prefix in modules:
                    hit.update(".".join(parts[:i]) for i in range(1, end + 1) if ".".join(parts[:i]) in modules)
                    break
    return hit


def read_allowlist(path: Path = ALLOWLIST) -> dict[str, str]:
    """`module  # reason` per line; blank lines and lines starting with # are skipped."""
    entries: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        module, _, reason = line.partition("#")
        module, reason = module.strip(), reason.strip()
        assert _DOTTED.match(module), f"{path.name}:{number}: {module!r} is not a dotted module name"
        assert reason, f"{path.name}:{number}: {module} has no reason (write `module  # why no test imports it`)"
        assert module not in entries, f"{path.name}:{number}: {module} is listed twice"
        entries[module] = reason
    return entries


class TestExtraction:
    def test_import_forms(self):
        names = imported_names(
            "import src.a.b\nimport src.c as c\nfrom src.d import e, f\nfrom src.g.h import (i)\nfrom . import rel\nfrom src.j import *\n"
        )
        assert names == {"src.a.b", "src.c", "src.d", "src.d.e", "src.d.f", "src.g.h", "src.g.h.i", "src.j"}

    def test_string_targets_only_in_target_calls(self):
        names = imported_names(
            "patch('src.k.Cls')\nimportlib.import_module('src.l')\nmonkeypatch.setattr('src.m.fn', 1)\n"
            "mock.patch('src.n.o.P', new=1)\nprint('src.not.a.target')\nx = 'src.also.not'\npatch(f'src.{x}')\npatch('plain text')\n"
        )
        assert names == {"src.k.Cls", "src.l", "src.m.fn", "src.n.o.P"}

    def test_matching_goes_through_the_longest_module_prefix_and_the_parents(self, tmp_path):
        (tmp_path / "t.py").write_text(
            "from src.pkg import mod\nfrom src.other import helper\npatch('src.deep.leaf.Class.attr')\nimport main\n"
        )
        modules = {"src", "src.pkg", "src.pkg.mod", "src.other", "src.deep", "src.deep.leaf", "src.unused", "main", "relay_main"}
        assert imported_modules(tmp_path, modules) == {
            "src", "src.pkg", "src.pkg.mod", "src.other", "src.deep", "src.deep.leaf", "main",
        }

    def test_allowlist_format(self, tmp_path):
        path = tmp_path / "list.txt"
        path.write_text("# header\n\nsrc.a.b  # frozen\nsrc.c # why\n")
        assert read_allowlist(path) == {"src.a.b": "frozen", "src.c": "why"}
        path.write_text("src.a.b\n")
        with pytest.raises(AssertionError, match="no reason"):
            read_allowlist(path)
        path.write_text("src.a.b # x\nsrc.a.b # y\n")
        with pytest.raises(AssertionError, match="twice"):
            read_allowlist(path)


@pytest.fixture(scope="module")
def state():
    modules = source_modules()
    return modules, imported_modules(REPO / "tests", modules), read_allowlist()


@pytest.mark.slow  # parsing ~1 300 test files takes ~3 s; the module-scoped fixture is paid by the first test that uses it
class TestEverySourceModuleHasATestThatImportsIt:
    def test_root_entry_points_are_among_the_modules(self, state):
        modules, _, _ = state
        assert set(ROOT_ENTRY_POINTS) <= modules

    def test_no_new_untested_module(self, state):
        modules, imported, allowlist = state
        new = sorted(modules - imported - set(allowlist))
        assert not new, (
            "No test imports these modules. Write a test for each (a test that imports it and checks its behaviour), "
            "or, with the owner's OK, add `module  # reason` to tests/unit/untested_modules_allowlist.txt:\n  "
            + "\n  ".join(new)
        )

    def test_allowlist_only_shrinks(self, state):
        _, imported, allowlist = state
        now_tested = sorted(set(allowlist) & imported)
        assert not now_tested, (
            "A test now imports these allowlisted modules: delete their lines from "
            "tests/unit/untested_modules_allowlist.txt (the list only shrinks):\n  " + "\n  ".join(now_tested)
        )

    def test_allowlist_names_existing_modules(self, state):
        modules, _, allowlist = state
        gone = sorted(set(allowlist) - modules)
        assert not gone, (
            "These allowlisted modules no longer exist: delete their lines from "
            "tests/unit/untested_modules_allowlist.txt:\n  " + "\n  ".join(gone)
        )
