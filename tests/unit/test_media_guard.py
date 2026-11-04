"""§9 guard 1, structural (UNTRUSTED_CONTENT_SANDBOX_RFC §9; S5 design §2.8, card F): media is decoded only inside the
sandbox. The in-process media path (extractor, launcher, loopback relay) is deleted, and no production module but the
drive child itself (`drive_parser/child.py`) imports the media runner `drive_parser.media`, or names it or the child's
media ops (`media_probe`, `media_windows`) as a string, except the sandbox engine, which starts the drive child by
string. A module that brought an in-process media path back would have to do one of these."""
import ast
import importlib.util
import re
from pathlib import Path
from typing import Iterator, List, Tuple

import pytest

REPO = Path(__file__).resolve().parents[2]
MEDIA_MODULE = "src.adapters.drive_parser.media"
CHILD = "src/adapters/drive_parser/child.py"
RUNNER = "src/adapters/drive_parser/media.py"
SANDBOX_ENGINE = "src/adapters/sandbox_engine/"
_MEDIA_NAME = re.compile(r"\bdrive_parser\.media\b")
_MEDIA_OPS = {"media_probe", "media_windows"}

IMPORT_ALLOWED = {CHILD, RUNNER}
STRING_ALLOWED_PREFIXES = (SANDBOX_ENGINE, CHILD, RUNNER)


def _module_name(rel: str) -> Tuple[str, bool]:
    parts = list(Path(rel).with_suffix("").parts)
    is_package = parts[-1] == "__init__"
    return ".".join(parts[:-1] if is_package else parts), is_package


def _imported(tree: ast.AST, module: str, is_package: bool) -> Iterator[str]:
    """Every module name an import statement of `tree` reaches, relative imports resolved."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = module.split(".") if is_package else module.split(".")[:-1]
                base = base[:len(base) - (node.level - 1)]
                target = ".".join(base + ([node.module] if node.module else []))
            else:
                target = node.module or ""
            yield target
            yield from (f"{target}.{alias.name}" for alias in node.names)


def _strings(tree: ast.AST) -> Iterator[str]:
    """String constants that are not docstrings (a docstring may describe the runner; code may not name it)."""
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.value


def violations(rel: str, source: str) -> List[str]:
    tree = ast.parse(source)
    module, is_package = _module_name(rel)
    found = []
    if rel not in IMPORT_ALLOWED:
        found += [f"{rel}: imports {name}" for name in _imported(tree, module, is_package)
                  if name == MEDIA_MODULE or name.startswith(MEDIA_MODULE + ".")]
    if not rel.startswith(STRING_ALLOWED_PREFIXES):
        found += [f"{rel}: names {value!r}" for value in _strings(tree)
                  if _MEDIA_NAME.search(value) or value in _MEDIA_OPS]
    return found


def _production_files() -> List[Path]:
    return sorted(REPO.glob("src/**/*.py")) + sorted(REPO.glob("*.py"))


@pytest.mark.parametrize("module", ["media_extractor", "media_launch", "media_relay"])
def test_the_in_process_media_path_is_gone(module):
    assert importlib.util.find_spec(f"src.adapters.drive_parser.{module}") is None
    assert not (REPO / "src/adapters/drive_parser" / f"{module}.py").exists()


@pytest.mark.slow     # parses every production module
def test_no_production_module_reaches_the_media_runner_outside_the_sandbox():
    found = [v for path in _production_files()
             for v in violations(path.relative_to(REPO).as_posix(), path.read_text(encoding="utf-8"))]
    assert found == []


@pytest.mark.slow     # parses every production module
def test_the_allowed_places_are_the_ones_that_still_use_it():
    """The allowlist is not stale: the drive child imports the runner and the sandbox engine names the child's ops."""
    child = ast.parse((REPO / CHILD).read_text(encoding="utf-8"))
    assert MEDIA_MODULE in set(_imported(child, "src.adapters.drive_parser.child", False))
    engine_strings = {s for path in sorted(REPO.glob(SANDBOX_ENGINE + "*.py"))
                      for s in _strings(ast.parse(path.read_text(encoding="utf-8")))}
    assert _MEDIA_OPS <= engine_strings


@pytest.mark.parametrize("rel,source", [
    ("src/services/x.py", "from src.adapters.drive_parser.media import run_windows\n"),
    ("src/services/x.py", "import src.adapters.drive_parser.media as m\n"),
    ("src/services/x.py", "from src.adapters.drive_parser import media\n"),
    ("src/adapters/drive_parser/x.py", "from .media import run_probe\n"),
    ("src/adapters/drive_parser/x.py", "from . import media\n"),
    ("src/composition/x.py", "from ..adapters.drive_parser.media import run_probe\n"),
    ("indexer_main.py", "import importlib\nimportlib.import_module('src.adapters.drive_parser.media')\n"),
    ("src/adapters/x_extractor.py", "REQUEST = {'op': 'media_windows'}\n"),
])
def test_the_guard_catches_a_way_back(rel, source):
    assert violations(rel, source)


def test_the_guard_leaves_docstrings_and_the_sandbox_engine_alone():
    assert violations("src/adapters/x.py", '"""Runs `drive_parser.media` in the sandbox."""\nX = 1\n') == []
    assert violations("src/adapters/sandbox_engine/x.py", "OPS = ('media_probe', 'media_windows')\n") == []
    assert violations("src/adapters/x.py", "from src.adapters.drive_parser.media_codec import y\n") == []
