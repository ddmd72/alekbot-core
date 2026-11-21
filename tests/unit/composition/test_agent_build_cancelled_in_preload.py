"""A build cancelled between registration and the cache write leaves no agent registered (defect: orphan registrations).

`ensure_agents_for_user` registers the eager agents, awaits the prompt-cache preload, then writes the cache entry. A cancel
in the preload used to leave the agents routable with no entry to evict them by, and the next build's registrations were
refused as duplicates. The preload is held with an assembly service whose `preload_cache` waits for the test.
"""
import asyncio
from typing import Any

import pytest

from src.domain.user import UserProfile
from tests.support.prod_graph import ACCOUNT_ID, prod_graph  # noqa: F401  (the fixture)

pytestmark = pytest.mark.slow

OTHER_USER = "u2"
CORE_IDS = ("router_agent", "quick_response_agent", "smart_response_agent")


class _HeldAssembly:
    """The real assembly service, except that `preload_cache` waits for the test."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    async def preload_cache(self, *args: Any, **kwargs: Any) -> Any:
        self.reached.set()
        await self.release.wait()
        return await self._real.preload_cache(*args, **kwargs)


def _agents_of(coordinator: Any, user_id: str) -> list:
    return sorted(agent_id for agent_id in coordinator.agents if agent_id.endswith(f"_{user_id}"))


async def test_a_build_cancelled_during_the_preload_unregisters_its_agents_and_the_next_build_registers_cleanly(
    prod_graph,  # noqa: F811
):
    app = prod_graph.app
    factory = app.agent_factory
    await app.user_repo.create_user(UserProfile(user_id=OTHER_USER, account_id=ACCOUNT_ID))
    real_assembly = factory.assembly_service
    held = _HeldAssembly(real_assembly)
    factory.assembly_service = held

    task = asyncio.create_task(factory.ensure_agents_for_user(OTHER_USER))
    await asyncio.wait_for(held.reached.wait(), timeout=30)
    assert all(f"{prefix}_{OTHER_USER}" in app.coordinator.agents for prefix in CORE_IDS)   # registered, in the window
    cancelled_build = dict(app.coordinator.agents)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _agents_of(app.coordinator, OTHER_USER) == []

    factory.assembly_service = real_assembly
    await asyncio.wait_for(factory.ensure_agents_for_user(OTHER_USER), timeout=30)

    built = _agents_of(app.coordinator, OTHER_USER)
    assert all(f"{prefix}_{OTHER_USER}" in built for prefix in CORE_IDS)
    for agent_id in built:   # every registered agent is the new build's, not a survivor of the cancelled one
        assert app.coordinator.agents[agent_id] is not cancelled_build.get(agent_id), agent_id
