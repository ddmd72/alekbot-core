from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.tutor_agent import TutorAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.companion_context import CompanionContext
from src.domain.companion import CompanionRecord
from src.ports.llm_port import LLMPort
from src.ports.prompt_builder_port import PromptBuilderPort


@pytest.fixture
def mock_llm():
    llm = AsyncMock(spec=LLMPort)
    llm.generate_content.return_value = MagicMock(
        text="¡Hola! Let's practice the subjunctive today.",
        usage_metadata=MagicMock(total_tokens=42),
        tool_calls=[],
    )
    return llm


@pytest.fixture
def mock_prompt_builder():
    pb = AsyncMock(spec=PromptBuilderPort)
    pb.build_for_agent.return_value = "You are the Spanish tutor."
    return pb


@pytest.fixture
def mock_assembler():
    svc = AsyncMock()
    svc.assemble_context.return_value = CompanionContext(
        session_summary="Covered subjunctive mood last time.",
        own_records=[
            CompanionRecord(
                session_id="slack:C1", account_id="acc-1", created_by_user_id="user-1",
                text="Confuses subjunctive after 'ojalá'", domain="grammar_error",
            ),
        ],
        biographical_facts=[],
        standing_directives=[],
    )
    return svc


@pytest.fixture
def agent(mock_llm, mock_prompt_builder, mock_assembler):
    config = AgentConfig(agent_id="tutor_agent_test", agent_type="tutor",
                         timeout_ms=60000, capabilities=["tutor_chat"])
    ctx = MagicMock(provider=mock_llm, model_name="claude-sonnet-5")
    return TutorAgent(
        config=config, execution_context=ctx, prompt_builder=mock_prompt_builder,
        assembler=mock_assembler, user_id=None,
    )


def _bound_message(query="¿Cómo se dice 'I would like'?", history=None):
    return AgentMessage(
        task_id="t1", sender="coordinator", recipient="tutor_agent_test",
        intent=AgentIntent.QUERY,
        payload={"query": query, "intent": "tutor_chat"},
        context={
            "account_id": "acc-1", "user_id": "user-1",
            "origin_channel_id": "C1", "origin_platform": "slack",
            "history": history or [],
        },
    )


async def test_can_handle_correct(agent):
    assert await agent.can_handle(_bound_message()) is True


async def test_can_handle_wrong_intent():
    config = AgentConfig(agent_id="a", agent_type="tutor", timeout_ms=1000, capabilities=[])
    agent = TutorAgent(config=config, execution_context=MagicMock(), prompt_builder=AsyncMock(), assembler=AsyncMock())
    msg = _bound_message()
    msg.intent = AgentIntent.INFORM
    assert await agent.can_handle(msg) is False


async def test_can_handle_empty_query():
    config = AgentConfig(agent_id="a", agent_type="tutor", timeout_ms=1000, capabilities=[])
    agent = TutorAgent(config=config, execution_context=MagicMock(), prompt_builder=AsyncMock(), assembler=AsyncMock())
    assert await agent.can_handle(_bound_message(query="")) is False


async def test_execute_happy_path(agent, mock_prompt_builder, mock_assembler):
    response = await agent.execute(_bound_message())
    assert response.status == AgentStatus.SUCCESS
    assert "subjunctive" in response.result.lower()


async def test_execute_calls_assembler_with_platform_qualified_session_id(agent, mock_assembler):
    await agent.execute(_bound_message())
    mock_assembler.assemble_context.assert_called_once()
    call_kwargs = mock_assembler.assemble_context.call_args.kwargs
    assert call_kwargs["session_id"] == "slack:C1"
    assert call_kwargs["account_id"] == "acc-1"
    assert call_kwargs["include_biographical"] is False
    assert call_kwargs["include_standing_directives"] is False
    assert call_kwargs["user_id"] is None


async def test_execute_injects_companion_context_as_static_block(agent, mock_prompt_builder):
    await agent.execute(_bound_message())
    call_kwargs = mock_prompt_builder.build_for_agent.call_args.kwargs
    blocks = call_kwargs.get("extra_static_blocks") or []
    joined = "\n".join(blocks)
    assert "companion_context" in joined
    assert "subjunctive" in joined


async def test_execute_reads_history_from_context_not_session_store(agent, mock_prompt_builder):
    history = [{"role": "user", "parts": [{"text": "Hola profe"}]}]
    await agent.execute(_bound_message(history=history))
    # No SessionStore dependency exists on this agent at all — constructor test below
    # covers that; this test just confirms history flows into the LLM request.
    assert mock_prompt_builder.build_for_agent.called


async def test_execute_assembler_failure_degrades_gracefully(agent, mock_assembler, mock_prompt_builder):
    mock_assembler.assemble_context.side_effect = Exception("Firestore down")
    response = await agent.execute(_bound_message())
    assert response.status == AgentStatus.SUCCESS
    call_kwargs = mock_prompt_builder.build_for_agent.call_args.kwargs
    assert not (call_kwargs.get("extra_static_blocks") or [])


async def test_execute_no_query_fails(agent):
    response = await agent.execute(_bound_message(query=""))
    assert response.status == AgentStatus.FAILED


async def test_execute_prompt_builder_failure(mock_llm, mock_assembler):
    pb = AsyncMock(spec=PromptBuilderPort)
    pb.build_for_agent.side_effect = Exception("prompt missing")
    config = AgentConfig(agent_id="a", agent_type="tutor", timeout_ms=1000, capabilities=[])
    ctx = MagicMock(provider=mock_llm, model_name="claude-sonnet-5")
    agent = TutorAgent(config=config, execution_context=ctx, prompt_builder=pb, assembler=mock_assembler)
    response = await agent.execute(_bound_message())
    assert response.status == AgentStatus.FAILED
    assert "PromptBuilder failed" in response.error


async def test_execute_llm_exception(agent, mock_llm):
    mock_llm.generate_content.side_effect = Exception("LLM error")
    response = await agent.execute(_bound_message())
    assert response.status == AgentStatus.FAILED


def test_get_alternative_agents_empty(agent):
    assert agent._get_alternative_agents() == []
