"""tests/support/semantic_mutants: the operators, the one-module import hook and the driver (toy module, toy tests)."""

import ast
import importlib
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.support.semantic_mutants import loader
from tests.support.semantic_mutants.operators import apply_mutant, enumerate_mutants

REPO_ROOT = Path(__file__).resolve().parents[2]


def mutate(source: str, operator: str, nth: int = 0) -> str:
    """The mutated source (unparsed) of the nth mutant of `operator`."""
    mutants = [m for m in enumerate_mutants(source) if m.operator == operator]
    return ast.unparse(apply_mutant(source, mutants[nth].number))


def operators_in(source: str):
    return [m.operator for m in enumerate_mutants(source)]


ORDERED = """
from google.cloud.firestore import Query
async def f(col, account_id):
    q = col.where("account_id", "==", account_id).order_by("created_at", direction=Query.DESCENDING)
    return q
"""


class TestOperators:
    def test_order_by_direction_is_flipped(self):
        assert "direction=Query.ASCENDING" in mutate(ORDERED, "order_by-flip")

    def test_order_by_without_a_direction_becomes_descending(self):
        assert "direction='DESCENDING'" in mutate("def f(q):\n    return q.order_by('x')\n", "order_by-flip")

    def test_order_by_direction_given_as_a_string_or_positionally_flips_too(self):
        assert "'asc'" in mutate("def f(q):\n    return q.order_by('x', 'desc')\n", "order_by-flip")
        assert "direction='DESCENDING'" in mutate("def f(q):\n    return q.order_by('x', direction='ASCENDING')\n", "order_by-flip")

    def test_order_by_is_dropped_from_the_chain(self):
        out = mutate(ORDERED, "order_by-drop")
        assert "order_by" not in out and "col.where('account_id', '==', account_id)" in out

    def test_one_where_is_dropped_and_the_tenancy_filter_is_named(self):
        source = "def f(c, uid, kind):\n    return c.where('user_id', '==', uid).where('kind', '==', kind)\n"
        mutants = [m for m in enumerate_mutants(source) if m.operator == "where-drop"]
        assert len(mutants) == 2
        assert sum("tenancy filter" in m.description for m in mutants) == 1
        by_tenancy = {("tenancy filter" in m.description): ast.unparse(apply_mutant(source, m.number)) for m in mutants}
        assert "user_id" not in by_tenancy[True] and "'kind'" in by_tenancy[True]
        assert "user_id" in by_tenancy[False] and "'kind'" not in by_tenancy[False]
        assert [ast.unparse(apply_mutant(source, m.number)).count(".where(") for m in mutants] == [1, 1]

    def test_select_loses_one_field_per_mutant(self):
        source = "def f(q):\n    return q.select(['a', 'b', 'c'])\n"
        outs = {mutate(source, "select-drop", i) for i in range(3)}
        assert outs == {
            "def f(q):\n    return q.select(['b', 'c'])",
            "def f(q):\n    return q.select(['a', 'c'])",
            "def f(q):\n    return q.select(['a', 'b'])",
        }

    def test_model_dump_include_and_exclude_lose_an_element(self):
        source = "def f(m):\n    return m.model_dump(include={'a', 'b'}), m.model_dump(exclude=['x'])\n"
        assert operators_in(source).count("dump-drop") == 3
        assert "exclude=[]" in mutate(source, "dump-drop", 2)

    def test_a_dict_literal_written_to_a_store_loses_one_key(self):
        source = "def f(ref, a):\n    ref.set({'a': a, 'b': 1, **extra})\n"
        outs = [mutate(source, "doc-drop", i) for i in range(2)]
        assert "ref.set({'b': 1, **extra})" in outs[0] and "ref.set({'a': a, **extra})" in outs[1]

    def test_a_dict_assigned_to_a_name_that_is_written_later_loses_one_key(self):
        source = "def f(ref, a):\n    doc = {'a': a, 'b': 1}\n    ref.update(doc)\n    other = {'z': 1}\n    return other\n"
        mutants = [m for m in enumerate_mutants(source) if m.operator == "doc-drop"]
        assert len(mutants) == 2  # 'z' is never written
        assert "doc = {'b': 1}" in ast.unparse(apply_mutant(source, mutants[0].number))

    @pytest.mark.parametrize(
        "annotation, success",
        [(" -> bool", "True"), (" -> list[int]", "[]"), (" -> Dict[str, int]", "{}"), (" -> str", "''"), (" -> int", "0"),
         (" -> Optional[Foo]", "None"), (" -> None", "None"), ("", "None"), (" -> Foo", "None")],
    )
    def test_except_body_returns_the_success_value_of_the_annotation(self, annotation, success):
        source = f"def f(x){annotation}:\n    try:\n        return x.go()\n    except ValueError as e:\n        raise RuntimeError() from e\n"
        out = mutate(source, "except-success")
        assert f"except ValueError as e:\n        return {success}" in out

    def test_except_that_already_returns_success_and_handlers_outside_functions_are_left_alone(self):
        assert "except-success" not in operators_in("def f() -> bool:\n    try:\n        pass\n    except Exception:\n        return True\n")
        assert "except-success" not in operators_in("try:\n    import x\nexcept ImportError:\n    x = None\n")

    def test_a_handler_that_only_logs_and_returns_success_is_equivalent_but_any_other_effect_keeps_the_mutant(self):
        logs = "def f(x) -> bool:\n    try:\n        return x.go()\n    except Exception as e:\n        logger.warning('x %s', e)\n        log.error('y')\n        return True\n"
        assert "except-success" not in operators_in(logs)
        for effect in ("x.cleanup()", "counter += 1", "self.logger_state = 1", "print('x')"):
            source = f"def f(x) -> bool:\n    try:\n        return x.go()\n    except Exception:\n        {effect}\n        return True\n"
            assert operators_in(source).count("except-success") == 1, effect
        other_value = "def f(x) -> bool:\n    try:\n        return x.go()\n    except Exception:\n        logger.warning('x')\n        return False\n"
        assert "except-success" in operators_in(other_value)

    def test_except_in_an_async_generator_is_not_mutated(self):
        assert "except-success" not in operators_in("async def g():\n    try:\n        yield 1\n    except Exception:\n        raise\n")

    def test_write_skip_replaces_one_statement_with_pass(self):
        source = "async def f(batch, a, b):\n    batch.set(a, 1)\n    await batch.update(b, 2)\n    await batch.commit()\n    d = {}\n    d.items()\n"
        assert operators_in(source).count("write-skip") == 2
        assert mutate(source, "write-skip", 0).count("pass") == 1 and "batch.set" not in mutate(source, "write-skip", 0)
        assert "batch.update" not in mutate(source, "write-skip", 1)

    def test_a_forwarded_keyword_is_dropped_only_for_a_bare_parameter_name(self):
        source = "def f(self, user_id, flag):\n    other = 1\n    return g(user_id=user_id, flag=flag, n=other, k=3, m=user_id.lower())\n"
        mutants = [m for m in enumerate_mutants(source) if m.operator == "kwarg-drop"]
        assert [m.description.split()[2] for m in mutants] == ["user_id=user_id", "flag=flag"]
        assert "g(flag=flag, n=other" in ast.unparse(apply_mutant(source, mutants[0].number))

    def test_numbering_and_keys_are_stable_and_every_mutant_compiles(self):
        source = ORDERED + "\nasync def g(batch, uid, ref):\n    batch.set(ref, {'a': uid})\n    return ref.select(['a', 'b'])\n"
        first, second = enumerate_mutants(source), enumerate_mutants(source)
        assert first == second and len({m.key for m in first}) == len(first) > 5
        for m in first:
            compile(apply_mutant(source, m.number), "<mutant>", "exec")
        assert enumerate_mutants("x = 1\n") == []

    def test_a_mutant_number_out_of_range_is_refused(self):
        with pytest.raises(ValueError, match="does not exist"):
            apply_mutant("x = 1\n", 1)

    def test_a_mutation_changes_exactly_one_site(self):
        original = ast.unparse(ast.parse(ORDERED)).splitlines()
        for m in enumerate_mutants(ORDERED):
            changed = [a for a, b in zip(original, ast.unparse(apply_mutant(ORDERED, m.number)).splitlines()) if a != b]
            assert len(changed) == 1, (m, changed)


@pytest.fixture
def toy_package(tmp_path, monkeypatch):
    (tmp_path / "toy_a.py").write_text("def answer():\n    return 1\n\ndef pair(x):\n    return x.set(1, {'a': 1})\n")
    (tmp_path / "toy_b.py").write_text("def answer():\n    return 1\n\ndef pair(x):\n    return x.set(1, {'a': 1})\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    yield tmp_path
    for name in ("toy_a", "toy_b"):
        sys.modules.pop(name, None)


class TestImportHook:
    def test_only_the_target_module_is_mutated(self, toy_package):
        mutants = enumerate_mutants((toy_package / "toy_a.py").read_text())
        skip = next(m for m in mutants if m.operator == "doc-drop")
        finder = loader.install("toy_a", skip.number)
        try:
            toy_a, toy_b = importlib.import_module("toy_a"), importlib.import_module("toy_b")
            seen_a, seen_b = [], []

            class Recorder:
                def __init__(self, sink):
                    self.sink = sink

                def set(self, key, doc):
                    self.sink.append(doc)

            toy_a.pair(Recorder(seen_a))
            toy_b.pair(Recorder(seen_b))
        finally:
            loader.uninstall(finder)
        assert finder.loaded and seen_a == [{}] and seen_b == [{"a": 1}]
        assert not any(isinstance(f, loader.MutantFinder) for f in sys.meta_path)

    def test_no_bytecode_is_written_for_a_mutant(self, toy_package):
        finder = loader.install("toy_a", 1)
        try:
            importlib.import_module("toy_a")
        finally:
            loader.uninstall(finder)
        assert not list(toy_package.rglob("*.pyc"))

    def test_a_module_that_is_already_imported_cannot_be_mutated(self, toy_package):
        importlib.import_module("toy_a")
        with pytest.raises(RuntimeError, match="already imported"):
            loader.install("toy_a", 1)


TOY_STORE = textwrap.dedent(
    '''
    class Db:
        def __init__(self):
            self.docs = {}

        def set(self, key, doc):
            self.docs[key] = dict(doc)


    class Query:
        def __init__(self, rows):
            self.rows = rows

        def where(self, field, value):
            return Query([r for r in self.rows if r[field] == value])

        def order_by(self, field, direction="ASCENDING"):
            return Query(sorted(self.rows, key=lambda r: r[field], reverse=direction == "DESCENDING"))

        def select(self, fields):
            return Query([{f: r[f] for f in fields} for r in self.rows])

        def all(self):
            return list(self.rows)


    class Store:
        def __init__(self, db):
            self.db = db

        def put(self, tenant, text, rank):
            self.db.set(f"{tenant}:{text}", {"tenant": tenant, "text": text, "rank": rank})

        def top(self, tenant):
            return Query(list(self.db.docs.values())).where("tenant", tenant).order_by("rank", direction="DESCENDING").select(["text", "rank"]).all()
    '''
)
STRONG = textwrap.dedent(
    """
    from toy_store import Db, Store


    def test_top_is_per_tenant_sorted_and_complete():
        store = Store(Db())
        for tenant, text, rank in [("t1", "a", 1), ("t1", "b", 3), ("t1", "c", 2), ("t2", "z", 9)]:
            store.put(tenant, text, rank)
        assert store.top("t1") == [{"text": "b", "rank": 3}, {"text": "c", "rank": 2}, {"text": "a", "rank": 1}]
        assert store.top("t2") == [{"text": "z", "rank": 9}]
        assert store.db.docs["t2:z"] == {"tenant": "t2", "text": "z", "rank": 9}
    """
)
WEAK = textwrap.dedent(
    """
    from toy_store import Db, Store


    def test_top_returns_rows():
        store = Store(Db())
        store.put("t1", "a", 1)
        assert len(store.top("t1")) == 1
    """
)


@pytest.mark.slow  # runs child pytest processes
class TestDriver:
    @pytest.fixture
    def toy(self, tmp_path):
        (tmp_path / "toy_store.py").write_text(TOY_STORE)
        (tmp_path / "test_strong.py").write_text(STRONG)
        (tmp_path / "test_weak.py").write_text(WEAK)
        return tmp_path

    def run(self, toy, tests, *extra):
        return subprocess.run(
            [sys.executable, "-m", "tests.support.semantic_mutants", "--module", "toy_store", "--tests", *tests,
             "--cwd", str(toy), *extra],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=300,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTEST_SUITE_LOCK": "0"},
        )

    def test_a_weak_test_lets_the_defects_survive_and_the_exit_code_says_so(self, toy):
        done = self.run(toy, ["test_weak.py"])
        assert done.returncode == 1, done.stdout + done.stderr
        survivors = done.stdout.split("SURVIVORS")[1]
        assert "where-drop" in survivors and "order_by-flip" in survivors and "order_by-drop" in survivors
        assert "write-skip" not in survivors  # the weak test notices an empty store

    def test_a_strong_test_kills_every_mutant(self, toy):
        done = self.run(toy, ["test_strong.py"])
        assert done.returncode == 0, done.stdout + done.stderr
        assert "0 survived" in done.stdout and "SURVIVORS" not in done.stdout

    def test_a_reviewer_rules_a_mutant_equivalent_with_its_key(self, toy):
        weak = self.run(toy, ["test_weak.py"])
        keys = [line.split("key=")[1] for line in weak.stdout.split("SURVIVORS")[1].splitlines() if "key=" in line]
        (toy / "equivalent.txt").write_text("".join(f"{key}  # unobservable in the toy\n" for key in keys))
        done = self.run(toy, ["test_weak.py"], "--equivalent", str(toy / "equivalent.txt"))
        assert done.returncode == 0, done.stdout
        assert f"{len(keys)} ruled equivalent" in done.stdout and "SURVIVORS" not in done.stdout

    def test_tests_that_never_load_the_module_are_an_error_not_a_survivor(self, toy):
        (toy / "test_unrelated.py").write_text("def test_nothing():\n    assert True\n")
        done = self.run(toy, ["test_unrelated.py"])
        assert done.returncode == 2, done.stdout + done.stderr
        assert "never loaded" in done.stdout and "SURVIVORS" not in done.stdout

    def test_a_failing_baseline_stops_the_run(self, toy):
        (toy / "test_red.py").write_text("def test_red():\n    assert False\n")
        done = self.run(toy, ["test_red.py"])
        assert done.returncode == 2 and "baseline" in done.stdout
