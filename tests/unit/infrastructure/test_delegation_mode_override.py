"""Per-call sync/async: the caller may override the intent's declared mode.

`mode = manifest.capabilities[intent]` made the choice a property of the *intent*, so a
caller could never say "this one I need now" or "this one can come back later".
Companion agents (COMPANION_AGENTS_RFC.md §6) need both directions against the same
intents.

The manifest stays the default: a caller that passes nothing behaves exactly as before.

Deliberately absent: any "async-locked" flag protecting intents that are long by nature
(deep research runs as a Cloud Run Job). Forcing one of those sync hits the existing
agent timeout and fails loudly — and that failure is the signal to add the flag, rather
than guarding a hypothesis now.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.agent import AgentResponse
from src.domain.llm import ToolCall
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_registry import AgentRegistry, AgentDescriptor, ExecutionMode


def _coordinator(declared: ExecutionMode):
    registry = MagicMock(spec=AgentRegistry)
    registry.get_agent_for_intent = MagicMock(return_value=AgentDescriptor(
        agent_id="memory_agent",
        capabilities={"search_memory": declared},
        dispatch_deadline_s=300,
    ))
    registry.get_available_intents = MagicMock(return_value=[{"name": "search_memory"}])

    queue = AsyncMock()
    queue.enqueue_agent_task = AsyncMock(return_value="task-1")

    coord = AgentCoordinator(registry=registry, task_queue=queue)
    coord.route_message = AsyncMock(return_value=AgentResponse.success(
        task_id="t1", agent_id="memory_agent", result="ok",
    ))
    return coord, queue


async def _delegate(coord, **kwargs):
    return await coord.handle_delegation(
        intent="search_memory", query="q", context={"user_id": "u1"},
        calling_agent_id="smart_agent", **kwargs,
    )


class TestManifestRemainsTheDefault:

    async def test_sync_intent_without_override_runs_sync(self):
        coord, queue = _coordinator(ExecutionMode.SYNC)
        await _delegate(coord)
        coord.route_message.assert_awaited_once()
        queue.enqueue_agent_task.assert_not_awaited()

    async def test_async_intent_without_override_enqueues(self):
        coord, queue = _coordinator(ExecutionMode.ASYNC)
        await _delegate(coord)
        queue.enqueue_agent_task.assert_awaited_once()
        coord.route_message.assert_not_awaited()

    async def test_explicit_none_is_the_same_as_omitting(self):
        coord, queue = _coordinator(ExecutionMode.SYNC)
        await _delegate(coord, mode_override=None)
        coord.route_message.assert_awaited_once()


class TestOverrideWins:

    async def test_sync_intent_can_be_forced_async(self):
        """'get back to me later' on something normally answered inline."""
        coord, queue = _coordinator(ExecutionMode.SYNC)
        await _delegate(coord, mode_override=ExecutionMode.ASYNC)
        queue.enqueue_agent_task.assert_awaited_once()
        coord.route_message.assert_not_awaited()

    async def test_async_intent_can_be_forced_sync(self):
        """'I need this to continue the conversation'."""
        coord, queue = _coordinator(ExecutionMode.ASYNC)
        await _delegate(coord, mode_override=ExecutionMode.SYNC)
        coord.route_message.assert_awaited_once()
        queue.enqueue_agent_task.assert_not_awaited()

    async def test_override_does_not_bypass_the_cycle_guard(self):
        coord, _ = _coordinator(ExecutionMode.SYNC)
        result = await coord.handle_delegation(
            intent="search_memory", query="q",
            context={"user_id": "u1", "_call_chain": ["memory_agent"]},
            calling_agent_id="memory_agent",
            mode_override=ExecutionMode.ASYNC,
        )
        assert "cycle" in (result.error or "").lower()


class TestOnlyDivergenceIsFlagged:
    """The model sets `mode` on nearly every call despite the schema saying to omit it
    (measured live 2026-08-25: 4/4, all agreeing with the manifest). Marking every
    override would bury the one case that changes behaviour and can time out."""

    async def test_agreeing_override_is_not_flagged(self, caplog):
        coord, _ = _coordinator(ExecutionMode.SYNC)
        with caplog.at_level("INFO"):
            await _delegate(coord, mode_override=ExecutionMode.SYNC)

        assert "OVERRIDE" not in caplog.text

    async def test_divergent_override_is_flagged_with_the_declared_mode(self, caplog):
        coord, _ = _coordinator(ExecutionMode.SYNC)
        with caplog.at_level("INFO"):
            await _delegate(coord, mode_override=ExecutionMode.ASYNC)

        assert "OVERRIDE, declared sync" in caplog.text

    async def test_depth_is_logged(self, caplog):
        """Without it the guard's state is invisible in production — ironic for a
        feature whose point is making an invisible failure visible."""
        coord, _ = _coordinator(ExecutionMode.SYNC)
        with caplog.at_level("INFO"):
            await coord.handle_delegation(
                intent="search_memory", query="q",
                context={"user_id": "u1", "_call_chain": ["smart_agent", "notes_agent"]},
                calling_agent_id="notes_agent",
            )

        assert "depth=3" in caplog.text


class TestToolSchemaExposesMode:

    def _schema(self):
        from src.agents.base_agent import BaseAgent
        return BaseAgent._build_delegate_tool_declaration(
            [{"name": "search_memory", "description": "search"}]
        )

    def test_mode_is_declared(self):
        props = self._schema()["parameters"]["properties"]
        assert "mode" in props

    def test_mode_is_optional(self):
        """Omitting it must stay valid — the manifest is the default."""
        assert "mode" not in self._schema()["parameters"]["required"]

    def test_mode_uses_plain_words_not_jargon(self):
        """The model reads this; SYNC/ASYNC are our words, not concepts it reasons with."""
        assert set(self._schema()["parameters"]["properties"]["mode"]["enum"]) == {"now", "later"}


class TestEngineForwardsMode:

    def _engine(self):
        from src.infrastructure.delegation_engine import DelegationEngine
        coordinator = MagicMock()
        coordinator.handle_delegation = AsyncMock(return_value=AgentResponse.success(
            task_id="t", agent_id="memory_agent", result="ok",
        ))
        return DelegationEngine(coordinator), coordinator

    async def test_later_becomes_async(self):
        engine, coordinator = self._engine()
        await engine._dispatch_single(
            ToolCall(name="delegate_to_specialist",
                     args={"intent": "search_memory", "query": "q", "mode": "later"}),
            {"user_id": "u1"}, {}, {}, "smart_agent", 0, 0.0,
        )
        assert coordinator.handle_delegation.await_args.kwargs["mode_override"] is ExecutionMode.ASYNC

    async def test_now_becomes_sync(self):
        engine, coordinator = self._engine()
        await engine._dispatch_single(
            ToolCall(name="delegate_to_specialist",
                     args={"intent": "search_memory", "query": "q", "mode": "now"}),
            {"user_id": "u1"}, {}, {}, "smart_agent", 0, 0.0,
        )
        assert coordinator.handle_delegation.await_args.kwargs["mode_override"] is ExecutionMode.SYNC

    async def test_absent_mode_forwards_none(self):
        engine, coordinator = self._engine()
        await engine._dispatch_single(
            ToolCall(name="delegate_to_specialist",
                     args={"intent": "search_memory", "query": "q"}),
            {"user_id": "u1"}, {}, {}, "smart_agent", 0, 0.0,
        )
        assert coordinator.handle_delegation.await_args.kwargs["mode_override"] is None

    async def test_unknown_mode_is_ignored_not_fatal(self):
        """A model inventing a third word must not break the delegation."""
        engine, coordinator = self._engine()
        await engine._dispatch_single(
            ToolCall(name="delegate_to_specialist",
                     args={"intent": "search_memory", "query": "q", "mode": "eventually"}),
            {"user_id": "u1"}, {}, {}, "smart_agent", 0, 0.0,
        )
        assert coordinator.handle_delegation.await_args.kwargs["mode_override"] is None
