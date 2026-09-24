"""ToolResult.failed — every non-success dispatch path (rejection, exception, max retries,
missing intent, a fan-out whose primary section errored) marks the result so LelikAgent can
skip posting a chat copy of a link found inside an error string (VOICE_COMPANION_RFC §4.10)."""
import pytest
from unittest.mock import AsyncMock

from src.domain.agent import AgentResponse
from src.domain.llm import ToolCall
from src.infrastructure.agent_manifest import SEARCH_WEB_MAPS_FANOUT, Intent
from src.infrastructure.delegation_engine import DelegationEngine


def _engine(coordinator=None):
    return DelegationEngine(coordinator or AsyncMock())


@pytest.mark.asyncio
async def test_missing_intent_is_failed():
    engine = _engine()
    result = await engine.dispatch(
        ToolCall(name="delegate_to_specialist", args={"query": "no intent given"}),
        {}, intent_remap={}, intent_fanout={}, calling_agent_id="x",
    )
    assert result.failed is True
    assert "SYSTEM ERROR" in result.result_str


@pytest.mark.asyncio
async def test_coordinator_rejection_is_failed():
    coordinator = AsyncMock()
    coordinator.handle_delegation.return_value = AgentResponse.failure(
        task_id="t", agent_id="a", error="rate limited: https://platform.openai.com/",
    )
    engine = _engine(coordinator)
    result = await engine.dispatch(
        ToolCall(name="delegate_to_specialist", args={"intent": "search_memory", "query": "q"}),
        {}, intent_remap={}, intent_fanout={}, calling_agent_id="x",
    )
    assert result.failed is True
    assert "rejected" in result.result_str


@pytest.mark.asyncio
async def test_successful_dispatch_is_not_failed():
    coordinator = AsyncMock()
    coordinator.handle_delegation.return_value = AgentResponse.success(task_id="t", agent_id="a", result="ok")
    engine = _engine(coordinator)
    result = await engine.dispatch(
        ToolCall(name="delegate_to_specialist", args={"intent": "search_memory", "query": "q"}),
        {}, intent_remap={}, intent_fanout={}, calling_agent_id="x",
    )
    assert result.failed is False


@pytest.mark.asyncio
async def test_max_retries_exceeded_path_is_failed():
    """max_retries=-1 makes range(max_retries + 1) empty, falling through to the tail
    return without ever calling the coordinator — the exact "Max retries exceeded" branch."""
    coordinator = AsyncMock()
    engine = _engine(coordinator)
    result = await engine.dispatch(
        ToolCall(name="delegate_to_specialist", args={"intent": "search_memory", "query": "q"}),
        {}, intent_remap={}, intent_fanout={}, calling_agent_id="x", max_retries=-1,
    )
    assert result.failed is True
    assert "Max retries exceeded" in result.result_str
    coordinator.handle_delegation.assert_not_awaited()


@pytest.mark.asyncio
async def test_exception_during_parallel_dispatch_is_failed():
    coordinator = AsyncMock()
    coordinator.handle_delegation.side_effect = RuntimeError("boom")
    engine = _engine(coordinator)
    results = await engine._execute_tool_calls(
        tool_calls=[ToolCall(name="delegate_to_specialist", args={"intent": "search_memory", "query": "q"})],
        context={}, intent_remap={}, intent_fanout={}, calling_agent_id="x",
        max_retries=1, retry_backoff=0,
    )
    assert len(results) == 1
    assert results[0].failed is True
    assert "AGENT ERROR" in results[0].result_str


@pytest.mark.asyncio
async def test_fanout_primary_rejection_is_failed_even_when_secondary_succeeds():
    coordinator = AsyncMock()

    async def handle_delegation(intent, **kwargs):
        if intent == "search_web":
            return AgentResponse.failure(task_id="t", agent_id="a", error="down")
        return AgentResponse.success(task_id="t", agent_id="a", result="maps ok")

    coordinator.handle_delegation.side_effect = handle_delegation
    engine = _engine(coordinator)
    result = await engine.dispatch(
        ToolCall(name="delegate_to_specialist", args={"intent": "search_web", "query": "weather"}),
        {}, intent_remap={}, intent_fanout={Intent.SEARCH_WEB: SEARCH_WEB_MAPS_FANOUT},
        calling_agent_id="x",
    )
    assert result.failed is True


@pytest.mark.asyncio
async def test_fanout_secondary_only_failure_is_not_failed():
    coordinator = AsyncMock()

    async def handle_delegation(intent, **kwargs):
        if intent == "search_web":
            return AgentResponse.success(task_id="t", agent_id="a", result="web ok")
        return AgentResponse.failure(task_id="t", agent_id="a", error="maps down")

    coordinator.handle_delegation.side_effect = handle_delegation
    engine = _engine(coordinator)
    result = await engine.dispatch(
        ToolCall(name="delegate_to_specialist", args={"intent": "search_web", "query": "weather"}),
        {}, intent_remap={}, intent_fanout={Intent.SEARCH_WEB: SEARCH_WEB_MAPS_FANOUT},
        calling_agent_id="x",
    )
    assert result.failed is False
