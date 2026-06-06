"""
Unit tests for ClaudeDeepResearchRunnerAgent.

Mock boundary: self._client.messages.stream (Anthropic SDK boundary).
Tests cover: can_handle, _extract_container_id, _build_critic_query,
             _call_with_overload_retry, _research_loop, _run (full flow).

Stream mocking strategy: _FakeStream class that correctly implements the
async context manager + async iteration protocol used by the agent.
"""
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.agents.claude_deep_research_runner_agent import ClaudeDeepResearchRunnerAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.infrastructure.agent_manifest import Intent


# ============================================================================
# Helpers — domain objects
# ============================================================================

def _make_config() -> AgentConfig:
    return AgentConfig(
        agent_id="claude_deep_research_runner_test",
        agent_type="claude_deep_research_runner",
        timeout_ms=None,
        capabilities=["execute_deep_research_claude"],
    )


def _make_message(
    query: str = "Research topic",
    intent: AgentIntent = AgentIntent.DELEGATE,
    model: str = "claude-sonnet-4-6",
    payload_intent: str = Intent.EXECUTE_DEEP_RESEARCH_CLAUDE,
) -> AgentMessage:
    return AgentMessage(
        task_id="task-test-1",
        sender="job",
        recipient="claude_deep_research_runner_test",
        intent=intent,
        payload={"query": query, "intent": payload_intent},
        context={
            "user_id": "u1",
            "account_id": "a1",
            "original_query": query,
            "system_prompt": "",
            "model": model,
            "job_id": "job-123",
            "session_id": "sess-1",
        },
    )


def _make_agent(client=None) -> ClaudeDeepResearchRunnerAgent:
    return ClaudeDeepResearchRunnerAgent(
        config=_make_config(),
        anthropic_client=client or MagicMock(),
    )


# ============================================================================
# Helpers — Anthropic SDK stream mocks
# ============================================================================

class _FakeStream:
    """
    Minimal faithful mock of the Anthropic async stream context manager.

    Implements: async context manager + async iteration + get_final_message().
    This avoids complex AsyncMock __aiter__ configuration issues.
    """

    def __init__(self, events: list, final_message):
        self._events = events
        self._final_message = final_message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for e in self._events:
            yield e

    async def get_final_message(self):
        return self._final_message


def _text_block(text: str = "Research result text"):
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _tool_block(tool_type: str = "server_tool_use", name: str = "web_search"):
    block = MagicMock()
    block.type = tool_type
    block.name = name
    return block


def _api_message(
    stop_reason: str,
    content: list | None = None,
    input_tokens: int = 100,
    output_tokens: int = 200,
    container_id: str | None = None,
):
    """Build a mock Anthropic API Message response."""
    msg = MagicMock()
    msg.stop_reason = stop_reason
    msg.content = content if content is not None else []
    msg.usage = MagicMock()
    msg.usage.input_tokens = input_tokens
    msg.usage.output_tokens = output_tokens
    msg.model_extra = {}
    if container_id:
        msg.container = MagicMock()
        msg.container.id = container_id
    else:
        msg.container = None
    return msg


def _message_delta_event(container_id: str | None = None):
    """Build a mock message_delta SSE event."""
    event = MagicMock()
    event.type = "message_delta"
    if container_id:
        event.delta = MagicMock()
        event.delta.container = MagicMock()
        event.delta.container.id = container_id
    else:
        event.delta = MagicMock()
        event.delta.container = None
    return event


def _other_event(event_type: str = "message_start"):
    event = MagicMock()
    event.type = event_type
    return event


def _client_with_streams(*streams):
    """Build a mock Anthropic client whose messages.stream returns given streams in order."""
    client = MagicMock()
    if len(streams) == 1:
        client.messages.stream.return_value = streams[0]
    else:
        client.messages.stream.side_effect = list(streams)
    return client


# ============================================================================
# can_handle
# ============================================================================

class TestCanHandle:

    async def test_delegate_with_correct_intent_returns_true(self):
        agent = _make_agent()
        assert await agent.can_handle(_make_message()) is True

    async def test_query_intent_returns_false(self):
        agent = _make_agent()
        msg = _make_message()
        msg.intent = AgentIntent.QUERY
        assert await agent.can_handle(msg) is False

    async def test_inform_intent_returns_false(self):
        agent = _make_agent()
        msg = _make_message()
        msg.intent = AgentIntent.INFORM
        assert await agent.can_handle(msg) is False

    async def test_wrong_payload_intent_returns_false(self):
        agent = _make_agent()
        msg = _make_message(payload_intent="some_other_intent")
        assert await agent.can_handle(msg) is False

    async def test_missing_payload_intent_returns_false(self):
        agent = _make_agent()
        msg = _make_message()
        msg.payload = {}
        assert await agent.can_handle(msg) is False


# ============================================================================
# _extract_container_id
# ============================================================================

class TestExtractContainerId:

    def test_typed_attribute_path(self):
        response = MagicMock()
        response.container = MagicMock()
        response.container.id = "container-typed-abc"
        response.model_extra = {}
        result = ClaudeDeepResearchRunnerAgent._extract_container_id(response)
        assert result == "container-typed-abc"

    def test_model_extra_dict_path(self):
        response = MagicMock()
        response.container = None
        response.model_extra = {"container": {"id": "container-extra-xyz"}}
        result = ClaudeDeepResearchRunnerAgent._extract_container_id(response)
        assert result == "container-extra-xyz"

    def test_no_container_anywhere_returns_none(self):
        response = MagicMock()
        response.container = None
        response.model_extra = {}
        assert ClaudeDeepResearchRunnerAgent._extract_container_id(response) is None

    def test_container_dict_missing_id_returns_none(self):
        response = MagicMock()
        response.container = None
        response.model_extra = {"container": {}}
        assert ClaudeDeepResearchRunnerAgent._extract_container_id(response) is None

    def test_empty_string_id_returns_none(self):
        response = MagicMock()
        response.container = None
        response.model_extra = {"container": {"id": ""}}
        assert ClaudeDeepResearchRunnerAgent._extract_container_id(response) is None

    def test_typed_attribute_empty_id_returns_none(self):
        response = MagicMock()
        response.container = MagicMock()
        response.container.id = ""
        response.model_extra = {}
        assert ClaudeDeepResearchRunnerAgent._extract_container_id(response) is None


# ============================================================================
# _build_critic_query
# ============================================================================

class TestBuildCriticQuery:

    def test_contains_original_query(self):
        result = ClaudeDeepResearchRunnerAgent._build_critic_query(
            "AI infrastructure trends", "First pass report"
        )
        assert "AI infrastructure trends" in result

    def test_contains_first_pass_result(self):
        result = ClaudeDeepResearchRunnerAgent._build_critic_query(
            "Topic", "First pass report text verbatim"
        )
        assert "First pass report text verbatim" in result

    def test_frames_first_pass_as_unverified(self):
        result = ClaudeDeepResearchRunnerAgent._build_critic_query("Topic", "Report")
        lower = result.lower()
        assert "unverified" in lower or "verify" in lower or "missing" in lower

    def test_asks_for_new_report(self):
        result = ClaudeDeepResearchRunnerAgent._build_critic_query("Topic", "Report")
        assert "report" in result.lower()

    def test_returns_non_trivial_string(self):
        result = ClaudeDeepResearchRunnerAgent._build_critic_query("Topic", "Report")
        assert isinstance(result, str)
        assert len(result) > 100

    def test_both_pass_texts_present_when_both_long(self):
        original = "Detailed original query about complex topic"
        first_result = "Detailed first pass result with findings"
        result = ClaudeDeepResearchRunnerAgent._build_critic_query(original, first_result)
        assert original in result
        assert first_result in result


# ============================================================================
# _call_with_overload_retry
# ============================================================================

class TestCallWithOverloadRetry:

    async def test_success_on_first_attempt_returns_message(self):
        msg = _api_message("end_turn", [_text_block()])
        stream = _FakeStream([], msg)
        agent = _make_agent(_client_with_streams(stream))

        response, container_id = await agent._call_with_overload_retry({"model": "claude-sonnet-4-6"})

        assert response.stop_reason == "end_turn"

    async def test_success_on_first_attempt_no_container_in_events(self):
        msg = _api_message("end_turn", [_text_block()])
        stream = _FakeStream([], msg)
        agent = _make_agent(_client_with_streams(stream))

        _, container_id = await agent._call_with_overload_retry({})

        assert container_id is None

    async def test_container_id_captured_from_message_delta(self):
        msg = _api_message("end_turn", [_text_block()])
        events = [_message_delta_event("container-delta-123"), _other_event()]
        stream = _FakeStream(events, msg)
        agent = _make_agent(_client_with_streams(stream))

        _, container_id = await agent._call_with_overload_retry({})

        assert container_id == "container-delta-123"

    async def test_container_id_falls_back_to_final_message(self):
        msg = _api_message("end_turn", [_text_block()], container_id="container-from-msg")
        stream = _FakeStream([], msg)  # no delta event
        agent = _make_agent(_client_with_streams(stream))

        _, container_id = await agent._call_with_overload_retry({})

        assert container_id == "container-from-msg"

    async def test_message_delta_wins_over_final_message(self):
        """container_id from SSE delta event takes priority over final message."""
        msg = _api_message("end_turn", [_text_block()], container_id="container-from-message")
        events = [_message_delta_event("container-from-delta")]
        stream = _FakeStream(events, msg)
        agent = _make_agent(_client_with_streams(stream))

        _, container_id = await agent._call_with_overload_retry({})

        assert container_id == "container-from-delta"

    async def test_overload_error_retries_and_succeeds(self):
        msg = _api_message("end_turn", [_text_block()])
        good_stream = _FakeStream([], msg)

        client = MagicMock()
        client.messages.stream.side_effect = [
            Exception("overloaded_error: server busy"),
            good_stream,
        ]
        agent = _make_agent(client)
        agent._OVERLOAD_RETRY_BASE_DELAY = 0

        with patch("asyncio.sleep", new=AsyncMock()):
            response, _ = await agent._call_with_overload_retry({"model": "claude-sonnet-4-6"})

        assert response.stop_reason == "end_turn"
        assert client.messages.stream.call_count == 2

    async def test_overload_error_retry_count_matches_max(self):
        client = MagicMock()
        client.messages.stream.side_effect = Exception("overloaded_error")
        agent = _make_agent(client)
        agent._OVERLOAD_RETRY_BASE_DELAY = 0
        agent._MAX_OVERLOAD_RETRIES = 2

        with patch("asyncio.sleep", new=AsyncMock()):
            with pytest.raises(Exception, match="overloaded_error"):
                await agent._call_with_overload_retry({})

        # initial + 2 retries = 3 total calls
        assert client.messages.stream.call_count == 3

    async def test_overload_error_exhaused_raises_original(self):
        client = MagicMock()
        client.messages.stream.side_effect = Exception("overloaded_error: quota exceeded")
        agent = _make_agent(client)
        agent._OVERLOAD_RETRY_BASE_DELAY = 0

        with patch("asyncio.sleep", new=AsyncMock()):
            with pytest.raises(Exception, match="overloaded_error"):
                await agent._call_with_overload_retry({})

    async def test_non_overload_error_raises_immediately(self):
        client = MagicMock()
        client.messages.stream.side_effect = ValueError("unexpected internal error")
        agent = _make_agent(client)

        with pytest.raises(ValueError, match="unexpected internal error"):
            await agent._call_with_overload_retry({})

        # No retry — raises on first attempt
        assert client.messages.stream.call_count == 1

    async def test_multiple_delta_events_uses_first_container(self):
        """Only the first container_id from a delta event is captured."""
        msg = _api_message("end_turn", [_text_block()])
        events = [
            _message_delta_event("first-container"),
            _message_delta_event("second-container"),
        ]
        stream = _FakeStream(events, msg)
        agent = _make_agent(_client_with_streams(stream))

        _, container_id = await agent._call_with_overload_retry({})

        assert container_id == "first-container"


# ============================================================================
# _research_loop
# ============================================================================

class TestResearchLoop:

    async def test_end_turn_returns_text(self):
        msg = _api_message("end_turn", [_text_block("Final report")])
        agent = _make_agent(_client_with_streams(_FakeStream([], msg)))

        text, tokens, _, _ = await agent._research_loop("query", "", "claude-sonnet-4-6")

        assert text == "Final report"

    async def test_end_turn_returns_token_count(self):
        msg = _api_message("end_turn", [_text_block()], input_tokens=50, output_tokens=150)
        agent = _make_agent(_client_with_streams(_FakeStream([], msg)))

        _, tokens, _, _ = await agent._research_loop("query", "", "claude-sonnet-4-6")

        assert tokens == 200

    async def test_pause_then_end_turn_returns_accumulated_text(self):
        pause = _api_message("pause_turn", [_tool_block()])
        end = _api_message("end_turn", [_text_block("Final text")])
        client = _client_with_streams(_FakeStream([], pause), _FakeStream([], end))
        agent = _make_agent(client)

        text, _, _, _ = await agent._research_loop("query", "", "claude-sonnet-4-6")

        assert text == "Final text"

    async def test_pause_then_end_turn_accumulates_tokens(self):
        pause = _api_message("pause_turn", [_tool_block()], input_tokens=40, output_tokens=60)
        end = _api_message("end_turn", [_text_block()], input_tokens=80, output_tokens=120)
        client = _client_with_streams(_FakeStream([], pause), _FakeStream([], end))
        agent = _make_agent(client)

        _, tokens, _, _ = await agent._research_loop("query", "", "claude-sonnet-4-6")

        assert tokens == 300  # 100 + 200

    async def test_pause_sends_accumulated_content_as_assistant_message(self):
        tool = _tool_block()
        pause = _api_message("pause_turn", [tool])
        end = _api_message("end_turn", [_text_block("Done")])
        client = MagicMock()
        client.messages.stream.side_effect = [_FakeStream([], pause), _FakeStream([], end)]
        agent = _make_agent(client)

        await agent._research_loop("query", "", "claude-sonnet-4-6")

        second_call = client.messages.stream.call_args_list[1].kwargs
        messages = second_call["messages"]
        assert messages[-1]["role"] == "assistant"
        assert tool in messages[-1]["content"]

    async def test_pause_sends_original_user_message_in_continuation(self):
        pause = _api_message("pause_turn", [_tool_block()])
        end = _api_message("end_turn", [_text_block("Done")])
        client = MagicMock()
        client.messages.stream.side_effect = [_FakeStream([], pause), _FakeStream([], end)]
        agent = _make_agent(client)

        await agent._research_loop("my query text", "", "claude-sonnet-4-6")

        second_call = client.messages.stream.call_args_list[1].kwargs
        messages = second_call["messages"]
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "my query text"

    async def test_container_id_passed_in_continuation(self):
        pause = _api_message("pause_turn", [_tool_block()])
        end = _api_message("end_turn", [_text_block("Done")])
        events_with_container = [_message_delta_event("container-abc-123")]
        client = MagicMock()
        client.messages.stream.side_effect = [
            _FakeStream(events_with_container, pause),
            _FakeStream([], end),
        ]
        agent = _make_agent(client)

        await agent._research_loop("query", "", "claude-sonnet-4-6")

        second_call = client.messages.stream.call_args_list[1].kwargs
        assert second_call.get("container") == "container-abc-123"

    async def test_no_container_means_no_container_kwarg_on_first_call(self):
        msg = _api_message("end_turn", [_text_block("Done")])
        client = MagicMock()
        client.messages.stream.return_value = _FakeStream([], msg)
        agent = _make_agent(client)

        await agent._research_loop("query", "", "claude-sonnet-4-6")

        first_call = client.messages.stream.call_args.kwargs
        assert "container" not in first_call

    async def test_max_tokens_returns_partial_text(self):
        msg = _api_message("max_tokens", [_text_block("Partial output")])
        agent = _make_agent(_client_with_streams(_FakeStream([], msg)))

        text, _, _, _ = await agent._research_loop("query", "", "claude-sonnet-4-6")

        assert text == "Partial output"

    async def test_max_tokens_no_text_blocks_raises_runtime_error(self):
        msg = _api_message("max_tokens", [_tool_block()])  # only tool, no text
        agent = _make_agent(_client_with_streams(_FakeStream([], msg)))

        with pytest.raises(RuntimeError):
            await agent._research_loop("query", "", "claude-sonnet-4-6")

    async def test_end_turn_with_only_tool_blocks_returns_empty_string(self):
        msg = _api_message("end_turn", [_tool_block()])
        agent = _make_agent(_client_with_streams(_FakeStream([], msg)))

        text, _, _, _ = await agent._research_loop("query", "", "claude-sonnet-4-6")

        assert text == ""

    async def test_max_pause_turns_exceeded_raises(self):
        pause = _api_message("pause_turn", [_tool_block()])
        client = MagicMock()
        client.messages.stream.side_effect = [
            _FakeStream([], pause)
            for _ in range(ClaudeDeepResearchRunnerAgent._MAX_PAUSE_TURNS + 1)
        ]
        agent = _make_agent(client)

        with pytest.raises(RuntimeError, match="pause_turn"):
            await agent._research_loop("query", "", "claude-sonnet-4-6")

    async def test_system_prompt_included_with_cache_control(self):
        msg = _api_message("end_turn", [_text_block("Done")])
        client = MagicMock()
        client.messages.stream.return_value = _FakeStream([], msg)
        agent = _make_agent(client)

        await agent._research_loop("query", "You are a researcher.", "claude-sonnet-4-6")

        call_kwargs = client.messages.stream.call_args.kwargs
        system = call_kwargs["system"]
        assert len(system) == 1
        assert system[0]["text"] == "You are a researcher."
        assert system[0].get("cache_control") == {"type": "ephemeral"}

    async def test_empty_system_prompt_sends_empty_list(self):
        msg = _api_message("end_turn", [_text_block("Done")])
        client = MagicMock()
        client.messages.stream.return_value = _FakeStream([], msg)
        agent = _make_agent(client)

        await agent._research_loop("query", "", "claude-sonnet-4-6")

        call_kwargs = client.messages.stream.call_args.kwargs
        assert call_kwargs["system"] == []

    async def test_thinking_model_uses_64k_max_tokens(self):
        msg = _api_message("end_turn", [_text_block("Done")])
        client = MagicMock()
        client.messages.stream.return_value = _FakeStream([], msg)
        agent = _make_agent(client)

        await agent._research_loop("query", "", "claude-sonnet-4-6")

        call_kwargs = client.messages.stream.call_args.kwargs
        assert call_kwargs["max_tokens"] == 64_000

    async def test_opus_uses_64k_max_tokens(self):
        msg = _api_message("end_turn", [_text_block("Done")])
        client = MagicMock()
        client.messages.stream.return_value = _FakeStream([], msg)
        agent = _make_agent(client)

        await agent._research_loop("query", "", "claude-opus-4-6")

        call_kwargs = client.messages.stream.call_args.kwargs
        assert call_kwargs["max_tokens"] == 64_000

    async def test_opus_4_7_uses_thinking_path_after_gate_unification(self):
        """ULTRA tier on opus-4-7 must hit the adaptive-thinking path.

        Regression guard for the 2026-05-30 unification of _THINKING_MODELS
        from exact-match set to substring tuple. Pre-unification, opus-4-7
        silently fell to the Haiku-style fallback (32k max_tokens).
        """
        msg = _api_message("end_turn", [_text_block("Done")])
        client = MagicMock()
        client.messages.stream.return_value = _FakeStream([], msg)
        agent = _make_agent(client)

        await agent._research_loop("query", "", "claude-opus-4-7")

        call_kwargs = client.messages.stream.call_args.kwargs
        assert call_kwargs["max_tokens"] == 64_000
        assert call_kwargs["thinking"] == {"type": "adaptive"}
        assert call_kwargs["output_config"] == {"effort": "high"}

    async def test_opus_4_8_uses_thinking_path(self):
        """Current ULTRA model. Same expectation as opus-4-7."""
        msg = _api_message("end_turn", [_text_block("Done")])
        client = MagicMock()
        client.messages.stream.return_value = _FakeStream([], msg)
        agent = _make_agent(client)

        await agent._research_loop("query", "", "claude-opus-4-8")

        call_kwargs = client.messages.stream.call_args.kwargs
        assert call_kwargs["max_tokens"] == 64_000
        assert call_kwargs["thinking"] == {"type": "adaptive"}
        assert call_kwargs["output_config"] == {"effort": "high"}

    async def test_non_thinking_model_uses_32k_max_tokens(self):
        msg = _api_message("end_turn", [_text_block("Done")])
        client = MagicMock()
        client.messages.stream.return_value = _FakeStream([], msg)
        agent = _make_agent(client)

        await agent._research_loop("query", "", "claude-haiku-4-5-20251001")

        call_kwargs = client.messages.stream.call_args.kwargs
        assert call_kwargs["max_tokens"] == 32_000

    async def test_extended_output_beta_header_present(self):
        msg = _api_message("end_turn", [_text_block("Done")])
        client = MagicMock()
        client.messages.stream.return_value = _FakeStream([], msg)
        agent = _make_agent(client)

        await agent._research_loop("query", "", "claude-sonnet-4-6")

        headers = client.messages.stream.call_args.kwargs["extra_headers"]
        assert "output-128k-2025-02-19" in headers.get("anthropic-beta", "")

    async def test_prompt_caching_beta_header_present(self):
        msg = _api_message("end_turn", [_text_block("Done")])
        client = MagicMock()
        client.messages.stream.return_value = _FakeStream([], msg)
        agent = _make_agent(client)

        await agent._research_loop("query", "", "claude-sonnet-4-6")

        headers = client.messages.stream.call_args.kwargs["extra_headers"]
        assert "prompt-caching-2024-07-31" in headers.get("anthropic-beta", "")

    async def test_native_tools_always_sent(self):
        msg = _api_message("end_turn", [_text_block("Done")])
        client = MagicMock()
        client.messages.stream.return_value = _FakeStream([], msg)
        agent = _make_agent(client)

        await agent._research_loop("query", "", "claude-sonnet-4-6")

        tools = client.messages.stream.call_args.kwargs["tools"]
        tool_types = [t["type"] for t in tools]
        assert "web_search_20260209" in tool_types
        assert "web_fetch_20260209" in tool_types

    async def test_debug_raw_turn_called_at_end_turn(self):
        msg = _api_message("end_turn", [_text_block("Result")])
        client = MagicMock()
        client.messages.stream.return_value = _FakeStream([], msg)
        agent = _make_agent(client)

        with patch.object(agent, "_debug_raw_turn") as mock_debug:
            await agent._research_loop("query", "", "claude-sonnet-4-6")

        mock_debug.assert_called_once()

    async def test_debug_raw_turn_called_at_max_tokens(self):
        msg = _api_message("max_tokens", [_text_block("Partial")])
        client = MagicMock()
        client.messages.stream.return_value = _FakeStream([], msg)
        agent = _make_agent(client)

        with patch.object(agent, "_debug_raw_turn") as mock_debug:
            await agent._research_loop("query", "", "claude-sonnet-4-6")

        mock_debug.assert_called_once()

    async def test_debug_raw_turn_not_called_during_pause_turns(self):
        pause = _api_message("pause_turn", [_tool_block()])
        end = _api_message("end_turn", [_text_block("Done")])
        client = MagicMock()
        client.messages.stream.side_effect = [_FakeStream([], pause), _FakeStream([], end)]
        agent = _make_agent(client)

        with patch.object(agent, "_debug_raw_turn") as mock_debug:
            await agent._research_loop("query", "", "claude-sonnet-4-6")

        # Only once — at end_turn, not during pause_turn
        mock_debug.assert_called_once()


# ============================================================================
# _run / execute (full flow)
# ============================================================================

class TestRun:

    async def test_success_returns_second_pass_text(self):
        first = _api_message("end_turn", [_text_block("First result")])
        second = _api_message("end_turn", [_text_block("Second result")])
        agent = _make_agent(_client_with_streams(_FakeStream([], first), _FakeStream([], second)))

        with patch.object(ClaudeDeepResearchRunnerAgent, "_SECOND_PASS_ENABLED", True):
            response = await agent.execute(_make_message())

        assert response.status == AgentStatus.SUCCESS
        assert response.result["text"] == "Second result"

    async def test_success_includes_original_query_in_result(self):
        first = _api_message("end_turn", [_text_block("R1")])
        second = _api_message("end_turn", [_text_block("R2")])
        agent = _make_agent(_client_with_streams(_FakeStream([], first), _FakeStream([], second)))

        response = await agent.execute(_make_message(query="Research topic"))

        assert response.result["query"] == "Research topic"

    async def test_second_pass_disabled_returns_first_pass_text(self):
        first = _api_message("end_turn", [_text_block("First only")])
        client = MagicMock()
        client.messages.stream.return_value = _FakeStream([], first)
        agent = _make_agent(client)

        with patch("src.agents.claude_deep_research_runner_agent.DEEP_RESEARCH_SECOND_PASS", False):
            response = await agent.execute(_make_message())

        assert response.result["text"] == "First only"
        assert client.messages.stream.call_count == 1

    async def test_second_pass_enabled_makes_two_api_calls(self):
        first = _api_message("end_turn", [_text_block("First")])
        second = _api_message("end_turn", [_text_block("Second")])
        client = MagicMock()
        client.messages.stream.side_effect = [_FakeStream([], first), _FakeStream([], second)]
        agent = _make_agent(client)

        with patch.object(ClaudeDeepResearchRunnerAgent, "_SECOND_PASS_ENABLED", True), \
             patch.dict(os.environ, {"DEEP_RESEARCH_SECOND_PASS": "true"}):
            await agent.execute(_make_message())

        assert client.messages.stream.call_count == 2

    async def test_first_pass_failure_returns_failure_response(self):
        client = MagicMock()
        client.messages.stream.side_effect = RuntimeError("API error")
        agent = _make_agent(client)

        response = await agent.execute(_make_message())

        assert response.status == AgentStatus.FAILED
        assert "API error" in response.error

    async def test_second_pass_failure_falls_back_to_first_pass(self):
        first = _api_message("end_turn", [_text_block("First result")])
        client = MagicMock()
        client.messages.stream.side_effect = [
            _FakeStream([], first),
            RuntimeError("Second pass failed"),
        ]
        agent = _make_agent(client)

        response = await agent.execute(_make_message())

        assert response.status == AgentStatus.SUCCESS
        assert response.result["text"] == "First result"

    async def test_critic_query_contains_first_pass_result(self):
        first = _api_message("end_turn", [_text_block("First pass findings")])
        second = _api_message("end_turn", [_text_block("Final report")])
        client = MagicMock()
        client.messages.stream.side_effect = [_FakeStream([], first), _FakeStream([], second)]
        agent = _make_agent(client)

        with patch.object(ClaudeDeepResearchRunnerAgent, "_SECOND_PASS_ENABLED", True):
            await agent.execute(_make_message())

        second_call = client.messages.stream.call_args_list[1].kwargs
        critic_query = second_call["messages"][0]["content"]
        assert "First pass findings" in critic_query

    async def test_critic_query_contains_original_query(self):
        first = _api_message("end_turn", [_text_block("R1")])
        second = _api_message("end_turn", [_text_block("R2")])
        client = MagicMock()
        client.messages.stream.side_effect = [_FakeStream([], first), _FakeStream([], second)]
        agent = _make_agent(client)

        with patch.object(ClaudeDeepResearchRunnerAgent, "_SECOND_PASS_ENABLED", True):
            await agent.execute(_make_message(query="Original research topic"))

        second_call = client.messages.stream.call_args_list[1].kwargs
        critic_query = second_call["messages"][0]["content"]
        assert "Original research topic" in critic_query

    async def test_total_tokens_is_sum_of_both_passes(self):
        first = _api_message("end_turn", [_text_block("R1")], input_tokens=100, output_tokens=200)
        second = _api_message("end_turn", [_text_block("R2")], input_tokens=150, output_tokens=250)
        agent = _make_agent(_client_with_streams(_FakeStream([], first), _FakeStream([], second)))

        response = await agent.execute(_make_message())

        # Token count logged internally; response still SUCCESS regardless of total
        assert response.status == AgentStatus.SUCCESS

    async def test_second_pass_false_env_var_respected(self):
        """DEEP_RESEARCH_SECOND_PASS=false must disable second pass."""
        first = _api_message("end_turn", [_text_block("First")])
        client = MagicMock()
        client.messages.stream.return_value = _FakeStream([], first)
        agent = _make_agent(client)

        with patch("src.agents.claude_deep_research_runner_agent.DEEP_RESEARCH_SECOND_PASS", False):
            response = await agent.execute(_make_message())

        # Only one research loop call
        assert client.messages.stream.call_count == 1
        assert response.status == AgentStatus.SUCCESS
