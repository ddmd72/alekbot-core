"""tests/support/semantic_mutants, the `wiring` operator set: wire-drop and call-skip, their selection and the root module `main`."""

import ast
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.support.semantic_mutants import driver
from tests.support.semantic_mutants.operators import apply_mutant, enumerate_mutants

REPO_ROOT = Path(__file__).resolve().parents[2]


def wiring(source: str, operator: str):
    return [m for m in enumerate_mutants(source, "wiring") if m.operator == operator]


def mutated(source: str, operator: str, nth: int = 0) -> str:
    return ast.unparse(apply_mutant(source, wiring(source, operator)[nth].number, "wiring"))


def all_mutated(source: str, operator: str):
    return [ast.unparse(apply_mutant(source, m.number, "wiring")) for m in wiring(source, operator)]


class TestWireDrop:
    def test_each_keyword_of_a_class_call_becomes_none_one_at_a_time(self):
        source = "def f(repo, queue):\n    return Service(repo=repo, queue=queue, retries=3)\n"
        assert all_mutated(source, "wire-drop") == [
            "def f(repo, queue):\n    return Service(repo=None, queue=queue, retries=3)",
            "def f(repo, queue):\n    return Service(repo=repo, queue=None, retries=3)",
        ]

    def test_each_positional_argument_becomes_none(self):
        source = "def f(a, b):\n    return Service(a, 'x', b)\n"
        assert all_mutated(source, "wire-drop") == [
            "def f(a, b):\n    return Service(None, 'x', b)",
            "def f(a, b):\n    return Service(a, 'x', None)",
        ]

    def test_create_and_build_functions_and_attribute_callables_are_wiring_calls(self):
        source = (
            "def f(a):\n"
            "    x = create_thing(a)\n"
            "    y = build_other(k=a)\n"
            "    z = factory.build_more(a)\n"
            "    return module.Handler(h=a), x, y, z\n"
        )
        assert len(wiring(source, "wire-drop")) == 4
        assert "create_thing(None)" in mutated(source, "wire-drop", 0)
        assert "build_other(k=None)" in mutated(source, "wire-drop", 1)
        assert "factory.build_more(None)" in mutated(source, "wire-drop", 2)
        assert "module.Handler(h=None)" in mutated(source, "wire-drop", 3)

    def test_an_awaited_wiring_call_is_mutated_too(self):
        source = "async def f(a):\n    return await build_thing(a)\n"
        assert mutated(source, "wire-drop") == "async def f(a):\n    return await build_thing(None)"

    def test_a_dict_literal_value_becomes_none_anywhere_in_the_module(self):
        source = "TABLE = {'a': make(1), 'b': 2}\n\ndef f(x):\n    return log(extra={'k': x, 'n': 1})\n"
        assert all_mutated(source, "wire-drop") == [
            "TABLE = {'a': None, 'b': 2}\n\ndef f(x):\n    return log(extra={'k': x, 'n': 1})",
            "TABLE = {'a': make(1), 'b': 2}\n\ndef f(x):\n    return log(extra={'k': None, 'n': 1})",
        ]

    def test_a_dict_spread_is_left_alone_and_its_neighbours_are_not(self):
        source = "def f(base, v):\n    return {**base, 'k': v}\n"
        assert all_mutated(source, "wire-drop") == ["def f(base, v):\n    return {**base, 'k': None}"]

    def test_constants_are_never_mutated(self):
        source = (
            "def f():\n"
            "    x = Service(1, 'a', True, None, -1, 2.5, b'z', f'plain', retries=3, name='n', flag=False, neg=-2)\n"
            "    y = build_x('a', k=None)\n"
            "    return {'a': 1, 'b': 'two', 'c': None, 'd': -3, 'e': True}, x, y\n"
        )
        assert wiring(source, "wire-drop") == []

    def test_an_fstring_with_a_placeholder_is_not_a_constant(self):
        source = "def f(tenant):\n    return Queue(name=f'q-{tenant}')\n"
        assert mutated(source, "wire-drop") == "def f(tenant):\n    return Queue(name=None)"

    def test_only_camel_case_create_and_build_calls_are_wiring_calls(self):
        source = (
            "def f(a, b):\n"
            "    plain(a, k=b)\n"
            "    obj.method(a, k=b)\n"
            "    API(a, k=b)\n"
            "    CONSTANT_CASE(a)\n"
            "    created(a)\n"
            "    rebuild_it(a)\n"
            "    builder(a)\n"
            "    logger.info('x', extra=a)\n"
            "    dict(a=a)\n"
            "    len(a)\n"
        )
        assert wiring(source, "wire-drop") == []

    def test_star_arguments_are_left_alone(self):
        source = "def f(args, kwargs, a):\n    return Service(*args, a, **kwargs)\n"
        assert all_mutated(source, "wire-drop") == ["def f(args, kwargs, a):\n    return Service(*args, None, **kwargs)"]

    def test_a_call_nested_in_an_argument_is_mutated_on_its_own(self):
        source = "def f(a):\n    return Outer(inner=Inner(x=a))\n"
        assert all_mutated(source, "wire-drop") == [
            "def f(a):\n    return Outer(inner=None)",
            "def f(a):\n    return Outer(inner=Inner(x=None))",
        ]

    def test_a_leading_underscore_does_not_hide_a_wiring_call(self):
        source = "def f(a, b):\n    x = _UserContext(profile=a)\n    y = _build_x(a)\n    z = _plain(b)\n    return x, y, z\n"
        assert all_mutated(source, "wire-drop") == [
            "def f(a, b):\n    x = _UserContext(profile=None)\n    y = _build_x(a)\n    z = _plain(b)\n    return (x, y, z)",
            "def f(a, b):\n    x = _UserContext(profile=a)\n    y = _build_x(None)\n    z = _plain(b)\n    return (x, y, z)",
        ]

    def test_a_collection_of_constants_is_a_constant_and_an_empty_one_too(self):
        source = (
            "def f():\n"
            "    return Foo(caps=['a', 'b'], t=('x', 1), s={'a'}, d={'k': 1, 'n': ['z']}, e=[], f={}, g=(), n=[['a'], ()])\n"
        )
        assert wiring(source, "wire-drop") == []
        assert wiring("X = {'a': [], 'b': {}, 'c': (1, 2)}\n", "wire-drop") == []

    def test_a_collection_with_one_non_constant_element_is_still_mutated(self):
        source = "def f(a):\n    return Foo(caps=['x', a], d={'k': a}, e={**a})\n"
        assert all_mutated(source, "wire-drop") == [
            "def f(a):\n    return Foo(caps=None, d={'k': a}, e={**a})",
            "def f(a):\n    return Foo(caps=['x', a], d=None, e={**a})",
            "def f(a):\n    return Foo(caps=['x', a], d={'k': a}, e=None)",
            "def f(a):\n    return Foo(caps=['x', a], d={'k': None}, e={**a})",
        ]

    def test_an_attribute_assignment_on_another_object_becomes_none(self):
        source = (
            "def f(agent, adapter, coordinator, fn):\n"
            "    agent.coordinator = coordinator\n"
            "    agent._quota_service = coordinator.quota\n"
            "    adapter.start = fn\n"
            "    adapter.a.b = fn\n"
        )
        assert all_mutated(source, "wire-drop") == [
            "def f(agent, adapter, coordinator, fn):\n    agent.coordinator = None\n    agent._quota_service = coordinator.quota\n    adapter.start = fn\n    adapter.a.b = fn",
            "def f(agent, adapter, coordinator, fn):\n    agent.coordinator = coordinator\n    agent._quota_service = None\n    adapter.start = fn\n    adapter.a.b = fn",
            "def f(agent, adapter, coordinator, fn):\n    agent.coordinator = coordinator\n    agent._quota_service = coordinator.quota\n    adapter.start = None\n    adapter.a.b = fn",
            "def f(agent, adapter, coordinator, fn):\n    agent.coordinator = coordinator\n    agent._quota_service = coordinator.quota\n    adapter.start = fn\n    adapter.a.b = None",
        ]

    def test_other_assignments_are_left_alone(self):
        source = (
            "class C:\n"
            "    def f(self, cls, agent, d, x):\n"
            "        self.x = x\n"
            "        self.a.b = x\n"
            "        cls.y = x\n"
            "        agent.n = 1\n"
            "        agent.caps = ['a']\n"
            "        d['k'] = x\n"
            "        agent.p = agent.q = x\n"
            "        local = x\n"
            "        agent.z: int = x\n"
        )
        assert wiring(source, "wire-drop") == []

    def test_a_task_spawn_and_an_exception_message_are_not_wiring(self):
        source = (
            "async def f(coro, uid):\n"
            "    asyncio.create_task(coro())\n"
            "    create_task(coro)\n"
            "    raise ValueError(f'User {uid} not found')\n"
            "    raise _CustomException(uid)\n"
            "    raise PermissionError(uid)\n"
        )
        assert wiring(source, "wire-drop") == []
        assert len(wiring("def f(a):\n    return create_thing(a), ErrorHandler(a), ExceptionLog(a)\n", "wire-drop")) == 3

    def test_a_conditional_expression_argument_is_replaced_as_a_whole_and_its_calls_on_their_own(self):
        source = "def f(q, c):\n    return Bar(svc=Baz(q) if c else None)\n"
        assert all_mutated(source, "wire-drop") == [
            "def f(q, c):\n    return Bar(svc=None)",
            "def f(q, c):\n    return Bar(svc=Baz(None) if c else None)",
        ]

    def test_a_call_in_a_lambda_body_is_mutated(self):
        source = "def f(reg):\n    reg.register_factory('slack', lambda cid: SlackChannel(client=cid))\n"
        assert all_mutated(source, "wire-drop") == [
            "def f(reg):\n    reg.register_factory('slack', lambda cid: SlackChannel(client=None))",
        ]


class TestCallSkip:
    @pytest.mark.parametrize(
        "statement",
        ["app.register_blueprint(bp)", "registry.register(key, value)", "svc.attach_worker(w)", "svc.set_agent_factory(f)",
         "self.container.set_x(1)", "factory.register_factory('a', build)"],
    )
    def test_each_wiring_statement_is_replaced_by_pass(self, statement):
        assert mutated(f"def f(app, registry, svc, factory, bp, key, value, w, f):\n    {statement}\n", "call-skip").endswith("    pass")

    def test_an_awaited_statement_is_skipped_too(self):
        source = "async def f(svc):\n    await svc.attach_worker(1)\n    return 1\n"
        assert mutated(source, "call-skip") == "async def f(svc):\n    pass\n    return 1"

    def test_only_the_chosen_statement_is_dropped(self):
        source = "def f(app, a, b):\n    app.register_blueprint(a)\n    app.register_blueprint(b)\n"
        assert all_mutated(source, "call-skip") == [
            "def f(app, a, b):\n    pass\n    app.register_blueprint(b)",
            "def f(app, a, b):\n    app.register_blueprint(a)\n    pass",
        ]

    def test_other_statement_calls_are_left_alone(self):
        source = (
            "def f(app, svc, d):\n"
            "    app.add_url_rule('/', view_func=svc)\n"
            "    svc.reset_x(1)\n"
            "    svc.registered()\n"
            "    svc.setup()\n"
            "    svc.unregister(1)\n"
            "    d.set(1, {})\n"
            "    d.update({})\n"
            "    register_blueprint(app)\n"
            "    set_value(1)\n"
        )
        assert wiring(source, "call-skip") == []

    def test_a_call_whose_value_is_used_is_not_a_statement_call(self):
        source = "def f(svc):\n    token = svc.register(1)\n    other = svc.set_x(2)\n    return svc.attach_y(3)\n"
        assert wiring(source, "call-skip") == []

    def test_logging_calls_are_left_alone(self):
        source = (
            "def f(logger, svc):\n"
            "    logger.info('registering')\n"
            "    logger.warning('attach_worker failed')\n"
            "    logger.set_context(user=1)\n"
            "    self.logger.set_level(10)\n"
            "    logging.getLogger('x').set_level(10)\n"
            "    svc.attach_worker(1)\n"
        )
        assert [m.lineno for m in wiring(source, "call-skip")] == [7]


    def test_a_leading_underscore_does_not_hide_a_registration(self):
        source = "def f(self, agents):\n    self._register_agents(agents)\n    self._plain(agents)\n"
        assert all_mutated(source, "call-skip") == ["def f(self, agents):\n    pass\n    self._plain(agents)"]

    def test_only_a_receiver_that_is_a_logger_is_exempt(self):
        source = (
            "def f(logger_agent, catalog, dialog, login, get, logger):\n"
            "    logger_agent.attach_x(1)\n"
            "    catalog.register(1)\n"
            "    dialog.set_x(1)\n"
            "    login.set_x(1)\n"
            "    get(logger).register(1)\n"
            "    logger.set_x(1)\n"
        )
        assert [m.lineno for m in wiring(source, "call-skip")] == [2, 3, 4, 5, 6]

    @pytest.mark.parametrize("receiver", ["logger", "log", "logging", "_logger", "self.logger", "self._logger", "app_logger",
                                          "logging.getLogger('x')", "getLogger()"])
    def test_every_logger_receiver_form_is_exempt(self, receiver):
        assert wiring(f"def f(self):\n    {receiver}.set_x(1)\n", "call-skip") == []


class TestSelection:
    DATA = (
        "async def f(col, ref, uid):\n"
        "    q = col.where('user_id', '==', uid).order_by('at')\n"
        "    ref.set({'a': 1})\n"
        "    try:\n"
        "        return q.select(['a', 'b'])\n"
        "    except ValueError:\n"
        "        raise\n"
    )
    WIRING = "def f(app, bp, repo):\n    app.register_blueprint(bp)\n    return Service(repo=repo, cfg={'k': repo})\n"

    def test_the_default_set_has_neither_wiring_operator(self):
        assert {m.operator for m in enumerate_mutants(self.WIRING)} <= {"kwarg-drop", "doc-drop", "write-skip", "except-success"}
        assert not [m for m in enumerate_mutants(self.WIRING) if m.operator in ("wire-drop", "call-skip")]
        assert enumerate_mutants(self.WIRING) == enumerate_mutants(self.WIRING, "default")

    def test_the_default_set_numbering_is_not_changed_by_the_wiring_set(self):
        assert [m.operator for m in enumerate_mutants(self.DATA)] == [
            "order_by-flip", "order_by-drop", "where-drop", "write-skip", "doc-drop", "select-drop", "select-drop", "except-success",
        ]

    def test_the_wiring_set_has_only_its_two_operators(self):
        assert {m.operator for m in enumerate_mutants(self.WIRING, "wiring")} == {"wire-drop", "call-skip"}
        assert enumerate_mutants(self.DATA, "wiring") == []

    def test_the_wiring_set_numbers_its_mutants_stably_and_every_one_compiles(self):
        first, second = enumerate_mutants(self.WIRING, "wiring"), enumerate_mutants(self.WIRING, "wiring")
        assert first == second and [m.number for m in first] == [1, 2, 3, 4] and len({m.key for m in first}) == 4
        for m in first:
            compile(apply_mutant(self.WIRING, m.number, "wiring"), "<mutant>", "exec")

    def test_a_wiring_mutation_changes_exactly_one_site(self):
        original = ast.unparse(ast.parse(self.WIRING)).splitlines()
        for m in enumerate_mutants(self.WIRING, "wiring"):
            changed = [a for a, b in zip(original, ast.unparse(apply_mutant(self.WIRING, m.number, "wiring")).splitlines()) if a != b]
            assert len(changed) == 1, (m, changed)

    def test_a_mutant_number_out_of_range_is_refused_in_the_wiring_set(self):
        with pytest.raises(ValueError, match="does not exist"):
            apply_mutant("x = 1\n", 1, "wiring")

    def test_an_unknown_operator_set_is_refused(self):
        with pytest.raises(ValueError, match="unknown operator set"):
            enumerate_mutants(self.WIRING, "everything")

    def test_the_mutant_child_does_not_inherit_the_callers_operator_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SEMANTIC_MUTANT_OPERATORS", "wiring")
        assert "SEMANTIC_MUTANT_OPERATORS" not in driver._child_env(tmp_path, {})
        assert driver._child_env(tmp_path, {"SEMANTIC_MUTANT_OPERATORS": "wiring"})["SEMANTIC_MUTANT_OPERATORS"] == "wiring"


TOY_WIRING = textwrap.dedent(
    '''
    class Repo:
        pass


    class Service:
        def __init__(self, repo):
            self.repo = repo


    class Registry:
        def __init__(self):
            self.items = {}

        def register_service(self, name, service):
            self.items[name] = service


    def build_app(registry, repo):
        service = Service(repo=repo)
        registry.register_service("svc", service)
        return {"service": service, "repo": repo}
    '''
)
STRONG = textwrap.dedent(
    """
    from toy_wiring import Registry, Repo, build_app


    def test_the_app_is_wired():
        registry, repo = Registry(), Repo()
        app = build_app(registry, repo)
        assert app["service"].repo is repo and app["repo"] is repo
        assert registry.items == {"svc": app["service"]}
    """
)
WEAK = textwrap.dedent(
    """
    from toy_wiring import Registry, Repo, build_app


    def test_the_app_is_built():
        assert build_app(Registry(), Repo()) is not None
    """
)


@pytest.mark.slow  # runs child pytest processes
class TestDriverFlag:
    @pytest.fixture
    def toy(self, tmp_path):
        (tmp_path / "toy_wiring.py").write_text(TOY_WIRING)
        (tmp_path / "test_strong.py").write_text(STRONG)
        (tmp_path / "test_weak.py").write_text(WEAK)
        return tmp_path

    def run(self, toy, tests, *extra):
        return subprocess.run(
            [sys.executable, "-m", "tests.support.semantic_mutants", "--module", "toy_wiring", "--tests", *tests,
             "--cwd", str(toy), *extra],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=300,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTEST_SUITE_LOCK": "0"},
        )

    def test_without_the_flag_the_wiring_operators_do_not_run(self, toy):
        done = self.run(toy, ["test_weak.py"])
        assert "wire-drop" not in done.stdout and "call-skip" not in done.stdout, done.stdout

    def test_the_flag_runs_the_wiring_operators_and_a_weak_test_lets_them_survive(self, toy):
        done = self.run(toy, ["test_weak.py"], "--operators", "wiring")
        assert done.returncode == 1, done.stdout + done.stderr
        assert "semantic mutants of toy_wiring: 4" in done.stdout
        survivors = done.stdout.split("SURVIVORS")[1]
        assert "wire-drop" in survivors and "call-skip" in survivors  # the weak test only checks that something came back
        assert "where-drop" not in done.stdout and "kwarg-drop" not in done.stdout

    def test_a_strong_test_kills_every_wiring_mutant(self, toy):
        done = self.run(toy, ["test_strong.py"], "--operators", "wiring")
        assert done.returncode == 0, done.stdout + done.stderr
        assert "4 mutants: 4 killed, 0 survived" in done.stdout

    def test_an_unknown_operator_set_is_a_usage_error(self, toy):
        done = self.run(toy, ["test_strong.py"], "--operators", "everything")
        assert done.returncode == 2 and "invalid choice" in done.stderr


ROOT_MODULE_CHECK = textwrap.dedent(
    """
    import json, os, sys, types

    sys.path.insert(0, os.getcwd())
    from tests.support.semantic_mutants import activate

    import main  # the root module, found by the path finder on the repository root


    def codes(code):
        found = {(code.co_name, code.co_firstlineno, code.co_code)}
        for const in code.co_consts:
            if isinstance(const, types.CodeType):
                found |= codes(const)
        return found


    original = compile(open(main.__file__).read(), main.__file__, "exec")
    expected = set()
    for const in original.co_consts:  # every function of the file, nested ones included
        if isinstance(const, types.CodeType):
            expected |= codes(const)
    loaded = set()
    for value in vars(main).values():
        if isinstance(value, types.FunctionType) and value.__module__ == "main":
            loaded |= codes(value.__code__)
    print(json.dumps({"loaded": activate._FINDER.loaded, "file": main.__file__, "changed": len(expected - loaded),
                      "total": len(expected)}))
    """
)


@pytest.mark.slow  # imports the whole application graph in a child process
class TestRootModule:
    def test_the_root_module_main_is_mutated_and_reported_as_loaded(self):
        source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        skip = next(m for m in enumerate_mutants(source, "wiring") if m.operator == "call-skip")
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "SEMANTIC_MUTANT_MODULE": "main",
               "SEMANTIC_MUTANT_NUMBER": str(skip.number), "SEMANTIC_MUTANT_OPERATORS": "wiring"}
        done = subprocess.run([sys.executable, "-c", ROOT_MODULE_CHECK], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=300)
        assert done.returncode == 0, done.stdout + done.stderr
        report = json.loads(done.stdout.strip().splitlines()[-1])
        assert report["loaded"] is True and report["total"] > 1
        assert report["changed"] >= 1  # some code object of the module differs from the unmutated compile
        assert Path(report["file"]) == REPO_ROOT / "main.py"

    def test_the_driver_finds_main_and_counts_its_mutants(self):
        assert driver.module_path("main", REPO_ROOT) == REPO_ROOT / "main.py"
        assert len(enumerate_mutants((REPO_ROOT / "main.py").read_text(encoding="utf-8"), "wiring")) > 0
