"""Sync/async is the intent's declared mode — the orchestrator cannot override it.

The per-call `mode: "now" | "later"` switch (2026-08-25) was removed 2026-10-06. "later"
had no path back to the caller: an ASYNC result is delivered to the USER by
AgentWorkerHandler, and only for generator intents. So the switch chose the result's
recipient, not the timing — and "now" on a generator lost its document on the notify()
path (morning briefing, 2026-10-06). See decisions/delegate_mode_parameter_removed.md.

A stray `mode` argument (a model that remembers the old schema, or fills every field) must
be ignored, never break the call and never change the dispatch.
"""
from unittest.mock import AsyncMock, MagicMock

from src.domain.agent import AgentResponse
from src.domain.llm import ToolCall
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_registry import AgentDescriptor, AgentRegistry, ExecutionMode
from src.infrastructure.delegation_engine import DelegationEngine


def _engine_over_real_coordinator(intent: str, declared: ExecutionMode):
    registry = MagicMock(spec=AgentRegistry)
    registry.get_agent_for_intent = MagicMock(return_value=AgentDescriptor(
        agent_id="specialist_agent",
        capabilities={intent: declared},
        dispatch_deadline_s=300,
    ))
    registry.get_available_intents = MagicMock(return_value=[{"name": intent}])

    queue = AsyncMock()
    queue.enqueue_agent_task = AsyncMock(return_value="task-1")

    coord = AgentCoordinator(registry=registry, task_queue=queue)
    coord.route_message = AsyncMock(return_value=AgentResponse.success(
        task_id="t1", agent_id="specialist_agent", result="ok",
    ))
    return DelegationEngine(coord), coord, queue


async def _dispatch(engine: DelegationEngine, args: dict):
    return await engine.dispatch(
        ToolCall(name="delegate_to_specialist", args=args),
        {"user_id": "u1"}, {}, {}, "smart_agent", 0, 0.0,
    )


class TestManifestDecidesTheMode:

    async def test_sync_intent_runs_sync(self):
        engine, coord, queue = _engine_over_real_coordinator("search_memory", ExecutionMode.SYNC)

        await _dispatch(engine, {"intent": "search_memory", "query": "q"})

        coord.route_message.assert_awaited_once()
        queue.enqueue_agent_task.assert_not_awaited()

    async def test_async_intent_enqueues(self):
        engine, coord, queue = _engine_over_real_coordinator("create_html_page", ExecutionMode.ASYNC)

        await _dispatch(engine, {"intent": "create_html_page", "query": "q"})

        queue.enqueue_agent_task.assert_awaited_once()
        coord.route_message.assert_not_awaited()


class TestToolSchemaHasNoMode:

    def test_mode_is_not_offered_to_the_model(self):
        from src.agents.base_agent import BaseAgent
        schema = BaseAgent._build_delegate_tool_declaration(
            [{"name": "search_memory", "description": "search"}]
        )
        assert "mode" not in schema["parameters"]["properties"]
        assert "mode" not in schema["parameters"]["required"]


class TestStrayModeIsIgnored:

    async def test_now_cannot_pull_a_generator_into_the_orchestrators_turn(self):
        """The 2026-10-06 incident: create_html_page with mode "now" ran inside Smart's
        notify() turn and the page was never delivered. It must go to the queue."""
        engine, coord, queue = _engine_over_real_coordinator("create_html_page", ExecutionMode.ASYNC)

        await _dispatch(engine, {"intent": "create_html_page", "query": "q", "mode": "now"})

        queue.enqueue_agent_task.assert_awaited_once()
        coord.route_message.assert_not_awaited()

    async def test_later_cannot_send_a_sync_answer_to_nowhere(self):
        """AgentWorkerHandler delivers no result for a SYNC-declared intent: "later" on
        search_memory would compute the answer and drop it."""
        engine, coord, queue = _engine_over_real_coordinator("search_memory", ExecutionMode.SYNC)

        result = await _dispatch(engine, {"intent": "search_memory", "query": "q", "mode": "later"})

        coord.route_message.assert_awaited_once()
        queue.enqueue_agent_task.assert_not_awaited()
        assert result.result_str == "ok"

    async def test_engine_passes_no_mode_to_the_coordinator(self):
        coordinator = MagicMock()
        coordinator.handle_delegation = AsyncMock(return_value=AgentResponse.success(
            task_id="t", agent_id="memory_agent", result="ok",
        ))
        engine = DelegationEngine(coordinator)

        await _dispatch(engine, {"intent": "search_memory", "query": "q", "mode": "later"})

        assert "mode_override" not in coordinator.handle_delegation.await_args.kwargs

    async def test_log_names_the_declared_mode_without_override_marker(self, caplog):
        engine, _, _ = _engine_over_real_coordinator("create_html_page", ExecutionMode.ASYNC)

        with caplog.at_level("INFO"):
            await _dispatch(engine, {"intent": "create_html_page", "query": "q", "mode": "now"})

        assert "mode=ExecutionMode.ASYNC" in caplog.text
        assert "OVERRIDE" not in caplog.text
