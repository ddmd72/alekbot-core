from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.lelik_summarizer_agent import LelikSummarizerAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentIntent, AgentStatus
from src.domain.companion_extraction import EXTRACTION_TASK
from src.ports.llm_port import LLMPort
from src.ports.prompt_builder_port import PromptBuilderPort

_SUMMARY_TEXT = "Discussed the Q3 budget with Ivan."


@pytest.fixture
def mock_llm():
    llm = AsyncMock(spec=LLMPort)
    llm.generate_content.return_value = MagicMock(
        text=_SUMMARY_TEXT,
        usage_metadata=MagicMock(total_tokens=42),
    )
    return llm


@pytest.fixture
def mock_prompt_builder():
    pb = AsyncMock(spec=PromptBuilderPort)
    pb.build_for_agent.return_value = "You are Lelik's end-of-call summarizer."
    return pb


@pytest.fixture
def agent(mock_llm, mock_prompt_builder):
    config = AgentConfig(agent_id="lelik_summarizer_test", agent_type="lelik_summarizer",
                         timeout_ms=60000, capabilities=["lelik_summarizer"])
    ctx = MagicMock(provider=mock_llm, model_name="claude-sonnet-5")
    return LelikSummarizerAgent(config=config, execution_context=ctx, prompt_builder=mock_prompt_builder)


def _message(messages=None, task=EXTRACTION_TASK):
    return AgentMessage(
        task_id="t1", sender="test", recipient="lelik_summarizer_test",
        intent=AgentIntent.DELEGATE,
        payload={"task": task, "messages": messages if messages is not None else [
            {"request_text": "let's talk budget", "response_text": "sure, Q3 first"},
        ]},
        context={"account_id": "acc-1"},
    )


async def test_can_handle_correct_task():
    config = AgentConfig(agent_id="a", agent_type="lelik_summarizer", timeout_ms=1000, capabilities=[])
    agent = LelikSummarizerAgent(config=config, execution_context=MagicMock(), prompt_builder=AsyncMock())
    assert await agent.can_handle(_message()) is True


async def test_can_handle_wrong_task():
    config = AgentConfig(agent_id="a", agent_type="lelik_summarizer", timeout_ms=1000, capabilities=[])
    agent = LelikSummarizerAgent(config=config, execution_context=MagicMock(), prompt_builder=AsyncMock())
    assert await agent.can_handle(_message(task="something_else")) is False


async def test_can_handle_empty_messages():
    config = AgentConfig(agent_id="a", agent_type="lelik_summarizer", timeout_ms=1000, capabilities=[])
    agent = LelikSummarizerAgent(config=config, execution_context=MagicMock(), prompt_builder=AsyncMock())
    assert await agent.can_handle(_message(messages=[])) is False


async def test_execute_returns_summary_and_discards_records(agent, mock_llm):
    response = await agent.execute(_message())

    assert response.status == AgentStatus.SUCCESS
    assert response.result["records"] == []
    assert "Q3 budget" in response.result["summary"]
    mock_llm.generate_content.assert_called_once()


async def test_execute_llm_exception(agent, mock_llm):
    mock_llm.generate_content.side_effect = Exception("LLM error")
    response = await agent.execute(_message())
    assert response.status == AgentStatus.FAILED


async def test_execute_prompt_builder_failure(mock_llm):
    pb = AsyncMock(spec=PromptBuilderPort)
    pb.build_for_agent.side_effect = Exception("prompt missing")
    config = AgentConfig(agent_id="a", agent_type="lelik_summarizer", timeout_ms=1000, capabilities=[])
    ctx = MagicMock(provider=mock_llm, model_name="claude-sonnet-5")
    agent = LelikSummarizerAgent(config=config, execution_context=ctx, prompt_builder=pb)
    response = await agent.execute(_message())
    assert response.status == AgentStatus.FAILED
    assert "PromptBuilder failed" in response.error


@pytest.mark.asyncio
async def test_prompt_is_built_for_the_callers_identity_so_language_overrides_apply(agent, mock_prompt_builder):
    """The summary lands in the user's chat, so it follows their LANG_* override - which
    only loads when build_for_agent knows who the user is."""
    message = _message()
    message.context["user_id"] = "user-1"

    await agent.execute(message)

    kwargs = mock_prompt_builder.build_for_agent.await_args.kwargs
    assert kwargs["user_id"] == "user-1"
    assert kwargs["account_id"] == "acc-1"
