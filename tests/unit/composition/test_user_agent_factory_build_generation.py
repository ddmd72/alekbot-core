"""The factory's per-user eviction generation (P15-S1, P15-S1b) — the behaviour the fix adds beyond the two pins.

`_evict_user` takes no lock, so a build in progress under the per-user lock learns of an eviction only through the
user's generation: a build an eviction overtook is redone, a bounded number of times, and a lazy build that finds its
entry evicted rebuilds the entry first. The pins live in `test_user_agent_factory_interleaving.py`.
"""
import asyncio
import time
from types import SimpleNamespace

from src.composition.user_agent_factory import _MAX_BUILD_ATTEMPTS, UserAgentFactory, _UserContext
from src.infrastructure.agent_coordinator import AgentCoordinator

USER = "u1"
LAZY_TYPE = "stub_lazy"
LAZY_BASE_ID = "stub_lazy_agent"


def _agent(agent_id, version):
    return SimpleNamespace(
        agent_id=agent_id, agent_type=agent_id.rsplit("_", 1)[0],
        config=SimpleNamespace(capabilities=[]), built_from=version,
        bind_runtime=lambda runtime: None,
    )


def _factory(evict_during_build=0):
    """A factory without its constructor; its build evicts the user mid-build for the first `evict_during_build`
    builds (as `$admin_cache_reset` would while the build awaits)."""
    factory = UserAgentFactory.__new__(UserAgentFactory)
    factory.coordinator = AgentCoordinator()
    factory.prompt_content_store = object()
    factory.quota_service = object()
    factory._cache = {}
    factory._cache_ttl = 3600
    factory._creation_locks = {}
    factory._generations = {}
    factory.built = 0

    async def fake_create(user_id):
        factory.built += 1
        version = factory.built
        await asyncio.sleep(0)
        if version <= evict_during_build:
            factory.invalidate_user_cache(user_id)
        agents = {key: _agent(f"{key}_{user_id}", version) for key in UserAgentFactory._EAGER_AGENT_KEYS}
        factory._register_agents(list(agents.values()))
        entry = {
            "last_used": time.time(),
            "_user_context": _UserContext(user_profile=SimpleNamespace(version=version), prompt_builder=object()),
            "_lazy_agent_ids": [],
            **agents,
        }
        factory._cache[user_id] = entry
        return entry

    factory._create_and_cache_agents = fake_create
    return factory


class _ContendedLock(asyncio.Lock):
    """The per-user creation lock, which records that a second acquirer arrived while it was held."""

    def __init__(self):
        super().__init__()
        self.contended = asyncio.Event()

    async def acquire(self):
        if self.locked():
            self.contended.set()
        return await super().acquire()


def _registered_versions(factory):
    return {agent.built_from for agent in factory.coordinator.agents.values()}


def test_an_eviction_of_an_uncached_user_still_advances_the_generation():
    factory = _factory()
    before = factory._generation(USER)

    assert factory._evict_user(USER) is False

    assert factory._generation(USER) == before + 1


async def test_a_build_overtaken_once_is_redone_and_only_the_redone_build_stays_registered():
    factory = _factory(evict_during_build=1)

    entry = await factory.ensure_agents_for_user(USER)

    assert factory.built == 2
    assert factory._cache[USER] is entry
    assert entry["_user_context"].user_profile.version == 2
    assert _registered_versions(factory) == {2}


async def test_a_build_overtaken_every_time_is_kept_after_the_last_attempt(caplog):
    factory = _factory(evict_during_build=100)

    entry = await factory.ensure_agents_for_user(USER)

    assert factory.built == _MAX_BUILD_ATTEMPTS
    assert factory._cache[USER] is entry
    assert _registered_versions(factory) == {_MAX_BUILD_ATTEMPTS}
    assert "keeping the last one" in caplog.text


async def test_a_lazy_build_that_finds_its_entry_evicted_rebuilds_it_and_is_tracked(monkeypatch):
    monkeypatch.setitem(UserAgentFactory._LAZY_BUILDERS, LAZY_TYPE,
                        lambda factory, user_id, ctx: _agent(f"{LAZY_BASE_ID}_{user_id}", ctx.user_profile.version))
    monkeypatch.setitem(UserAgentFactory._LAZY_AGENT_IDS, LAZY_TYPE, LAZY_BASE_ID)
    factory = _factory()
    await factory.ensure_agents_for_user(USER)
    lock = _ContendedLock()
    factory._creation_locks[USER] = lock

    async with lock:
        build = asyncio.create_task(factory.create_agent_on_demand(LAZY_TYPE, USER))
        await asyncio.wait_for(lock.contended.wait(), 2.0)
        factory.invalidate_user_cache(USER)
    built = await build

    assert lock.contended.is_set()
    assert built is True
    entry = factory._cache[USER]
    agent = factory.coordinator.get_agent(f"{LAZY_BASE_ID}_{USER}")
    assert agent is not None and agent.agent_id in entry["_lazy_agent_ids"]
    assert agent.built_from == entry["_user_context"].user_profile.version == 2
