"""An eager agent is complete the moment the coordinator can route to it (P9 T5b, round 4; defect P9-D10).

`ensure_agents_for_user` registers the eager agents, then awaits the prompt-cache preload. A request routed by id while it
waits reaches an agent as registered. The window is held open here with an assembly service whose `preload_cache` awaits an
event; user `u2` is built in a task until its ids are on the coordinator, and the agents are read before the event is set.

The lazy path writes its three attributes before `register_agent`, so it has no window; a registration hook pins that order.
"""
import asyncio
from typing import Any, AsyncIterator, Dict, NamedTuple

import pytest

from src.domain.user import UserProfile
from tests.support.prod_graph import ACCOUNT_ID, ProdGraph, prod_graph  # noqa: F401  (the fixture)

pytestmark = pytest.mark.slow

OTHER_USER = "u2"
CORE_IDS = ("router_agent", "quick_response_agent", "smart_response_agent")


class InWindow(NamedTuple):
    graph: ProdGraph
    agents: Dict[str, Any]       # agent id -> the agent, as registered while the preload is still waiting


class _HeldAssembly:
    """The real assembly service, except that `preload_cache` waits for the test."""

    def __init__(self, real: Any, release: asyncio.Event) -> None:
        self._real, self._release = real, release

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    async def preload_cache(self, *args: Any, **kwargs: Any) -> Any:
        await self._release.wait()
        return await self._real.preload_cache(*args, **kwargs)


@pytest.fixture
async def in_window(prod_graph) -> AsyncIterator[InWindow]:  # noqa: F811
    app = prod_graph.app
    factory = app.agent_factory
    await app.user_repo.create_user(UserProfile(user_id=OTHER_USER, account_id=ACCOUNT_ID))
    release = asyncio.Event()
    factory.assembly_service = _HeldAssembly(factory.assembly_service, release)
    task = asyncio.create_task(factory.ensure_agents_for_user(OTHER_USER))
    try:
        for _ in range(500):
            if all(f"{prefix}_{OTHER_USER}" in app.coordinator.agents for prefix in CORE_IDS):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("the agents were never registered while the preload waited")
        assert not task.done()
        agents = {agent_id: agent for agent_id, agent in app.coordinator.agents.items() if agent_id.endswith(f"_{OTHER_USER}")}
        yield InWindow(prod_graph, agents)
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=30)


async def test_the_router_quick_and_smart_agents_reach_the_coordinator_before_they_are_routable(in_window):
    coordinator = in_window.graph.app.coordinator

    for prefix in CORE_IDS:
        assert in_window.agents[f"{prefix}_{OTHER_USER}"].coordinator is coordinator, prefix


@pytest.mark.xfail(strict=True, reason="P9-D10, fixed in the next commit")
async def test_every_routable_eager_agent_already_has_its_coordinator_prompt_store_and_quota_service(in_window):
    app = in_window.graph.app
    factory = app.agent_factory
    assert factory.prompt_content_store is not None and factory.quota_service is not None
    problems = []
    for agent_id, agent in sorted(in_window.agents.items()):
        if agent.coordinator is not app.coordinator:
            problems.append(f"{agent_id}: no coordinator")
        if agent._prompt_content_store is not factory.prompt_content_store:
            problems.append(f"{agent_id}: no prompt-content store")
        if agent._quota_service is not factory.quota_service:
            problems.append(f"{agent_id}: no quota service")
    assert len(in_window.agents) >= 10
    assert not problems, "\n".join(problems)


async def test_a_lazy_agent_has_its_coordinator_prompt_store_and_quota_service_when_it_is_registered(prod_graph):  # noqa: F811
    app = prod_graph.app
    factory = app.agent_factory
    seen = {}
    real_register = app.coordinator.register_agent

    def recording_register(agent: Any) -> None:
        seen[agent.agent_id] = (agent.coordinator, agent._prompt_content_store, agent._quota_service)
        real_register(agent)

    app.coordinator.unregister_agent("tutor_agent_u1")
    app.coordinator.register_agent = recording_register
    try:
        assert await factory.create_agent_on_demand("tutor", "u1") is True
    finally:
        app.coordinator.register_agent = real_register

    assert seen == {"tutor_agent_u1": (app.coordinator, factory.prompt_content_store, factory.quota_service)}
