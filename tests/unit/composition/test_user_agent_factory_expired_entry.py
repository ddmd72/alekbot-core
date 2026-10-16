"""A request that finds the user's cache entry expired must replace its agents, not shadow them.

The TTL sweep runs every 5 minutes but lags (Cloud Run throttles CPU between requests), so a
request can arrive after `last_used + ttl` and before the sweep. `ensure_agents_for_user` then
rebuilt the set without evicting the expired one: every registration was refused ("already
registered", 13 ERRORs on 2026-10-16 08:32), the coordinator kept routing to the stale
instances, and the cache held agents the coordinator did not know.
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

    async def fake_create(user_id):
        # Stands in for _create_and_cache_agents: registers through the real _register_agents.
        factory.built += 1
        agents = {key: _agent(f"{key}_{user_id}", factory.built)
                  for key in UserAgentFactory._EAGER_AGENT_KEYS}
        factory._register_agents(list(agents.values()))
        entry = {"last_used": time.time(), "_lazy_agent_ids": [], **agents}
        factory._cache[user_id] = entry
        return entry

    factory._create_and_cache_agents = fake_create
    return factory


@pytest.mark.asyncio
async def test_expired_entry_is_replaced_and_the_coordinator_routes_to_the_new_agents(caplog):
    factory = _factory()
    entry = await factory.ensure_agents_for_user(USER)
    entry["last_used"] = time.time() - factory._cache_ttl - 60  # expired, sweep has not run yet

    rebuilt = await factory.ensure_agents_for_user(USER)

    assert factory.built == 2
    assert factory.coordinator.get_agent(f"smart_agent_{USER}") is rebuilt["smart_agent"]
    assert factory.coordinator.get_agent(f"router_agent_{USER}").config_version == 2
    assert "already registered" not in caplog.text


@pytest.mark.asyncio
async def test_expired_entry_lazy_agents_are_unregistered_so_they_are_rebuilt():
    factory = _factory()
    entry = await factory.ensure_agents_for_user(USER)
    lazy = _agent(f"lelik_agent_{USER}", 1)
    factory.coordinator.register_agent(lazy)
    entry["_lazy_agent_ids"].append(lazy.agent_id)
    entry["last_used"] = time.time() - factory._cache_ttl - 60

    await factory.ensure_agents_for_user(USER)

    assert factory.coordinator.get_agent(lazy.agent_id) is None


@pytest.mark.asyncio
async def test_fresh_entry_is_reused_without_a_rebuild():
    factory = _factory()
    first = await factory.ensure_agents_for_user(USER)

    second = await factory.ensure_agents_for_user(USER)

    assert second is first
    assert factory.built == 1


@pytest.mark.asyncio
async def test_expiry_of_one_user_leaves_other_users_agents_registered():
    factory = _factory()
    entry = await factory.ensure_agents_for_user(USER)
    await factory.ensure_agents_for_user("u2")
    entry["last_used"] = time.time() - factory._cache_ttl - 60

    await factory.ensure_agents_for_user(USER)

    assert factory.coordinator.get_agent("smart_agent_u2") is not None
