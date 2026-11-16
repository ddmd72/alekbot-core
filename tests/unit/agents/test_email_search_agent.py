"""
Unit tests for EmailSearchAgent.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.agents.email_search_agent import EmailSearchAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.email_details import EmailDetails
from src.domain.user import PerformanceTier
from src.ports.llm_port import (
    AgentExecutionContext,
    LLMResponse,
    LLMPort,
    ProviderCapabilities,
)
from src.ports.prompt_builder_port import PromptBuilderPort
from src.services.email_card_search_service import EmailCardSearchService
from src.services.email_search_service import EmailSearchService
from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_execution_context(mock_llm) -> AgentExecutionContext:
    return AgentExecutionContext(
        agent_type="email_search",
        provider=mock_llm,
        model_name="gemini-test",
        tier=PerformanceTier.ECO,
        capabilities=ProviderCapabilities(),
        resilience_port=InMemoryProviderResilience(),
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
        text="{}", tool_calls=[]
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
    # reviewer (email fresh headers): search_emails now also reads the index watermark and lists live headers;
    # an indexed mailbox with nothing newer keeps these tests on the index path they check
    svc.index_watermark.return_value = datetime(2026, 1, 16, tzinfo=timezone.utc)
    svc.list_headers.return_value = ([], False)
    return svc


@pytest.fixture
def agent(mock_llm, mock_prompt_builder, mock_search_service):
    config = AgentConfig(agent_id="email_search_agent_user123", agent_type="email_search")
    return EmailSearchAgent(
        config=config,
        execution_context=_make_execution_context(mock_llm),
        prompt_builder=mock_prompt_builder,
        email_search_service=mock_search_service,
        card_search=AsyncMock(spec=EmailCardSearchService),
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
# execute — failure paths
# ---------------------------------------------------------------------------

class TestExecuteFailure:

    async def test_empty_query_returns_failure(self, agent):
        msg = _make_message(query="")
        response = await agent.execute(msg)
        assert response.status == AgentStatus.FAILED

# ---------------------------------------------------------------------------
# can_handle — email_id path
# ---------------------------------------------------------------------------

class TestCanHandleEmailId:

    async def test_returns_true_with_email_id(self, agent):
        msg = AgentMessage(
            intent=AgentIntent.QUERY,
            payload={"intent": "get_email_details", "email_id": "msg_example001"},
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

    def _msg(self, email_id: str = "msg_example001") -> AgentMessage:
        return AgentMessage(
            intent=AgentIntent.QUERY,
            payload={"intent": "get_email_details", "email_id": email_id},
            sender="smart", recipient="email_search_agent", task_id="t",
            context={"user_id": "user123", "account_id": "acc1"},
        )

    # Reviewer (U4a A4): the agent reads the details with their attachment parts (one listing, D11), so the
    # service call is `get_details_with_parts` and its text is the result.
    async def test_success_calls_get_details(self, agent, mock_search_service):
        mock_search_service.get_details_with_parts.return_value = EmailDetails("Email body text here")

        response = await agent.execute(self._msg())

        assert response.status == AgentStatus.SUCCESS
        mock_search_service.get_details_with_parts.assert_awaited_once_with(
            email_id="msg_example001",
            user_id="user123",
        )

    async def test_success_result_contains_body(self, agent, mock_search_service):
        mock_search_service.get_details_with_parts.return_value = EmailDetails("Important email body")

        response = await agent.execute(self._msg())

        assert response.result == "Important email body"

    async def test_metadata_contains_email_id(self, agent, mock_search_service):
        mock_search_service.get_details_with_parts.return_value = EmailDetails("body")

        response = await agent.execute(self._msg("abc123"))

        assert response.metadata["email_id"] == "abc123"

    async def test_exception_returns_failure(self, agent, mock_search_service):
        mock_search_service.get_details_with_parts.side_effect = RuntimeError("Gmail error")

        response = await agent.execute(self._msg())

        assert response.status == AgentStatus.FAILED
        assert "get_email_details failed" in response.error
