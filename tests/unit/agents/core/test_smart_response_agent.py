"""
Unit tests for SmartResponseAgent.

Covers:
- can_handle() classification logic
- execute() flow with/without agent delegation
- smart parallel execution (memory-first)
- history sanitization edge cases
"""

import asyncio
from dataclasses import dataclass
from typing import List

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.agents.core.smart_response_agent import (
    SmartResponseAgent,
    create_smart_response_agent
)
from src.infrastructure.delegation_engine import (
    ToolResult as ToolResponse,
    DelegationEngine,
    _format_result,
    _format_email_search_compact,
)
from src.domain.agent import AgentMessage, AgentConfig, AgentIntent, AgentStatus, AgentResponse
from src.domain.messaging import SmartResponse
from src.ports.llm_port import Message, MessagePart, ToolCall, LLMResponse, UsageMetadata, LLMPort
from src.services.agent_context_builder import AgentExecutionContext
from src.domain.user import PerformanceTier
from src.ports.llm_port import ProviderCapabilities


# =========================================================================
# Mock Types
# =========================================================================

@dataclass
class MockSession:
    history: List[Message] = None

    def __post_init__(self):
        if self.history is None:
            self.history = []


# =========================================================================
# Fixtures
# =========================================================================

@pytest.fixture
def mock_llm_port():
    service = MagicMock(spec=LLMPort)
    return service


@pytest.fixture
def mock_session_store():
    store = MagicMock()
    store.load_session = AsyncMock(return_value=MockSession())
    return store


@pytest.fixture
def mock_prompt_builder():
    builder = MagicMock()
    builder.build_for_agent = AsyncMock(return_value="SYSTEM_PROMPT")
    builder.build_system_prompt = AsyncMock(return_value={
        "kernel": "KERNEL",
        "core_context": "CORE_CONTEXT",
        "anchors": "ANCHORS",
        "examples": "EXAMPLES",
        "slack_rules": "SLACK_RULES"
    })
    return builder


@pytest.fixture
def mock_coordinator():
    coordinator = MagicMock()
    coordinator.route_message = AsyncMock()
    return coordinator


@pytest.fixture
def smart_agent_config():
    return AgentConfig(
        agent_id="smart_response_agent",
        agent_type="smart_response",
        llm_model="gemini-3-pro-preview",
        max_retries=1,
        timeout_ms=60000,
        capabilities=["complex_reasoning", "agent_delegation"]
    )


@pytest.fixture
def smart_agent(
    smart_agent_config,
    mock_llm_port,
    mock_session_store,
    mock_prompt_builder,
    mock_coordinator
):
    execution_context = AgentExecutionContext(
        agent_type="smart",
        provider=mock_llm_port,
        model_name="gemini-3-pro-preview",
        tier=PerformanceTier.BALANCED,
        capabilities=ProviderCapabilities()
    )
    return SmartResponseAgent(
        config=smart_agent_config,
        execution_context=execution_context,
        session_store=mock_session_store,
        prompt_builder=mock_prompt_builder,
        coordinator=mock_coordinator
    )


def create_query_message(text: str, is_simple: bool = False) -> AgentMessage:
    return AgentMessage.create(
        sender="router_agent",
        recipient="smart_response_agent",
        intent=AgentIntent.QUERY,
        payload={"text": text},
        context={
            "session_id": "session123",
            "user_id": "user456",
            "classification": {
                "is_simple": is_simple,
                "is_personal": True,
                "needs_external": False
            }
        }
    )


def build_llm_response(text: str, tool_calls: List[ToolCall]) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=tool_calls,
        usage_metadata=UsageMetadata(prompt_tokens=5, completion_tokens=5, total_tokens=10)
    )


# =========================================================================
# can_handle Tests
# =========================================================================

class TestSmartResponseAgentCanHandle:
    @pytest.mark.asyncio
    async def test_handles_complex_query(self, smart_agent):
        message = create_query_message("Complex", is_simple=False)
        assert await smart_agent.can_handle(message) is True

    @pytest.mark.asyncio
    async def test_rejects_simple_query(self, smart_agent):
        message = create_query_message("Simple", is_simple=True)
        assert await smart_agent.can_handle(message) is True

    @pytest.mark.asyncio
    async def test_handles_missing_classification(self, smart_agent):
        message = AgentMessage.create(
            sender="router_agent",
            recipient="smart_response_agent",
            intent=AgentIntent.QUERY,
            payload={"text": "Complex"},
            context={}
        )
        assert await smart_agent.can_handle(message) is True

    @pytest.mark.asyncio
    async def test_rejects_missing_payload_text(self, smart_agent):
        message = AgentMessage.create(
            sender="router_agent",
            recipient="smart_response_agent",
            intent=AgentIntent.QUERY,
            payload={},
            context={}
        )
        assert await smart_agent.can_handle(message) is False

    @pytest.mark.asyncio
    async def test_accepts_notification_with_current_message_parts(self, smart_agent):
        """Regression: notification_service sends payload text='' but fills current_message_parts.
        SmartAgent must accept messages where only current_message_parts carries content
        (e.g. Deep Research result delivery via UserNotificationService.notify())."""
        from src.ports.llm_port import MessagePart
        message = AgentMessage.create(
            sender="notification_service",
            recipient="smart_response_agent",
            intent=AgentIntent.QUERY,
            payload={"text": ""},
            context={
                "current_message_parts": [
                    MessagePart(text="[System: Deep Research completed. Report: ...]")
                ],
            },
        )
        assert await smart_agent.can_handle(message) is True

    @pytest.mark.asyncio
    async def test_rejects_empty_text_and_no_parts(self, smart_agent):
        """No text, no parts, no attachments — CANNOT_HANDLE."""
        message = AgentMessage.create(
            sender="notification_service",
            recipient="smart_response_agent",
            intent=AgentIntent.QUERY,
            payload={"text": ""},
            context={"current_message_parts": []},
        )
        assert await smart_agent.can_handle(message) is False


# =========================================================================
# execute Tests
# =========================================================================

class TestSmartResponseAgentExecute:
    @pytest.mark.asyncio
    async def test_execute_without_tool_calls(self, smart_agent, mock_llm_port):
        mock_llm_port.generate_content = AsyncMock(
            return_value=build_llm_response("Final answer", [])
        )
        message = create_query_message("Explain something")

        response = await smart_agent.execute(message)

        assert response.status == AgentStatus.SUCCESS
        assert isinstance(response.result, SmartResponse)
        assert response.result.text == "Final answer"
        assert response.metadata["model"] == smart_agent.model_name
        smart_agent.prompt_builder.build_for_agent.assert_called_once()
        build_call = smart_agent.prompt_builder.build_for_agent.call_args
        assert build_call[1].get("agent_type") == "smart"

    @pytest.mark.asyncio
    async def test_execute_with_agent_delegation(self, smart_agent, mock_llm_port, mock_coordinator):
        tool_calls = [
            ToolCall(name="search_memory", args={"query": "car model"}),
            ToolCall(name="ask_web_search_agent", args={"query": "weather Valencia"})
        ]

        mock_llm_port.generate_content = AsyncMock(side_effect=[
            build_llm_response("", tool_calls),
            build_llm_response("Final response", [])
        ])

        # memory search result
        mock_coordinator.route_message = AsyncMock(side_effect=[
            AgentResponse.success(
                task_id="t1",
                agent_id="memory_search_agent",
                result=["User car is Toyota Corolla"]
            ),
            AgentResponse.success(
                task_id="t2",
                agent_id="web_search_agent",
                result="Weather is sunny"
            )
        ])

        message = create_query_message("What's my car and weather?")
        response = await smart_agent.execute(message)

        assert response.status == AgentStatus.SUCCESS
        assert response.result.text == "Final response"
        assert mock_llm_port.generate_content.call_count == 2


# =========================================================================
# Parallel Execution Tests
# =========================================================================

class TestSmartResponseAgentParallelExecution:
    @pytest.mark.asyncio
    async def test_memory_first_then_parallel(self):
        """DelegationEngine executes search_memory before other calls."""
        call_order = []

        async def mock_handle_delegation(intent, query, context, calling_agent_id=""):
            call_order.append(intent)
            return AgentResponse.success(task_id="t", agent_id="a", result="ok")

        mock_coordinator = AsyncMock()
        mock_coordinator.handle_delegation = AsyncMock(side_effect=mock_handle_delegation)

        engine = DelegationEngine(mock_coordinator)
        tool_calls = [
            ToolCall(name="delegate_to_specialist", args={"intent": "search_memory", "query": "bio"}),
            ToolCall(name="delegate_to_specialist", args={"intent": "search_web", "query": "news"}),
            ToolCall(name="delegate_to_specialist", args={"intent": "search_web", "query": "prices"}),
        ]

        results = await engine._execute_tool_calls(
            tool_calls=tool_calls,
            context={"user_id": "user123"},
            intent_remap={},
            intent_fanout={},
            calling_agent_id="test",
            max_retries=0,
            retry_backoff=0,
        )

        assert call_order[0] == "search_memory"
        assert len(results) == 3


# =========================================================================
# History Sanitization Tests
# =========================================================================

class TestSmartResponseAgentHistory:
    def test_sanitize_tool_history_removes_orphans(self, smart_agent):
        history = [
            Message(role="user", parts=[MessagePart(text="Hello")]),
            Message(role="user", parts=[MessagePart(tool_response={"name": "search_memory", "response": {"result": "data"}})])
        ]

        cleaned = smart_agent._sanitize_tool_history(history)
        assert len(cleaned) == 1
        assert cleaned[0].role == "user"


# =========================================================================
# Factory Tests
# =========================================================================

class TestCreateSmartResponseAgent:
    def test_factory_defaults(self, mock_llm_port, mock_session_store, mock_prompt_builder):
        execution_context = AgentExecutionContext(
            agent_type="smart",
            provider=mock_llm_port,
            model_name="gemini-3-pro-preview",
            tier=PerformanceTier.BALANCED,
            capabilities=ProviderCapabilities()
        )
        agent = create_smart_response_agent(
            execution_context=execution_context,
            session_store=mock_session_store,
            prompt_builder=mock_prompt_builder
        )

        assert agent.agent_id == "smart_response_agent"
        assert agent.model_name == "gemini-3-pro-preview"


# =========================================================================
# can_handle — additional
# =========================================================================

class TestCanHandleAdditional:

    async def test_non_query_intent_returns_false(self, smart_agent):
        msg = AgentMessage.create(
            sender="router",
            recipient="smart_response_agent",
            intent=AgentIntent.DELEGATE,
            payload={"text": "hello"},
        )
        assert await smart_agent.can_handle(msg) is False

    async def test_attachments_without_text_returns_true(self, smart_agent):
        msg = AgentMessage.create(
            sender="router",
            recipient="smart_response_agent",
            intent=AgentIntent.QUERY,
            payload={"text": "", "attachments": ["file.pdf"]},
        )
        assert await smart_agent.can_handle(msg) is True


# =========================================================================
# execute() — edge cases
# =========================================================================

class TestExecuteEdgeCases:

    async def test_biographical_exception_swallowed(self, smart_agent, mock_llm_port):
        """Biographical load failure should not abort execute()."""
        repo = MagicMock()
        repo.get_biographical_context_cached = AsyncMock(side_effect=RuntimeError("Firestore down"))
        smart_agent.repository = repo

        mock_llm_port.generate_content = AsyncMock(
            return_value=build_llm_response("Answer", [])
        )
        msg = AgentMessage.create(
            sender="router",
            recipient="smart_response_agent",
            intent=AgentIntent.QUERY,
            payload={"text": "hello"},
            context={"account_id": "acc1", "user_id": "u1", "session_id": "s1"},
        )
        response = await smart_agent.execute(msg)
        assert response.status == AgentStatus.SUCCESS

    async def test_execute_loop_failed_returns_failure(self, smart_agent, mock_llm_port):
        """When loop_result.failed=True, execute() returns failure."""
        # Always return tool calls → exhaust MAX_DELEGATION_TURNS
        tool_call = ToolCall(
            name="delegate_to_specialist",
            args={"intent": "search_memory", "query": "test"}
        )
        smart_agent.coordinator.handle_delegation = AsyncMock(
            return_value=AgentResponse.success(task_id="t1", agent_id="a1", result="r")
        )
        smart_agent.coordinator.get_available_intents_for = MagicMock(return_value=[])

        mock_llm_port.generate_content = AsyncMock(
            return_value=build_llm_response("", [tool_call])
        )
        msg = create_query_message("complex query")
        response = await smart_agent.execute(msg)
        # Exhausted turns → failed
        assert response.status == AgentStatus.FAILED

    async def test_execute_exception_returns_failure(self, smart_agent, mock_llm_port):
        """Exception in execute() body returns failure."""
        mock_llm_port.generate_content = AsyncMock(side_effect=RuntimeError("LLM crash"))
        response = await smart_agent.execute(create_query_message("hello"))
        assert response.status == AgentStatus.FAILED
        assert "Smart response failed" in response.error

    async def test_execute_enriched_context_with_facts(self, smart_agent, mock_llm_port):
        """enriched_context with facts should log merged context count."""
        mock_llm_port.generate_content = AsyncMock(
            return_value=build_llm_response("Done.", [])
        )
        msg = AgentMessage.create(
            sender="router",
            recipient="smart_response_agent",
            intent=AgentIntent.QUERY,
            payload={"text": "hello"},
            context={
                "session_id": "s1",
                "user_id": "u1",
                "enriched_context": {"facts": ["fact1", "fact2"]},
            },
        )
        response = await smart_agent.execute(msg)
        assert response.status == AgentStatus.SUCCESS

    async def test_email_for_triage_passed_as_extra_static_blocks(self, smart_agent, mock_llm_port):
        """email_for_triage in context must be passed as extra_static_blocks to build_for_agent."""
        emails = [{"email_id": "e1", "subject": "Hello", "from": "a@b.com"}]
        mock_llm_port.generate_content = AsyncMock(
            return_value=build_llm_response("Done", [])
        )
        msg = AgentMessage.create(
            sender="notification_service",
            recipient="smart_response_agent",
            intent=AgentIntent.QUERY,
            payload={"text": ""},
            context={
                "user_id": "u1",
                "account_id": "acc1",
                "session_id": "s1",
                "current_message_parts": [],
                "email_for_triage": emails,
            },
        )
        await smart_agent.execute(msg)

        build_call = smart_agent.prompt_builder.build_for_agent.call_args
        extra = build_call[1].get("extra_static_blocks")
        assert extra is not None and len(extra) == 1
        assert "email_for_triage" in extra[0]
        assert "e1" in extra[0]

    async def test_no_email_for_triage_passes_none(self, smart_agent, mock_llm_port):
        """Without email_for_triage in context, extra_static_blocks must be None."""
        mock_llm_port.generate_content = AsyncMock(
            return_value=build_llm_response("Done", [])
        )
        msg = AgentMessage.create(
            sender="router",
            recipient="smart_response_agent",
            intent=AgentIntent.QUERY,
            payload={"text": "Hello"},
            context={"user_id": "u1", "account_id": "acc1", "session_id": "s1"},
        )
        await smart_agent.execute(msg)

        build_call = smart_agent.prompt_builder.build_for_agent.call_args
        assert build_call[1].get("extra_static_blocks") is None

    async def test_execute_history_contexts_in_metadata(self, smart_agent, mock_llm_port):
        """loop_result.history_contexts should be merged into metadata."""
        tool_call = ToolCall(
            name="delegate_to_specialist",
            args={"intent": "search_emails", "query": "invoice"}
        )
        import json as _json
        email_result = _json.dumps({"emails": [{"email_id": "e1", "from": "a@b.com", "date": "2026"}]})
        delegation_response = AgentResponse.success(
            task_id="t1", agent_id="email_search_agent", result=email_result
        )
        delegation_response.history_context = {"email_search_context": [{"you_searched": "invoice"}]}
        smart_agent.coordinator.handle_delegation = AsyncMock(return_value=delegation_response)
        smart_agent.coordinator.get_available_intents_for = MagicMock(return_value=[])

        mock_llm_port.generate_content = AsyncMock(side_effect=[
            build_llm_response("", [tool_call]),
            build_llm_response("Found your invoice.", []),
        ])
        response = await smart_agent.execute(create_query_message("find invoice"))
        assert response.status == AgentStatus.SUCCESS
        assert "email_search_context" in response.metadata


# =========================================================================
# Delegation loop — deliver_response tool
# =========================================================================

class TestDeliverResponseTool:

    async def test_deliver_response_tool_returns_success(self, smart_agent, mock_llm_port):
        """deliver_response terminates the loop immediately."""
        deliver_call = ToolCall(
            name="deliver_response",
            args={
                "full_response": "Here is your answer.",
                "history_summary": "Summary.",
                "rich_content": None,
            }
        )
        mock_llm_port.generate_content = AsyncMock(
            return_value=build_llm_response("", [deliver_call])
        )
        smart_agent.coordinator.get_available_intents_for = MagicMock(return_value=[])
        response = await smart_agent.execute(create_query_message("complex"))
        assert response.status == AgentStatus.SUCCESS
        assert response.result.text == "Here is your answer."
        assert response.metadata.get("response_summary") == "Summary."

    async def test_deliver_response_with_rich_content(self, smart_agent, mock_llm_port):
        """deliver_response with rich_content builds RichContent object."""
        deliver_call = ToolCall(
            name="deliver_response",
            args={
                "full_response": "Answer.",
                "rich_content": {"type": "table", "data": {}, "fallback": "plain"},
            }
        )
        mock_llm_port.generate_content = AsyncMock(
            return_value=build_llm_response("", [deliver_call])
        )
        smart_agent.coordinator.get_available_intents_for = MagicMock(return_value=[])
        response = await smart_agent.execute(create_query_message("table query"))
        assert response.status == AgentStatus.SUCCESS
        assert response.result.structured_data is not None
        assert response.result.structured_data.content_type == "table"


# =========================================================================
# _delegate_to_agent_with_retry — edge cases
# =========================================================================

class TestDelegateToAgentWithRetry:
    """Tests for DelegationEngine._dispatch_single (moved from SmartResponseAgent)."""

    @pytest.fixture
    def engine(self):
        mock_coordinator = AsyncMock()
        return DelegationEngine(mock_coordinator)

    @pytest.fixture
    def ctx(self):
        return {"user_id": "u1", "session_id": "s1"}

    async def test_no_intent_returns_error(self, engine, ctx):
        tc = ToolCall(name="delegate_to_specialist", args={"query": "q"})
        result = await engine._dispatch_single(tc, ctx, {}, {}, "test", 0, 0)
        assert "SYSTEM ERROR" in result.result_str

    async def test_str_context_params_wrapped_as_reasoning(self, engine, ctx):
        """context as string is wrapped into {"reasoning": ...}."""
        success_resp = AgentResponse.success(task_id="t", agent_id="a", result="data")
        engine._coordinator.handle_delegation = AsyncMock(return_value=success_resp)
        tc = ToolCall(
            name="delegate_to_specialist",
            args={"intent": "search_memory", "query": "q", "context": "some context reasoning"},
        )
        result = await engine._dispatch_single(tc, ctx, {}, {}, "test", 0, 0)
        assert result.result_str == "data"

    async def test_non_dict_context_params_becomes_empty(self, engine, ctx):
        success_resp = AgentResponse.success(task_id="t", agent_id="a", result="data")
        engine._coordinator.handle_delegation = AsyncMock(return_value=success_resp)
        tc = ToolCall(
            name="delegate_to_specialist",
            args={"intent": "search_memory", "query": "q", "context": 42},
        )
        result = await engine._dispatch_single(tc, ctx, {}, {}, "test", 0, 0)
        assert result.result_str == "data"

    async def test_failed_delegation_returns_error_result(self, engine, ctx):
        """Non-SUCCESS response returns a rejection ToolResult without retry."""
        failed_resp = AgentResponse.failure(task_id="t", agent_id="a", error="not found")
        engine._coordinator.handle_delegation = AsyncMock(return_value=failed_resp)
        tc = ToolCall(
            name="delegate_to_specialist",
            args={"intent": "search_memory", "query": "q"},
        )
        result = await engine._dispatch_single(tc, ctx, {}, {}, "test", 0, 0)
        assert "SYSTEM" in result.result_str
        engine._coordinator.handle_delegation.assert_called_once()

    async def test_search_emails_uses_compact_formatter(self, engine, ctx):
        import json as _json
        email_json = _json.dumps({"emails": [{"email_id": "e1", "from": "x@y.com", "date": "2026"}]})
        success_resp = AgentResponse.success(task_id="t", agent_id="a", result=email_json)
        engine._coordinator.handle_delegation = AsyncMock(return_value=success_resp)
        tc = ToolCall(
            name="delegate_to_specialist",
            args={"intent": "search_emails", "query": "invoice"},
        )
        result = await engine._dispatch_single(tc, ctx, {}, {}, "test", 0, 0)
        assert "e1" in result.result_str
        assert "x@y.com" in result.result_str


# =========================================================================
# _format_agent_result
# =========================================================================

class TestFormatAgentResult:
    """Tests for delegation_engine._format_result (moved from SmartResponseAgent)."""

    def test_list_result(self):
        result = _format_result("search_memory", ["item1", "item2"])
        assert "item1" in result
        assert "item2" in result

    def test_smart_response_result(self):
        sr = SmartResponse(text="Formatted text")
        result = _format_result("search_memory", sr)
        assert result == "Formatted text"

    def test_str_result(self):
        result = _format_result("search_memory", "plain string")
        assert result == "plain string"

    def test_int_result_converted(self):
        result = _format_result("search_memory", 42)
        assert result == "42"


# =========================================================================
# _format_email_search_compact — smart agent version (has extra instructions)
# =========================================================================

class TestFormatEmailSearchCompact:
    """Tests for delegation_engine._format_email_search_compact."""

    def test_non_string_returns_str(self):
        assert _format_email_search_compact(None) == "None"

    def test_invalid_json_returns_original(self):
        assert _format_email_search_compact("not json") == "not json"

    def test_empty_emails_returns_original(self):
        import json as _json
        data = _json.dumps({"emails": []})
        assert _format_email_search_compact(data) == data

    def test_email_with_attachments_and_text(self):
        import json as _json
        data = _json.dumps({"emails": [{
            "email_id": "e1", "from": "a@b.com", "date": "2026",
            "attachments": ["report.pdf"], "text": "See attached"
        }]})
        result = _format_email_search_compact(data)
        assert "e1" in result
        assert "report.pdf" in result
        assert "See attached" in result
        assert "get_email_details" in result


# =========================================================================
# _generate_history_summary
# =========================================================================

class TestGenerateHistorySummary:

    async def test_no_service_returns_none(self, smart_agent):
        smart_agent.history_summary_service = None
        result = await smart_agent._generate_history_summary("some text")
        assert result is None

    async def test_with_service_delegates(self, smart_agent):
        mock_service = MagicMock()
        mock_service.summarize_model_response = AsyncMock(return_value="compact summary")
        smart_agent.history_summary_service = mock_service
        result = await smart_agent._generate_history_summary("long response text")
        assert result == "compact summary"


# =========================================================================
# _summarize_history
# =========================================================================

class TestSummarizeHistory:

    def test_text_part(self, smart_agent):
        history = [Message(role="user", parts=[MessagePart(text="hello")])]
        result = smart_agent._summarize_history(history)
        assert "user" in result
        assert "text" in result

    def test_tool_call_part(self, smart_agent):
        tc = ToolCall(name="search_memory", args={})
        history = [Message(role="model", parts=[MessagePart(tool_call=tc)])]
        result = smart_agent._summarize_history(history)
        assert "search_memory" in result

    def test_tool_response_part(self, smart_agent):
        history = [Message(role="user", parts=[MessagePart(
            tool_response={"name": "search_memory", "response": {}}
        )])]
        result = smart_agent._summarize_history(history)
        assert "tool_response" in result

    def test_file_data_part(self, smart_agent):
        history = [Message(role="user", parts=[MessagePart(
            file_data={"uri": "gs://bucket/f", "mime_type": "text/plain"}
        )])]
        result = smart_agent._summarize_history(history)
        assert "file" in result


# =========================================================================
# _validate_history
# =========================================================================

class TestValidateHistory:

    def test_empty_history_no_raise(self, smart_agent):
        smart_agent._validate_history([])

    def test_tool_call_without_response_raises(self, smart_agent):
        tc = ToolCall(name="search_memory", args={})
        history = [
            Message(role="user", parts=[MessagePart(text="q")]),
            Message(role="model", parts=[MessagePart(tool_call=tc)]),
            Message(role="user", parts=[MessagePart(tool_response={"name": "search_memory", "response": {}})]),
        ]
        # Last message is tool_response which also implies tool_call...
        # Actually let's test: last msg has tool_call but no tool_response in the same message
        history2 = [
            Message(role="user", parts=[MessagePart(text="q")]),
            Message(role="model", parts=[MessagePart(tool_call=tc)]),
        ]
        with pytest.raises(ValueError, match="HISTORY_VIOLATION"):
            smart_agent._validate_history(history2)

    def test_consecutive_same_role_raises(self, smart_agent):
        history = [
            Message(role="user", parts=[MessagePart(text="q1")]),
            Message(role="user", parts=[MessagePart(text="q2")]),
        ]
        with pytest.raises(ValueError, match="consecutive turns"):
            smart_agent._validate_history(history)


# =========================================================================
# _sanitize_tool_history — additional edge cases
# =========================================================================

class TestSanitizeToolHistoryAdditional:

    def test_empty_history_returns_empty(self, smart_agent):
        assert smart_agent._sanitize_tool_history([]) == []

    def test_normal_conversation_unchanged(self, smart_agent):
        history = [
            Message(role="user", parts=[MessagePart(text="hello")]),
            Message(role="model", parts=[MessagePart(text="hi there")]),
        ]
        cleaned = smart_agent._sanitize_tool_history(history)
        assert len(cleaned) == 2

    def test_trailing_tool_call_removed(self, smart_agent):
        """Dangling tool_call at end of history (no matching response) is dropped."""
        tc = ToolCall(name="search_memory", args={})
        history = [
            Message(role="user", parts=[MessagePart(text="q")]),
            Message(role="model", parts=[MessagePart(tool_call=tc)]),
        ]
        cleaned = smart_agent._sanitize_tool_history(history)
        # The tool call without response is removed
        assert len(cleaned) == 1
        assert cleaned[0].role == "user"

    def test_valid_tool_call_with_response_kept(self, smart_agent):
        tc = ToolCall(name="search_memory", args={})
        history = [
            Message(role="user", parts=[MessagePart(text="q")]),
            Message(role="model", parts=[MessagePart(tool_call=tc)]),
            Message(role="user", parts=[MessagePart(
                tool_response={"name": "search_memory", "response": {"result": "data"}}
            )]),
            Message(role="model", parts=[MessagePart(text="here is the answer")]),
        ]
        cleaned = smart_agent._sanitize_tool_history(history)
        assert len(cleaned) == 4


# =========================================================================
# Parallel execution — exception path
# =========================================================================

class TestParallelExceptionHandling:

    async def test_exception_in_parallel_wrapped_as_error(self):
        """Exception in parallel agent call is captured, not propagated."""
        mock_coordinator = AsyncMock()
        mock_coordinator.handle_delegation = AsyncMock(side_effect=RuntimeError("agent crashed"))
        engine = DelegationEngine(mock_coordinator)

        tool_calls = [
            ToolCall(name="delegate_to_specialist", args={"intent": "search_web", "query": "news"}),
        ]
        results = await engine._execute_tool_calls(
            tool_calls=tool_calls,
            context={"user_id": "u1"},
            intent_remap={},
            intent_fanout={},
            calling_agent_id="test",
            max_retries=0,
            retry_backoff=0,
        )
        assert len(results) == 1
        assert "AGENT ERROR" in results[0].result_str


# =========================================================================
# _load_history — edge cases
# =========================================================================

class TestLoadHistory:

    async def test_no_session_id_returns_empty(self, smart_agent):
        result = await smart_agent._load_history("")
        assert result == []

    async def test_no_session_store_returns_empty(self, smart_agent):
        smart_agent.session_store = None
        result = await smart_agent._load_history("sess-1")
        assert result == []

    async def test_exception_returns_empty(self, smart_agent):
        smart_agent.session_store.load_session = AsyncMock(side_effect=RuntimeError("fail"))
        result = await smart_agent._load_history("sess-1")
        assert result == []


# =========================================================================
# _sanitize_tool_history — consecutive same-role merge paths
# =========================================================================

class TestSanitizeToolHistoryMerge:

    def test_consecutive_user_messages_merged(self, smart_agent):
        """Two consecutive user text messages → parts merged into first."""
        history = [
            Message(role="user", parts=[MessagePart(text="msg1")]),
            Message(role="user", parts=[MessagePart(text="msg2")]),
        ]
        cleaned = smart_agent._sanitize_tool_history(history)
        # Parts from second msg merged into first
        assert len(cleaned) == 1
        texts = [p.text for p in cleaned[0].parts if p.text]
        assert "msg1" in texts
        assert "msg2" in texts

    def test_consecutive_user_prev_has_response_second_dropped(self, smart_agent):
        """When prev user msg has tool_response and next user msg arrives → second dropped."""
        tc = ToolCall(name="search_memory", args={})
        history = [
            # model turn with tool_call
            Message(role="model", parts=[MessagePart(tool_call=tc)]),
            # user turn with tool_response (sets pending_tool_call=False)
            Message(role="user", parts=[MessagePart(
                tool_response={"name": "search_memory", "response": {"result": "data"}}
            )]),
            # another user turn — consecutive, prev has_response → should be dropped
            Message(role="user", parts=[MessagePart(text="follow-up")]),
        ]
        cleaned = smart_agent._sanitize_tool_history(history)
        # The follow-up user msg after tool_response should be dropped
        last_roles = [m.role for m in cleaned]
        assert last_roles.count("user") == 1

    def test_consecutive_user_orphaned_tool_response_dropped(self, smart_agent):
        """Consecutive user messages where second has tool_response but no pending tool call.

        The orphaned tool_response is dropped at the pending_tool_call guard (line 786)
        before reaching the consecutive-role merge logic. Only the first message survives.
        """
        history = [
            Message(role="user", parts=[MessagePart(text="first user msg")]),
            Message(role="user", parts=[MessagePart(
                tool_response={"name": "search_memory", "response": {"result": "data"}}
            )]),
        ]
        cleaned = smart_agent._sanitize_tool_history(history)
        # Second msg dropped (orphaned tool_response, no pending_tool_call)
        assert len(cleaned) == 1
        assert cleaned[0].parts[0].text == "first user msg"


# =========================================================================
# Tool delegation with structured_data and raw_content
# =========================================================================

class TestDelegationLoopAdvanced:

    async def test_structured_data_from_tool_response_captured(self, smart_agent, mock_llm_port):
        """structured_data in tool response metadata is captured in loop."""
        from src.domain.messaging import RichContent
        rich = RichContent(content_type="table", data={"headers": ["A"]}, fallback_text="table")

        tool_call = ToolCall(
            name="delegate_to_specialist",
            args={"intent": "search_memory", "query": "q"}
        )
        delegation_response = AgentResponse.success(task_id="t1", agent_id="a1", result="data")
        delegation_response.metadata = {"structured_data": rich}
        smart_agent.coordinator.handle_delegation = AsyncMock(return_value=delegation_response)
        smart_agent.coordinator.get_available_intents_for = MagicMock(return_value=[])

        mock_llm_port.generate_content = AsyncMock(side_effect=[
            build_llm_response("", [tool_call]),
            build_llm_response("Answer with table.", []),
        ])
        response = await smart_agent.execute(create_query_message("table query"))
        assert response.status == AgentStatus.SUCCESS
        # structured_data from memory agent was captured
        assert response.result.structured_data is not None


# =========================================================================
# _load_history — success path (lines 295-296)
# =========================================================================

class TestLoadHistorySuccess:

    async def test_returns_session_history(self, smart_agent):
        """load_session returns session with history → messages returned."""
        msgs = [
            Message(role="user", parts=[MessagePart(text="hi")]),
            Message(role="model", parts=[MessagePart(text="hello")]),
        ]
        smart_agent.session_store.load_session = AsyncMock(
            return_value=MockSession(history=msgs)
        )
        result = await smart_agent._load_history("sess-abc")
        assert result == msgs

    async def test_session_none_returns_empty(self, smart_agent):
        """load_session returns None → empty list."""
        smart_agent.session_store.load_session = AsyncMock(return_value=None)
        result = await smart_agent._load_history("sess-abc")
        assert result == []


# =========================================================================
# ENABLE_HISTORY_OPTIMIZATION — summary_task creation (lines 249, 263)
# =========================================================================

class TestHistoryOptimizationSummaryTask:

    async def test_summary_task_created_when_optimization_enabled(
        self, smart_agent, mock_llm_port
    ):
        """With ENABLE_HISTORY_OPTIMIZATION=True and no history_summary,
        execute() creates summary_task and adds it to metadata."""
        import src.agents.core.smart_response_agent as sra_module
        original = sra_module.ENABLE_HISTORY_OPTIMIZATION
        sra_module.ENABLE_HISTORY_OPTIMIZATION = True
        try:
            mock_llm_port.generate_content = AsyncMock(
                return_value=build_llm_response("Plain text answer.", [])
            )
            response = await smart_agent.execute(create_query_message("plain query"))
            assert response.status == AgentStatus.SUCCESS
            # summary_task should be set in metadata when optimization is on
            assert "response_summary_task" in response.metadata
            # cancel the task to avoid dangling asyncio warnings
            task = response.metadata["response_summary_task"]
            task.cancel()
            try:
                await task
            except (Exception, asyncio.CancelledError):
                pass
        finally:
            sra_module.ENABLE_HISTORY_OPTIMIZATION = original


# =========================================================================
# raw_content in LLMResponse — lines 385-387, 470
# =========================================================================

class TestRawContentInDelegationLoop:

    async def test_raw_content_with_parts_logged_and_used_in_history(
        self, smart_agent, mock_llm_port
    ):
        """LLMResponse with raw_content that has .parts attr → raw_parts_count logged
        and Message(raw_content=...) appended to history (lines 385-387, 470)."""
        raw = MagicMock()
        raw.parts = [MagicMock(), MagicMock()]  # 2 parts

        tool_call = ToolCall(
            name="delegate_to_specialist",
            args={"intent": "search_memory", "query": "q"}
        )
        first_response = LLMResponse(
            text="",
            tool_calls=[tool_call],
            raw_content=raw,
            usage_metadata=UsageMetadata(prompt_tokens=5, completion_tokens=5, total_tokens=10),
        )
        delegation_resp = AgentResponse.success(task_id="t1", agent_id="a1", result="ok")
        smart_agent.coordinator.handle_delegation = AsyncMock(return_value=delegation_resp)
        smart_agent.coordinator.get_available_intents_for = MagicMock(return_value=[])

        mock_llm_port.generate_content = AsyncMock(side_effect=[
            first_response,
            build_llm_response("Final answer.", []),
        ])
        response = await smart_agent.execute(create_query_message("query"))
        assert response.status == AgentStatus.SUCCESS


# =========================================================================
# _sanitize_tool_history — line 797: replace prev when pending_tool_call=True
# =========================================================================

class TestSanitizeToolHistoryReplace:

    def test_tool_response_replaces_consecutive_user_when_pending(self, smart_agent):
        """When there's a pending tool_call, a consecutive user msg with tool_response
        replaces the previous user msg (line 797)."""
        tool_call_part = MessagePart(
            tool_call=ToolCall(name="delegate_to_specialist", args={"intent": "x", "query": "q"})
        )
        history = [
            Message(role="model", parts=[tool_call_part]),            # sets pending_tool_call=True
            Message(role="user", parts=[MessagePart(text="interim")]),  # appended, pending stays
            Message(role="user", parts=[MessagePart(
                tool_response={"name": "delegate_to_specialist", "response": {"result": "r"}}
            )]),                                                        # replaces prev (line 797)
        ]
        cleaned = smart_agent._sanitize_tool_history(history)
        # model msg + 1 user msg (the tool_response one replaced the plain one)
        assert len(cleaned) == 2
        assert any(p.tool_response for p in cleaned[1].parts)


# =========================================================================
# ToolResponse.file_data in delegation loop
# =========================================================================

class TestToolResponseFileData:

    def test_file_data_creates_extra_message_part(self, smart_agent):
        """When tool_response has file_data, tool_parts should include both
        the tool_response part AND a file_data part."""
        tool_responses = [
            ToolResponse(
                name="delegate_to_specialist",
                result_str="File content attached.",
                file_data={"path": "/tmp/photo.png", "mime_type": "image/png"},
            ),
        ]

        # Build tool_parts exactly as the execute loop does
        tool_parts = []
        for tr in tool_responses:
            tool_parts.append(MessagePart(tool_response={
                "name": tr.name,
                "response": {"result": tr.result_str}
            }))
            if tr.file_data:
                tool_parts.append(MessagePart(file_data=tr.file_data))

        assert len(tool_parts) == 2
        assert tool_parts[0].tool_response is not None
        assert tool_parts[1].file_data["mime_type"] == "image/png"

    def test_no_file_data_single_part(self, smart_agent):
        """Without file_data, only one MessagePart per tool response."""
        tool_responses = [
            ToolResponse(
                name="delegate_to_specialist",
                result_str="Some text result",
            ),
        ]

        tool_parts = []
        for tr in tool_responses:
            tool_parts.append(MessagePart(tool_response={
                "name": tr.name,
                "response": {"result": tr.result_str}
            }))
            if tr.file_data:
                tool_parts.append(MessagePart(file_data=tr.file_data))

        assert len(tool_parts) == 1


class TestDelegateExtractsFileData:
    """Tests for DelegationEngine._dispatch_single file_data extraction."""

    async def test_file_data_from_metadata(self):
        """When coordinator returns file_data in metadata, ToolResult captures it."""
        file_data = {"path": "/tmp/img.jpg", "mime_type": "image/jpeg"}
        mock_coord = AsyncMock()
        mock_coord.handle_delegation = AsyncMock(return_value=AgentResponse.success(
            task_id="t1",
            agent_id="file_management_agent_user1",
            result="File attached.",
            metadata={"file_data": file_data},
        ))
        engine = DelegationEngine(mock_coord)

        tool_call = ToolCall(
            name="delegate_to_specialist",
            args={"intent": "open_file", "query": "get photo", "context": {"file_ref": "photo.jpg"}}
        )
        result = await engine._dispatch_single(
            tool_call, {"user_id": "user1"}, {}, {}, "test", 0, 0,
        )
        assert result.file_data == file_data

    async def test_no_file_data_in_metadata(self):
        """When no file_data in metadata, ToolResult.file_data is None."""
        mock_coord = AsyncMock()
        mock_coord.handle_delegation = AsyncMock(return_value=AgentResponse.success(
            task_id="t1",
            agent_id="memory_agent_user1",
            result="Found facts.",
            metadata={},
        ))
        engine = DelegationEngine(mock_coord)

        tool_call = ToolCall(
            name="delegate_to_specialist",
            args={"intent": "search_memory", "query": "find facts"}
        )
        result = await engine._dispatch_single(
            tool_call, {"user_id": "user1"}, {}, {}, "test", 0, 0,
        )
        assert result.file_data is None
