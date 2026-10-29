"""EmailClassificationAgent: a failed page is marked, a page of noise is not."""

import json
from unittest.mock import AsyncMock

import pytest

from src.agents.email_classification_agent import MAX_TURNS, EmailClassificationAgent
from src.domain.agent import AgentConfig
from src.ports.email_provider_port import EmailProviderPort
from src.ports.llm_port import LLMPort, LLMResponse, ToolCall
from src.ports.prompt_builder_port import PromptBuilderPort
from tests.unit.agents.test_email_classification_agent import (
    _make_execution_context,
    _make_meta,
)


@pytest.fixture
def mock_llm():
    return AsyncMock(spec=LLMPort)


@pytest.fixture
def agent(mock_llm):
    pb = AsyncMock(spec=PromptBuilderPort)
    pb.build_for_agent.return_value = "prompt"
    return EmailClassificationAgent(
        config=AgentConfig(agent_id="t", agent_type="email_classifier"),
        execution_context=_make_execution_context(mock_llm),
        prompt_builder=pb,
        gmail=AsyncMock(spec=EmailProviderPort),
    )


async def test_all_noise_page_carries_no_failure(agent, mock_llm):
    emails = [_make_meta("e1"), _make_meta("e2")]
    mock_llm.generate_content.return_value = LLMResponse(
        text=json.dumps([
            {"email_id": e.email_id, "valuable": False, "category": None, "fact": None, "tags": []}
            for e in emails
        ]),
        tool_calls=[],
    )

    results = await agent.classify_batch(emails, "user123")

    assert [r.failure for r in results] == [None, None]


async def test_parse_error_is_marked(agent, mock_llm):
    mock_llm.generate_content.return_value = LLMResponse(text="not json", tool_calls=[])

    results = await agent.classify_batch([_make_meta("e1"), _make_meta("e2")], "user123")

    assert [r.failure for r in results] == ["parse_error", "parse_error"]
    assert not any(r.valuable for r in results)


async def test_max_turns_is_marked(agent, mock_llm):
    mock_llm.generate_content.return_value = LLMResponse(
        tool_calls=[ToolCall(name="unknown_tool", args={})]
    )

    results = await agent.classify_batch([_make_meta("e1")], "user123")

    assert [r.failure for r in results] == ["max_turns"]
    assert mock_llm.generate_content.call_count == MAX_TURNS


async def test_unexpected_exception_is_marked_with_its_class(agent, mock_llm):
    mock_llm.generate_content.side_effect = RuntimeError("boom")

    results = await agent.classify_batch([_make_meta("e1")], "user123")

    assert [r.failure for r in results] == ["RuntimeError"]
