"""`FirestoreFactRepository` is constructed in one place: `ServiceContainer`.

A second instance built elsewhere is half-wired (no embedding service, no biographical context service): the Cabinet's
own bare instance logged "BiographicalContextService not injected, skipping cache refresh" and never refreshed the
baseline after an invalidation. Consumers receive `container.repository`. Scope is production code: `src/` and the root
entry points (`main.py`, `*_main.py`); scripts and tests build their own fixtures.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLASS_NAME = "FirestoreFactRepository"
ALLOWED = {"src/composition/service_container.py"}


def find_constructions(source: str) -> list[int]:
    """Line numbers of calls that construct the class, by bare name or as an attribute (`mod.FirestoreFactRepository(`)."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
        if name == CLASS_NAME:
            lines.append(node.lineno)
    return lines


def _production_files() -> list[Path]:
    files = sorted((ROOT / "src").rglob("*.py"))
    files += sorted(ROOT.glob("*_main.py"))
    if (ROOT / "main.py").exists():
        files.append(ROOT / "main.py")
    return files


def test_fact_repository_is_constructed_only_in_the_service_container():
    violations = []
    for path in _production_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWED:
            continue
        for line in find_constructions(path.read_text(encoding="utf-8")):
            violations.append(f"{rel}:{line}")
    assert not violations, (
        "FirestoreFactRepository must be built only by ServiceContainer (a second instance lacks the embedding and "
        "biographical context services); take container.repository instead: " + ", ".join(violations)
    )


def test_the_service_container_still_constructs_it():
    """Guards the guard: if the allowed file stops constructing it, the allowlist is stale."""
    for rel in ALLOWED:
        assert find_constructions((ROOT / rel).read_text(encoding="utf-8")), rel


def test_detector_sees_bare_and_attribute_calls_and_ignores_references():
    assert find_constructions("repo = FirestoreFactRepository(db, cfg)") == [1]
    assert find_constructions("import m\nrepo = m.FirestoreFactRepository(db)") == [2]
    assert find_constructions("from m import FirestoreFactRepository\nx: FirestoreFactRepository = y") == []
    assert find_constructions("isinstance(r, FirestoreFactRepository)") == []
