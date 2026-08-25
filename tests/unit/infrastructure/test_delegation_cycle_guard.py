"""Delegation cycle guard.

Until now the delegation graph was acyclic only *by convention*: orchestrators call
specialists, and the three specialists that delegate (`notes`, `doc_planner`,
`doc_generator`) only call downward. `calling_agent_id` was documented as "For logging
only" — no depth limit, no call stack, no detection.

Companion agents (COMPANION_AGENTS_RFC.md) break that convention by design: a companion
calls Alek, and Alek can call the companion back.

The failure this prevents is silent, not loud. Through the async path every hop returns
an ack immediately, so `A →(async)→ B →(async)→ A` blocks nobody and looks like ordinary
activity while spending money indefinitely. That is why the chain must survive the Cloud
Task payload, and why refusal raises an alert rather than only a failure response.
"""
import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.domain.agent import AgentResponse, AgentStatus
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_registry import AgentRegistry, AgentDescriptor, ExecutionMode


def _registry(agent_id="memory_agent", intent="search_memory", mode=ExecutionMode.SYNC):
    registry = MagicMock(spec=AgentRegistry)
    desc = AgentDescriptor(
        agent_id=agent_id,
        capabilities={intent: mode},
        dispatch_deadline_s=300,
    )
    registry.get_agent_for_intent = MagicMock(return_value=desc)
    registry.get_available_intents = MagicMock(return_value=[{"name": intent}])
    return registry


def _coordinator(agent_id="memory_agent", intent="search_memory", mode=ExecutionMode.SYNC):
    """Coordinator whose sync dispatch is captured instead of executed."""
    queue = AsyncMock()
    queue.enqueue_agent_task = AsyncMock(return_value="task-1")
    alert = AsyncMock()
    alert.post = AsyncMock()

    coord = AgentCoordinator(
        registry=_registry(agent_id, intent, mode),
        task_queue=queue,
        alert_sink=alert,
    )
    coord.route_message = AsyncMock(return_value=AgentResponse.success(
        task_id="t1", agent_id=agent_id, result="ok",
    ))
    return coord, queue, alert


def _sync_context(coord) -> dict:
    """Context the coordinator handed to the dispatched agent."""
    return coord.route_message.await_args.args[0].context


class TestCycleIsRefused:

    async def test_direct_cycle_is_refused(self):
        """An agent already in the chain must not be entered again."""
        coord, _, _ = _coordinator(agent_id="tutor_agent")

        result = await coord.handle_delegation(
            intent="search_memory",
            query="q",
            context={"user_id": "u1", "_call_chain": ["tutor_agent"]},
            calling_agent_id="tutor_agent",
        )

        assert result.status == AgentStatus.FAILED

    async def test_refusal_names_the_cycle(self):
        """'too deep' is not actionable; the actual loop is."""
        coord, _, _ = _coordinator(agent_id="tutor_agent")

        result = await coord.handle_delegation(
            intent="search_memory",
            query="q",
            context={"user_id": "u1", "_call_chain": ["tutor_agent", "smart_agent"]},
            calling_agent_id="smart_agent",
        )

        assert "tutor_agent" in result.error
        assert "smart_agent" in result.error

    async def test_indirect_cycle_is_refused(self):
        coord, _, _ = _coordinator(agent_id="a_agent")

        result = await coord.handle_delegation(
            intent="search_memory",
            query="q",
            context={"user_id": "u1", "_call_chain": ["a_agent", "b_agent", "c_agent"]},
            calling_agent_id="c_agent",
        )

        assert result.status == AgentStatus.FAILED

    async def test_cycle_does_not_dispatch(self):
        coord, _, _ = _coordinator(agent_id="tutor_agent")

        await coord.handle_delegation(
            intent="search_memory",
            query="q",
            context={"user_id": "u1", "_call_chain": ["tutor_agent"]},
            calling_agent_id="tutor_agent",
        )

        coord.route_message.assert_not_awaited()

    async def test_refusal_raises_an_alert(self):
        """A specialist failure is wrapped as a tool result and the orchestrator still
        returns SUCCESS — without an alert the loop is invisible."""
        coord, _, alert = _coordinator(agent_id="tutor_agent")

        await coord.handle_delegation(
            intent="search_memory",
            query="q",
            context={"user_id": "u1", "_call_chain": ["tutor_agent"]},
            calling_agent_id="tutor_agent",
        )

        alert.post.assert_awaited_once()
        assert "tutor_agent" in alert.post.await_args.args[0]

    async def test_missing_alert_sink_does_not_break_refusal(self):
        coord = AgentCoordinator(registry=_registry("tutor_agent"), task_queue=AsyncMock())
        coord.route_message = AsyncMock()

        result = await coord.handle_delegation(
            intent="search_memory",
            query="q",
            context={"user_id": "u1", "_call_chain": ["tutor_agent"]},
            calling_agent_id="tutor_agent",
        )

        assert result.status == AgentStatus.FAILED


class TestDepthCap:

    async def test_non_repeating_runaway_is_capped(self):
        """A chain of all-distinct agents never trips the cycle check — the cap catches it."""
        chain = [f"agent_{i}" for i in range(AgentCoordinator.MAX_DELEGATION_DEPTH)]
        coord, _, _ = _coordinator(agent_id="memory_agent")

        result = await coord.handle_delegation(
            intent="search_memory",
            query="q",
            context={"user_id": "u1", "_call_chain": chain},
            calling_agent_id="agent_7",
        )

        assert result.status == AgentStatus.FAILED
        coord.route_message.assert_not_awaited()

    async def test_legitimate_depth_still_passes(self):
        """Smart → notes → compute is ordinary; the cap must not fire on real traffic."""
        coord, _, _ = _coordinator(agent_id="compute_agent")

        result = await coord.handle_delegation(
            intent="search_memory",
            query="q",
            context={"user_id": "u1", "_call_chain": ["smart_agent", "notes_agent"]},
            calling_agent_id="notes_agent",
        )

        assert result.status == AgentStatus.SUCCESS


class TestChainPropagation:

    async def test_chain_starts_empty_and_is_seeded(self):
        coord, _, _ = _coordinator(agent_id="memory_agent")

        await coord.handle_delegation(
            intent="search_memory", query="q", context={"user_id": "u1"},
            calling_agent_id="smart_agent",
        )

        assert _sync_context(coord)["_call_chain"] == ["memory_agent"]

    async def test_chain_grows_by_the_dispatched_agent(self):
        coord, _, _ = _coordinator(agent_id="memory_agent")

        await coord.handle_delegation(
            intent="search_memory",
            query="q",
            context={"user_id": "u1", "_call_chain": ["smart_agent"]},
            calling_agent_id="smart_agent",
        )

        assert _sync_context(coord)["_call_chain"] == ["smart_agent", "memory_agent"]

    async def test_caller_context_is_not_mutated(self):
        """The caller may dispatch several tool calls in parallel from one context —
        appending in place would cross-contaminate the siblings."""
        coord, _, _ = _coordinator(agent_id="memory_agent")
        context = {"user_id": "u1", "_call_chain": ["smart_agent"]}

        await coord.handle_delegation(
            intent="search_memory", query="q", context=context,
            calling_agent_id="smart_agent",
        )

        assert context["_call_chain"] == ["smart_agent"]


class TestChainSurvivesTheAsyncBoundary:

    async def test_chain_reaches_the_task_payload(self):
        coord, queue, _ = _coordinator(agent_id="research_agent", mode=ExecutionMode.ASYNC)

        await coord.handle_delegation(
            intent="search_memory",
            query="q",
            context={"user_id": "u1", "_call_chain": ["smart_agent"]},
            calling_agent_id="smart_agent",
        )

        enqueued = queue.enqueue_agent_task.await_args.kwargs["context"]
        assert enqueued["_call_chain"] == ["smart_agent", "research_agent"]

    async def test_chain_is_json_serialisable(self):
        """The Cloud Task body is json.dumps'd — a set or a custom type would break the
        hop and take the guard with it."""
        coord, queue, _ = _coordinator(agent_id="research_agent", mode=ExecutionMode.ASYNC)

        await coord.handle_delegation(
            intent="search_memory", query="q", context={"user_id": "u1"},
            calling_agent_id="smart_agent",
        )

        enqueued = queue.enqueue_agent_task.await_args.kwargs["context"]
        assert json.loads(json.dumps(enqueued))["_call_chain"] == ["research_agent"]

    async def test_cycle_through_the_queue_is_caught_on_return(self):
        """The case the guard exists for: each async hop acks immediately, so nothing
        blocks and the loop is invisible without the chain riding in the payload."""
        coord, queue, _ = _coordinator(agent_id="tutor_agent", mode=ExecutionMode.ASYNC)

        await coord.handle_delegation(
            intent="search_memory", query="q", context={"user_id": "u1"},
            calling_agent_id="tutor_agent",
        )
        round_tripped = json.loads(json.dumps(
            queue.enqueue_agent_task.await_args.kwargs["context"]
        ))

        result = await coord.handle_delegation(
            intent="search_memory", query="q", context=round_tripped,
            calling_agent_id="worker",
        )

        assert result.status == AgentStatus.FAILED


class TestOrdinaryTrafficIsUnaffected:

    async def test_sync_delegation_still_succeeds(self):
        coord, _, alert = _coordinator()

        result = await coord.handle_delegation(
            intent="search_memory", query="q", context={"user_id": "u1"},
            calling_agent_id="smart_agent",
        )

        assert result.status == AgentStatus.SUCCESS
        alert.post.assert_not_awaited()

    async def test_unknown_intent_still_fails_without_alerting(self):
        coord, _, alert = _coordinator()
        coord._registry.get_agent_for_intent = MagicMock(return_value=None)

        result = await coord.handle_delegation(
            intent="nope", query="q", context={"user_id": "u1"},
            calling_agent_id="smart_agent",
        )

        assert result.status == AgentStatus.FAILED
        alert.post.assert_not_awaited()
