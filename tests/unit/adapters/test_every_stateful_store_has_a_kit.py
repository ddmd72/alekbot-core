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
    BUILDERS, CLIENT_FAKES, KITS, REPO, read_allowlist, stateful_store_classes, store_classes_in_source,
)

STORES = stateful_store_classes()


# ---------------------------------------------------------------------------------------------------------- the rule

def _name_of(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _fakes(tree: ast.Module, base_fakes: frozenset = CLIENT_FAKES) -> Set[str]:
    """The fake names, plus every class of the file deriving from one (to a fixed point)."""
    fakes = set(base_fakes)
    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    grew = True
    while grew:
        grew = False
        for cls in classes:
            if cls.name not in fakes and any(_name_of(b) in fakes for b in cls.bases):
                fakes.add(cls.name)
                grew = True
    return fakes


def _tainted_names(tree: ast.Module, fakes: Set[str]) -> Set[str]:
    """Names and attribute names that hold a fake or something built over one."""
    tainted: Set[str] = set()
    # a function that returns/contains a fake call is a fixture of it: a parameter of that name is tainted
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(isinstance(n, ast.Call) and _name_of(n.func) in fakes for n in ast.walk(fn)):
                tainted.add(fn.name)
            for arg in fn.args.args + fn.args.kwonlyargs:
                if arg.annotation is not None and any(
                        isinstance(n, ast.Name) and n.id in fakes for n in ast.walk(arg.annotation)):
                    tainted.add(arg.arg)

    def hit(expr: ast.AST) -> bool:
        for n in ast.walk(expr):
            if isinstance(n, ast.Call) and _name_of(n.func) in fakes:
                return True
            if isinstance(n, ast.Name) and (n.id in fakes or n.id in tainted):
                return True
            if isinstance(n, ast.Attribute) and n.attr in tainted:
                return True
        return False

    grew = True
    while grew:
        grew = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            else:
                continue
            if not hit(value):
                continue
            for target in targets:
                for t in ast.walk(target):
                    if isinstance(t, ast.Name) and t.id != "self" and t.id not in tainted:
                        tainted.add(t.id)
                        grew = True
                    if isinstance(t, ast.Attribute) and t.attr not in tainted:
                        tainted.add(t.attr)
                        grew = True
    return tainted


def _references_fake(scope: ast.AST, fakes: Set[str], tainted: Set[str]) -> bool:
    for n in ast.walk(scope):
        if isinstance(n, ast.Name) and (n.id in fakes or n.id in tainted):
            return True
        if isinstance(n, ast.Call) and _name_of(n.func) in fakes:
            return True
        if isinstance(n, ast.Attribute) and n.attr in tainted:
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


def kit_problem(tree: ast.Module, dotted_class: str) -> Optional[str]:
    """None when the source runs the class over a fake with semantics; else one line saying what is missing."""
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
    fakes = _fakes(tree)
    tainted = _tainted_names(tree, fakes)
    parents: Dict[ast.AST, ast.AST] = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    for call in calls:
        args = list(call.args) + [k.value for k in call.keywords]
        if any(_references_fake(a, fakes, tainted) for a in args):
            return None
        scope: ast.AST = call
        while scope in parents and parents[scope] is not tree:
            scope = parents[scope]
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and _references_fake(
                scope, fakes, tainted) and _used(tree, scope.name, scope):
            return None
    return (f"constructs {cls} with no argument derived from a fake ({', '.join(sorted(CLIENT_FAKES))}), and in no used "
            "helper that references one")


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
    assert _problem(HEAD + "def t():\n    FirestoreWhitelistRepository(EvalDb(), None)\n") is None
    assert _problem(HEAD + "def db():\n    return EvalDb()\n\ndef r(db):\n    return FirestoreWhitelistRepository(db, 1)\n") is None
    assert _problem(HEAD + "class S:\n    def __init__(self):\n        self.db = EvalDb()\n"
                    "        self.repo = FirestoreWhitelistRepository(self.db, 1)\n") is None
    assert _problem(HEAD + "def make(db: EvalDb):\n    return FirestoreWhitelistRepository(None)\n\n"
                    "def t():\n    make(EvalDb())\n") is None
    assert _problem(HEAD + "class MyDb(EvalDb):\n    pass\n\ndef t():\n    FirestoreWhitelistRepository(MyDb())\n") is None


def test_rule_refuses_a_mock_client_a_missing_import_a_missing_construction_and_an_unused_helper():
    mocked = ("from unittest.mock import MagicMock\n"
              "from src.adapters.firestore_whitelist_repo import FirestoreWhitelistRepository\n"
              "def t():\n    FirestoreWhitelistRepository(MagicMock(), None)\n")
    assert "no argument derived from a fake" in _problem(mocked)
    assert "does not import" in _problem("from tests.support.firestore_eval import EvalDb\nx = EvalDb()\n")
    assert "never constructs" in _problem(HEAD + "x = EvalDb()\n")
    unused = HEAD + "def helper(db: EvalDb):\n    return FirestoreWhitelistRepository(None)\n"
    assert "no argument derived from a fake" in _problem(unused)


def test_rule_accepts_a_named_builder_without_the_class_import():
    cls = "src.adapters.firestore_memory_write_lease.FirestoreMemoryWriteLease"
    assert _problem("def t():\n    world = ReviewWorld()\n", cls) is None
    assert "does not import" in _problem("def t():\n    world = FactStore()\n", cls)


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
