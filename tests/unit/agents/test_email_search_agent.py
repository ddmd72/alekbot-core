"""
Unit tests for EmailSearchAgent.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.email_search_agent import EmailSearchAgent
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
from src.services.email_search_service import EmailSearchService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SEARCH_RESULT = json.dumps({
    "count": 2,
    "emails": [
        {"email_id": "e1", "from": "a@b.com", "date": "2026-01-15", "text": "flight booked", "attachments": []},
        {"email_id": "e2", "from": "c@d.com", "date": "2026-01-16", "text": "hotel booked", "attachments": []},
    ],
})

VALID_KEYS = {
    "primary_query": "flight booking confirmation",
    "alternative_query": "Ryanair reservation reference",
    "tags": ["travel", "flight"],
}


def _make_execution_context(mock_llm) -> AgentExecutionContext:
    return AgentExecutionContext(
        agent_type="email_search",
        provider=mock_llm,
        model_name="gemini-test",
        tier=PerformanceTier.ECO,
        capabilities=ProviderCapabilities(),
    )


def _make_message(
    query: str = "find my flight booking",
    history: list | None = None,
    intent: AgentIntent = AgentIntent.QUERY,
) -> AgentMessage:
    return AgentMessage(
        intent=intent,
        payload={"query": query, "history": history or []},
        sender="quick_response_agent",
        recipient="email_search_agent",
        task_id="task_1",
        context={"user_id": "user123", "account_id": "acc1"},
    )


@pytest.fixture
def mock_llm():
    m = AsyncMock(spec=LLMPort)
    m.generate_content.return_value = LLMResponse(
        text=json.dumps(VALID_KEYS), tool_calls=[]
    )
    return m


@pytest.fixture
def mock_prompt_builder():
    pb = AsyncMock(spec=PromptBuilderPort)
    pb.build_for_agent.return_value = "class EmailSearchAgent extends Agent { ... }"
    return pb


@pytest.fixture
def mock_search_service():
    svc = AsyncMock(spec=EmailSearchService)
    svc.vector_search.return_value = SEARCH_RESULT
    return svc


@pytest.fixture
def agent(mock_llm, mock_prompt_builder, mock_search_service):
    config = AgentConfig(agent_id="email_search_agent_user123", agent_type="email_search")
    return EmailSearchAgent(
        config=config,
        execution_context=_make_execution_context(mock_llm),
        prompt_builder=mock_prompt_builder,
        email_search_service=mock_search_service,
        user_id="user123",
    )


# ---------------------------------------------------------------------------
# can_handle
# ---------------------------------------------------------------------------

class TestCanHandle:

    async def test_returns_true_with_query(self, agent):
        msg = _make_message(query="find emails")
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

    async def test_result_contains_search_json(self, agent):
        msg = _make_message()
        response = await agent.execute(msg)
        assert response.result == SEARCH_RESULT

    async def test_metadata_contains_queries(self, agent):
        msg = _make_message()
        response = await agent.execute(msg)
        assert response.metadata["primary_query"] == VALID_KEYS["primary_query"]
        assert response.metadata["alternative_query"] == VALID_KEYS["alternative_query"]
        assert response.metadata["tags"] == VALID_KEYS["tags"]

    async def test_vector_search_called_with_extracted_keys(self, agent, mock_search_service):
        msg = _make_message(query="find my flight booking")
        await agent.execute(msg)

        mock_search_service.vector_search.assert_awaited_once_with(
            primary_query=VALID_KEYS["primary_query"],
            alternative_query=VALID_KEYS["alternative_query"],
            tags=VALID_KEYS["tags"],
            user_id="user123",
            date_from=None,
            date_to=None,
        )

    async def test_uses_raw_query_when_extract_returns_empty(
        self, agent, mock_llm, mock_search_service
    ):
        mock_llm.generate_content.return_value = LLMResponse(text="not json", tool_calls=[])

        msg = _make_message(query="my original query")
        await agent.execute(msg)

        call = mock_search_service.vector_search.call_args
        assert call.kwargs["primary_query"] == "my original query"
        assert call.kwargs["alternative_query"] == "my original query"


# ---------------------------------------------------------------------------
# execute — failure paths
# ---------------------------------------------------------------------------

class TestExecuteFailure:

    async def test_empty_query_returns_failure(self, agent):
        msg = _make_message(query="")
        response = await agent.execute(msg)
        assert response.status == AgentStatus.FAILED

    async def test_vector_search_exception_returns_failure(self, agent, mock_search_service):
        mock_search_service.vector_search.side_effect = RuntimeError("Firestore error")

        msg = _make_message()
        response = await agent.execute(msg)

        assert response.status == AgentStatus.FAILED
        assert "Email search failed" in response.error


# ---------------------------------------------------------------------------
# _extract_search_queries
# ---------------------------------------------------------------------------

class TestExtractSearchQueries:

    async def test_valid_json_returns_dict(self, agent, mock_llm):
        mock_llm.generate_content.return_value = LLMResponse(
            text=json.dumps(VALID_KEYS), tool_calls=[]
        )

        result = await agent._extract_search_queries("flight", [], "user123", "acc1")

        assert result["primary_query"] == VALID_KEYS["primary_query"]
        assert result["alternative_query"] == VALID_KEYS["alternative_query"]
        assert result["tags"] == VALID_KEYS["tags"]

    async def test_invalid_json_returns_empty(self, agent, mock_llm):
        mock_llm.generate_content.return_value = LLMResponse(text="not valid json", tool_calls=[])

        result = await agent._extract_search_queries("flight", [], "user123", "acc1")

        assert result == {}

    async def test_llm_exception_returns_empty(self, agent, mock_llm):
        mock_llm.generate_content.side_effect = RuntimeError("API down")

        result = await agent._extract_search_queries("flight", [], "user123", "acc1")

        assert result == {}

    async def test_prompt_builder_failure_uses_empty_prompt(
        self, agent, mock_prompt_builder, mock_llm
    ):
        mock_prompt_builder.build_for_agent.side_effect = RuntimeError("Firestore error")
        mock_llm.generate_content.return_value = LLMResponse(
            text=json.dumps(VALID_KEYS), tool_calls=[]
        )

        # Should not raise — falls back to empty system_instruction
        result = await agent._extract_search_queries("flight", [], "user123", "acc1")

        assert result == VALID_KEYS
        req: LLMRequest = mock_llm.generate_content.call_args.kwargs.get("request") or \
                          mock_llm.generate_content.call_args.args[0]
        assert req.system_instruction == ""

    async def test_history_injected_last_three_turns(self, agent, mock_llm):
        mock_llm.generate_content.return_value = LLMResponse(
            text=json.dumps(VALID_KEYS), tool_calls=[]
        )
        history = [
            {"role": "user", "text": "turn 1"},
            {"role": "model", "text": "turn 2"},
            {"role": "user", "text": "turn 3"},
            {"role": "model", "text": "turn 4"},
            {"role": "user", "text": "turn 5"},
        ]

        await agent._extract_search_queries("query", history, "user123", "acc1")

        req: LLMRequest = mock_llm.generate_content.call_args.kwargs.get("request") or \
                          mock_llm.generate_content.call_args.args[0]
        # last 3 history turns + 1 user request = 4 messages
        assert len(req.messages) == 4
        assert req.messages[0].parts[0].text == "turn 3"
        assert req.messages[1].parts[0].text == "turn 4"
        assert req.messages[2].parts[0].text == "turn 5"
        assert "EMAIL_SEARCH_REQUEST" in req.messages[3].parts[0].text

    async def test_user_text_wraps_query(self, agent, mock_llm):
        mock_llm.generate_content.return_value = LLMResponse(
            text=json.dumps(VALID_KEYS), tool_calls=[]
        )

        await agent._extract_search_queries("my query here", [], "user123", "acc1")

        req: LLMRequest = mock_llm.generate_content.call_args.kwargs.get("request") or \
                          mock_llm.generate_content.call_args.args[0]
        user_msg = req.messages[-1].parts[0].text
        assert 'EMAIL_SEARCH_REQUEST "my query here"' == user_msg

    async def test_llm_request_params(self, agent, mock_llm):
        mock_llm.generate_content.return_value = LLMResponse(
            text=json.dumps(VALID_KEYS), tool_calls=[]
        )

        await agent._extract_search_queries("q", [], "user123", "acc1")

        req: LLMRequest = mock_llm.generate_content.call_args.kwargs.get("request") or \
                          mock_llm.generate_content.call_args.args[0]
        assert req.model_name == "gemini-test"
        assert req.temperature == 0.0
        assert req.max_tokens == 250
        assert req.response_mime_type == "application/json"
        assert req.disable_safety is True
        assert req.tools == []


# ---------------------------------------------------------------------------
# can_handle — email_id path
# ---------------------------------------------------------------------------

class TestCanHandleEmailId:

    async def test_returns_true_with_email_id(self, agent):
        msg = AgentMessage(
            intent=AgentIntent.QUERY,
            payload={"intent": "get_email_details", "email_id": "19bd6ad70f3e911b"},
            sender="smart", recipient="email_search_agent", task_id="t",
            context={"user_id": "user123"},
        )
        assert await agent.can_handle(msg) is True

    async def test_returns_true_with_email_id_and_filename(self, agent):
        msg = AgentMessage(
            intent=AgentIntent.QUERY,
            payload={"intent": "get_email_attachment", "email_id": "19bd6ad70f3e911b", "filename": "doc.pdf"},
            sender="smart", recipient="email_search_agent", task_id="t",
            context={"user_id": "user123"},
        )
        assert await agent.can_handle(msg) is True

    async def test_returns_false_empty_payload(self, agent):
        msg = AgentMessage(
            intent=AgentIntent.QUERY,
            payload={},
            sender="smart", recipient="email_search_agent", task_id="t",
            context={"user_id": "user123"},
        )
        assert await agent.can_handle(msg) is False


# ---------------------------------------------------------------------------
# execute — get_email_details routing
# ---------------------------------------------------------------------------

class TestGetEmailDetails:

    def _msg(self, email_id: str = "19bd6ad70f3e911b") -> AgentMessage:
        return AgentMessage(
            intent=AgentIntent.QUERY,
            payload={"intent": "get_email_details", "email_id": email_id},
            sender="smart", recipient="email_search_agent", task_id="t",
            context={"user_id": "user123", "account_id": "acc1"},
        )

    async def test_success_calls_get_details(self, agent, mock_search_service):
        mock_search_service.get_details.return_value = "Email body text here"

        response = await agent.execute(self._msg())

        assert response.status == AgentStatus.SUCCESS
        mock_search_service.get_details.assert_awaited_once_with(
            email_id="19bd6ad70f3e911b",
            user_id="user123",
        )

    async def test_success_result_contains_body(self, agent, mock_search_service):
        mock_search_service.get_details.return_value = "Important email body"

        response = await agent.execute(self._msg())

        assert response.result == "Important email body"

    async def test_metadata_contains_email_id(self, agent, mock_search_service):
        mock_search_service.get_details.return_value = "body"

        response = await agent.execute(self._msg("abc123"))

        assert response.metadata["email_id"] == "abc123"

    async def test_exception_returns_failure(self, agent, mock_search_service):
        mock_search_service.get_details.side_effect = RuntimeError("Gmail error")

        response = await agent.execute(self._msg())

        assert response.status == AgentStatus.FAILED
        assert "get_email_details failed" in response.error

    async def test_vector_search_not_called(self, agent, mock_search_service):
        mock_search_service.get_details.return_value = "body"

        await agent.execute(self._msg())

        mock_search_service.vector_search.assert_not_called()


# ---------------------------------------------------------------------------
# execute — get_email_attachment routing
# ---------------------------------------------------------------------------

class TestGetEmailAttachment:

    def _msg(
        self,
        email_id: str = "19bd6ad70f3e911b",
        filename: str = "cerfa-15646.pdf",
    ) -> AgentMessage:
        return AgentMessage(
            intent=AgentIntent.QUERY,
            payload={"intent": "get_email_attachment", "email_id": email_id, "filename": filename},
            sender="smart", recipient="email_search_agent", task_id="t",
            context={"user_id": "user123", "account_id": "acc1"},
        )

    async def test_success_calls_get_attachment(self, agent, mock_search_service):
        mock_search_service.get_attachment.return_value = "Parsed PDF text"

        response = await agent.execute(self._msg())

        assert response.status == AgentStatus.SUCCESS
        mock_search_service.get_attachment.assert_awaited_once_with(
            email_id="19bd6ad70f3e911b",
            filename="cerfa-15646.pdf",
            user_id="user123",
        )

    async def test_result_contains_text(self, agent, mock_search_service):
        mock_search_service.get_attachment.return_value = "Document content"

        response = await agent.execute(self._msg())

        assert response.result == "Document content"

    async def test_metadata_contains_email_id_and_filename(self, agent, mock_search_service):
        mock_search_service.get_attachment.return_value = "text"

        response = await agent.execute(self._msg("e1", "file.pdf"))

        assert response.metadata["email_id"] == "e1"
        assert response.metadata["filename"] == "file.pdf"

    async def test_exception_returns_failure(self, agent, mock_search_service):
        mock_search_service.get_attachment.side_effect = RuntimeError("Gmail error")

        response = await agent.execute(self._msg())

        assert response.status == AgentStatus.FAILED
        assert "get_email_attachment failed" in response.error

    async def test_vector_search_not_called(self, agent, mock_search_service):
        mock_search_service.get_attachment.return_value = "text"

        await agent.execute(self._msg())

        mock_search_service.vector_search.assert_not_called()

    async def test_filename_required_to_trigger_attachment_path(
        self, agent, mock_search_service
    ):
        """email_id only (no filename) → get_details path, not attachment."""
        mock_search_service.get_details.return_value = "body"
        msg = AgentMessage(
            intent=AgentIntent.QUERY,
            payload={"intent": "get_email_details", "email_id": "e1"},  # no filename
            sender="smart", recipient="email_search_agent", task_id="t",
            context={"user_id": "user123"},
        )

        await agent.execute(msg)

        mock_search_service.get_details.assert_awaited_once()
        mock_search_service.get_attachment.assert_not_called()
