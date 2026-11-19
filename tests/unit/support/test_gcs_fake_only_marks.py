"""`gcs_fake_only` is earned, not trusted (tests/support/gcs_kit_backend.py), the GCS twin of `test_h1_only_marks.py`: a GCS
kit test skipped on the test bucket must drive a hook only the GCS fake has, and say why. Every
`@pytest.mark.gcs_fake_only("<reason>")` under `tests/` carries a non-empty reason and references one of `HOOKS` in its own
body or in a module-level fixture or helper it reaches by name (transitively). Faults and interleavings (`fail_next`, `on`)
are NOT hooks: they run on the test bucket too. A test that fails on the real bucket for any other reason is fixed on the
fake's side or pinned as a strict xfail `P12-D<n>`, never marked."""
import ast
import re
from pathlib import Path
from typing import Dict, List, Set

import pytest

REPO = Path(__file__).resolve().parents[3]
HOOKS = (
    r"\.signed_urls\b",         # the fake's record of signing requests (real signing needs a real credential)
    r"\bFakeGcs\(",             # a second, independent fake
    r"\bclock\s*=",             # the fake's injected clock (the server sets the times)
)
SKIP = {Path(__file__).resolve(), (REPO / "tests" / "support" / "gcs_kit_backend.py").resolve()}


def _is_mark(node: ast.expr) -> bool:
    target = node.func if isinstance(node, ast.Call) else node
    return isinstance(target, ast.Attribute) and target.attr == "gcs_fake_only"


def _reason(node: ast.expr) -> str:
    if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) \
            and isinstance(node.args[0].value, str):
        return node.args[0].value.strip()
    return ""


def problems(source: str) -> List[str]:
    """What is wrong with the `gcs_fake_only` marks of one module's source (empty: nothing)."""
    tree = ast.parse(source)
    defs: Dict[str, ast.AST] = {n.name: n for n in tree.body
                                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    found: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and any(_is_mark(d) for d in node.decorator_list):
            found.append(f"{node.name}: mark the tests, not the class")
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets) \
                and any(_is_mark(n) for n in ast.walk(node.value) if isinstance(n, ast.expr)):
            found.append("pytestmark: mark the tests, not the module")
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        marks = [d for d in node.decorator_list if _is_mark(d)]
        if not marks:
            continue
        if not _reason(marks[0]):
            found.append(f"{node.name}: gcs_fake_only without a reason")
        seen: Set[str] = set()
        todo: List[ast.AST] = [node]
        text = ""
        while todo:
            current = todo.pop()
            text += ast.get_source_segment(source, current) or ""
            names = {n.id for n in ast.walk(current) if isinstance(n, ast.Name)}
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names |= {a.arg for a in current.args.args + current.args.kwonlyargs}
            for name in sorted(names - seen):
                seen.add(name)
                if name in defs and defs[name] is not current:
                    todo.append(defs[name])
        if not any(re.search(hook, text) for hook in HOOKS):
            found.append(f"{node.name}: gcs_fake_only on a test that drives none of the GCS fake-only hooks {HOOKS}")
    return found


def _marked_files() -> List[Path]:
    return sorted(p for p in (REPO / "tests").rglob("*.py")
                  if p.resolve() not in SKIP and p.name != "conftest.py" and "gcs_fake_only" in p.read_text(encoding="utf-8"))


def test_some_kit_uses_the_mark():
    assert _marked_files()


@pytest.mark.parametrize("path", _marked_files(), ids=lambda p: str(p.relative_to(REPO)))
def test_every_gcs_fake_only_mark_has_a_reason_and_a_fake_only_hook(path):
    assert problems(path.read_text(encoding="utf-8")) == []


def test_the_rule_refuses_no_reason_no_hook_a_fault_and_class_or_module_marks():
    source = '''
import pytest
pytestmark = [pytest.mark.gcs_fake_only("x")]

@pytest.mark.gcs_fake_only("")
async def test_no_reason(fake):
    assert fake.signed_urls == []

@pytest.mark.gcs_fake_only("an outage")
async def test_a_fault_is_not_a_hook(fake, store):
    fake.fail_next("download", RuntimeError())
    fake.on("get", lambda: None)

@pytest.mark.gcs_fake_only("x")
class TestAll:
    pass
'''
    assert problems(source) == [
        "pytestmark: mark the tests, not the module", "test_no_reason: gcs_fake_only without a reason",
        "test_a_fault_is_not_a_hook: gcs_fake_only on a test that drives none of the GCS fake-only hooks " + str(HOOKS),
        "TestAll: mark the tests, not the class"]


def test_the_rule_follows_a_fixture_and_a_helper_to_the_hook():
    source = '''
import pytest

def requests(fake):
    return fake.signed_urls

@pytest.fixture
def ticking(kit_gcs):
    return kit_gcs(buckets={"b": "plain"}, clock=lambda: None)

@pytest.mark.gcs_fake_only("pins the signing request")
async def test_through_a_helper(fake):
    assert requests(fake) == []

@pytest.mark.gcs_fake_only("needs the fake's clock")
async def test_through_a_fixture(ticking):
    assert ticking
'''
    assert problems(source) == []
