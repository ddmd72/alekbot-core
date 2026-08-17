"""
Terminal-tool turns that also carry real work.

The engine terminates on `terminal_tool` (Smart passes "deliver_response"). That
branch used to return BEFORE dispatching the turn's other tool calls, which was
harmless only while no adapter declared the terminal tool as a real function.

GrokAdapter does declare it — constrained JSON and function calling compete on
xAI, so the schema becomes the parameters of a synthesized `deliver_response`
(see `GrokAdapter._build_request` / `TERMINAL_TOOL_NAME`). The model can then
legitimately commit to work and answer in one batch, and on 2026-08-17 the
morning briefing did exactly that: `create_html_page` next to
`deliver_response`. The page was never enqueued.

These tests pin the contract: a co-emitted call is dispatched, its metadata
lands in the result, and the terminal answer is still what comes back.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.agent import AgentResponse, DeliveryItem
from src.domain.llm import LLMRequest, LLMResponse, Message, MessagePart, ToolCall
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.delegation_engine import DelegationEngine

TERMINAL = "deliver_response"


def _terminal_call(text: str = "here is your answer") -> ToolCall:
    return ToolCall(name=TERMINAL, args={"full_response": text})


def _delegation_call(intent: str, query: str = "do the thing") -> ToolCall:
    return ToolCall(
        name="delegate_to_specialist",
        args={"intent": intent, "query": query},
    )


def _base_request() -> LLMRequest:
    return LLMRequest(
        model_name="grok-4.6",
        messages=[Message(role="user", parts=[MessagePart(text="brief me")])],
    )


@pytest.fixture
def coordinator():
    coord = MagicMock(spec=AgentCoordinator)
    coord.handle_delegation = AsyncMock(
        return_value=AgentResponse.success(
            task_id="t1", agent_id="html_page_generator_agent", result="queued",
        )
    )
    return coord


async def _run(coordinator, tool_calls):
    """Drive one turn that returns `tool_calls`, then assert we never loop again."""
    engine = DelegationEngine(coordinator=coordinator)
    call_llm = AsyncMock(return_value=LLMResponse(text="", tool_calls=tool_calls))
    result = await engine.execute(
        call_llm=call_llm,
        base_request=_base_request(),
        context={"user_id": "u1"},
        max_turns=5,
        terminal_tool=TERMINAL,
        calling_agent_id="smart_u1",
    )
    # The terminal tool ends the loop — exactly one LLM turn, never a second.
    assert call_llm.await_count == 1
    return result


class TestTerminalToolWithSiblings:
    async def test_co_emitted_delegation_is_dispatched(self, coordinator):
        """The regression: create_html_page beside deliver_response must reach the queue."""
        result = await _run(
            coordinator,
            [_delegation_call("create_html_page", "render the briefing"), _terminal_call()],
        )

        coordinator.handle_delegation.assert_awaited_once()
        kwargs = coordinator.handle_delegation.await_args.kwargs
        assert kwargs["intent"] == "create_html_page"
        assert kwargs["query"] == "render the briefing"
        assert kwargs["calling_agent_id"] == "smart_u1"
        # ...and the terminal answer is still what the caller gets back.
        assert result.terminal_tool_args == {"full_response": "here is your answer"}

    async def test_sibling_order_does_not_matter(self, coordinator):
        """Terminal call first in the batch must not shadow the work after it."""
        result = await _run(
            coordinator,
            [_terminal_call(), _delegation_call("create_html_page")],
        )

        coordinator.handle_delegation.assert_awaited_once()
        assert coordinator.handle_delegation.await_args.kwargs["intent"] == "create_html_page"
        assert result.terminal_tool_args["full_response"] == "here is your answer"

    async def test_multiple_siblings_all_dispatched(self, coordinator):
        await _run(
            coordinator,
            [
                _delegation_call("create_html_page"),
                _delegation_call("save_to_memory"),
                _terminal_call(),
            ],
        )

        assert coordinator.handle_delegation.await_count == 2
        dispatched = {
            c.kwargs["intent"] for c in coordinator.handle_delegation.await_args_list
        }
        assert dispatched == {"create_html_page", "save_to_memory"}

    async def test_terminal_alone_dispatches_nothing(self, coordinator):
        """The normal Grok path: no work co-emitted, no delegation attempted."""
        result = await _run(coordinator, [_terminal_call()])

        coordinator.handle_delegation.assert_not_awaited()
        assert result.terminal_tool_args == {"full_response": "here is your answer"}

    async def test_duplicate_terminal_call_is_not_dispatched(self, coordinator):
        """A second deliver_response is not work — dispatching it would be an empty intent."""
        result = await _run(coordinator, [_terminal_call("first"), _terminal_call("second")])

        coordinator.handle_delegation.assert_not_awaited()
        assert result.terminal_tool_args == {"full_response": "first"}


class TestSiblingMetadataReachesResult:
    async def test_delivery_items_and_history_context_are_kept(self, coordinator):
        item = DeliveryItem(type="html_gcs_link", data={"link_text": "Ранковий вісник"})
        coordinator.handle_delegation = AsyncMock(
            return_value=AgentResponse.success(
                task_id="t1",
                agent_id="html_page_generator_agent",
                result="queued",
                delivery_items=[item],
                history_context={"web_search_context": "sources"},
                metadata={"structured_data": {"page": 1}},
            )
        )

        result = await _run(
            coordinator, [_delegation_call("create_html_page"), _terminal_call()],
        )

        assert result.delivery_items == [item]
        assert result.history_contexts == {"web_search_context": ["sources"]}
        assert result.structured_data == {"page": 1}


class TestSiblingFailureIsContained:
    async def test_failing_sibling_still_returns_the_answer(self, coordinator):
        """A dropped page is a bug; a swallowed answer would be a worse one."""
        coordinator.handle_delegation = AsyncMock(side_effect=RuntimeError("queue down"))

        result = await _run(
            coordinator, [_delegation_call("create_html_page"), _terminal_call()],
        )

        coordinator.handle_delegation.assert_awaited_once()
        assert result.terminal_tool_args == {"full_response": "here is your answer"}
        assert result.failed is False
