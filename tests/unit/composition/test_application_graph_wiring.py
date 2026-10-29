"""The production application graph holds what production needs: a `None` ledger and an instance ledger, both exact (P9 design §5a, §5e).

`prod_graph` builds the real `build_application_graph(config, db)` over the deploy's sealed environment with fakes at the
SDK boundaries, starts it, and builds one user's eager and lazy agents. `graph_walk` reads every value the graph holds:

- every attribute that is `None` must be a line of `wiring_none_ledger.txt` with a reason, and every line must still be
  true (a dependency that production should have and does not is the #148 class);
- the container-owned adapters and services are counted per class and must equal `wiring_instance_ledger.txt` (a second
  construction of a repository is the other half of #148);
- the platform seams: Telegram is built, the workers are attached by identity, both channel factories answer.

Each test function builds the graph once. Pure checks (the walk, the ledger comparison) have fast self-tests on small
inline graphs; the checks that read the real graph each have one test that breaks a thing in it and expects the failure.
"""
import asyncio
import copy
import functools
import types
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import List

import pytest
from pydantic import BaseModel

from src.agents.base_agent import BaseAgent
from tests.support import graph_walk, wiring_ledger
from tests.support.cloud_tasks_fake import FakeCloudTasksClient
from tests.support.prod_graph import ProdGraph, prod_graph  # noqa: F401  (the fixture)

HERE = Path(__file__).parent
NONE_LEDGER = HERE / "wiring_none_ledger.txt"
INSTANCE_LEDGER = HERE / "wiring_instance_ledger.txt"


# ----------------------------------------------------------------------------- the checks on the real graph

def none_problems(app) -> List[str]:
    nodes = graph_walk.walk(app)
    entries = wiring_ledger.parse(NONE_LEDGER.read_text())
    return wiring_ledger.compare_nones(graph_walk.none_attributes(nodes), entries, graph_walk.attribute_values(nodes))


def instance_problems(app) -> List[str]:
    entries = wiring_ledger.parse(INSTANCE_LEDGER.read_text())
    counts = graph_walk.instance_counts(graph_walk.walk_outside_agents(app))
    stores = graph_walk.store_counts(graph_walk.walk(app))
    return wiring_ledger.compare_counts(counts, entries) + wiring_ledger.compare_store_counts(stores, entries)


def platform_problems(graph: ProdGraph) -> List[str]:
    from src.domain.language import LanguageCode

    app = graph.app
    worker = app.worker_handler
    factory = app.notification_service._channel_factory
    problems = []

    def check(ok: bool, message: str) -> None:
        if not ok:
            problems.append(message)

    check(app.telegram_adapter is not None, "the Telegram adapter was not built (TELEGRAM_BOT_TOKEN is in the deploy)")
    check(worker._agent_worker is app.agent_worker_handler, "WorkerHandler does not hold the graph's AgentWorkerHandler")
    check(worker._slack_worker == app.slack_adapter.handle_queued_event, "the Slack worker is not the Slack adapter's handle_queued_event")
    check(app.telegram_adapter is not None and worker._telegram_worker == app.telegram_adapter.handle_queued_update,
          "the Telegram worker is not the Telegram adapter's handle_queued_update")
    check(sorted(factory._factories) == ["slack", "telegram"], f"channel factories are {sorted(factory._factories)}, not slack and telegram")
    check(sorted(app.notification_service._platform_media) == ["slack", "telegram"],
          f"platform media are {sorted(app.notification_service._platform_media)}, not slack and telegram")
    check(factory.create("slack", "C1") is not None, "the channel factory builds no Slack channel")
    check(factory.create("telegram", "1") is not None, "the channel factory builds no Telegram channel")
    check(app.language_service._system_default == LanguageCode.UK, "the language default is not SYSTEM_DEFAULT_LANGUAGE=uk of the deploy")
    # D1: a synchronous document item is delivered by the ConversationHandler through the same service the async path uses.
    handlers = {"slack": app.slack_adapter.conversation_handler,
                "telegram": app.telegram_adapter.conversation_handler if app.telegram_adapter else None}
    for platform, handler in handlers.items():
        check(handler is not None and handler._doc_delivery_service is not None, f"{platform} ConversationHandler has no doc_delivery_service")
        check(handler is not None and handler._doc_delivery_service is app.agent_worker_handler._doc_delivery_service,
              f"{platform} ConversationHandler does not share the async path's doc_delivery_service")
    return problems


def started_state_problems(graph: ProdGraph) -> List[str]:
    app = graph.app
    problems = []
    loops = {"billing_agent._flush_task": app.billing_agent._flush_task,
             "logger_agent._flush_task": app.logger_agent._flush_task,
             "agent_factory._sweep_task": app.agent_factory._sweep_task}
    for name, task in loops.items():
        if task is None or task.done():
            problems.append(f"{name} is not running after start()")
    if app.agent_task_queue is None:
        problems.append("there is no agent-task queue (GOOGLE_CLOUD_PROJECT is in the deploy)")
    elif not any(c.created_queues or c.get_queue_calls for c in FakeCloudTasksClient.instances):
        problems.append("start() did not ask Cloud Tasks for the agent-task queue")
    failed = sorted(agent_type for agent_type, built in graph.lazy.items() if built is not True)
    if failed:
        problems.append(f"lazy agents that create_agent_on_demand did not build: {failed} (ledger them with a reason, or fix)")
    return problems


# ----------------------------------------------------------------------------- the production graph

@pytest.mark.slow
async def test_every_none_in_the_production_graph_is_ledgered_and_every_line_is_true(prod_graph):
    problems = none_problems(prod_graph.app)
    assert not problems, "wiring_none_ledger.txt and the graph disagree:\n" + "\n".join(problems)


@pytest.mark.slow
async def test_the_container_owned_instances_equal_the_ledger(prod_graph):
    problems = instance_problems(prod_graph.app)
    assert not problems, "wiring_instance_ledger.txt and the graph disagree:\n" + "\n".join(problems)


@pytest.mark.slow
async def test_the_platforms_the_workers_and_the_started_state_are_wired(prod_graph):
    problems = platform_problems(prod_graph) + started_state_problems(prod_graph)
    assert not problems, "\n".join(problems)


# ----------------------------------------------------------------------------- the checks fire on the real graph

@pytest.mark.slow
async def test_a_dependency_lost_from_the_graph_is_reported_by_its_path(prod_graph):
    handler = prod_graph.app.slack_adapter.conversation_handler
    shared = prod_graph.app.notification_service._platform_rich_content["slack"]
    # The chat handler and the notification service hold one RichContentService; the walk reports it by the path it reaches first
    assert handler._rich_content_service is shared
    handler._rich_content_service._media_port = None

    problems = none_problems(prod_graph.app)

    assert any("unledgered None" in p and "app.notification_service._platform_rich_content['slack']._media_port" in p
               for p in problems), problems


@pytest.mark.slow
async def test_a_second_construction_of_a_repository_is_reported_by_class(prod_graph):
    container = prod_graph.app.container
    container.second_repository = copy.copy(container.repository)

    problems = instance_problems(prod_graph.app)

    repo_class = type(container.repository).__qualname__
    assert any(repo_class in p and "ledger says 1, the graph has 2" in p for p in problems), problems


@pytest.mark.slow
async def test_a_broken_platform_seam_is_reported(prod_graph):
    app = prod_graph.app
    app.worker_handler._telegram_worker = None
    app.slack_adapter.conversation_handler._doc_delivery_service = None
    app.agent_factory._sweep_task.cancel()
    await asyncio.gather(app.agent_factory._sweep_task, return_exceptions=True)
    prod_graph.lazy["lelik"] = False

    problems = platform_problems(prod_graph) + started_state_problems(prod_graph)

    assert any("Telegram worker" in p for p in problems), problems
    assert any("slack ConversationHandler has no doc_delivery_service" in p for p in problems), problems
    assert any("agent_factory._sweep_task is not running" in p for p in problems), problems
    assert any("['lelik']" in p for p in problems), problems


@pytest.mark.slow
async def test_a_telegram_adapter_that_was_not_built_is_reported(prod_graph):
    prod_graph.app.telegram_adapter = None

    problems = platform_problems(prod_graph)

    assert any("Telegram adapter was not built" in p for p in problems), problems


@pytest.mark.slow
async def test_a_second_store_built_inside_an_agent_is_reported_by_the_full_walk(prod_graph):
    """The #148 shape one level down: the container-owned count does not enter agents, the store count does."""
    app = prod_graph.app
    agent = app.coordinator.agents["smart_response_agent_u1"]
    agent.private_repository = copy.copy(app.container.repository)

    problems = instance_problems(app)

    repo_class = type(app.container.repository).__qualname__
    owned = wiring_ledger.compare_counts(graph_walk.instance_counts(graph_walk.walk_outside_agents(app)),
                                         wiring_ledger.parse(INSTANCE_LEDGER.read_text()))
    assert owned == [], "the container-owned count must not see it"
    assert any(repo_class in p and "2 instance(s) in the full walk" in p and "ledger says 1" in p for p in problems), problems


@pytest.mark.slow
async def test_a_second_coordinator_a_second_handler_and_a_second_factory_are_reported(prod_graph):
    app = prod_graph.app
    app.container.second_coordinator = copy.copy(app.coordinator)
    app.container.second_worker = copy.copy(app.worker_handler)
    app.container.second_factory = copy.copy(app.agent_factory)
    app.container.second_handler = copy.copy(app.slack_adapter.conversation_handler)

    problems = instance_problems(app)

    for name, was, now in (("AgentCoordinator", 1, 2), ("WorkerHandler", 1, 2), ("UserAgentFactory", 1, 2), ("ConversationHandler", 2, 3)):
        assert any(name in p and f"ledger says {was}, the graph has {now}" in p for p in problems), (name, problems)


def drop_from_factory(monkeypatch, factory_class, **dropped) -> None:
    """Make `factory_class.create_adapter` receive `None` for the named keyword arguments, as a lost line in the graph would."""
    original = factory_class.create_adapter

    def create_adapter(*args, **kwargs):
        kwargs.update({name: None for name in dropped})
        return original(*args, **kwargs)

    monkeypatch.setattr(factory_class, "create_adapter", staticmethod(create_adapter))


@pytest.mark.slow
async def test_a_dependency_dropped_while_building_an_adapter_is_reported_twice_over(monkeypatch):
    """The D1 shape: the graph stops passing `doc_delivery_service` to the Slack handler and `memory_fact_service` to Telegram's."""
    from src.composition.slack_adapter_factory import SlackAdapterFactory
    from src.composition.telegram_adapter_factory import TelegramAdapterFactory
    from tests.support import prod_graph as builder

    drop_from_factory(monkeypatch, SlackAdapterFactory, doc_delivery_service=None)
    drop_from_factory(monkeypatch, TelegramAdapterFactory, memory_fact_service=None)
    graph = await builder.build(monkeypatch)
    try:
        nones = none_problems(graph.app)
        platform = platform_problems(graph)
    finally:
        await graph.app.shutdown()

    assert any("unledgered None: app.slack_adapter.conversation_handler._doc_delivery_service" in p for p in nones), nones
    assert any("unledgered None: app.telegram_adapter.conversation_handler._memory_facts" in p for p in nones), nones
    assert any("slack ConversationHandler has no doc_delivery_service" in p for p in platform), platform


# ----------------------------------------------------------------------------- the walk, on small inline graphs

def src_class(name: str, module: str = "src.adapters.fake", bases=(object,), **attrs):
    """A class that says it lives in `module`, so the walk treats it as part of the application."""
    return type(name, bases, {"__module__": module, **attrs})


def make(klass, **attributes):
    obj = object.__new__(klass)
    obj.__dict__.update(attributes)
    return obj


Holder = src_class("Holder")
Leaf = src_class("Leaf")


def paths(nodes):
    return {n.path for n in nodes}


def test_the_walk_follows_attributes_containers_methods_closures_and_partials():
    leaf = make(Leaf, value=None)
    held_by_method = make(Holder, leaf=leaf)

    def closure_over(held):
        def inner():
            return held
        return inner

    root = make(
        Holder,
        in_list=[make(Leaf, x=1)],
        in_dict={"k": make(Leaf, x=2)},
        in_tuple=(make(Leaf, x=3),),
        in_set={make(Leaf, x=4)},
        method=types_method(held_by_method),
        closure=closure_over(make(Leaf, x=5)),
        partial=functools.partial(lambda a, b=None: None, make(Leaf, x=6), b=make(Leaf, x=7)),
    )

    found = paths(graph_walk.walk(root))

    for expected in ("app.in_list[0]", "app.in_dict['k']", "app.in_tuple[0]", "app.in_set{0}", "app.method.__self__",
                     "app.method.__self__.leaf", "app.closure.<closure held>", "app.partial.args[0]", "app.partial.keywords[b]"):
        assert expected in found, (expected, sorted(found))


def types_method(obj):
    return types.MethodType(lambda self: None, obj)


def test_the_walk_reports_the_shortest_path_and_survives_a_cycle():
    deep = make(Leaf, x=None)
    a = make(Holder, deep=deep)
    b = make(Holder, a=a)
    a.back = b
    root = make(Holder, direct=deep, via=b)

    nodes = graph_walk.walk(root)

    assert [n.path for n in nodes if n.obj is deep] == ["app.direct"]
    assert len(nodes) == len({id(n.obj) for n in nodes})


def test_the_walk_reports_a_dataclass_outside_the_domain_but_not_a_pydantic_model_or_a_domain_class():
    @dataclass
    class Value:
        missing: object = None

    Value.__module__ = "src.composition.fake"

    class Model(BaseModel):
        missing: object = None

    Model.__module__ = "src.services.fake"
    in_domain = make(src_class("Plain", module="src.domain.fake"), missing=None)
    root = make(Holder, dataclass=Value(), model=Model(), plain=in_domain)

    found = {n.path for n in graph_walk.none_attributes(graph_walk.walk(root))}

    assert found == {"app.dataclass.missing"}


def test_a_third_party_object_is_a_leaf():
    third_party = make(src_class("Client", module="slack_bolt.fake"), inner=make(Leaf, x=None))
    root = make(Holder, client=third_party)

    assert "app.client.inner" not in paths(graph_walk.walk(root))


def test_the_instance_count_does_not_enter_an_agent_but_the_none_walk_does():
    agent_class = src_class("FakeAgent", module="src.agents.fake", bases=(BaseAgent,))
    agent_class.__abstractmethods__ = frozenset()
    service_class = src_class("FakeService", module="src.services.fake")
    outside = make(service_class)
    inside = make(service_class, missing=None)
    agent = make(agent_class, copy_of_service=inside)
    root = make(src_class("Root", module="src.infrastructure.fake"), service=outside, agent=agent)

    counts = graph_walk.instance_counts(graph_walk.walk_outside_agents(root))
    nones = {n.path for n in graph_walk.none_attributes(graph_walk.walk(root))}

    assert counts == Counter({"src.services.fake.FakeService": 1})
    assert "app.agent.copy_of_service.missing" in nones


def test_slots_are_read_like_dict_attributes():
    slotted = src_class("Slotted", __slots__=("present", "absent"))
    obj = slotted()
    obj.present = None

    assert graph_walk.attributes(obj) == {"present": None}


# ----------------------------------------------------------------------------- the ledger comparison, on small inline data

def row(path, klass, attr):
    return graph_walk.NoneAttribute(path, klass, attr)


Owner = src_class("Owner")
SubOwner = src_class("SubOwner", bases=(Owner,))
Declaration = src_class("Declaration", module="src.infrastructure.fake")


def compare(lines, observed, values=None):
    return wiring_ledger.compare_nones(observed, wiring_ledger.parse(lines), values or {})


def test_a_ledger_that_matches_has_no_problem():
    observed = [row("app.a.x", Owner, "x"), row("app.b.y", SubOwner, "y")]
    ledger = "app.a.x  # lazy first use builds it\nOwner.y  # runtime filled by a request\n"

    assert compare(ledger, observed, {("Owner", "y"): [None]}) == []


def test_a_none_that_is_not_in_the_ledger_is_reported():
    problems = compare("", [row("app.a.x", Owner, "x")])

    assert problems == ["unledgered None: app.a.x (Owner.x)"]


def test_a_line_whose_none_is_gone_is_reported_stale():
    assert "stale path" in compare("app.a.x  # lazy first use builds it\n", [])[0]
    assert "stale 'Owner.x'" in compare("Owner.x  # lazy first use builds it\n", [])[0]


def test_a_class_line_fails_when_one_instance_has_a_value_unless_it_ends_with_a_star():
    observed = [row("app.a.x", Declaration, "x")]
    values = {("Declaration", "x"): [None, "wired"]}

    strict = compare("Declaration.x  # runtime filled by a request\n", observed, values)
    star = compare("Declaration.x*  # by-design optional declaration field\n", observed, values)

    assert len(strict) == 1 and "1 instance(s) have a value" in strict[0]
    assert star == []


@pytest.mark.parametrize("module", ["src.adapters.fake", "src.services.fake", "src.handlers.fake", "src.composition.fake"])
def test_a_star_line_is_refused_on_a_wiring_layer_class(module):
    owner = src_class("Wired", module=module)

    problems = compare("Wired.x*  # by-design optional declaration field\n", [row("app.a.x", owner, "x")], {("Wired", "x"): [None, "wired"]})

    assert len(problems) == 1 and "`*` is refused" in problems[0] and module in problems[0]


def test_a_class_line_covers_a_subclass_instance():
    observed = [row("app.a.x", SubOwner, "x")]

    assert compare("Owner.x  # runtime filled by a request\n", observed, {("Owner", "x"): [None]}) == []


def test_a_repeated_key_and_a_started_loop_are_reported():
    ledger = "app.a.x  # lazy first use builds it\napp.a.x  # lazy first use builds it\nBillingAgent._flush_task  # runtime the loop starts later\n"
    observed = [row("app.a.x", Owner, "x"), row("app.b._flush_task", Owner, "_flush_task")]

    problems = compare(ledger, observed, {})

    assert any("repeats line 1" in p for p in problems)
    assert any("a started loop is never ledgered" in p for p in problems)


@pytest.mark.parametrize("line", [
    "app.a.x",                                   # no reason
    "app.a.x  # because",                        # no vocabulary word
    "app.a.x  # lazy",                           # no evidence
    "app.a.x  # lazy builds",                    # one word of evidence
    "app.a.x  # defect first use builds it",     # a defect with no id
])
def test_a_malformed_ledger_line_is_refused_with_its_line_number(line):
    with pytest.raises(ValueError, match="line 2"):
        wiring_ledger.parse("# header\n" + line + "\n")


def test_a_defect_line_carries_its_id():
    [entry] = wiring_ledger.parse("Owner.x  # defect:D8 the declared fallback is unreachable\n")

    assert entry.reason == "defect:D8"


def test_a_reason_with_a_suffix_after_the_defect_id_is_refused():
    with pytest.raises(ValueError, match="line 1"):
        wiring_ledger.parse("Owner.x  # defect:D8-owner the fix waits for the owner\n")


def test_the_instance_ledger_fails_both_ways_on_a_count_and_on_a_class():
    entries = wiring_ledger.parse("src.a.One 1  # by-design single instance\nsrc.a.Two 2  # by-design two stores\n")

    problems = wiring_ledger.compare_counts(Counter({"src.a.One": 2, "src.a.New": 1}), entries)

    assert "src.a.One: ledger says 1, the graph has 2" in problems
    assert "unledgered class: src.a.New has 1 instance(s)" in problems
    assert "src.a.Two: ledger says 2, the graph has 0" in problems


def test_the_store_count_over_the_full_walk_fails_when_it_exceeds_the_ledger():
    entries = wiring_ledger.parse("src.adapters.firestore_repo.Repo 1  # by-design single instance\n")

    assert wiring_ledger.compare_store_counts(Counter({"src.adapters.firestore_repo.Repo": 1}), entries) == []
    assert "2 instance(s) in the full walk" in wiring_ledger.compare_store_counts(Counter({"src.adapters.firestore_repo.Repo": 2}), entries)[0]
    assert "the ledger says 0" in wiring_ledger.compare_store_counts(Counter({"src.adapters.gcs_new.New": 1}), entries)[0]


def test_only_store_adapters_are_counted_in_the_store_count():
    store = make(src_class("Repo", module="src.adapters.firestore_repo"))
    prompt_v3_store = make(src_class("TokenRepo", module="src.adapters.prompt_v3.firestore_token_repository"))
    other = make(src_class("Proxy", module="src.adapters.alerting_proxy"))
    service = make(src_class("Svc", module="src.services.gcs_thing"))
    root = make(Holder, a=store, b=prompt_v3_store, c=other, d=service)

    counts = graph_walk.store_counts(graph_walk.walk(root))

    assert counts == Counter({"src.adapters.firestore_repo.Repo": 1, "src.adapters.prompt_v3.firestore_token_repository.TokenRepo": 1})


def test_the_committed_ledgers_parse_and_use_the_vocabulary():
    nones = wiring_ledger.parse(NONE_LEDGER.read_text())
    counts = wiring_ledger.parse(INSTANCE_LEDGER.read_text())

    assert nones and counts
    assert not [e for e in nones if "doc_delivery_service" in e.key], "D1 is fixed: no doc_delivery_service line"
    wiring_ledger.parse_counts(counts)
