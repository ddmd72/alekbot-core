"""`h1_only` is earned, not trusted (tests/support/kit_backend.py): a kit test skipped on the real test database must drive
a hook only the evaluating fake has, and say why. Every `@pytest.mark.h1_only("<reason>")` under `tests/` carries a
non-empty reason and references one of `HOOKS` in its own body or in a module-level fixture or helper it reaches by name
(transitively). A test that fails on the real backend for any other reason is fixed on the fake's side or pinned as a
strict xfail `P12-D<n>`, never marked; a missing index is a setup gap, never a mark."""
import ast
import re
from pathlib import Path
from typing import Dict, List, Set

import pytest

REPO = Path(__file__).resolve().parents[3]
# Regular expressions over source text. `.commits` and `.executed` are the fake's commit and query logs, read (never an
# assignment of a name of the test's own); `._check_index` is the fake's index refusal; `.fail =` is the fake
# collection's query-failure switch, assigned (never `pytest.fail(` or a comparison). The
# switch is not honoured on the real backend: the fake raises it from every query of the collection, and a real query is
# built from the reference, not from the wrapper; to run a fault on real, replace the method the adapter calls instead.
HOOKS = tuple(re.escape(h) for h in ("interfere_on_txn_", "raise_on_txn_read", "EvalDocRef", ".versions", ".cols",
                                     "EvalDb(", "indexes=[]")) + (
    r"\.commits\b(?!\s*=(?!=))", r"\.fail\s*=(?!=)",
    r"\.executed\b(?!\s*=(?!=))",     # the fake's query log, read
    r"\._check_index\b",               # the fake's index refusal, read or replaced
    r"\.txn_versions\b",               # the fake's per-document transaction stamps, read
    r"\.transactions\[",               # the drive fake's transaction log (and each one's `raw` writes), read
    r"\bcontended\(",                  # the drive-note fake's buffered stand-in for `async_transactional`
)
# A naive datetime written raw: the real client stores it as UTC, so only the fake returns it naive. Counted only in the marked
# test's own source (a helper that builds one somewhere would otherwise excuse any test that calls it).
OWN_HOOKS = (re.escape("tzinfo=None"),)
SKIP = {Path(__file__).resolve(), (REPO / "tests" / "support" / "kit_backend.py").resolve()}


def _is_h1_only(node: ast.expr) -> bool:
    target = node.func if isinstance(node, ast.Call) else node
    return isinstance(target, ast.Attribute) and target.attr == "h1_only"


def _reason(node: ast.expr) -> str:
    if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) \
            and isinstance(node.args[0].value, str):
        return node.args[0].value.strip()
    return ""


def problems(source: str) -> List[str]:
    """What is wrong with the `h1_only` marks of one module's source (empty: nothing)."""
    tree = ast.parse(source)
    defs: Dict[str, ast.AST] = {n.name: n for n in tree.body
                                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    found: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and any(_is_h1_only(d) for d in node.decorator_list):
            found.append(f"{node.name}: mark the tests, not the class")
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets) \
                and any(_is_h1_only(n) for n in ast.walk(node.value) if isinstance(n, ast.expr)):
            found.append("pytestmark: mark the tests, not the module")
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        marks = [d for d in node.decorator_list if _is_h1_only(d)]
        if not marks:
            continue
        if not _reason(marks[0]):
            found.append(f"{node.name}: h1_only without a reason")
        own = ast.get_source_segment(source, node) or ""
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
        if not (any(re.search(hook, text) for hook in HOOKS) or any(re.search(hook, own) for hook in OWN_HOOKS)):
            found.append(f"{node.name}: h1_only on a test that drives none of the fake-only hooks {HOOKS + OWN_HOOKS}")
    return found


def _marked_files() -> List[Path]:
    return sorted(p for p in (REPO / "tests").rglob("*.py")
                  if p.resolve() not in SKIP and p.name != "conftest.py" and "h1_only" in p.read_text(encoding="utf-8"))


def test_some_kit_uses_the_mark():
    assert _marked_files()


@pytest.mark.parametrize("path", _marked_files(), ids=lambda p: str(p.relative_to(REPO)))
def test_every_h1_only_mark_has_a_reason_and_a_fake_only_hook(path):
    assert problems(path.read_text(encoding="utf-8")) == []


def test_the_rule_refuses_no_reason_no_hook_and_class_or_module_marks():
    source = '''
import pytest
pytestmark = [pytest.mark.h1_only("x")]

@pytest.fixture
def stores(kit_db):
    return kit_db()

@pytest.mark.h1_only("")
async def test_no_reason(stores):
    stores.db.versions

@pytest.mark.h1_only("waits")
async def test_no_hook(stores):
    assert stores

@pytest.mark.h1_only("x")
class TestAll:
    pass
'''
    assert problems(source) == ["pytestmark: mark the tests, not the module", "test_no_reason: h1_only without a reason",
                                "test_no_hook: h1_only on a test that drives none of the fake-only hooks " + str(HOOKS + OWN_HOOKS),
                                "TestAll: mark the tests, not the class"]


def test_the_rule_follows_a_fixture_and_a_helper_to_the_hook():
    source = '''
import pytest

def broken(db):
    db.raise_on_txn_read("c", "d", RuntimeError())

@pytest.fixture
def store(kit_db):
    return kit_db()

@pytest.mark.h1_only("injects a read error")
async def test_through_a_helper(store):
    broken(store.db)
'''
    assert problems(source) == []


def test_a_naive_datetime_counts_only_in_the_marked_tests_own_source():
    source = '''
import pytest

def naive(at):
    return at.replace(tzinfo=None)

@pytest.mark.h1_only("a naive datetime survives only in the fake")
async def test_own(stores):
    stores.docs()["k"] = {"expires_at": T0.replace(tzinfo=None)}

@pytest.mark.h1_only("a naive datetime survives only in the fake")
async def test_through_a_helper(stores):
    stores.docs()["k"] = {"expires_at": naive(T0)}
'''
    assert problems(source) == [
        "test_through_a_helper: h1_only on a test that drives none of the fake-only hooks " + str(HOOKS + OWN_HOOKS)]


@pytest.mark.parametrize("line,hooked", [
    ("assert store.docs == {} and store.db.commits == []", True),
    ("store.db.collection(EXCL).fail = ServiceUnavailable('down')", True),
    ("self.commits = []", False),
    ("assert store.db.commits_seen == 2", False),
    ("pytest.fail('no')", False),
    ("assert col.fail == None", False),
    ("store.failures = 3", False),
    ("assert store.db.collection(COL).executed == []", True),
    ("check_index = store.db._check_index", True),
    ("store.db._check_index = refuse", True),
    ("self.executed = []", False),
    ("assert run.executed_at is None", False),
    ("assert check_index_ok", False),
    ("stamp = db.collection(COL).txn_versions[ACC]", True),
    ("assert run.txn_versions_seen == 1", False),
    ("parent = db.transactions[-1].raw", True),
    ("assert store.transactions_seen == 1", False),
    ("stand_in = contended(db, interloper)", True),
    ("assert uncontended(db)", False),
])
def test_the_commit_log_and_the_fail_switch_count_only_as_themselves(line, hooked):
    source = "import pytest\n\n@pytest.mark.h1_only('fake-only')\nasync def test_x(store):\n    " + line + "\n"
    assert (problems(source) == []) is hooked
