"""`build_agents`: the factory gets the container's agent services and each bundle's piece, one long-turn registry serves the
factory and the chat's long-turn service, and the language preference writer refreshes agents through this factory."""
import dataclasses
from types import SimpleNamespace

import pytest
from anthropic import AsyncAnthropic

from src.composition import agents_setup
from src.composition.agents_setup import build_agents
from src.config.environment import EnvironmentConfig
from tests.support.firestore_eval import EvalDb


class _Factory:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def ensure_agents_for_user(self, user_id):
        return None


SERVICES = {"session_store": object(), "llm_port": object()}
CONTAINER = SimpleNamespace(session_store=SERVICES["session_store"], profile_repo=object(), assembly_service=object(),
                            agent_services=lambda: dict(SERVICES))
BOOT = SimpleNamespace(deploy_topology=object(), video_duration=object())
IDENTITY = SimpleNamespace(user_repo=object(), account_repo=object())
OPS = SimpleNamespace(task_queue=object(), quota_service=object(), alert_sink=object())
COMPANIONS = SimpleNamespace(context_assembler=object())
LANGUAGES = SimpleNamespace(localization=object(), resolver=object())
SEARCH = SimpleNamespace(fact_search=object())
COORDINATOR, NOTIFY, JOBS, IMAGES, VIDEOS = (object() for _ in range(5))


@pytest.fixture
def build(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setattr(agents_setup, "UserAgentFactory", _Factory)
    env = EnvironmentConfig()

    def run(config):
        return build_agents(config, EvalDb(), env, container=CONTAINER, coordinator=COORDINATOR, boot=BOOT,  # type: ignore[arg-type]
                            identity=IDENTITY, ops=OPS, job_registry=JOBS, image_registry=IMAGES,
                            video_registry=VIDEOS, companions=COMPANIONS, notification_service=NOTIFY,
                            languages=LANGUAGES, memory_search=SEARCH)
    return run


def test_the_factory_gets_the_container_services_and_each_bundles_piece(build):
    config = {"ANTHROPIC_API_KEY": "sk-ant-test"}

    agents = build(config)

    kwargs = agents.factory.kwargs
    assert {k: kwargs[k] for k in SERVICES} == SERVICES
    expected = {"config": config, "fact_search": SEARCH.fact_search, "deploy_topology": BOOT.deploy_topology,
                "coordinator": COORDINATOR, "user_repo": IDENTITY.user_repo, "account_repo": IDENTITY.account_repo,
                "job_registry": JOBS, "image_registry": IMAGES, "video_registry": VIDEOS, "task_queue": OPS.task_queue,
                "quota_service": OPS.quota_service, "companion_context_assembler": COMPANIONS.context_assembler,
                "notification_service": NOTIFY, "localization": LANGUAGES.localization,
                "language_service": LANGUAGES.resolver, "alert_sink": OPS.alert_sink,
                "video_duration": BOOT.video_duration}
    assert {k: kwargs[k] for k in expected} == expected
    assert isinstance(kwargs["anthropic_client"], AsyncAnthropic)


def test_without_an_anthropic_key_there_is_no_anthropic_client(build):
    assert build({}).factory.kwargs["anthropic_client"] is None


def test_one_long_turn_registry_serves_the_factory_and_the_chats_long_turn_service(build):
    agents = build({})

    registry = agents.factory.kwargs["long_turn_registry"]
    assert agents.long_turn_service._registry is registry
    assert agents.long_turn_service._sessions is CONTAINER.session_store


def test_the_language_preferences_refresh_agents_through_this_factory(build):
    agents = build({})

    preferences = agents.language_preferences
    assert preferences._ensure_agents == agents.factory.ensure_agents_for_user
    assert (preferences._user_repo, preferences._profile_repo, preferences._prompt_builder) == (
        IDENTITY.user_repo, CONTAINER.profile_repo, CONTAINER.assembly_service)
    assert preferences._resolver is LANGUAGES.resolver and preferences._notification_service is NOTIFY


def test_the_bundle_is_frozen(build):
    with pytest.raises(dataclasses.FrozenInstanceError):
        build({}).factory = None  # type: ignore[misc]
