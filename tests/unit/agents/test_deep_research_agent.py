"""
Unit tests for DeepResearchAgent.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.deep_research_agent import DeepResearchAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.user import PerformanceTier
from src.ports.deep_research_port import DeepResearchPort
from src.ports.prompt_builder_port import PromptBuilderPort


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config() -> AgentConfig:
    return AgentConfig(
        agent_id="deep_research_agent_test",
        agent_type="deep_research",
        timeout_ms=60_000,
        capabilities=["deep_research"],
    )


def _make_message(
    query: str = "What is the current state of quantum computing?",
    intent: AgentIntent = AgentIntent.QUERY,
    language: str = "English",
) -> AgentMessage:
    return AgentMessage(
        task_id="task_dr_1",
        sender="smart_agent",
        recipient="deep_research_agent",
        intent=intent,
        payload={"query": query, "brief": query, "language": language},
        context={"user_id": "u1", "account_id": "a1"},
    )


def _make_agent(
    job_port: DeepResearchPort,
    tier: PerformanceTier = PerformanceTier.BALANCED,
    prompt_builder=None,
    user_id: str = "u1",
) -> DeepResearchAgent:
    return DeepResearchAgent(
        config=_make_config(),
        job_port=job_port,
        tier=tier,
        prompt_builder=prompt_builder,
        user_id=user_id,
    )


# ---------------------------------------------------------------------------
# can_handle
# ---------------------------------------------------------------------------

async def test_can_handle_correct_intent(mock_job_port):
    agent = _make_agent(mock_job_port)
    assert await agent.can_handle(_make_message()) is True


async def test_can_handle_wrong_intent(mock_job_port):
    agent = _make_agent(mock_job_port)
    msg = _make_message()
    msg.intent = AgentIntent.INFORM
    assert await agent.can_handle(msg) is False


async def test_can_handle_empty_query(mock_job_port):
    agent = _make_agent(mock_job_port)
    assert await agent.can_handle(_make_message(query="")) is False


# ---------------------------------------------------------------------------
# execute — happy path
# ---------------------------------------------------------------------------

async def test_execute_returns_started(mock_job_port):
    mock_job_port.create_interaction.return_value = "job_abc123"
    agent = _make_agent(mock_job_port)
    response = await agent.execute(_make_message())

    assert response.status == AgentStatus.SUCCESS
    assert response.result["status"] == "started"
    assert response.result["interaction_id"] == "job_abc123"


async def test_execute_passes_tier_to_port(mock_job_port):
    mock_job_port.create_interaction.return_value = "job_xyz"
    agent = _make_agent(mock_job_port, tier=PerformanceTier.PERFORMANCE)
    await agent.execute(_make_message())

    call_kwargs = mock_job_port.create_interaction.call_args.kwargs
    assert call_kwargs["tier"] == PerformanceTier.PERFORMANCE


# ---------------------------------------------------------------------------
# execute — prompt builder
# ---------------------------------------------------------------------------

async def test_execute_passes_system_prompt_from_builder(mock_job_port):
    prompt_builder = AsyncMock(spec=PromptBuilderPort)
    prompt_builder.build_for_agent.return_value = "You are a research assistant."
    mock_job_port.create_interaction.return_value = "job_pb1"

    agent = _make_agent(mock_job_port, prompt_builder=prompt_builder)
    await agent.execute(_make_message())

    call_kwargs = mock_job_port.create_interaction.call_args.kwargs
    assert call_kwargs["system_prompt"] == "You are a research assistant."
    prompt_builder.build_for_agent.assert_called_once_with("deep_research", "u1")


async def test_execute_no_prompt_builder_passes_none(mock_job_port):
    mock_job_port.create_interaction.return_value = "job_nopb"
    agent = _make_agent(mock_job_port, prompt_builder=None)
    await agent.execute(_make_message())

    call_kwargs = mock_job_port.create_interaction.call_args.kwargs
    assert call_kwargs["system_prompt"] is None


async def test_execute_prompt_builder_failure_continues(mock_job_port):
    """PromptBuilder failure must not block job submission."""
    prompt_builder = AsyncMock(spec=PromptBuilderPort)
    prompt_builder.build_for_agent.side_effect = RuntimeError("Firestore unavailable")
    mock_job_port.create_interaction.return_value = "job_fallback"

    agent = _make_agent(mock_job_port, prompt_builder=prompt_builder)
    response = await agent.execute(_make_message())

    assert response.status == AgentStatus.SUCCESS
    mock_job_port.create_interaction.assert_called_once()
    assert mock_job_port.create_interaction.call_args.kwargs["system_prompt"] is None


# ---------------------------------------------------------------------------
# execute — error handling
# ---------------------------------------------------------------------------

async def test_execute_port_exception_returns_failure(mock_job_port):
    mock_job_port.create_interaction.side_effect = RuntimeError("API down")
    agent = _make_agent(mock_job_port)
    response = await agent.execute(_make_message())

    assert response.status == AgentStatus.FAILED
    assert "API down" in response.error


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_job_port():
    port = AsyncMock(spec=DeepResearchPort)
    port.create_interaction.return_value = "job_default"
    return port
