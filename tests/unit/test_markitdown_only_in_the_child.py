"""markitdown runs only in the parser child: no `src/` module outside `src/adapters/drive_parser/` imports it
(lazy imports inside functions included)."""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
CHILD = SRC / "adapters" / "drive_parser"


def _imports_markitdown(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "markitdown" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and node.level == 0 and (node.module or "").split(".")[0] == "markitdown":
            return True
    return False


def test_no_src_module_outside_the_child_imports_markitdown():
    offenders = [str(p.relative_to(SRC)) for p in SRC.rglob("*.py")
                 if CHILD not in p.parents and _imports_markitdown(ast.parse(p.read_text(encoding="utf-8")))]
    assert offenders == []


def test_the_guard_sees_a_lazy_import():
    tree = ast.parse("def f():\n    from markitdown.converters import CsvConverter\n")
    assert _imports_markitdown(tree)
