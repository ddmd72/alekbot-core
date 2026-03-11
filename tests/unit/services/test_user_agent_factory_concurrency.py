"""
Unit tests for UserAgentFactory concurrency fixes (P1).

Covers:
- Fast path: cached agents returned without lock
- Slow path: per-user asyncio.Lock prevents duplicate creation
- Double-checked locking: second waiter uses cache populated by first
- Different users do NOT serialize on each other (independent locks)
- ensure_agents_for_user delegates creation to _create_and_cache_agents
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock


# ---------------------------------------------------------------------------
# Minimal factory builder — all shared services injected as mocks
# ---------------------------------------------------------------------------

def _make_factory():
    """Create a UserAgentFactory with all external dependencies mocked."""
    from src.composition.user_agent_factory import UserAgentFactory

    return UserAgentFactory(
        config={"GEMINI_API_KEY": "k", "ANTHROPIC_API_KEY": "k"},
        env_config=MagicMock(),
        coordinator=MagicMock(),
        user_repo=AsyncMock(),
        account_repo=AsyncMock(),
        session_store=MagicMock(),
        llm_port=MagicMock(),
        claude_service=MagicMock(),
        grok_service=None,
        embedding_service=MagicMock(),
        repository=AsyncMock(),
        config_service=MagicMock(),
        biographical_context_service=MagicMock(),
        registry=MagicMock(),
        context_builder=MagicMock(),
        component_service=MagicMock(),
        assembly_service=None,
        fact_write_service=MagicMock(),
        fact_management_adapter_factory=MagicMock(return_value=MagicMock()),
        email_search_service=MagicMock(),
    )


# ---------------------------------------------------------------------------
# Fast path
# ---------------------------------------------------------------------------

class TestFastPath:

    async def test_returns_cached_without_creation(self):
        factory = _make_factory()
        sentinel = {"last_used": 9_999_999_999, "router_agent": MagicMock()}
        factory._cache["u1"] = sentinel

        factory._create_and_cache_agents = AsyncMock()
        result = await factory.ensure_agents_for_user("u1")

        assert result is sentinel
        factory._create_and_cache_agents.assert_not_awaited()

    async def test_expired_cache_triggers_creation(self):
        factory = _make_factory()
        factory._cache["u1"] = {"last_used": 0}  # Expired (Unix epoch)

        expected = {"last_used": 9_999_999_999, "router_agent": MagicMock()}
        factory._create_and_cache_agents = AsyncMock(return_value=expected)

        result = await factory.ensure_agents_for_user("u1")

        assert result is expected
        factory._create_and_cache_agents.assert_awaited_once_with("u1")

    async def test_missing_user_triggers_creation(self):
        factory = _make_factory()
        expected = {"last_used": 9_999_999_999}
        factory._create_and_cache_agents = AsyncMock(return_value=expected)

        result = await factory.ensure_agents_for_user("new_user")

        assert result is expected
        factory._create_and_cache_agents.assert_awaited_once_with("new_user")


# ---------------------------------------------------------------------------
# Per-user lock: double-checked locking
# ---------------------------------------------------------------------------

class TestPerUserLock:

    async def test_concurrent_first_call_creates_agents_once(self):
        """
        Two concurrent calls for the same unknown user must call
        _create_and_cache_agents exactly once. Second caller uses cache.
        """
        factory = _make_factory()
        creation_count = 0
        creation_started = asyncio.Event()

        async def slow_creation(user_id: str):
            nonlocal creation_count
            creation_count += 1
            creation_started.set()
            await asyncio.sleep(0.05)  # Simulate I/O delay
            result = {"last_used": 9_999_999_999, "agent": f"created for {user_id}"}
            factory._cache[user_id] = result  # Populate cache (simulates real behaviour)
            return result

        factory._create_and_cache_agents = slow_creation

        # Launch two concurrent calls for the same user
        r1, r2 = await asyncio.gather(
            factory.ensure_agents_for_user("u1"),
            factory.ensure_agents_for_user("u1"),
        )

        assert creation_count == 1, f"Expected 1 creation, got {creation_count}"
        # Both callers get the same result
        assert r1 is r2

    async def test_different_users_do_not_serialize(self):
        """
        Two concurrent calls for DIFFERENT users must run in parallel
        (their locks are independent).
        """
        factory = _make_factory()
        started = []
        barrier = asyncio.Event()

        async def slow_creation(user_id: str):
            started.append(user_id)
            if len(started) == 2:
                barrier.set()
            else:
                await asyncio.wait_for(barrier.wait(), timeout=1.0)
            result = {"last_used": 9_999_999_999}
            factory._cache[user_id] = result
            return result

        factory._create_and_cache_agents = slow_creation

        await asyncio.gather(
            factory.ensure_agents_for_user("ua"),
            factory.ensure_agents_for_user("ub"),
        )

        # Both users started creation simultaneously (barrier was reached)
        assert set(started) == {"ua", "ub"}

    async def test_second_waiter_sees_cache_after_first_completes(self):
        """
        Simulate exact race: both coroutines pass fast-path simultaneously,
        then first acquires lock and creates, second acquires lock after first
        releases and must see the cache entry.
        """
        factory = _make_factory()
        call_order = []

        async def create_with_tracking(user_id: str):
            call_order.append("create_start")
            await asyncio.sleep(0)  # yield
            result = {"last_used": 9_999_999_999}
            factory._cache[user_id] = result
            call_order.append("create_end")
            return result

        factory._create_and_cache_agents = create_with_tracking

        r1, r2 = await asyncio.gather(
            factory.ensure_agents_for_user("u1"),
            factory.ensure_agents_for_user("u1"),
        )

        # Creation happened exactly once
        assert call_order.count("create_start") == 1

    async def test_per_user_lock_created_on_demand(self):
        factory = _make_factory()
        assert "u_new" not in factory._creation_locks

        factory._create_and_cache_agents = AsyncMock(
            return_value={"last_used": 9_999_999_999}
        )
        await factory.ensure_agents_for_user("u_new")

        assert "u_new" in factory._creation_locks

    async def test_repeated_calls_reuse_same_lock(self):
        """Lock for a user must be the same object across multiple calls."""
        factory = _make_factory()

        def _caching_mock(uid):
            result = {"last_used": 9_999_999_999}
            factory._cache[uid] = result
            return result

        factory._create_and_cache_agents = AsyncMock(side_effect=_caching_mock)
        # Populate cache via first call
        await factory.ensure_agents_for_user("u1")
        first_lock = factory._creation_locks.get("u1")

        # Expire cache and call again
        factory._cache["u1"]["last_used"] = 0
        factory._create_and_cache_agents = AsyncMock(side_effect=_caching_mock)
        await factory.ensure_agents_for_user("u1")
        second_lock = factory._creation_locks.get("u1")

        assert first_lock is second_lock
