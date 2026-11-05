"""Third-party distributions the main image needs: the import closure of its four entry points, mapped to distributions.

The main image (`Dockerfile`, `requirements.txt`) runs `main.py`, `indexer_main.py`, `relay_main.py` and `job_main.py`.
File-parsing libraries live in the sandbox images and `requirements-dev.txt`; this guard fails when code reachable from
an entry point imports a distribution `requirements.txt` neither names nor pulls in as a dependency. Pure AST for the
walk (`sandbox_closure.closure`); `importlib.metadata` only for the name mapping.
"""
from __future__ import annotations

import importlib.metadata as metadata
import re
import sys
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Set

from tests.unit.sandbox_closure import ROOT, closure

ENTRYPOINTS = ("main.py", "indexer_main.py", "relay_main.py", "job_main.py")
REQUIREMENTS = ROOT / "requirements.txt"

# Imported only inside functions that no deployed path calls. Each entry names the chain and the cut that would remove it.
NOT_EXECUTED: dict = {}


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_names(text: str) -> Set[str]:
    names: Set[str] = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", line)
        if m:
            names.add(normalize(m.group(0)))
    return names


def _requires(dist: str) -> List[str]:
    try:
        reqs = metadata.requires(dist) or []
    except metadata.PackageNotFoundError:
        return []
    out = []
    for r in reqs:
        if "extra ==" in r:
            continue
        m = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", r)
        if m:
            out.append(m.group(0))
    return out


def installed_set(roots: Iterable[str], requires: Callable[[str], List[str]] = _requires) -> Set[str]:
    """Every distribution named in `roots` plus its declared (non-extra) dependencies, transitively."""
    seen: Set[str] = set()
    stack = [normalize(r) for r in roots]
    while stack:
        d = stack.pop()
        if d in seen:
            continue
        seen.add(d)
        stack.extend(normalize(r) for r in requires(d))
    return seen


def missing_distributions(
    top_levels: Iterable[str], provided: Set[str], dist_map: Dict[str, List[str]]
) -> Dict[str, List[str]]:
    """Top-level names no provided distribution supplies (namespace packages such as `google` need any one of theirs)."""
    missing: Dict[str, List[str]] = {}
    for top in sorted(set(top_levels)):
        candidates = dist_map.get(top, [])
        if not any(normalize(c) in provided for c in candidates):
            missing[top] = candidates
    return missing


def needed_top_levels() -> Set[str]:
    _, external = closure(ENTRYPOINTS)
    tops = {e.split(".")[0] for e in external}
    return {t for t in tops if t and t != "src" and t not in sys.stdlib_module_names
            and not (ROOT / t).exists() and not (ROOT / f"{t}.py").exists()}


def main_image_missing() -> Dict[str, List[str]]:
    tops = needed_top_levels() - set(NOT_EXECUTED)
    provided = installed_set(requirement_names(Path(REQUIREMENTS).read_text()))
    return missing_distributions(tops, provided, metadata.packages_distributions())
