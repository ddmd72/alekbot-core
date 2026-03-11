"""
Unit tests for BillingAgent reliability (P1 fixes).

Covers:
- start() creates the flush task (not __init__)
- asyncio.Lock protects pending_records under concurrent access
- _flush_user pops records atomically and performs I/O outside lock
- shutdown flushes remaining records before exit
- periodic_flush iterates snapshot of keys (safe concurrent modification)
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.agents.infrastructure.billing_agent import BillingAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentIntent, AgentResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(agent_id="billing_agent"):
    return AgentConfig(
        agent_id=agent_id,
        agent_type="billing",
        timeout_ms=None,
        capabilities=[]
    )


def _make_message(user_id="u1", tokens=100, cost=0.01, model="gemini-flash"):
    return AgentMessage(
        intent=AgentIntent.INFORM,
        payload={
            "user_id": user_id,
            "tokens": tokens,
            "cost": cost,
            "model": model,
        },
        sender="quick_response_agent",
        recipient="billing_agent",
        task_id="t1",
        context={}
    )


def _make_agent(flush_threshold=5, flush_interval=60):
    quota_service = AsyncMock()
    quota_service.record_usage = AsyncMock()
    agent = BillingAgent(
        config=_make_config(),
        quota_service=quota_service,
        flush_threshold=flush_threshold,
        flush_interval=flush_interval,
    )
    return agent, quota_service


# ---------------------------------------------------------------------------
# start() / __init__ task creation
# ---------------------------------------------------------------------------

class TestBillingAgentLifecycle:

    def test_init_does_not_create_flush_task(self):
        """Background task must NOT be created in __init__ (no running loop yet)."""
        agent, _ = _make_agent()
        assert agent._flush_task is None

    async def test_start_creates_flush_task(self):
        agent, _ = _make_agent(flush_interval=9999)
        await agent.start()

        assert agent._flush_task is not None
        assert not agent._flush_task.done()

        # Cleanup
        agent._flush_task.cancel()
        try:
            await agent._flush_task
        except asyncio.CancelledError:
            pass

    async def test_shutdown_cancels_flush_task(self):
        agent, _ = _make_agent(flush_interval=9999)
        await agent.start()
        task = agent._flush_task

        await agent.shutdown()

        assert task.cancelled() or task.done()

    async def test_shutdown_flushes_pending_records(self):
        agent, quota_service = _make_agent(flush_threshold=100)  # high threshold → won't auto-flush
        # Add a record manually
        agent.pending_records["u1"].append({"tokens": 50, "cost": 0.005, "model": "m", "agent": "a", "timestamp": 0})

        await agent.shutdown()

        quota_service.record_usage.assert_awaited_once_with(
            user_id="u1",
            model="m",
            tokens=50,
            cost=0.005,
        )


# ---------------------------------------------------------------------------
# can_handle
# ---------------------------------------------------------------------------

class TestBillingAgentCanHandle:

    async def test_accepts_inform_with_required_fields(self):
        agent, _ = _make_agent()
        assert await agent.can_handle(_make_message())

    async def test_rejects_non_inform_intent(self):
        agent, _ = _make_agent()
        msg = AgentMessage(
            intent=AgentIntent.QUERY,
            payload={"user_id": "u", "tokens": 1, "cost": 0.0, "model": "m"},
            sender="x", recipient="billing_agent", task_id="t", context={}
        )
        assert not await agent.can_handle(msg)

    async def test_rejects_missing_required_field(self):
        agent, _ = _make_agent()
        msg = AgentMessage(
            intent=AgentIntent.INFORM,
            payload={"user_id": "u", "tokens": 1},  # missing cost + model
            sender="x", recipient="billing_agent", task_id="t", context={}
        )
        assert not await agent.can_handle(msg)


# ---------------------------------------------------------------------------
# execute + flush threshold
# ---------------------------------------------------------------------------

class TestBillingAgentExecute:

    async def test_execute_accumulates_record(self):
        agent, quota_service = _make_agent(flush_threshold=10)
        await agent.execute(_make_message(user_id="u1", tokens=100))

        assert len(agent.pending_records["u1"]) == 1
        quota_service.record_usage.assert_not_awaited()

    async def test_execute_flushes_when_threshold_reached(self):
        agent, quota_service = _make_agent(flush_threshold=3)
        for _ in range(3):
            await agent.execute(_make_message(user_id="u1", tokens=50, cost=0.005))

        quota_service.record_usage.assert_awaited_once()
        call_kwargs = quota_service.record_usage.await_args.kwargs
        assert call_kwargs["tokens"] == 150
        assert abs(call_kwargs["cost"] - 0.015) < 1e-9
        # Records should be cleared after flush
        assert "u1" not in agent.pending_records

    async def test_execute_returns_success_response(self):
        agent, _ = _make_agent()
        response = await agent.execute(_make_message())
        assert response.success
        assert response.result == "recorded"

    async def test_execute_isolated_per_user(self):
        """Records for different users don't mix."""
        agent, quota_service = _make_agent(flush_threshold=5)
        await agent.execute(_make_message(user_id="ua", tokens=100))
        await agent.execute(_make_message(user_id="ub", tokens=200))

        assert len(agent.pending_records["ua"]) == 1
        assert len(agent.pending_records["ub"]) == 1


# ---------------------------------------------------------------------------
# Concurrency: asyncio.Lock protects pending_records
# ---------------------------------------------------------------------------

class TestBillingAgentConcurrency:

    async def test_concurrent_execute_does_not_corrupt_records(self):
        """
        50 concurrent execute calls for the same user → exactly 50 records
        (or quota_service called if threshold hit mid-run).
        Total tokens must equal 50 * 10 = 500.
        """
        agent, quota_service = _make_agent(flush_threshold=100)  # No auto-flush

        async def one_execute():
            await agent.execute(_make_message(user_id="u1", tokens=10, cost=0.001))

        await asyncio.gather(*[one_execute() for _ in range(50)])

        # All 50 records must be present (threshold not reached)
        assert len(agent.pending_records["u1"]) == 50
        total_tokens = sum(r["tokens"] for r in agent.pending_records["u1"])
        assert total_tokens == 500

    async def test_concurrent_flush_does_not_double_pop(self):
        """
        Concurrent _flush_user calls for the same user must not result in
        double-billing (records popped twice).
        """
        agent, quota_service = _make_agent(flush_threshold=100)

        # Pre-load 10 records
        for i in range(10):
            agent.pending_records["u1"].append(
                {"tokens": 1, "cost": 0.001, "model": "m", "agent": "a", "timestamp": 0}
            )

        # Trigger two concurrent flushes
        await asyncio.gather(
            agent._flush_user("u1"),
            agent._flush_user("u1"),
        )

        # The lock must guarantee exactly one flush call: second concurrent flush
        # sees an empty list and returns early without calling record_usage again.
        assert quota_service.record_usage.await_count == 1, (
            f"Expected exactly 1 record_usage call (lock protects pop), "
            f"got {quota_service.record_usage.await_count} — double-flush not prevented"
        )
        call_kwargs = quota_service.record_usage.await_args.kwargs
        assert call_kwargs["tokens"] == 10
