"""Static import closure of the sandbox service (UNTRUSTED_CONTENT_SANDBOX_RFC §4.3, §4.11; plan G13, G15).

Shared by the closure test and the Dockerfile COPY test, so the image's allowlist and the code the service imports
are computed once. Pure AST over the repo; nothing is imported or executed.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, Set, Tuple

ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = "sandbox_main.py"


def _module_file(dotted: str) -> str | None:
    """Repo-relative file of a `src.*` dotted name (module or package `__init__`), else None."""
    rel = dotted.replace(".", "/")
    if (ROOT / f"{rel}.py").is_file():
        return f"{rel}.py"
    if (ROOT / rel / "__init__.py").is_file():
        return f"{rel}/__init__.py"
    return None


def _imported_names(path: str, tree: ast.AST) -> Set[str]:
    package = ".".join(Path(path).parent.parts)
    names: Set[str] = set()
    for node in ast.walk(tree):          # module-level and function-level alike
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                base = ".".join(parts[: len(parts) - (node.level - 1)])
                module = f"{base}.{node.module}" if node.module else base
            else:
                module = node.module or ""
            names.add(module)
            names.update(f"{module}.{a.name}" for a in node.names)
    return names


def closure() -> Tuple[Set[str], Set[str]]:
    """(repo files in the closure, external dotted import names). Package `__init__.py` files on the way are included."""
    files: Dict[str, bool] = {}
    external: Set[str] = set()
    stack = [ENTRYPOINT]
    while stack:
        path = stack.pop()
        if path in files:
            continue
        files[path] = True
        parent = Path(path).parent
        while str(parent) != ".":          # ancestors' __init__.py execute on import
            init = f"{parent}/__init__.py"
            if (ROOT / init).is_file():
                stack.append(init)
            parent = parent.parent
        for name in _imported_names(path, ast.parse((ROOT / path).read_text())):
            if name.split(".")[0] == "src":
                target = _module_file(name)
                if target:
                    stack.append(target)
            elif name:
                external.add(name)
    return set(files), external
