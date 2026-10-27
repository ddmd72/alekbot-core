"""P15 · J1: the factory's per-user cache under interleavings the per-user lock does not cover.

`UserAgentFactory` builds under a per-user `asyncio.Lock` (`ensure_agents_for_user`, `create_agent_on_demand`), but its
single eviction path `_evict_user` (`invalidate_user_cache`, the TTL sweep) takes no lock. Each race below is forced with
`asyncio.Event` gates: the gated step sets `reached`, the test acts only after it, then releases, and asserts the gate was
reached, so a run that happened to be serial cannot pass.

The factory is the real class without its constructor (`UserAgentFactory.__new__`), a real `AgentCoordinator`, the real
`_evict_user` / `_register_agents` / `create_agent_on_demand` / `ensure_agents_for_user`, and a build that stands in for
`_create_and_cache_agents`: it reads the profile through the real `_load_user_profile`, registers stub agents through
`_register_agents`, and stores an entry of the production shape (`_user_context`, `_lazy_agent_ids`, the eager keys).
"""
import asyncio
import time
from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest

from src.composition.user_agent_factory import UserAgentFactory, _UserContext
from src.domain.agent import AgentResponse, AgentStatus
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import ALL_DESCRIPTORS, Intent
from src.infrastructure.agent_registry import AgentRegistry

USER = "u1"
LAZY_TYPE = "stub_lazy"
LAZY_BASE_ID = "stub_lazy_agent"
GATE_TIMEOUT_S = 2.0

# Eager keys whose agent id the manifest fixes (the coordinator routes delegations to `<descriptor id>_<user>`).
_EAGER_IDS = {"memory_agent": "facts_memory_agent"}


class _Gate:
    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def hold(self) -> None:
        self.reached.set()
        await self.release.wait()


class _ContendedLock(asyncio.Lock):
    """The per-user creation lock, which records that a second acquirer arrived while it was held."""

    def __init__(self) -> None:
        super().__init__()
        self.contended = asyncio.Event()

    async def acquire(self) -> bool:
        if self.locked():
            self.contended.set()
        return await super().acquire()


class _StubAgent:
    """A registered agent stamped with the profile version it was built from; answers every message."""

    def __init__(self, agent_id: str, built_from: Any) -> None:
        self.agent_id = agent_id
        self.agent_type = agent_id.rsplit("_", 1)[0]
        self.config = SimpleNamespace(capabilities=[])
        self.built_from = built_from

    async def process(self, message) -> AgentResponse:
        return AgentResponse.success(
            task_id=message.task_id, agent_id=self.agent_id, result=f"answer from {self.agent_id}",
        )


class _UserRepo:
    """The stored user profile: `version` is what the next read returns."""

    def __init__(self) -> None:
        self.version = 1

    async def get_user(self, user_id: str):
        return SimpleNamespace(user_id=user_id, is_active=True, version=self.version)


def _factory(registry: Optional[AgentRegistry] = None) -> UserAgentFactory:
    factory = UserAgentFactory.__new__(UserAgentFactory)
    factory.coordinator = AgentCoordinator(registry=registry, agent_factory=factory)
    factory.user_repo = _UserRepo()
    factory.prompt_content_store = object()
    factory.quota_service = object()
    factory._cache = {}
    factory._cache_ttl = 3600
    factory._creation_locks = {}
    factory.build_gate = None

    async def fake_create(user_id: str) -> Dict[str, object]:
        # Stands in for _create_and_cache_agents: profile read, (optional gate), register, write the entry.
        profile = await factory._load_user_profile(user_id)
        if factory.build_gate is not None:
            await factory.build_gate.hold()
        agents = {
            key: _StubAgent(f"{_EAGER_IDS.get(key, key)}_{user_id}", profile)
            for key in UserAgentFactory._EAGER_AGENT_KEYS
        }
        factory._register_agents(list(agents.values()))
        entry = {
            "last_used": time.time(),
            "_user_context": _UserContext(user_profile=profile, prompt_builder=object()),
            "_lazy_agent_ids": [],
            **agents,
        }
        factory._cache[user_id] = entry
        return entry

    factory._create_and_cache_agents = fake_create
    return factory


def _lazy_builder(factory: UserAgentFactory, user_id: str, ctx: _UserContext) -> _StubAgent:
    return _StubAgent(f"{LAZY_BASE_ID}_{user_id}", ctx)


@pytest.fixture
def lazy_type(monkeypatch):
    monkeypatch.setitem(UserAgentFactory._LAZY_BUILDERS, LAZY_TYPE, _lazy_builder)
    monkeypatch.setitem(UserAgentFactory._LAZY_AGENT_IDS, LAZY_TYPE, LAZY_BASE_ID)


def _registry() -> AgentRegistry:
    registry = AgentRegistry()
    for descriptor in ALL_DESCRIPTORS:
        registry.register(descriptor)
    return registry


async def _lazy_build_waiting_on_the_lock(factory: UserAgentFactory, evict: bool):
    """The test holds the per-user lock; a lazy build starts and blocks on it; optionally the user is evicted; release.
    Returns the lock and the build's return value."""
    await factory.ensure_agents_for_user(USER)
    lock = _ContendedLock()
    factory._creation_locks[USER] = lock
    async with lock:
        build = asyncio.create_task(factory.create_agent_on_demand(LAZY_TYPE, USER))
        await asyncio.wait_for(lock.contended.wait(), GATE_TIMEOUT_S)
        if evict:
            factory.invalidate_user_cache(USER)
    return lock, await build


class TestLazyBuildAfterAnUnlockedEviction:

    async def test_control_a_lazy_build_that_waited_on_the_lock_is_tracked_by_the_current_entry(self, lazy_type):
        factory = _factory()

        lock, built = await _lazy_build_waiting_on_the_lock(factory, evict=False)

        assert lock.contended.is_set()
        assert built is True
        agent = factory.coordinator.get_agent(f"{LAZY_BASE_ID}_{USER}")
        assert agent is not None
        assert agent.agent_id in factory._cache[USER]["_lazy_agent_ids"]

    @pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
        "P15-S1: create_agent_on_demand reads the cache entry before the per-user lock; an eviction while it "
        "waits leaves the lazy agent registered but tracked only by the detached entry; owner decision"))
    async def test_a_lazy_agent_built_after_an_eviction_is_tracked_or_not_registered(self, lazy_type):
        """`create_agent_on_demand` reads `self._cache[user]` (user_agent_factory.py:701) and only then waits at the
        per-user lock (:717); `invalidate_user_cache` -> `_evict_user` (:281-309) takes no lock. The waiter then builds
        from the evicted entry's context and appends its id to that detached entry: the agent stays registered with the
        coordinator, no entry tracks it, and no later eviction unregisters it.

        Note for the owner: a fix must re-read the entry inside the lock and handle `None` there WITHOUT calling
        `ensure_agents_for_user` (it takes the same per-user lock, which is not re-entrant)."""
        factory = _factory()

        lock, built = await _lazy_build_waiting_on_the_lock(factory, evict=True)

        assert lock.contended.is_set()
        assert built in (True, False)  # a fix that finds no entry inside the lock may refuse (False)
        agent = factory.coordinator.get_agent(f"{LAZY_BASE_ID}_{USER}")
        entry = factory._cache.get(USER)
        assert agent is None or (entry is not None and agent.agent_id in entry["_lazy_agent_ids"])


class TestResetDuringABuild:

    async def _build_with_a_gate(self, factory: UserAgentFactory, reset: bool) -> None:
        factory.build_gate = _Gate()
        build = asyncio.create_task(factory.ensure_agents_for_user(USER))
        await asyncio.wait_for(factory.build_gate.reached.wait(), GATE_TIMEOUT_S)
        if reset:
            factory.invalidate_user_cache(USER)
            factory.user_repo.version = 2
        factory.build_gate.release.set()
        await build

    @staticmethod
    def _registered_versions(factory: UserAgentFactory) -> set:
        return {agent.built_from.version for agent in factory.coordinator.agents.values()}

    async def test_control_without_a_reset_the_build_caches_and_registers_what_it_read(self):
        factory = _factory()

        await self._build_with_a_gate(factory, reset=False)

        assert factory.build_gate.reached.is_set()
        assert factory._cache[USER]["_user_context"].user_profile.version == 1
        assert self._registered_versions(factory) == {1}

    @pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
        "P15-S1b: invalidate_user_cache during an eager build in progress is lost; the build caches the entry "
        "read before the reset; owner decision"))
    async def test_no_entry_built_from_the_profile_read_before_a_reset_is_cached_after_it(self):
        """`ensure_agents_for_user` holds the per-user lock while `_create_and_cache_agents` awaits (the profile read,
        user_agent_factory.py:335/668-672) and writes the entry at the end (:666); `_evict_user` (:297) is unlocked and
        finds no entry yet, so the reset (`$admin_cache_reset`) does nothing and the build then caches what it read
        before the reset. The eager agents are registered (:628) before the entry is written (:666), so the stale build
        is also what the coordinator routes to.

        Note for the owner: this test's build is a stand-in for `_create_and_cache_agents`. A fix must sit in
        `ensure_agents_for_user` / `_evict_user` (e.g. a per-user generation bumped by the eviction and checked by the
        locked caller before it keeps the build); a generation check placed inside `_create_and_cache_agents` before
        :666 is bypassed by the stand-in, and then the stand-in must be updated with the fix."""
        factory = _factory()

        await self._build_with_a_gate(factory, reset=True)

        assert factory.build_gate.reached.is_set()
        entry = factory._cache.get(USER)
        assert entry is None or entry["_user_context"].user_profile.version == 2
        assert 1 not in self._registered_versions(factory)


class TestEvictionDuringATurn:

    async def _turn(self, factory: UserAgentFactory, gate: _Gate, evict: bool):
        async def turn():
            context = {"user_id": USER}
            first = await factory.coordinator.handle_delegation(
                Intent.SEARCH_MEMORY, "first question", context, calling_agent_id=f"smart_agent_{USER}")
            await gate.hold()
            second = await factory.coordinator.handle_delegation(
                Intent.SEARCH_MEMORY, "second question", context, calling_agent_id=f"smart_agent_{USER}")
            return first, second

        await factory.ensure_agents_for_user(USER)
        running = asyncio.create_task(turn())
        await asyncio.wait_for(gate.reached.wait(), GATE_TIMEOUT_S)
        if evict:
            factory.invalidate_user_cache(USER)
        gate.release.set()
        return await running

    async def test_control_both_delegations_of_a_turn_reach_the_eager_specialist(self):
        factory = _factory(_registry())
        gate = _Gate()

        first, second = await self._turn(factory, gate, evict=False)

        assert gate.reached.is_set()
        assert first.status == AgentStatus.SUCCESS
        assert second.status == AgentStatus.SUCCESS

    @pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
        "P15-S4: an eviction during a turn unregisters the eager specialists, and the coordinator re-creates "
        "only lazy ones, so the turn's next delegation is 'Unknown recipient'; owner decision"))
    async def test_a_delegation_after_an_eviction_mid_turn_still_reaches_a_specialist(self):
        """The turn delegates to the eager memory specialist, pauses (gate), the user is evicted
        (`invalidate_user_cache`), the turn delegates again. `route_message` re-creates only lazy recipients
        (`_try_lazy_load`, agent_coordinator.py:183,503-505: eager descriptors are skipped), so the second
        delegation ends in the `Unknown recipient` failure (:218-223)."""
        factory = _factory(_registry())
        gate = _Gate()

        first, second = await self._turn(factory, gate, evict=True)

        assert gate.reached.is_set()
        assert first.status == AgentStatus.SUCCESS
        assert not (second.error or "").startswith("Unknown recipient")
        assert second.status == AgentStatus.SUCCESS
