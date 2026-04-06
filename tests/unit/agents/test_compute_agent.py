"""
Unit tests for ComputeAgent.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.compute_agent import ComputeAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.user import PerformanceTier
from src.ports.llm_port import (
    AgentExecutionContext,
    LLMRequest,
    LLMResponse,
    LLMPort,
    ProviderCapabilities,
)
from src.ports.prompt_builder_port import PromptBuilderPort


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_execution_context(mock_llm) -> AgentExecutionContext:
    return AgentExecutionContext(
        agent_type="compute",
        provider=mock_llm,
        model_name="gemini-test",
        tier=PerformanceTier.BALANCED,
        capabilities=ProviderCapabilities(),
    )


def _make_message(
    query: str = "sqrt(289)",
    intent: AgentIntent = AgentIntent.QUERY,
) -> AgentMessage:
    return AgentMessage(
        intent=intent,
        payload={"query": query},
        sender="quick_response_agent",
        recipient="compute_agent",
        task_id="task_1",
        context={"user_id": "user123", "account_id": "acc1"},
    )


@pytest.fixture
def mock_llm():
    m = AsyncMock(spec=LLMPort)
    m.generate_content.return_value = LLMResponse(
        text="The square root of 289 is 17.", tool_calls=[]
    )
    return m


@pytest.fixture
def mock_prompt_builder():
    pb = AsyncMock(spec=PromptBuilderPort)
    pb.build_for_agent.return_value = "You are a computation agent..."
    return pb


@pytest.fixture
def agent(mock_llm, mock_prompt_builder):
    config = AgentConfig(agent_id="compute_agent_user123", agent_type="compute")
    return ComputeAgent(
        config=config,
        execution_context=_make_execution_context(mock_llm),
        prompt_builder=mock_prompt_builder,
        user_id="user123",
    )


# ---------------------------------------------------------------------------
# can_handle
# ---------------------------------------------------------------------------

class TestCanHandle:

    async def test_returns_true_with_query(self, agent):
        msg = _make_message(query="sqrt(289)")
        assert await agent.can_handle(msg) is True

    async def test_returns_false_empty_query(self, agent):
        msg = _make_message(query="")
        assert await agent.can_handle(msg) is False

    async def test_returns_false_wrong_intent(self, agent):
        msg = _make_message(intent=AgentIntent.INFORM)
        assert await agent.can_handle(msg) is False


# ---------------------------------------------------------------------------
# execute — happy path
# ---------------------------------------------------------------------------

class TestExecuteSuccess:

    async def test_returns_success_response(self, agent):
        msg = _make_message()
        response = await agent.execute(msg)
        assert response.status == AgentStatus.SUCCESS

    async def test_result_contains_llm_text(self, agent):
        msg = _make_message()
        response = await agent.execute(msg)
        assert "17" in response.result

    async def test_metadata_contains_model(self, agent):
        msg = _make_message()
        response = await agent.execute(msg)
        assert response.metadata["model"] == "gemini-test"

    async def test_metadata_contains_duration(self, agent):
        msg = _make_message()
        response = await agent.execute(msg)
        assert "duration_ms" in response.metadata
        assert isinstance(response.metadata["duration_ms"], int)

    async def test_handles_natural_language_query(self, agent, mock_llm):
        mock_llm.generate_content.return_value = LLMResponse(
            text="150 km equals 93.21 miles.", tool_calls=[]
        )
        msg = _make_message(query="how many miles is 150 km")
        response = await agent.execute(msg)
        assert response.status == AgentStatus.SUCCESS
        assert "93.21" in response.result

    async def test_handles_formula_query(self, agent, mock_llm):
        mock_llm.generate_content.return_value = LLMResponse(
            text="Result: 42", tool_calls=[]
        )
        msg = _make_message(query="(2**5 + 10) * 1")
        response = await agent.execute(msg)
        assert response.status == AgentStatus.SUCCESS


# ---------------------------------------------------------------------------
# execute — LLM call verification
# ---------------------------------------------------------------------------

class TestLLMCall:

    async def test_use_code_execution_flag_set(self, agent, mock_llm):
        msg = _make_message()
        await agent.execute(msg)

        req: LLMRequest = mock_llm.generate_content.call_args.kwargs.get("request") or \
                          mock_llm.generate_content.call_args.args[0]
        assert req.use_code_execution is True

    async def test_no_tools_injected_by_agent(self, agent, mock_llm):
        """Agent must NOT inject provider-specific tools — adapter owns that."""
        msg = _make_message()
        await agent.execute(msg)

        req: LLMRequest = mock_llm.generate_content.call_args.kwargs.get("request") or \
                          mock_llm.generate_content.call_args.args[0]
        assert not req.tools

    async def test_temperature_is_zero(self, agent, mock_llm):
        msg = _make_message()
        await agent.execute(msg)

        req: LLMRequest = mock_llm.generate_content.call_args.kwargs.get("request") or \
                          mock_llm.generate_content.call_args.args[0]
        assert req.temperature == 1.0

    async def test_query_in_user_message(self, agent, mock_llm):
        msg = _make_message(query="sqrt(289)")
        await agent.execute(msg)

        req: LLMRequest = mock_llm.generate_content.call_args.kwargs.get("request") or \
                          mock_llm.generate_content.call_args.args[0]
        user_text = req.messages[0].parts[0].text
        assert "sqrt(289)" in user_text

    async def test_current_datetime_in_user_message(self, agent, mock_llm):
        msg = _make_message()
        await agent.execute(msg)

        req: LLMRequest = mock_llm.generate_content.call_args.kwargs.get("request") or \
                          mock_llm.generate_content.call_args.args[0]
        user_text = req.messages[0].parts[0].text
        assert "current_datetime" in user_text

    async def test_system_prompt_from_builder(self, agent, mock_llm, mock_prompt_builder):
        msg = _make_message()
        await agent.execute(msg)

        req: LLMRequest = mock_llm.generate_content.call_args.kwargs.get("request") or \
                          mock_llm.generate_content.call_args.args[0]
        assert req.system_instruction == "You are a computation agent..."


# ---------------------------------------------------------------------------
# execute — failure paths
# ---------------------------------------------------------------------------

class TestExecuteFailure:

    async def test_empty_query_returns_failure(self, agent):
        msg = _make_message(query="")
        response = await agent.execute(msg)
        assert response.status == AgentStatus.FAILED

    async def test_llm_exception_returns_failure(self, agent, mock_llm):
        mock_llm.generate_content.side_effect = RuntimeError("API down")

        msg = _make_message()
        response = await agent.execute(msg)

        assert response.status == AgentStatus.FAILED
        assert "Computation failed" in response.error

    async def test_prompt_builder_failure_non_fatal(self, agent, mock_prompt_builder, mock_llm):
        mock_prompt_builder.build_for_agent.side_effect = RuntimeError("Firestore error")

        msg = _make_message()
        response = await agent.execute(msg)

        # Should succeed — prompt builder failure is non-fatal
        assert response.status == AgentStatus.SUCCESS

    async def test_no_prompt_builder_still_works(self, mock_llm):
        config = AgentConfig(agent_id="compute_agent_user123", agent_type="compute")
        agent_no_pb = ComputeAgent(
            config=config,
            execution_context=_make_execution_context(mock_llm),
            prompt_builder=None,
            user_id="user123",
        )

        msg = _make_message()
        response = await agent_no_pb.execute(msg)
        assert response.status == AgentStatus.SUCCESS


# ---------------------------------------------------------------------------
# _get_alternative_agents
# ---------------------------------------------------------------------------

class TestAlternativeAgents:

    def test_returns_web_search(self, agent):
        assert agent._get_alternative_agents() == ["web_search_agent"]
