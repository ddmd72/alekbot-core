"""main.py must not re-import a module-level name inside a function.

A function-local `from x import Name` makes `Name` local to the WHOLE function, so any use
of it earlier in that function raises UnboundLocalError at startup. That took down a deploy
on 2026-10-09 (revision failed to start: "cannot access local variable
'FirestoreDedupStore'"). main.py's startup has no unit coverage, so check it statically.
"""
import ast
from pathlib import Path

MAIN = Path(__file__).resolve().parents[2] / "main.py"


def _module_level_imports(tree: ast.Module) -> set:
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
    return names


def test_no_function_local_import_shadows_a_module_import():
    tree = ast.parse(MAIN.read_text())
    module_names = _module_level_imports(tree)
    shadowed = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    name = (alias.asname or alias.name).split(".")[0]
                    if name in module_names:
                        shadowed.append(f"{fn.name}:{node.lineno} {name}")
    assert not shadowed, f"function-local imports shadow module-level names: {shadowed}"
