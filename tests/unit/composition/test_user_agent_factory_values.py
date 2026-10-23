"""The per-user agents get the ids, timeouts, caches and user values production gives them (P9 T5b, round 4).

One graph per test, built the way `prod_graph` builds it (the deploy's sealed environment, fakes at the SDK boundaries,
one user's eager and lazy agents) with two differences a deploy can legitimately have: the Twilio variables are absent from
the process environment (the Twilio client reads its own when handed `None`), and user `u1` carries a non-default timezone
and an explicit Smart thinking effort, so a value dropped on the way to an agent differs from the default the agent falls
back to. Every check reads the attribute that holds the value in the object production uses.
"""
from typing import AsyncIterator

import pytest

from src.domain.billing import BillingAccount
from src.domain.user import UserBotConfig, UserProfile
from src.infrastructure.agent_config import (
    CLAUDE_DEEP_RESEARCH_RUNNER, COMPUTE, CONSOLIDATION, DEEP_RESEARCH, DOC_GENERATOR, DOC_PLANNER, DOMAIN_RESEARCHER,
    EMAIL_SEARCH, HTML_PAGE_GENERATOR, IMAGE_GENERATION, MAPS_SEARCH, MEMORY_SEARCH, NOTES, PDF_GENERATOR, TASKS,
    TUTOR, VIDEO_GENERATION, WEB_SEARCH, WEB_SEARCH_LIGHT,
)
from tests.support import prod_graph as prod_graph_module
from tests.support.prod_graph import ACCOUNT_ID, USER_ID, ProdGraph

pytestmark = pytest.mark.slow

TIMEZONE = "Europe/Madrid"
SMART_EFFORT = "high"

# agent id prefix -> the timeout its CFG declares (a literal where the factory writes one)
TIMEOUTS = {
    "notes_agent": NOTES.timeout_ms,
    "facts_memory_agent": MEMORY_SEARCH.timeout_ms,
    "web_search_agent": WEB_SEARCH.timeout_ms,
    "email_search_agent": EMAIL_SEARCH.timeout_ms,
    "maps_search_agent": MAPS_SEARCH.timeout_ms,
    "compute_agent": COMPUTE.timeout_ms,
    "tasks_agent": TASKS.timeout_ms,
    "consolidation_agent": CONSOLIDATION.timeout_ms,
    "help_agent": 5_000,
    "doc_generator_agent": DOC_GENERATOR.timeout_ms,
    "doc_planner_agent": DOC_PLANNER.timeout_ms,
    "pdf_generator_agent": PDF_GENERATOR.timeout_ms,
    "html_page_generator_agent": HTML_PAGE_GENERATOR.timeout_ms,
    "deep_research_agent": DEEP_RESEARCH.timeout_ms,
    "claude_deep_research_runner": CLAUDE_DEEP_RESEARCH_RUNNER.timeout_ms,
    "web_search_light_agent": WEB_SEARCH_LIGHT.timeout_ms,
    "file_management_agent": 120_000,
    "domain_researcher_agent": DOMAIN_RESEARCHER.timeout_ms,
    "tutor_agent": TUTOR.timeout_ms,
    "image_generation_agent": IMAGE_GENERATION.timeout_ms,
    "video_generation_agent": VIDEO_GENERATION.timeout_ms,
}

# the per-user cache key -> the id of the agent it must hold
CACHED = {
    "router_agent": "router_agent",
    "quick_agent": "quick_response_agent",
    "smart_agent": "smart_response_agent",
    "memory_agent": "facts_memory_agent",
    "web_agent": "web_search_agent",
    "email_search_agent": "email_search_agent",
    "maps_agent": "maps_search_agent",
    "compute_agent": "compute_agent",
    "notes_agent": "notes_agent",
    "tasks_agent": "tasks_agent",
    "consolidation_agent": "consolidation_agent",
    "help_agent": "help_agent",
}


@pytest.fixture
async def user_graph(monkeypatch) -> AsyncIterator[ProdGraph]:
    import src.config.settings as settings

    real_load = settings.load_settings

    def load_without_twilio_variables():
        config = real_load()
        for name in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"):
            monkeypatch.delenv(name)
        return config

    async def seed(app) -> None:
        await app.account_repo.create_account(BillingAccount(account_id=ACCOUNT_ID))
        profile = UserProfile(
            user_id=USER_ID, account_id=ACCOUNT_ID,
            config=UserBotConfig(timezone=TIMEZONE, agent_thinking={"smart": SMART_EFFORT}),
        )
        await app.user_repo.create_user(profile)

    monkeypatch.setattr(settings, "load_settings", load_without_twilio_variables)
    monkeypatch.setattr(prod_graph_module, "seed", seed)
    graph = await prod_graph_module.build(monkeypatch)
    try:
        yield graph
    finally:
        await graph.app.shutdown()


async def test_every_per_user_agent_has_its_id_and_the_timeout_its_cfg_declares(user_graph):
    coordinator = user_graph.app.coordinator
    problems = []
    for prefix, timeout in TIMEOUTS.items():
        agent = coordinator.get_agent(f"{prefix}_{USER_ID}")
        if agent is None:
            problems.append(f"{prefix}_{USER_ID} is not registered")
        elif agent.config.agent_id != f"{prefix}_{USER_ID}" or agent.config.timeout_ms != timeout:
            problems.append(f"{prefix}: config is ({agent.config.agent_id!r}, {agent.config.timeout_ms!r}), "
                            f"expected ({prefix}_{USER_ID!r}, {timeout!r})")
    assert not problems, "\n".join(problems)


async def test_the_per_user_cache_holds_the_agents_the_coordinator_resolves(user_graph):
    app = user_graph.app
    cached = app.agent_factory._cache[USER_ID]
    problems = []
    for key, prefix in CACHED.items():
        resolved = app.coordinator.get_agent(f"{prefix}_{USER_ID}")
        if resolved is None or cached[key] is not resolved:
            problems.append(f"_cache[{USER_ID!r}][{key!r}] is {cached[key]!r}, the coordinator resolves {resolved!r}")
    assert not problems, "\n".join(problems)


async def test_the_users_timezone_and_smart_thinking_effort_reach_their_agents(user_graph):
    cached = user_graph.app.agent_factory._cache[USER_ID]

    assert cached["notes_agent"]._user_tz_name == TIMEZONE
    assert cached["smart_agent"]._default_thinking_effort == SMART_EFFORT


async def test_the_telephony_adapter_holds_the_configured_twilio_credentials(user_graph):
    telephony = user_graph.app.agent_factory._telephony

    assert telephony._client.username == user_graph.offline.env["TWILIO_ACCOUNT_SID"]
    assert telephony._client.password == user_graph.offline.env["TWILIO_AUTH_TOKEN"]
