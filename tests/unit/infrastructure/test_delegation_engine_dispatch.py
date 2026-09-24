"""DelegationEngine.dispatch — the single-call dispatch a second caller (LelikAgent) uses."""
import pytest
from unittest.mock import AsyncMock

from src.domain.agent import AgentResponse
from src.domain.llm import ToolCall
from src.infrastructure.agent_manifest import SEARCH_WEB_MAPS_FANOUT, Intent
from src.infrastructure.delegation_engine import DelegationEngine, normalize_delegate_context


def test_normalize_wraps_free_text_as_reasoning():
    assert normalize_delegate_context("because") == {"reasoning": "because"}


def test_normalize_keeps_dicts_and_drops_junk():
    assert normalize_delegate_context({"a": 1}) == {"a": 1}
    assert normalize_delegate_context(None) == {}
    assert normalize_delegate_context(["x"]) == {}
    assert normalize_delegate_context("") == {}


@pytest.mark.asyncio
async def test_public_dispatch_fans_search_web_out_to_maps():
    coordinator = AsyncMock()
    coordinator.handle_delegation.return_value = AgentResponse.success(task_id="t", agent_id="a", result="ok")
    engine = DelegationEngine(coordinator)

    result = await engine.dispatch(
        ToolCall(name="delegate_to_specialist", args={"intent": "search_web", "query": "weather Valencia"}),
        {"user_id": "u1"},
        intent_remap={},
        intent_fanout={Intent.SEARCH_WEB: SEARCH_WEB_MAPS_FANOUT},
        calling_agent_id="lelik_agent_u1",
    )

    intents = sorted(c.kwargs["intent"] for c in coordinator.handle_delegation.await_args_list)
    assert intents == ["maps_query", "search_web"]
    assert "Primary specialist: Web Search" in result.result_str
