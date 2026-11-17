"""`build_memory`: one owner-zone resolver, one memory-write lease and one profile step shared by consolidation, the janitor,
the review and the answers; one interactive router and one fact-update adapter shared by the review and the answers; each
optional service built exactly when what it needs exists."""
import dataclasses
from types import SimpleNamespace

import pytest

from src.composition import memory_setup
from src.composition.memory_setup import build_memory
from src.config.environment import EnvironmentConfig
from src.domain.fact_review_question import FACT_QUESTION_KIND
from src.domain.settings import SearchConfig
from src.infrastructure.agent_config import CONSOLIDATION
from tests.support.firestore_eval import EvalDb

COORDINATOR, FACTORY = object(), object()
IDENTITY = SimpleNamespace(account_repo=object(), user_repo=object(), iam_service=object())
ROUTING = SimpleNamespace(consolidation_queue=object(), channel_binding_adapter=object())
SEARCH = SimpleNamespace(person_cards=object(), place_atlas=object())
NOTIFICATIONS = SimpleNamespace(slack_app=SimpleNamespace(client=object()), channel_factory=object(), state_repo=object(),
                                service=object())
LANGUAGES = SimpleNamespace(resolver=object(), localization_port=object(), system_default=object())
URL = "https://svc.example.test/cabinet?tab=memory"


def _container():
    container = SimpleNamespace(
        repository=object(), embedding_service=object(), fact_lifecycle_counters=object(), fact_review_store=object(),
        indexed_email_repo=object(), session_store=object(), profile_answers=object(), confirmed_people=object(),
        biographical_context_service=object(), fact_management_adapters=[])

    def create_fact_management_adapter(search):
        adapter = SimpleNamespace(search=search)
        container.fact_management_adapters.append(adapter)
        return adapter
    container.create_fact_management_adapter = create_fact_management_adapter
    return container


@pytest.fixture
def build(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.delenv("FACT_LIFECYCLE_SWEEP_DRY_RUN", raising=False)
    monkeypatch.delenv("FACT_PROFILER_MAX_PER_RUN", raising=False)
    steps = []

    def profile_step(container, *, max_per_run, person_cards, atlas, quota):
        steps.append(SimpleNamespace(container=container, max_per_run=max_per_run, person_cards=person_cards,
                                     atlas=atlas, quota=quota))
        return steps[-1]
    monkeypatch.setattr(memory_setup, "build_fact_profile_service", profile_step)
    env = EnvironmentConfig()

    def run(*, queue="QUEUE", alert="ALERT", container=None):
        container = container or _container()
        ops = SimpleNamespace(task_queue=queue, alert_sink=alert, quota_service=object())
        memory = build_memory(EvalDb(), env, container=container, coordinator=COORDINATOR, agent_factory=FACTORY,  # type: ignore[arg-type]
                              identity=IDENTITY, ops=ops, routing=ROUTING, memory_search=SEARCH,
                              notifications=NOTIFICATIONS, languages=LANGUAGES, cabinet_memory_url=URL)
        return SimpleNamespace(memory=memory, container=container, ops=ops, steps=steps)
    return run


def test_consolidation_the_janitor_and_the_review_share_the_zones_the_lease_and_the_profile_step(build):
    built = build()
    memory, container, [step] = built.memory, built.container, built.steps

    consolidation, sweep, review = memory.consolidation_service, memory.fact_lifecycle_sweep, memory.fact_review
    assert consolidation._owner_zones is sweep._owner_zones is review._owner_zones is memory.fact_answers._owner_zones
    assert consolidation._lease is review._lease
    assert consolidation._profile_step is step and review._profile_step is step
    assert (step.container, step.person_cards, step.atlas, step.quota) == (
        container, SEARCH.person_cards, SEARCH.place_atlas, built.ops.quota_service)
    assert consolidation._owner_zones._account_repo is IDENTITY.account_repo


def test_consolidation_runs_the_routings_queue_through_the_coordinator_and_factory(build):
    built = build()
    consolidation = built.memory.consolidation_service

    assert (consolidation._queue, consolidation._coordinator, consolidation._agent_factory) == (
        ROUTING.consolidation_queue, COORDINATOR, FACTORY)
    assert consolidation._run_timeout_s == CONSOLIDATION.timeout_ms / 1000
    assert consolidation._task_queue == "QUEUE" and consolidation._user_repo is IDENTITY.user_repo
    assert consolidation._indexed_email_repo is built.container.indexed_email_repo


@pytest.mark.parametrize("raw, dry_run", [(None, True), ("off", False), ("on", True)])
def test_the_janitor_is_log_only_unless_its_knob_is_an_explicit_off(build, monkeypatch, raw, dry_run):
    if raw is not None:
        monkeypatch.setenv("FACT_LIFECYCLE_SWEEP_DRY_RUN", raw)

    sweep = build().memory.fact_lifecycle_sweep

    assert sweep._dry_run is dry_run
    assert sweep._alert_sink == "ALERT" and sweep._task_queue == "QUEUE"


def test_the_profile_step_cap_is_its_knob(build, monkeypatch):
    monkeypatch.setenv("FACT_PROFILER_MAX_PER_RUN", "7")

    assert build().steps[0].max_per_run == 7


def test_the_review_and_the_answers_share_one_router_one_text_set_and_one_fact_update_adapter(build):
    built = build()
    memory, container = built.memory, built.container
    answers, review, choice = memory.fact_answers, memory.fact_review, memory.interactive_choice

    [adapter] = container.fact_management_adapters
    assert answers._fact_updates is adapter and review._fact_updates is adapter
    defaults = SearchConfig()
    assert adapter.search._total_limit == defaults.DEFAULT_SEMANTIC_SEARCH_LIMIT
    assert answers._interactive is review._interactive is choice._interactive
    assert answers._texts is review._texts
    router = answers._interactive._adapters
    assert router["slack"]._client is NOTIFICATIONS.slack_app.client
    assert router["telegram"]._channels is NOTIFICATIONS.channel_factory
    assert choice._handlers == {FACT_QUESTION_KIND: answers} and choice._iam is IDENTITY.iam_service
    assert review._answer_service is answers and review._cabinet_url == URL
    assert (review._notification_state, review._channel_bindings, review._notifications) == (
        NOTIFICATIONS.state_repo, ROUTING.channel_binding_adapter, NOTIFICATIONS.service)
    assert answers._profile_answers._atlas is SEARCH.place_atlas
    assert review._person_cards is SEARCH.person_cards


def test_without_the_ops_sink_there_is_no_review_but_the_answers_stay(build):
    memory = build(alert=None).memory

    assert memory.fact_review is None
    assert memory.fact_answers is not None and memory.interactive_choice is not None


def test_without_a_task_queue_there_are_no_answers_no_clicks_no_review_and_no_fact_update_adapter(build):
    built = build(queue=None)

    assert (built.memory.fact_answers, built.memory.interactive_choice, built.memory.fact_review) == (None, None, None)
    assert built.container.fact_management_adapters == []
    assert built.memory.consolidation_service._task_queue is None


def test_the_cabinet_writes_through_the_containers_fact_repository_and_the_bundle_is_frozen(build):
    built = build()

    assert built.memory.cabinet_facts._repo is built.container.repository
    with pytest.raises(dataclasses.FrozenInstanceError):
        built.memory.fact_review = None  # type: ignore[misc]
