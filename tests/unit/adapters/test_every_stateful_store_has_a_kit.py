"""H2 inventory meta-test (TEST_COVERAGE_SYSTEMIC_RFC §3, P11 card M): every stateful store adapter of `src/adapters/` is run by
a kit over a fake with semantics, or is on `stateful_store_allowlist.txt` with a reason; the allowlist only shrinks.

What counts as "run by a kit" is read from the kit's source, by AST, never by importing it: the file imports the class and
constructs it with an argument that derives from a fake of `CLIENT_FAKES` (an `EvalDb(...)`, a fixture returning one, a name
or attribute assigned from one, a parameter annotated with one), or constructs it inside a module-level helper that
references a fake and is used; or the file calls a named builder of `BUILDERS` that builds the class. The classes, the kits,
the fakes and the builders are defined once, in `tests/support/store_inventory.py`."""
import ast
import re
from functools import lru_cache
from typing import Dict, List, Optional, Set

import pytest

from tests.support.store_inventory import (
    BUILDERS, CLIENT_FAKES, KITS, MOCKS, NOT_STORE_MODULES, REPO, _imports_cloud_client, cloud_client_modules, matches_store_globs,
    read_allowlist, stateful_store_classes, store_classes_in_source,
)

STORES = stateful_store_classes()


# ---------------------------------------------------------------------------------------------------------- the rule

def _name_of(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _with_derived(tree: ast.Module, names: Set[str]) -> Set[str]:
    """`names` plus every class of the file deriving from one (to a fixed point)."""
    out = set(names)
    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    grew = True
    while grew:
        grew = False
        for cls in classes:
            if cls.name not in out and any(_name_of(b) in out for b in cls.bases):
                out.add(cls.name)
                grew = True
    return out


def _default_patch(call: ast.Call) -> bool:
    """`patch(target)` / `patch.object(obj, name)` with no replacement given: the replacement is a `MagicMock`."""
    if any(k.arg in {"new", "new_callable"} for k in call.keywords):
        return False
    owner = call.func.value if isinstance(call.func, ast.Attribute) else None
    if _name_of(call.func) == "patch":
        return len(call.args) < 2
    return _name_of(call.func) == "object" and owner is not None and _name_of(owner) == "patch" and len(call.args) < 3


class _Flow:
    """Where a kind of value (`sources`: the fake names, or the mock names) flows in one file: the names and attribute names
    that hold it or something built from it (assignment, `with ... as`, a parameter annotated with it, a fixture returning
    it). `mock=True` also counts a default `patch(...)` as a source, and a function is a fixture of it only when it returns it."""

    def __init__(self, tree: ast.Module, sources: Set[str], mock: bool = False) -> None:
        self.sources, self.mock = sources, mock
        self.tainted: Set[str] = set()
        functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for fn in functions:
            for arg in fn.args.args + fn.args.kwonlyargs:
                if arg.annotation is not None and any(
                        isinstance(n, ast.Name) and n.id in sources for n in ast.walk(arg.annotation)):
                    self.tainted.add(arg.arg)
            if not mock and any(isinstance(n, ast.Call) and _name_of(n.func) in sources for n in ast.walk(fn)):
                self.tainted.add(fn.name)       # a fake built anywhere in a function: it is that fake's fixture
        grew = True
        while grew:
            grew = False
            for node in ast.walk(tree):
                pairs: List[tuple] = []
                if isinstance(node, ast.Assign):
                    pairs = [(t, node.value) for t in node.targets]
                elif isinstance(node, ast.AnnAssign) and node.value is not None:
                    pairs = [(node.target, node.value)]
                elif isinstance(node, (ast.With, ast.AsyncWith)):
                    pairs = [(i.optional_vars, i.context_expr) for i in node.items if i.optional_vars is not None]
                elif mock and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name not in self.tainted and any(
                            isinstance(r, ast.Return) and r.value is not None and self.hit(r.value)
                            for r in ast.walk(node)):
                        self.tainted.add(node.name)
                        grew = True
                for target, value in pairs:
                    if not self.hit(value):
                        continue
                    for t in ast.walk(target):
                        if isinstance(t, ast.Name) and t.id != "self" and t.id not in self.tainted:
                            self.tainted.add(t.id)
                            grew = True
                        if isinstance(t, ast.Attribute) and t.attr not in self.tainted:
                            self.tainted.add(t.attr)
                            grew = True

    def hit(self, expr: ast.AST) -> bool:
        for n in ast.walk(expr):
            if isinstance(n, ast.Call) and (
                    _name_of(n.func) in self.sources or (self.mock and _default_patch(n))):
                return True
            if isinstance(n, ast.Name) and (n.id in self.sources or n.id in self.tainted):
                return True
            if isinstance(n, ast.Attribute) and n.attr in self.tainted:
                return True
        return False


def _used(tree: ast.Module, name: str, own: ast.AST) -> bool:
    """`name` is called, or is a parameter name (a fixture), somewhere other than its own definition."""
    for n in ast.walk(tree):
        if n is own:
            continue
        if isinstance(n, ast.Call) and _name_of(n.func) == name:
            return True
        if isinstance(n, ast.arg) and n.arg == name:
            return True
    return False


def _verdict(call: ast.Call, scope: ast.AST, tree: ast.Module, fake: _Flow, mock: _Flow) -> str:
    """`fake`, `mock` or `none` for one construction of the class.

    `fake`: an argument derives from a fake; or, with none from a fake, the enclosing top-level helper, fixture or class
    references one and is used. `mock`: no argument derives from a fake and one derives from a mock (`MagicMock()`,
    `create_autospec(...)`, a default `patch(...)`), or the enclosing definition assigns a mock to an attribute
    (`adapter._client = MagicMock()`). A mock for a configuration argument next to a fake client is fine. A construction in
    a definition nothing uses (not a `test*`, not called, not a fixture parameter) counts for nothing."""
    args = list(call.args) + [k.value for k in call.keywords]
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        if not (scope.name.startswith(("test", "Test")) or _used(tree, scope.name, scope)):
            return "none"
    if any(fake.hit(a) for a in args):
        return "fake"
    if any(mock.hit(a) for a in args):
        return "mock"
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        for n in ast.walk(scope):
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Attribute) for t in n.targets) and mock.hit(n.value) \
                    and not fake.hit(n.value):
                return "mock"
        if fake.hit(scope):
            return "fake"
    return "none"


def kit_problem(tree: ast.Module, dotted_class: str) -> Optional[str]:
    """None when the source runs the class over a fake with semantics; else one line saying what is missing or wrong. Every
    construction of the class is judged (`_verdict`); one over a mock refuses the kit, and one over a fake is required."""
    module, _, cls = dotted_class.rpartition(".")
    for builder, (_, built) in BUILDERS.items():
        if dotted_class in built and any(
                isinstance(n, ast.Call) and _name_of(n.func) == builder for n in ast.walk(tree)):
            return None
    imported = any(
        (isinstance(n, ast.ImportFrom) and n.module == module and cls in {a.name for a in n.names})
        or (isinstance(n, ast.Import) and module in {a.name for a in n.names})
        for n in ast.walk(tree))
    if not imported:
        return f"does not import {cls} (and calls none of the builders that build it)"
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and _name_of(n.func) == cls]
    if not calls:
        return f"imports {cls} but never constructs it"
    fake = _Flow(tree, _with_derived(tree, CLIENT_FAKES))
    mock = _Flow(tree, _with_derived(tree, MOCKS), mock=True)
    parents: Dict[ast.AST, ast.AST] = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    verdicts = []
    for call in calls:
        scope: ast.AST = call
        while scope in parents and parents[scope] is not tree:
            scope = parents[scope]
        verdicts.append(_verdict(call, scope, tree, fake, mock))
    if "mock" in verdicts:
        return (f"constructs {cls} over a mock ({', '.join(sorted(MOCKS))}, a default patch): a mock client has no "
                "semantics; use a fake of " + ", ".join(sorted(CLIENT_FAKES)))
    if "fake" not in verdicts:
        return (f"constructs {cls} with no argument derived from a fake ({', '.join(sorted(CLIENT_FAKES))}), and in no used "
                "helper that references one")
    return None


@lru_cache(maxsize=None)
def _tree(rel_path: str) -> ast.Module:
    return ast.parse((REPO / rel_path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------------------- the inventory against the kits

def test_the_inventory_is_what_the_rule_picks():
    """Guards the discovery itself: a rename of a helper or a new store module that the rule stopped seeing would otherwise
    shrink the set silently."""
    assert len(STORES) == len(set(STORES))
    assert "src.adapters.firestore_repo.FirestoreFactRepository" in STORES
    assert "src.adapters.gcp_task_queue.GcpTaskQueue" in STORES
    assert "src.adapters.prompt_v3.firestore_token_repository.FirestoreTokenRepository" in STORES
    # private helpers and classes with no port base are not stores
    assert not any(s.endswith(("_GcpRetryForwarder", "_DomainEncoder")) for s in STORES)


def test_every_stateful_store_is_in_kits_xor_the_allowlist():
    listed = [name for name, _ in read_allowlist()]
    problems: List[str] = []
    for store in STORES:
        in_kits, in_list = store in KITS, store in listed
        if not in_kits and not in_list:
            problems.append(f"{store}: neither in KITS (tests/support/store_inventory.py) nor in stateful_store_allowlist.txt")
        if in_kits and in_list:
            problems.append(f"{store}: in KITS and on the allowlist; delete the allowlist line")
    assert not problems, "\n".join(problems)


def test_allowlist_lines_are_well_formed_and_not_stale():
    entries = read_allowlist()
    names = [name for name, _ in entries]
    problems = [f"{n}: listed twice" for n in sorted({n for n in names if names.count(n) > 1})]
    for name, reason in entries:
        if len(reason.split()) < 2:
            problems.append(f"{name}: the reason is missing or a single word ({reason!r})")
        if name not in STORES:
            problems.append(f"{name}: stale line, the class is gone or is no longer a stateful store")
        if name in KITS:
            problems.append(f"{name}: stale line, the class has a kit now (the list only shrinks)")
    assert not problems, "\n".join(problems)


def test_a_module_importing_a_cloud_client_is_a_store_module_or_excluded_with_a_reason():
    """The discovery goes by file name; this is the second rule, by import: a store module with another name is still seen."""
    problems: List[str] = []
    importing = cloud_client_modules()
    for module in importing:
        path = REPO / (module.replace(".", "/") + ".py")
        if not matches_store_globs(path) and module not in NOT_STORE_MODULES:
            problems.append(f"{module}: imports a google.cloud client but matches no store glob and is not in NOT_STORE_MODULES")
    for module, reason in NOT_STORE_MODULES.items():
        path = REPO / (module.replace(".", "/") + ".py")
        if len(reason.split()) < 2:
            problems.append(f"{module}: the reason is missing")
        if not path.is_file():
            problems.append(f"{module}: stale exclusion, the module is gone")
        elif module not in importing:
            problems.append(f"{module}: stale exclusion, it no longer imports a google.cloud client")
        elif matches_store_globs(path):
            problems.append(f"{module}: stale exclusion, it matches a store glob now (a stateful store needs a kit or an allowlist line)")
    assert not problems, "\n".join(problems)


def test_the_import_rule_reads_every_import_form():
    forms = ["from google.cloud import firestore", "from google.cloud import storage as gcs", "from google.cloud import tasks_v2",
             "from google.cloud import bigquery", "from google.cloud.firestore_v1 import AsyncClient", "import google.cloud.storage",
             "from google.cloud.bigquery import Client", "def f():\n    from google.cloud import firestore"]
    for form in forms:
        assert _imports_cloud_client(ast.parse(form)), form
    for form in ["from google.cloud import logging", "import google.auth", "from ..ports import x", "import firestore"]:
        assert not _imports_cloud_client(ast.parse(form)), form


def test_kits_keys_are_stateful_store_classes():
    """Typo guard: a key that names no stateful class would never be checked against the source."""
    stray = sorted(set(KITS) - set(STORES))
    assert not stray, f"KITS keys that are not stateful store classes: {stray}"


@pytest.mark.parametrize("store,kit", [(s, k) for s, kits in sorted(KITS.items()) for k in kits],
                         ids=lambda v: v.rsplit(".", 1)[-1] if v.startswith("src.") else v.rsplit("/", 1)[-1])
def test_each_kit_runs_its_store_over_a_fake_with_semantics(store, kit):
    path = REPO / kit
    assert path.is_file(), f"{store}: the kit file {kit} does not exist"
    problem = kit_problem(_tree(kit), store)
    assert problem is None, f"{store}: {kit} {problem}"


def test_every_kit_entry_is_a_nonempty_tuple():
    empty = sorted(k for k, v in KITS.items() if not v or len(set(v)) != len(v))
    assert not empty, f"KITS entries with no file or a repeated file: {empty}"


def test_each_builder_builds_its_classes_and_some_kit_calls_it():
    problems: List[str] = []
    for builder, (rel, built) in sorted(BUILDERS.items()):
        tree = _tree(rel)
        definition = next((n for n in ast.walk(tree)
                           if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == builder), None)
        if definition is None:
            problems.append(f"{builder}: not defined in {rel}")
            continue
        constructed = {_name_of(n.func) for n in ast.walk(definition) if isinstance(n, ast.Call)}
        for dotted in built:
            if dotted not in STORES:
                problems.append(f"{builder}: lists {dotted}, which is not a stateful store")
            if dotted.rsplit(".", 1)[-1] not in constructed:
                problems.append(f"{builder}: its definition in {rel} does not construct {dotted.rsplit('.', 1)[-1]}")
        call = re.compile(rf"\b{builder}\(")      # a text search: parsing every kit here would cost more than this test is worth
        callers = [k for kits in KITS.values() for k in kits if call.search((REPO / k).read_text(encoding="utf-8"))]
        if not callers:
            problems.append(f"{builder}: no KITS file calls it; delete the builder entry")
    assert not problems, "\n".join(problems)


# ------------------------------------------------------------------------------------------ the rule on made-up sources

CLS = "src.adapters.firestore_whitelist_repo.FirestoreWhitelistRepository"
HEAD = ("from src.adapters.firestore_whitelist_repo import FirestoreWhitelistRepository\n"
        "from tests.support.firestore_eval import EvalDb\n")


def _problem(source: str, cls: str = CLS) -> Optional[str]:
    return kit_problem(ast.parse(source), cls)


def test_rule_accepts_a_direct_fake_a_fixture_an_assigned_attribute_and_an_annotated_helper():
    assert _problem(HEAD + "def test_t():\n    FirestoreWhitelistRepository(EvalDb(), None)\n") is None
    assert _problem(HEAD + "def db():\n    return EvalDb()\n\ndef test_r(db):\n    return FirestoreWhitelistRepository(db, 1)\n") is None
    assert _problem(HEAD + "class S:\n    def __init__(self):\n        self.db = EvalDb()\n"
                    "        self.repo = FirestoreWhitelistRepository(self.db, 1)\n\ndef test_s():\n    S()\n") is None
    assert _problem(HEAD + "def make(db: EvalDb):\n    return FirestoreWhitelistRepository(None)\n\n"
                    "def test_t():\n    make(EvalDb())\n") is None
    assert _problem(HEAD + "class MyDb(EvalDb):\n    pass\n\ndef test_t():\n    FirestoreWhitelistRepository(MyDb())\n") is None


def test_rule_refuses_a_missing_import_a_missing_construction_and_an_unused_helper():
    assert "does not import" in _problem("from tests.support.firestore_eval import EvalDb\nx = EvalDb()\n")
    assert "never constructs" in _problem(HEAD + "x = EvalDb()\n")
    unused = HEAD + "def helper(db: EvalDb):\n    return FirestoreWhitelistRepository(None)\n"
    assert "no argument derived from a fake" in _problem(unused)
    never_called = HEAD + "def _never():\n    return FirestoreWhitelistRepository(EvalDb())\n"
    assert "no argument derived from a fake" in _problem(never_called)


MOCK_HEAD = "from unittest import mock\nfrom unittest.mock import AsyncMock, MagicMock, create_autospec\n"


def test_rule_refuses_a_construction_over_a_mock_client():
    mocked = HEAD + MOCK_HEAD + "def test_t():\n    FirestoreWhitelistRepository(MagicMock(), None)\n"
    assert "over a mock" in _problem(mocked)
    # the review's case 1: the scope builds an EvalDb on the line before, but the store is handed a mock
    accounts = ("from src.adapters.firestore_account_repo import FirestoreAccountRepository\n" + HEAD + MOCK_HEAD +
                "class Accounts:\n    def __init__(self):\n        self.db = EvalDb()\n"
                "        self.repo = FirestoreAccountRepository(MagicMock(), 'accounts')\n\n"
                "def test_t():\n    Accounts()\n")
    assert "over a mock" in _problem(accounts, "src.adapters.firestore_account_repo.FirestoreAccountRepository")
    # the review's case 2: the helper takes a fake and then replaces the store's client with a mock
    storage = ("from src.adapters.gcs_file_storage_adapter import GcsFileStorageAdapter\n"
               "from tests.support.gcs_fake import FakeGcs\n" + MOCK_HEAD +
               "def _adapter(fake: FakeGcs):\n    adapter = GcsFileStorageAdapter('b')\n"
               "    adapter._client = MagicMock()\n    return adapter\n\n"
               "def test_t():\n    _adapter(FakeGcs())\n")
    gcs = "src.adapters.gcs_file_storage_adapter.GcsFileStorageAdapter"
    assert "over a mock" in _problem(storage, gcs)
    assert _problem(storage.replace("MagicMock()", "fake.client()"), gcs) is None     # control: the same helper over the fake
    for spelling in ("AsyncMock()", "mock.Mock()", "create_autospec(object)", "mock.patch('x.y')", "mock.patch.object(a, 'b')"):
        variant = HEAD + MOCK_HEAD + f"def test_t():\n    FirestoreWhitelistRepository({spelling}, None)\n"
        assert "over a mock" in _problem(variant), spelling
    named = HEAD + MOCK_HEAD + "def test_t():\n    client = MagicMock()\n    FirestoreWhitelistRepository(client, None)\n"
    assert "over a mock" in _problem(named)
    with_patch = HEAD + MOCK_HEAD + ("def test_t():\n    with mock.patch('x.y') as client:\n"
                                     "        FirestoreWhitelistRepository(client, None)\n")
    assert "over a mock" in _problem(with_patch)
    one_bad = HEAD + MOCK_HEAD + ("def test_a():\n    FirestoreWhitelistRepository(EvalDb(), None)\n\n"
                                  "def test_b():\n    FirestoreWhitelistRepository(MagicMock(), None)\n")
    assert "over a mock" in _problem(one_bad)       # one good construction does not excuse a mocked one


def test_rule_accepts_a_mock_that_is_not_the_client_and_a_patch_with_a_replacement():
    config_mock = HEAD + MOCK_HEAD + "def test_t():\n    env = MagicMock()\n    FirestoreWhitelistRepository(EvalDb(), env)\n"
    assert _problem(config_mock) is None
    replaced = HEAD + MOCK_HEAD + ("def test_t():\n    with mock.patch('x.y', EvalDb):\n"
                                   "        FirestoreWhitelistRepository(EvalDb(), None)\n")
    assert _problem(replaced) is None


def test_rule_accepts_a_named_builder_without_the_class_import():
    cls = "src.adapters.firestore_memory_write_lease.FirestoreMemoryWriteLease"
    assert _problem("def test_t():\n    world = ReviewWorld()\n", cls) is None
    assert "does not import" in _problem("def test_t():\n    world = FactStore()\n", cls)


def test_discovery_reads_port_bases_and_client_taking_classes_only():
    source = (
        "from ..ports.thing import ThingPort\nfrom src.ports.other import OtherPort\nimport json\n"
        "class A(ThingPort):\n    pass\n"
        "class B(OtherPort, object):\n    pass\n"
        "class NoPort:\n    def __init__(self, db, env):\n        pass\n"
        "class NoClient:\n    def __init__(self, env):\n        pass\n"
        "class NoInit:\n    pass\n"
        "class Encoder(json.JSONEncoder):\n    def __init__(self, db):\n        pass\n"
        "class _Private(ThingPort):\n    pass\n"
        "class Local(NoPort):\n    pass\n")
    assert store_classes_in_source(source) == ["A", "B", "NoPort"]
