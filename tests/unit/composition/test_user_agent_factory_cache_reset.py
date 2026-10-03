"""`$admin_cache_reset` must put agents built from the CURRENT config in service.

The coordinator routes by agent_id. Before the fix, invalidate_user_cache only dropped the
factory's cache entry: the old agents stayed registered, their rebuilt replacements were refused
registration ("already registered"), and requests kept reaching the old config snapshot until a
restart. Lazy agents (Alek gateway, Lelik, ...) were orphaned for good, because their ids lived
in the dropped entry.
"""
import time
from types import SimpleNamespace

import pytest

from src.composition.user_agent_factory import UserAgentFactory
from src.infrastructure.agent_coordinator import AgentCoordinator

USER = "u1"


def _agent(agent_id, version):
    return SimpleNamespace(
        agent_id=agent_id, agent_type=agent_id.split("_")[0],
        config=SimpleNamespace(capabilities=[]), config_version=version,
    )


def _factory():
    factory = UserAgentFactory.__new__(UserAgentFactory)
    factory.coordinator = AgentCoordinator()
    factory._cache = {}
    factory._cache_ttl = 3600
    factory._creation_locks = {}
    factory.built = 0

    async def fake_create(self_user_id):
        # Stands in for _create_and_cache_agents: builds from "the config now" and registers
        # through the real _register_agents, exactly like production.
        factory.built += 1
        version = factory.built
        agents = {key: _agent(f"{key}_{self_user_id}", version)
                  for key in UserAgentFactory._EAGER_AGENT_KEYS}
        factory._register_agents(list(agents.values()))
        entry = {"last_used": time.time(), "_lazy_agent_ids": [], **agents}
        factory._cache[self_user_id] = entry
        return entry

    factory._create_and_cache_agents = fake_create
    return factory


@pytest.mark.asyncio
async def test_after_reset_the_coordinator_routes_to_agents_built_from_the_new_config():
    factory = _factory()
    await factory.ensure_agents_for_user(USER)
    assert factory.coordinator.get_agent(f"smart_agent_{USER}").config_version == 1

    factory.invalidate_user_cache(USER)
    await factory.ensure_agents_for_user(USER)

    assert factory.coordinator.get_agent(f"smart_agent_{USER}").config_version == 2
    assert factory.coordinator.get_agent(f"router_agent_{USER}").config_version == 2


@pytest.mark.asyncio
async def test_reset_unregisters_lazy_agents_so_they_are_rebuilt():
    factory = _factory()
    entry = await factory.ensure_agents_for_user(USER)
    lazy = _agent(f"alek_agent_{USER}", 1)
    factory.coordinator.register_agent(lazy)
    entry["_lazy_agent_ids"].append(lazy.agent_id)

    factory.invalidate_user_cache(USER)

    assert factory.coordinator.get_agent(lazy.agent_id) is None
    assert factory.coordinator.get_agent(f"smart_agent_{USER}") is None
    assert USER not in factory._cache


def test_reset_of_an_uncached_user_is_a_no_op():
    factory = _factory()
    factory.invalidate_user_cache("nobody")  # must not raise
    assert factory._evict_user("nobody") is False


@pytest.mark.asyncio
async def test_other_users_agents_survive_a_reset():
    factory = _factory()
    await factory.ensure_agents_for_user(USER)
    await factory.ensure_agents_for_user("u2")

    factory.invalidate_user_cache(USER)

    assert factory.coordinator.get_agent("smart_agent_u2") is not None
