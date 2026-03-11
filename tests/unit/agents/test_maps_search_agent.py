"""
Unit tests for MapsSearchAgent (MCP-backed).

Single intent: maps_query
  payload: {"query": "<natural language task>"}

Tests cover:
  - can_handle routing
  - Prompt builder: build_for_agent called with correct args; fallback on exception
  - Tool loop: LLM requests tool → agent executes → LLM formats
  - Multi-tool calls in one turn
  - Tool execution error (MapsToolError) → error injected as result, loop continues
  - Direct answer (no tool_calls) → returned immediately
  - Max turns guard → forced format call
  - Empty query → failure
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.maps_search_agent import MapsSearchAgent, _MAX_TURNS
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.llm import ToolCall
from src.domain.user import PerformanceTier
from src.ports.llm_port import (
    AgentExecutionContext,
    LLMResponse,
    LLMPort,
    ProviderCapabilities,
)
from src.ports.maps_tools_port import MapsToolError, MapsToolsPort
from src.ports.prompt_builder_port import PromptBuilderPort


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TOOL_DECLS = [
    {
        "name": "places_search",
        "description": "Find places.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
    },
    {
        "name": "route_computation",
        "description": "Compute route.",
        "parameters": {
            "type": "object",
            "properties": {
                "origin": {"type": "string"},
                "destination": {"type": "string"},
            },
        },
    },
    {
        "name": "weather_lookup",
        "description": "Get weather.",
        "parameters": {
            "type": "object",
            "properties": {"location": {"type": "string"}},
        },
    },
]


def _make_execution_context(mock_llm) -> AgentExecutionContext:
    return AgentExecutionContext(
        agent_type="maps_search",
        provider=mock_llm,
        model_name="gemini-flash-latest",
        tier=PerformanceTier.BALANCED,
        capabilities=ProviderCapabilities(native_tools=True),
    )


def _make_message(
    payload: dict | None = None,
    intent: AgentIntent = AgentIntent.QUERY,
) -> AgentMessage:
    return AgentMessage(
        intent=intent,
        payload=payload if payload is not None else {"query": "знайди кафе поблизу"},
        sender="smart_response_agent",
        recipient="maps_search_agent",
        task_id="task_1",
        context={"user_id": "user123", "account_id": "acc1"},
    )


def _make_tool_call(name: str, args: dict) -> ToolCall:
    return ToolCall(name=name, args=args)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_llm() -> AsyncMock:
    m = AsyncMock(spec=LLMPort)
    m.get_capabilities.return_value = ProviderCapabilities(native_tools=True)
    return m


@pytest.fixture
def mock_maps_port() -> AsyncMock:
    m = AsyncMock(spec=MapsToolsPort)
    m.get_tool_declarations.return_value = _TOOL_DECLS
    m.call_tool.return_value = {"places": [{"name": "Кафе Центр", "address": "вул. Хрещатик 1"}]}
    return m


@pytest.fixture
def mock_prompt_builder() -> MagicMock:
    m = MagicMock(spec=PromptBuilderPort)
    m.build_for_agent = AsyncMock(return_value="MapsSearchAgent extends Agent { ... }")
    return m


@pytest.fixture
def agent(mock_llm, mock_maps_port, mock_prompt_builder) -> MapsSearchAgent:
    return MapsSearchAgent(
        config=AgentConfig(
            agent_id="maps_search_agent_user123",
            agent_type="maps_search",
            timeout_ms=90_000,
            capabilities=["location_search", "place_search", "routing", "weather"],
        ),
        execution_context=_make_execution_context(mock_llm),
        maps_port=mock_maps_port,
        prompt_builder=mock_prompt_builder,
        account_id="acc1",
        user_id="user123",
    )


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

def test_max_turns_value():
    assert _MAX_TURNS == 10


# ---------------------------------------------------------------------------
# can_handle
# ---------------------------------------------------------------------------

class TestCanHandle:
    async def test_query_intent_with_query(self, agent):
        msg = _make_message({"query": "кафе поблизу"})
        assert await agent.can_handle(msg) is True

    async def test_wrong_intent(self, agent):
        msg = _make_message(intent=AgentIntent.DELEGATE)
        assert await agent.can_handle(msg) is False

    async def test_empty_query(self, agent):
        msg = _make_message({"query": ""})
        assert await agent.can_handle(msg) is False

    async def test_missing_query_key(self, agent):
        msg = _make_message({})
        assert await agent.can_handle(msg) is False


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

class TestPromptBuilder:
    async def test_build_for_agent_called_with_correct_args(self, agent, mock_prompt_builder, mock_llm):
        """build_for_agent called with agent_type='maps_search', include_biographical=True."""
        mock_llm.generate_content.return_value = LLMResponse(text="Відповідь.", tool_calls=[])

        await agent.execute(_make_message())

        mock_prompt_builder.build_for_agent.assert_awaited_once_with(
            agent_type="maps_search",
            account_id="acc1",
            user_id="user123",
            include_biographical=True,
        )

    async def test_fallback_to_empty_on_prompt_build_failure(self, mock_llm, mock_maps_port, mock_prompt_builder):
        """When build_for_agent raises, system instruction falls back to empty string and execute still succeeds."""
        mock_prompt_builder.build_for_agent.side_effect = KeyError("blueprint not found")
        mock_llm.generate_content.return_value = LLMResponse(text="Відповідь.", tool_calls=[])

        a = MapsSearchAgent(
            config=AgentConfig(
                agent_id="maps_search_agent_test",
                agent_type="maps_search",
                timeout_ms=90_000,
                capabilities=[],
            ),
            execution_context=_make_execution_context(mock_llm),
            maps_port=mock_maps_port,
            prompt_builder=mock_prompt_builder,
            account_id="acc1",
        )

        response = await a.execute(_make_message())
        assert response.status == AgentStatus.SUCCESS

    async def test_system_instruction_and_tool_decls_fetched_per_execute(self, agent, mock_prompt_builder, mock_maps_port, mock_llm):
        """Both prompt build and tool declarations are fetched on each execute call."""
        mock_llm.generate_content.return_value = LLMResponse(text="OK", tool_calls=[])

        await agent.execute(_make_message())

        mock_prompt_builder.build_for_agent.assert_awaited_once()
        build_call = mock_prompt_builder.build_for_agent.await_args
        assert build_call.kwargs.get("agent_type") == "maps_search"
        assert build_call.kwargs.get("account_id") == "acc1"
        mock_maps_port.get_tool_declarations.assert_awaited_once()


# ---------------------------------------------------------------------------
# Tool loop — happy paths
# ---------------------------------------------------------------------------

class TestToolLoop:
    async def test_direct_answer_no_tool_calls(self, agent, mock_llm, mock_maps_port):
        """LLM answers directly without calling any tool."""
        mock_llm.generate_content.return_value = LLMResponse(
            text="Погода в Києві: +12°C, хмарно.",
            tool_calls=[],
        )

        response = await agent.execute(_make_message({"query": "яка погода в Києві?"}))

        assert response.status == AgentStatus.SUCCESS
        assert "погода" in response.result["text"].lower()
        mock_maps_port.call_tool.assert_not_called()
        assert mock_llm.generate_content.call_count == 1

    async def test_single_tool_call(self, agent, mock_llm, mock_maps_port):
        """LLM calls one tool, then formats the result."""
        mock_llm.generate_content.side_effect = [
            LLMResponse(
                text=None,
                tool_calls=[_make_tool_call("places_search", {"query": "кафе поблизу"})],
            ),
            LLMResponse(text="Знайдено: Кафе Центр на Хрещатику.", tool_calls=[]),
        ]

        response = await agent.execute(_make_message({"query": "знайди кафе поблизу"}))

        assert response.status == AgentStatus.SUCCESS
        assert "Кафе Центр" in response.result["text"]
        mock_maps_port.call_tool.assert_awaited_once_with(
            "places_search", {"query": "кафе поблизу"}
        )
        assert mock_llm.generate_content.call_count == 2

    async def test_route_tool_call(self, agent, mock_llm, mock_maps_port):
        """LLM calls route_computation tool."""
        mock_maps_port.call_tool.return_value = {
            "distance": "540 km",
            "duration": "5 hours 30 min",
        }
        mock_llm.generate_content.side_effect = [
            LLMResponse(
                text=None,
                tool_calls=[
                    _make_tool_call(
                        "route_computation",
                        {"origin": "Kyiv", "destination": "Lviv"},
                    )
                ],
            ),
            LLMResponse(text="Маршрут Київ–Львів: 540 км, ~5 год 30 хв.", tool_calls=[]),
        ]

        response = await agent.execute(
            _make_message({"query": "маршрут з Києва до Львова"})
        )

        assert response.status == AgentStatus.SUCCESS
        assert "540" in response.result["text"]
        mock_maps_port.call_tool.assert_awaited_once_with(
            "route_computation", {"origin": "Kyiv", "destination": "Lviv"}
        )

    async def test_multiple_tool_calls_in_one_turn(self, agent, mock_llm, mock_maps_port):
        """LLM requests two tools in the same turn."""
        mock_maps_port.call_tool.side_effect = [
            {"places": [{"name": "Аптека"}]},
            {"current": {"temp": "10°C", "condition": "cloudy"}},
        ]
        mock_llm.generate_content.side_effect = [
            LLMResponse(
                text=None,
                tool_calls=[
                    _make_tool_call("places_search", {"query": "аптека"}),
                    _make_tool_call("weather_lookup", {"location": "Kyiv"}),
                ],
            ),
            LLMResponse(text="Аптека поблизу: є. Погода: +10°C.", tool_calls=[]),
        ]

        response = await agent.execute(_make_message({"query": "аптека і погода"}))

        assert response.status == AgentStatus.SUCCESS
        assert mock_maps_port.call_tool.await_count == 2

    async def test_tool_declarations_fetched_once(self, agent, mock_llm, mock_maps_port):
        """get_tool_declarations called once per execute()."""
        mock_llm.generate_content.return_value = LLMResponse(
            text="Відповідь.", tool_calls=[]
        )

        await agent.execute(_make_message())

        mock_maps_port.get_tool_declarations.assert_awaited_once()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    async def test_tool_error_injected_as_result(self, agent, mock_llm, mock_maps_port):
        """MapsToolError → error dict injected in tool_response; loop continues."""
        mock_maps_port.call_tool.side_effect = MapsToolError("quota exceeded")
        mock_llm.generate_content.side_effect = [
            LLMResponse(
                text=None,
                tool_calls=[_make_tool_call("places_search", {"query": "кафе"})],
            ),
            LLMResponse(text="На жаль, не вдалось отримати дані карт.", tool_calls=[]),
        ]

        response = await agent.execute(_make_message())

        assert response.status == AgentStatus.SUCCESS
        assert mock_llm.generate_content.call_count == 2

    async def test_llm_exception_returns_failure(self, agent, mock_llm):
        """Unexpected LLM exception → AgentResponse.failure."""
        mock_llm.generate_content.side_effect = RuntimeError("LLM unavailable")

        response = await agent.execute(_make_message())

        assert response.status == AgentStatus.FAILED
        assert "Maps search failed" in response.error

    async def test_empty_query_returns_failure(self, agent):
        response = await agent.execute(_make_message({"query": ""}))
        assert response.status == AgentStatus.FAILED

    async def test_no_text_in_final_response_returns_partial(self, agent, mock_llm):
        """LLM returns empty text and no tool_calls → partial status."""
        mock_llm.generate_content.return_value = LLMResponse(text="", tool_calls=[])

        response = await agent.execute(_make_message())

        assert response.status == "partial"


# ---------------------------------------------------------------------------
# Max turns guard
# ---------------------------------------------------------------------------

class TestMaxTurns:
    async def test_max_turns_forces_format_call(self, agent, mock_llm, mock_maps_port):
        """After _MAX_TURNS tool-call turns, agent makes a final format call."""
        mock_llm.generate_content.side_effect = (
            [
                LLMResponse(
                    text=None,
                    tool_calls=[_make_tool_call("places_search", {"query": "x"})],
                )
            ]
            * _MAX_TURNS
            + [LLMResponse(text="Ось результати.", tool_calls=[])]
        )

        response = await agent.execute(_make_message())

        assert response.status == AgentStatus.SUCCESS
        assert mock_llm.generate_content.call_count == _MAX_TURNS + 1
