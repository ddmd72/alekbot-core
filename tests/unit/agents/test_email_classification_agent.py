"""
Unit tests for EmailClassificationAgent.
"""

import json
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from src.agents.email_classification_agent import EmailClassificationAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.email import EmailClassificationResult, EmailMetadata
from src.domain.user import PerformanceTier
from src.ports.llm_port import (
    AgentExecutionContext, LLMRequest, LLMResponse, LLMPort, ProviderCapabilities,
)
from src.ports.prompt_builder_port import PromptBuilderPort
from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience


def _make_execution_context(mock_llm, model_name: str = "gemini-test") -> AgentExecutionContext:
    return AgentExecutionContext(
        agent_type="email_classifier",
        provider=mock_llm,
        model_name=model_name,
        tier=PerformanceTier.BALANCED,
        capabilities=ProviderCapabilities(),
        resilience_port=InMemoryProviderResilience(),
    )


def _make_meta(email_id: str, subject: str = "Test", from_addr: str = "a@example.com") -> EmailMetadata:
    return EmailMetadata(
        email_id=email_id,
        provider="gmail",
        subject=subject,
        from_address=from_addr,
        date=datetime(2025, 3, 15),
        labels=[],
        snippet="Test snippet",
    )


def _llm_response(items: list) -> LLMResponse:
    """Build a final-turn LLMResponse (no tool_calls) with JSON text."""
    return LLMResponse(text=json.dumps(items), tool_calls=[])


@pytest.fixture
def mock_llm():
    return AsyncMock(spec=LLMPort)


@pytest.fixture
def mock_prompt_builder():
    pb = AsyncMock(spec=PromptBuilderPort)
    pb.build_for_agent.return_value = "EmailFactExtractor extends Agent { ... }"
    return pb


@pytest.fixture
def agent(mock_llm, mock_prompt_builder):
    config = AgentConfig(agent_id="test_email_classifier", agent_type="email_classifier")
    return EmailClassificationAgent(
        config=config,
        execution_context=_make_execution_context(mock_llm),
        prompt_builder=mock_prompt_builder,
    )


class TestEmailClassificationAgent:

    async def test_classify_batch_returns_all_emails(self, agent, mock_llm):
        """Every input email appears in the output with the gate's decision and reason."""
        emails = [_make_meta("id1"), _make_meta("id2")]
        mock_llm.generate_content.return_value = _llm_response([
            {"email_id": "id1", "decision": "keep", "reason": "record"},
            {"email_id": "id2", "decision": "drop", "reason": "promotion"},
        ])

        results = await agent.classify_batch(emails, "user123", account_id="acc-1")

        assert len(results) == 2
        assert results[0].email_id == "id1"
        assert results[0].keep is True
        assert results[0].reason == "record"
        assert results[0].forced is False
        assert results[1].email_id == "id2"
        assert results[1].keep is False
        assert results[1].reason == "promotion"

    async def test_classify_batch_fills_missing_emails(self, agent, mock_llm):
        """If the LLM omits an email_id, it is added as a forced keep (lean to keep, email index v2 §4)."""
        emails = [_make_meta("id1"), _make_meta("id2")]
        mock_llm.generate_content.return_value = _llm_response([
            {"email_id": "id1", "decision": "drop", "reason": "newsletter"},
        ])

        results = await agent.classify_batch(emails, "user123", account_id="acc-1")

        assert len(results) == 2
        missing = next(r for r in results if r.email_id == "id2")
        assert missing.keep is True
        assert missing.forced is True
        assert missing.reason is None
        assert missing.failure is None

    async def test_classify_batch_handles_invalid_json(self, agent, mock_llm):
        """Invalid JSON on all retries → parse_error after MAX_PARSE_RETRIES+1 attempts."""
        emails = [_make_meta("id1")]
        mock_llm.generate_content.return_value = LLMResponse(
            text="not json at all", tool_calls=[]
        )

        results = await agent.classify_batch(emails, "user123", account_id="acc-1")

        assert len(results) == 1
        assert results[0].keep is False
        assert results[0].failure == "parse_error"
        # One initial attempt + one retry (MAX_PARSE_RETRIES=1)
        assert mock_llm.generate_content.call_count == 2

    async def test_classify_batch_handles_llm_error(self, agent, mock_llm):
        """LLM exception → all emails returned failed and not kept."""
        emails = [_make_meta("id1"), _make_meta("id2")]
        mock_llm.generate_content.side_effect = RuntimeError("API error")

        results = await agent.classify_batch(emails, "user123", account_id="acc-1")

        assert len(results) == 2
        assert all(not r.keep for r in results)
        assert all(r.failure == "RuntimeError" for r in results)

    async def test_classify_batch_empty_input(self, agent, mock_llm):
        """Empty input → no LLM call, empty list returned."""
        results = await agent.classify_batch([], "user123", account_id="acc-1")

        assert results == []
        mock_llm.generate_content.assert_not_called()

    async def test_classify_batch_sends_correct_request(self, agent, mock_llm):
        """Verify LLMRequest fields: model, disable_safety, no tools (no gmail)."""
        emails = [_make_meta("id1")]
        mock_llm.generate_content.return_value = _llm_response([
            {"email_id": "id1", "valuable": False, "category": None,
             "fact": None, "tags": []},
        ])

        await agent.classify_batch(emails, "user123", account_id="acc-1")

        call_args = mock_llm.generate_content.call_args
        req: LLMRequest = call_args.kwargs.get("request") or call_args.args[0]
        assert req.model_name == "gemini-test"
        assert not hasattr(req, "temperature")
        # No gmail injected → single-pass mode → JSON mode active
        assert req.response_mime_type == "application/json"
        assert req.disable_safety is True
        assert req.system_instruction is not None  # prompt_builder builds it
        assert req.tools is None
        assert len(req.messages) == 1
        assert req.messages[0].role == "user"

    async def test_classify_batch_retries_on_invalid_json(self, agent, mock_llm):
        """Invalid JSON on first attempt → LLM retry → valid JSON on second attempt → success."""
        emails = [_make_meta("id1")]
        valid_response = _llm_response([
            {"email_id": "id1", "decision": "drop", "reason": "promotion"},
        ])
        mock_llm.generate_content.side_effect = [
            LLMResponse(text="not json at all", tool_calls=[]),
            valid_response,
        ]

        results = await agent.classify_batch(emails, "user123", account_id="acc-1")

        assert len(results) == 1
        assert results[0].keep is False
        assert results[0].reason == "promotion"
        assert results[0].failure is None
        assert mock_llm.generate_content.call_count == 2

    async def test_classify_batch_requires_prompt_builder(self, mock_llm):
        """Agent without prompt_builder raises RuntimeError on classify_batch."""
        config = AgentConfig(agent_id="test_email_classifier", agent_type="email_classifier")
        agent_no_pb = EmailClassificationAgent(
            config=config,
            execution_context=_make_execution_context(mock_llm),
        )
        emails = [_make_meta("id1")]

        with pytest.raises(RuntimeError, match="prompt_builder"):
            await agent_no_pb.classify_batch(emails, "user123", account_id="acc-1")

    async def test_can_handle_always_false(self, agent):
        """can_handle() always returns False — agent is not in the delegation chain."""
        msg = AgentMessage(
            intent=AgentIntent.QUERY,
            payload={"text": "classify"},
            sender="router",
            recipient="email_classifier",
            task_id="t1",
            context={},
        )
        assert await agent.can_handle(msg) is False

    async def test_execute_returns_failure(self, agent):
        """execute() returns a failure AgentResponse — not in the delegation chain."""
        msg = AgentMessage(
            intent=AgentIntent.QUERY,
            payload={"text": "classify"},
            sender="router",
            recipient="email_classifier",
            task_id="t1",
            context={},
        )
        response = await agent.execute(msg)
        assert response.status == AgentStatus.FAILED
