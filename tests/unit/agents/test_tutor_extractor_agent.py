import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.tutor_extractor_agent import TutorExtractorAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentIntent, AgentStatus
from src.domain.companion_extraction import EXTRACTION_TASK
from src.ports.llm_port import LLMPort
from src.ports.prompt_builder_port import PromptBuilderPort

_VALID_JSON = json.dumps({
    "records": [{"text": "Confuses subjunctive after 'ojalá'", "domain": "grammar_error", "tags": ["subjunctive"]}],
    "summary": "Session covered subjunctive mood; one recurring error.",
})


@pytest.fixture
def mock_llm():
    llm = AsyncMock(spec=LLMPort)
    llm.generate_content.return_value = MagicMock(
        text=_VALID_JSON,
        usage_metadata=MagicMock(total_tokens=123),
    )
    return llm


@pytest.fixture
def mock_prompt_builder():
    pb = AsyncMock(spec=PromptBuilderPort)
    pb.build_for_agent.return_value = "You are the tutor extractor."
    return pb


@pytest.fixture
def agent(mock_llm, mock_prompt_builder):
    config = AgentConfig(agent_id="tutor_extractor_test", agent_type="tutor_extractor",
                         timeout_ms=300000, capabilities=["tutor_extractor"])
    ctx = MagicMock(provider=mock_llm, model_name="claude-sonnet-5")
    return TutorExtractorAgent(config=config, execution_context=ctx, prompt_builder=mock_prompt_builder)


def _message(messages=None, task=EXTRACTION_TASK):
    return AgentMessage(
        task_id="t1", sender="test", recipient="tutor_extractor_test",
        intent=AgentIntent.DELEGATE,
        payload={"task": task, "messages": messages if messages is not None else [{"role": "user", "parts": [{"text": "hola"}]}]},
        context={"account_id": "acc-1"},
    )


async def test_can_handle_correct_task():
    config = AgentConfig(agent_id="a", agent_type="tutor_extractor", timeout_ms=1000, capabilities=[])
    agent = TutorExtractorAgent(config=config, execution_context=MagicMock(), prompt_builder=AsyncMock())
    assert await agent.can_handle(_message()) is True


async def test_can_handle_wrong_task():
    config = AgentConfig(agent_id="a", agent_type="tutor_extractor", timeout_ms=1000, capabilities=[])
    agent = TutorExtractorAgent(config=config, execution_context=MagicMock(), prompt_builder=AsyncMock())
    assert await agent.can_handle(_message(task="something_else")) is False


async def test_can_handle_empty_messages():
    config = AgentConfig(agent_id="a", agent_type="tutor_extractor", timeout_ms=1000, capabilities=[])
    agent = TutorExtractorAgent(config=config, execution_context=MagicMock(), prompt_builder=AsyncMock())
    assert await agent.can_handle(_message(messages=[])) is False


async def test_execute_happy_path(agent, mock_llm):
    response = await agent.execute(_message())
    assert response.status == AgentStatus.SUCCESS
    assert response.result["summary"].startswith("Session covered")
    assert response.result["records"][0]["domain"] == "grammar_error"
    mock_llm.generate_content.assert_called_once()


async def test_execute_invalid_json_fails(agent, mock_llm):
    mock_llm.generate_content.return_value = MagicMock(text="not json", usage_metadata=MagicMock(total_tokens=1))
    response = await agent.execute(_message())
    assert response.status == AgentStatus.FAILED
    assert "Invalid extraction output" in response.error


async def test_execute_missing_keys_fails(agent, mock_llm):
    mock_llm.generate_content.return_value = MagicMock(text=json.dumps({"records": []}), usage_metadata=MagicMock(total_tokens=1))
    response = await agent.execute(_message())
    assert response.status == AgentStatus.FAILED


async def test_execute_llm_exception(agent, mock_llm):
    mock_llm.generate_content.side_effect = Exception("LLM error")
    response = await agent.execute(_message())
    assert response.status == AgentStatus.FAILED


async def test_execute_prompt_builder_failure(mock_llm):
    pb = AsyncMock(spec=PromptBuilderPort)
    pb.build_for_agent.side_effect = Exception("prompt missing")
    config = AgentConfig(agent_id="a", agent_type="tutor_extractor", timeout_ms=1000, capabilities=[])
    ctx = MagicMock(provider=mock_llm, model_name="claude-sonnet-5")
    agent = TutorExtractorAgent(config=config, execution_context=ctx, prompt_builder=pb)
    response = await agent.execute(_message())
    assert response.status == AgentStatus.FAILED
    assert "PromptBuilder failed" in response.error
