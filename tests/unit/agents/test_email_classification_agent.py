"""
Unit tests for EmailClassificationAgent.
"""

import json
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from src.agents.email_classification_agent import EmailClassificationAgent, MAX_TURNS
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.email import EmailClassificationResult, EmailFullContent, EmailMetadata, OAuthCredentials
from src.domain.user import PerformanceTier
from src.ports.email_provider_port import EmailProviderPort
from src.ports.llm_port import (
    AgentExecutionContext, LLMRequest, LLMResponse, LLMPort, ProviderCapabilities, ToolCall,
)
from src.ports.prompt_builder_port import PromptBuilderPort


def _make_execution_context(mock_llm, model_name: str = "gemini-test") -> AgentExecutionContext:
    return AgentExecutionContext(
        agent_type="email_classifier",
        provider=mock_llm,
        model_name=model_name,
        tier=PerformanceTier.BALANCED,
        capabilities=ProviderCapabilities(),
    )


def _make_meta(email_id: str, subject: str = "Test", from_addr: str = "a@b.com") -> EmailMetadata:
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
        """All input emails appear in output, even if not all returned by LLM."""
        emails = [_make_meta("id1"), _make_meta("id2")]
        mock_llm.generate_content.return_value = _llm_response([
            {"email_id": "id1", "valuable": True, "category": "travel",
             "fact": "User booked flight", "tags": ["flight"]},
            {"email_id": "id2", "valuable": False, "category": None,
             "fact": None, "tags": []},
        ])

        results = await agent.classify_batch(emails, "user123")

        assert len(results) == 2
        assert results[0].email_id == "id1"
        assert results[0].valuable is True
        assert results[0].category == "travel"
        assert results[0].fact == "User booked flight"
        assert results[1].email_id == "id2"
        assert results[1].valuable is False

    async def test_classify_batch_fills_missing_emails(self, agent, mock_llm):
        """If LLM omits an email_id, it is added as not-valuable."""
        emails = [_make_meta("id1"), _make_meta("id2")]
        mock_llm.generate_content.return_value = _llm_response([
            {"email_id": "id1", "valuable": True, "category": "finance",
             "fact": "Invoice paid", "tags": ["invoice"]},
        ])

        results = await agent.classify_batch(emails, "user123")

        assert len(results) == 2
        missing = next(r for r in results if r.email_id == "id2")
        assert missing.valuable is False

    async def test_classify_batch_handles_invalid_json(self, agent, mock_llm):
        """Invalid JSON on all retries → parse_error after MAX_PARSE_RETRIES+1 attempts."""
        emails = [_make_meta("id1")]
        mock_llm.generate_content.return_value = LLMResponse(
            text="not json at all", tool_calls=[]
        )

        results = await agent.classify_batch(emails, "user123")

        assert len(results) == 1
        assert results[0].valuable is False
        # One initial attempt + one retry (MAX_PARSE_RETRIES=1)
        assert mock_llm.generate_content.call_count == 2

    async def test_classify_batch_handles_llm_error(self, agent, mock_llm):
        """LLM exception → all emails returned as not-valuable."""
        emails = [_make_meta("id1"), _make_meta("id2")]
        mock_llm.generate_content.side_effect = RuntimeError("API error")

        results = await agent.classify_batch(emails, "user123")

        assert len(results) == 2
        assert all(not r.valuable for r in results)

    async def test_classify_batch_empty_input(self, agent, mock_llm):
        """Empty input → no LLM call, empty list returned."""
        results = await agent.classify_batch([], "user123")

        assert results == []
        mock_llm.generate_content.assert_not_called()

    async def test_classify_batch_sends_correct_request(self, agent, mock_llm):
        """Verify LLMRequest fields: model, temperature=0, disable_safety, no tools (no gmail)."""
        emails = [_make_meta("id1")]
        mock_llm.generate_content.return_value = _llm_response([
            {"email_id": "id1", "valuable": False, "category": None,
             "fact": None, "tags": []},
        ])

        await agent.classify_batch(emails, "user123")

        call_args = mock_llm.generate_content.call_args
        req: LLMRequest = call_args.kwargs.get("request") or call_args.args[0]
        assert req.model_name == "gemini-test"
        assert req.temperature == 0.0
        # No gmail injected → single-pass mode → JSON mode active
        assert req.response_mime_type == "application/json"
        assert req.disable_safety is True
        assert req.system_instruction is not None  # prompt_builder builds it
        assert req.tools is None
        assert len(req.messages) == 1
        assert req.messages[0].role == "user"

    async def test_classify_batch_tags_lowercased(self, agent, mock_llm):
        """Tags returned by LLM are normalized to lowercase."""
        emails = [_make_meta("id1")]
        mock_llm.generate_content.return_value = _llm_response([
            {"email_id": "id1", "valuable": True, "category": "travel",
             "fact": "Flight booked", "tags": ["Flight", "RYANAIR"]},
        ])

        results = await agent.classify_batch(emails, "user123")

        assert results[0].tags == ["flight", "ryanair"]

    async def test_classify_batch_retries_on_invalid_json(self, agent, mock_llm):
        """Invalid JSON on first attempt → LLM retry → valid JSON on second attempt → success."""
        emails = [_make_meta("id1")]
        valid_response = _llm_response([
            {"email_id": "id1", "valuable": False, "category": None,
             "fact": None, "tags": []},
        ])
        mock_llm.generate_content.side_effect = [
            LLMResponse(text="not json at all", tool_calls=[]),
            valid_response,
        ]

        results = await agent.classify_batch(emails, "user123")

        assert len(results) == 1
        assert results[0].valuable is False
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
            await agent_no_pb.classify_batch(emails, "user123")

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

    async def test_classify_batch_tool_calling_path(self, mock_llm, mock_prompt_builder):
        """LLM calls get_email_details → gmail fetched → final classification returned."""
        gmail_mock = AsyncMock(spec=EmailProviderPort)
        gmail_mock.batch_get_full_content.return_value = {
            "id1": EmailFullContent(
                email_id="id1",
                body_text="Booking confirmed. Reference: XY123.",
                body_html=None,
                attachments=[],
                attachment_binaries={},
            )
        }
        credentials = OAuthCredentials(
            user_id="user123",
            provider="gmail",
            access_token="tok",
            refresh_token="ref",
            token_expiry=datetime(2099, 1, 1),
            scopes=["gmail.readonly"],
            email_address="user@test.com",
        )
        config = AgentConfig(agent_id="test_email_classifier", agent_type="email_classifier")
        agent_with_tools = EmailClassificationAgent(
            config=config,
            execution_context=_make_execution_context(mock_llm),
            prompt_builder=mock_prompt_builder,
            gmail=gmail_mock,
        )

        mock_llm.generate_content.side_effect = [
            # Turn 1: LLM requests full content for id1
            LLMResponse(tool_calls=[ToolCall(name="get_email_details", args={"email_ids": ["id1"]})]),
            # Turn 2: final classification
            _llm_response([
                {"email_id": "id1", "valuable": True, "category": "travel",
                 "fact": "Booking confirmed ref XY123", "tags": ["booking"]},
            ]),
        ]

        results = await agent_with_tools.classify_batch(
            [_make_meta("id1", subject="Booking confirmed")], "user123", credentials=credentials
        )

        assert len(results) == 1
        assert results[0].valuable is True
        assert results[0].category == "travel"
        gmail_mock.batch_get_full_content.assert_awaited_once()
        assert mock_llm.generate_content.call_count == 2

    async def test_classify_batch_max_turns_reached(self, mock_llm, mock_prompt_builder):
        """If LLM never produces a final answer — always returns tool_calls — max_turns_reached."""
        gmail_mock = AsyncMock(spec=EmailProviderPort)
        gmail_mock.batch_get_full_content.return_value = {}
        credentials = OAuthCredentials(
            user_id="user123",
            provider="gmail",
            access_token="tok",
            refresh_token="ref",
            token_expiry=datetime(2099, 1, 1),
            scopes=[],
            email_address="",
        )
        config = AgentConfig(agent_id="test_email_classifier", agent_type="email_classifier")
        agent_with_tools = EmailClassificationAgent(
            config=config,
            execution_context=_make_execution_context(mock_llm),
            prompt_builder=mock_prompt_builder,
            gmail=gmail_mock,
        )
        mock_llm.generate_content.return_value = LLMResponse(
            tool_calls=[ToolCall(name="get_email_details", args={"email_ids": ["id1"]})]
        )

        results = await agent_with_tools.classify_batch(
            [_make_meta("id1")], "user123", credentials=credentials
        )

        assert len(results) == 1
        assert results[0].valuable is False
        assert mock_llm.generate_content.call_count == MAX_TURNS
