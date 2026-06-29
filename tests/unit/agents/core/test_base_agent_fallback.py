"""
Unit tests for BaseAgent._call_llm fallback provider logic.

Covers:
- LLMRateLimitError → transparent retry with fallback_provider
- LLMUnavailableError → transparent retry with fallback_provider
- No fallback configured → original exception propagates
- Fallback uses fallback_model_name in request
- Structured log event="llm_fallback" is emitted
- Non-transient exception (RuntimeError) → not caught, propagates as-is
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass, field
from typing import Optional, List

from src.agents.base_agent import BaseAgent
from src.domain.agent import AgentMessage, AgentResponse, AgentConfig
from src.domain.exceptions import (
    BothProvidersUnavailableError,
    LLMNetworkError,
    LLMRateLimitError,
    LLMServerError,
    LLMTimeoutError,
    LLMUnavailableError,
    ProviderBreakerOpenError,
    TranscriptLockedError,
)
from src.ports.llm_port import (
    LLMPort, LLMRequest, LLMResponse, ProviderCapabilities, Message, MessagePart,
    UsageMetadata,
)
from src.ports.provider_resilience_port import ProviderResiliencePort
from src.domain.user import PerformanceTier
from src.services.agent_context_builder import AgentExecutionContext
from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience


# ============================================================================
# Minimal concrete agent for testing (only _call_llm matters)
# ============================================================================

class _MinimalAgent(BaseAgent):
    """Concrete subclass with trivial can_handle/execute — only used to reach _call_llm."""

    def __init__(self, config: AgentConfig, llm: LLMPort):
        super().__init__(config)
        self.llm = llm  # BaseAgent._call_llm looks for self.llm first

    async def can_handle(self, message: AgentMessage) -> bool:
        return True

    async def execute(self, message: AgentMessage) -> AgentResponse:
        raise NotImplementedError("not used in these tests")


# ============================================================================
# Helpers
# ============================================================================

def _make_config() -> AgentConfig:
    return AgentConfig(
        agent_id="test_agent",
        agent_type="quick",
    )


def _make_request() -> LLMRequest:
    return LLMRequest(
        model_name="gemini-flash",
        system_instruction="test",
        messages=[Message(role="user", parts=[MessagePart(text="hi")])],
    )


def _make_llm_response(text: str = "ok") -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=[],
        raw_content=None,
        usage_metadata=UsageMetadata(prompt_tokens=5, completion_tokens=3, total_tokens=8),
    )


def _make_execution_context(
    primary_llm: LLMPort,
    fallback_llm: Optional[LLMPort] = None,
    fallback_model: str = "claude-sonnet-4-6",
) -> AgentExecutionContext:
    return AgentExecutionContext(
        agent_type="quick",
        provider=primary_llm,
        model_name="gemini-flash",
        tier=PerformanceTier.BALANCED,
        capabilities=ProviderCapabilities(),
        provider_name="gemini",
        fallback_provider=fallback_llm,
        fallback_model_name=fallback_model if fallback_llm else None,
        fallback_provider_name="claude" if fallback_llm else None,
        resilience_port=InMemoryProviderResilience(),
    )


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def primary_llm():
    llm = MagicMock(spec=LLMPort)
    llm.generate_content = AsyncMock(return_value=_make_llm_response("primary ok"))
    return llm


@pytest.fixture
def fallback_llm():
    llm = MagicMock(spec=LLMPort)
    llm.generate_content = AsyncMock(return_value=_make_llm_response("fallback ok"))
    return llm


@pytest.fixture
def agent(primary_llm) -> _MinimalAgent:
    return _MinimalAgent(config=_make_config(), llm=primary_llm)


# ============================================================================
# Tests
# ============================================================================

@pytest.mark.asyncio
async def test_rate_limit_triggers_fallback(agent, primary_llm, fallback_llm):
    """LLMRateLimitError from primary → fallback_provider called transparently."""
    primary_llm.generate_content = AsyncMock(
        side_effect=LLMRateLimitError("rate limit", http_status=429)
    )
    ctx = _make_execution_context(primary_llm, fallback_llm, fallback_model="claude-sonnet-4-6")
    agent._set_execution_context(ctx)

    response = await agent._call_llm(_make_request())

    assert response.text == "fallback ok"
    fallback_llm.generate_content.assert_called_once()
    sent_request: LLMRequest = fallback_llm.generate_content.call_args.kwargs["request"]
    assert isinstance(sent_request, LLMRequest)
    assert sent_request.messages[0].parts[0].text == "hi"  # original content preserved


@pytest.mark.asyncio
async def test_unavailable_triggers_fallback(agent, primary_llm, fallback_llm):
    """LLMUnavailableError from primary → fallback_provider called transparently."""
    primary_llm.generate_content = AsyncMock(
        side_effect=LLMUnavailableError("unavailable", http_status=503)
    )
    ctx = _make_execution_context(primary_llm, fallback_llm)
    agent._set_execution_context(ctx)

    response = await agent._call_llm(_make_request())

    assert response.text == "fallback ok"
    fallback_llm.generate_content.assert_called_once()
    sent_request: LLMRequest = fallback_llm.generate_content.call_args.kwargs["request"]
    assert isinstance(sent_request, LLMRequest)
    assert sent_request.messages[0].parts[0].text == "hi"  # original content preserved


@pytest.mark.asyncio
async def test_fallback_uses_fallback_model_name(agent, primary_llm, fallback_llm):
    """Fallback request must use fallback_model_name, not primary model_name."""
    primary_llm.generate_content = AsyncMock(
        side_effect=LLMRateLimitError("rate limit", http_status=429)
    )
    ctx = _make_execution_context(primary_llm, fallback_llm, fallback_model="claude-haiku-4-5-20251001")
    agent._set_execution_context(ctx)

    await agent._call_llm(_make_request())

    call_kwargs = fallback_llm.generate_content.call_args
    sent_request: LLMRequest = call_kwargs.kwargs.get("request") or call_kwargs.args[0]
    assert sent_request.model_name == "claude-haiku-4-5-20251001"


@pytest.mark.asyncio
async def test_no_fallback_configured_re_raises(agent, primary_llm):
    """When no fallback_provider set, primary failure surfaces as the terminal
    BothProvidersUnavailableError carrying the original cause. Replaces the
    old bare re-raise — uniform terminal type for failover-exhaustion."""
    exc = LLMRateLimitError("rate limit", http_status=429)
    primary_llm.generate_content = AsyncMock(side_effect=exc)
    ctx = _make_execution_context(primary_llm, fallback_llm=None)
    agent._set_execution_context(ctx)

    with pytest.raises(BothProvidersUnavailableError) as exc_info:
        await agent._call_llm(_make_request())
    assert exc_info.value.primary_cause is exc


@pytest.mark.asyncio
async def test_no_context_set_re_raises(agent, primary_llm):
    """When _set_execution_context was never called, primary failure surfaces as
    BothProvidersUnavailableError (uniform terminal type — caller cannot tell
    "no context" from "open fallback" without forensic detail)."""
    exc = LLMRateLimitError("rate limit", http_status=429)
    primary_llm.generate_content = AsyncMock(side_effect=exc)
    # _agent_execution_context stays None (no _set_execution_context call)

    with pytest.raises(BothProvidersUnavailableError) as exc_info:
        await agent._call_llm(_make_request())
    assert exc_info.value.primary_cause is exc


@pytest.mark.asyncio
async def test_non_transient_exception_not_caught(agent, primary_llm, fallback_llm):
    """RuntimeError is not a transient LLM error — must not be swallowed by fallback logic."""
    primary_llm.generate_content = AsyncMock(side_effect=RuntimeError("unexpected"))
    ctx = _make_execution_context(primary_llm, fallback_llm)
    agent._set_execution_context(ctx)

    with pytest.raises(RuntimeError, match="unexpected"):
        await agent._call_llm(_make_request())

    fallback_llm.generate_content.assert_not_called()


@pytest.mark.asyncio
async def test_fallback_emits_structured_log(agent, primary_llm, fallback_llm):
    """On fallback, logger.warning is called with event='llm_fallback' in extra."""
    primary_llm.generate_content = AsyncMock(
        side_effect=LLMRateLimitError("rate limit", http_status=429)
    )
    ctx = _make_execution_context(primary_llm, fallback_llm)
    agent._set_execution_context(ctx)

    with patch("src.agents.base_agent.logger") as mock_logger:
        await agent._call_llm(_make_request())

    mock_logger.warning.assert_called_once()
    call_args = mock_logger.warning.call_args
    extra = call_args.kwargs.get("extra", {})
    assert extra.get("event") == "llm_fallback"
    assert extra.get("primary_provider") == "gemini"
    assert extra.get("fallback_provider") == "claude"
    assert extra.get("error_type") == "rate_limit"
    assert extra.get("http_status") == 429


@pytest.mark.asyncio
async def test_fallback_log_error_type_unavailable(agent, primary_llm, fallback_llm):
    """LLMUnavailableError → error_type='unavailable' in structured log."""
    primary_llm.generate_content = AsyncMock(
        side_effect=LLMUnavailableError("overloaded", http_status=503)
    )
    ctx = _make_execution_context(primary_llm, fallback_llm)
    agent._set_execution_context(ctx)

    with patch("src.agents.base_agent.logger") as mock_logger:
        await agent._call_llm(_make_request())

    extra = mock_logger.warning.call_args.kwargs.get("extra", {})
    assert extra.get("error_type") == "unavailable"
    assert extra.get("http_status") == 503


# ============================================================================
# F4.5 Phase 2 — provider resilience port flow
# ============================================================================

def _make_ctx_with_resilience(
    primary_llm: LLMPort,
    resilience: ProviderResiliencePort,
    fallback_llm: Optional[LLMPort] = None,
) -> AgentExecutionContext:
    return AgentExecutionContext(
        agent_type="quick",
        provider=primary_llm,
        model_name="gemini-flash",
        tier=PerformanceTier.BALANCED,
        capabilities=ProviderCapabilities(),
        provider_name="gemini",
        fallback_provider=fallback_llm,
        fallback_model_name="claude-sonnet-4-6" if fallback_llm else None,
        fallback_provider_name="claude" if fallback_llm else None,
        resilience_port=resilience,
    )


@pytest.mark.asyncio
async def test_breaker_open_short_circuits_to_fallback(agent, primary_llm, fallback_llm):
    """Pre-call check: is_provider_open=True → primary skipped → fallback called."""
    resilience = MagicMock(spec=ProviderResiliencePort)
    resilience.is_provider_open.side_effect = lambda name: name == "gemini"
    ctx = _make_ctx_with_resilience(primary_llm, resilience, fallback_llm)
    agent._set_execution_context(ctx)

    response = await agent._call_llm(_make_request())

    assert response.text == "fallback ok"
    primary_llm.generate_content.assert_not_called()  # short-circuited
    fallback_llm.generate_content.assert_called_once()


@pytest.mark.asyncio
async def test_breaker_open_does_not_call_record_failure_on_primary(agent, primary_llm, fallback_llm):
    """ProviderBreakerOpenError is the consequence of past failures, not a new one."""
    resilience = MagicMock(spec=ProviderResiliencePort)
    resilience.is_provider_open.side_effect = lambda name: name == "gemini"
    ctx = _make_ctx_with_resilience(primary_llm, resilience, fallback_llm)
    agent._set_execution_context(ctx)

    await agent._call_llm(_make_request())

    # Only is_provider_open queries — no record_failure for the breaker-open case.
    resilience.record_failure.assert_not_called()


@pytest.mark.asyncio
async def test_record_success_called_on_primary_success(agent, primary_llm):
    """Happy path: primary succeeds → record_success(primary_name)."""
    resilience = MagicMock(spec=ProviderResiliencePort)
    resilience.is_provider_open.return_value = False
    ctx = _make_ctx_with_resilience(primary_llm, resilience, fallback_llm=None)
    agent._set_execution_context(ctx)

    await agent._call_llm(_make_request())

    resilience.record_success.assert_called_once_with("gemini")
    resilience.record_failure.assert_not_called()


@pytest.mark.asyncio
async def test_record_failure_called_on_real_transient_error(agent, primary_llm, fallback_llm):
    """Real failure (LLMRateLimitError) → record_failure(primary_name) before fallback."""
    primary_llm.generate_content = AsyncMock(
        side_effect=LLMRateLimitError("429", http_status=429)
    )
    resilience = MagicMock(spec=ProviderResiliencePort)
    resilience.is_provider_open.return_value = False
    ctx = _make_ctx_with_resilience(primary_llm, resilience, fallback_llm)
    agent._set_execution_context(ctx)

    await agent._call_llm(_make_request())

    resilience.record_failure.assert_called_once_with("gemini")


@pytest.mark.asyncio
async def test_no_record_success_on_fallback_success(agent, primary_llm, fallback_llm):
    """Asymmetric policy: fallback success does NOT call record_success(fallback_name).
    Phase 1 record_success is full-reset; calling on fallback would erase
    accumulated fallback failures after one lucky call (see decision record)."""
    primary_llm.generate_content = AsyncMock(
        side_effect=LLMUnavailableError("503", http_status=503)
    )
    resilience = MagicMock(spec=ProviderResiliencePort)
    resilience.is_provider_open.return_value = False
    ctx = _make_ctx_with_resilience(primary_llm, resilience, fallback_llm)
    agent._set_execution_context(ctx)

    await agent._call_llm(_make_request())

    # record_success called for primary on the path? No — primary failed.
    # record_success called for fallback? No — by design.
    resilience.record_success.assert_not_called()


@pytest.mark.asyncio
async def test_record_failure_on_fallback_failure_raises_both_unavailable(agent, primary_llm, fallback_llm):
    """Both fail → record_failure for both, raises BothProvidersUnavailableError."""
    primary_exc = LLMRateLimitError("primary 429", http_status=429)
    fallback_exc = LLMUnavailableError("fallback 503", http_status=503)
    primary_llm.generate_content = AsyncMock(side_effect=primary_exc)
    fallback_llm.generate_content = AsyncMock(side_effect=fallback_exc)
    resilience = MagicMock(spec=ProviderResiliencePort)
    resilience.is_provider_open.return_value = False
    ctx = _make_ctx_with_resilience(primary_llm, resilience, fallback_llm)
    agent._set_execution_context(ctx)

    with pytest.raises(BothProvidersUnavailableError) as exc_info:
        await agent._call_llm(_make_request())

    assert exc_info.value.primary_cause is primary_exc
    assert exc_info.value.primary_name == "gemini"
    assert exc_info.value.fallback_name == "claude"
    # Both providers recorded as failed.
    failures = {call.args[0] for call in resilience.record_failure.call_args_list}
    assert failures == {"gemini", "claude"}


@pytest.mark.asyncio
async def test_fallback_breaker_open_raises_both_unavailable_without_calling_fallback(
    agent, primary_llm, fallback_llm
):
    """Primary fails real, fallback breaker open → fallback.generate_content NOT called."""
    primary_exc = LLMUnavailableError("primary 503", http_status=503)
    primary_llm.generate_content = AsyncMock(side_effect=primary_exc)
    resilience = MagicMock(spec=ProviderResiliencePort)
    # Primary closed at pre-call; fallback open at pre-fallback check.
    resilience.is_provider_open.side_effect = lambda name: name == "claude"
    ctx = _make_ctx_with_resilience(primary_llm, resilience, fallback_llm)
    agent._set_execution_context(ctx)

    with patch("src.agents.base_agent.logger") as mock_logger:
        with pytest.raises(BothProvidersUnavailableError) as exc_info:
            await agent._call_llm(_make_request())

    assert exc_info.value.primary_cause is primary_exc
    fallback_llm.generate_content.assert_not_called()
    # Structured log signals the both-open scenario distinctly.
    events = [
        call.kwargs.get("extra", {}).get("event")
        for call in mock_logger.warning.call_args_list
    ]
    assert "llm_both_open" in events


@pytest.mark.asyncio
async def test_timeout_error_triggers_fallback(agent, primary_llm, fallback_llm):
    """LLMTimeoutError triggers same fallback flow as LLMRateLimitError."""
    primary_llm.generate_content = AsyncMock(
        side_effect=LLMTimeoutError("budget exhausted")
    )
    ctx = _make_execution_context(primary_llm, fallback_llm)
    agent._set_execution_context(ctx)

    response = await agent._call_llm(_make_request())

    assert response.text == "fallback ok"
    fallback_llm.generate_content.assert_called_once()


@pytest.mark.asyncio
async def test_network_error_triggers_fallback(agent, primary_llm, fallback_llm):
    """LLMNetworkError triggers same fallback flow."""
    primary_llm.generate_content = AsyncMock(
        side_effect=LLMNetworkError("DNS failure")
    )
    ctx = _make_execution_context(primary_llm, fallback_llm)
    agent._set_execution_context(ctx)

    response = await agent._call_llm(_make_request())

    assert response.text == "fallback ok"


@pytest.mark.asyncio
async def test_server_error_triggers_fallback(agent, primary_llm, fallback_llm):
    """LLMServerError (non-503 5xx) triggers same fallback flow."""
    primary_llm.generate_content = AsyncMock(
        side_effect=LLMServerError("500 internal", http_status=500)
    )
    ctx = _make_execution_context(primary_llm, fallback_llm)
    agent._set_execution_context(ctx)

    response = await agent._call_llm(_make_request())

    assert response.text == "fallback ok"


# ============================================================================
# TD-2 — transcript-integrity invariant (one transcript = one provider)
#
# When request.messages carries a provider-locked turn (tool_call/tool_response
# part, or raw_content), a primary FAILOVER error must NOT cross-provider-
# failover (that corrupts the transcript: orphan tool_use_id / thinking-replay).
# Instead: retry the SAME provider for transient errors, else terminal
# TranscriptLockedError → upstream Smart→Quick fallback (clean transcript).
# See decisions/transcript_integrity_one_provider.md.
# ============================================================================

def _make_locked_request() -> LLMRequest:
    """A provider-locked multi-turn transcript: contains a tool turn
    (tool_response part). Mirrors mid-delegation-loop state."""
    return LLMRequest(
        model_name="gemini-flash",
        system_instruction="test",
        messages=[
            Message(role="user", parts=[MessagePart(text="hi")]),
            Message(role="user", parts=[MessagePart(
                tool_response={"name": "search_memory", "response": {"result": "x"}}
            )]),
        ],
    )


def _make_raw_content_request() -> LLMRequest:
    """Provider-locked via raw_content only (thinking-replay case): a model turn
    carrying a provider SDK object but NO tool parts."""
    return LLMRequest(
        model_name="gemini-flash",
        system_instruction="test",
        messages=[
            Message(role="user", parts=[MessagePart(text="hi")]),
            Message(
                role="model",
                parts=[MessagePart(text="reasoning trace")],
                raw_content=object(),
            ),
        ],
    )


@pytest.mark.asyncio
async def test_transcript_locked_server_error_retries_same_provider_then_succeeds(
    agent, primary_llm, fallback_llm
):
    """Locked transcript + transient LLMServerError that clears on retry →
    SAME provider retried, fallback never touched, transcript intact."""
    primary_llm.generate_content = AsyncMock(side_effect=[
        LLMServerError("529 overloaded", http_status=529),
        _make_llm_response("primary recovered"),
    ])
    ctx = _make_execution_context(primary_llm, fallback_llm)
    agent._set_execution_context(ctx)

    with patch("src.agents.base_agent.asyncio.sleep", new=AsyncMock()):
        response = await agent._call_llm(_make_locked_request())

    assert response.text == "primary recovered"
    assert primary_llm.generate_content.call_count == 2  # initial + 1 retry
    fallback_llm.generate_content.assert_not_called()


@pytest.mark.asyncio
async def test_transcript_locked_exhausts_same_provider_raises_transcript_locked(
    agent, primary_llm, fallback_llm
):
    """Locked transcript + persistent transient error → same-provider retries
    exhausted → terminal TranscriptLockedError, fallback NEVER called,
    record_failure counted per failed attempt (initial + retries)."""
    exc = LLMServerError("529 overloaded", http_status=529)
    primary_llm.generate_content = AsyncMock(side_effect=exc)
    resilience = MagicMock(spec=ProviderResiliencePort)
    resilience.is_provider_open.return_value = False
    ctx = _make_ctx_with_resilience(primary_llm, resilience, fallback_llm)
    agent._set_execution_context(ctx)

    with patch("src.agents.base_agent.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(TranscriptLockedError) as exc_info:
            await agent._call_llm(_make_locked_request())

    assert exc_info.value.provider_name == "gemini"
    assert exc_info.value.cause is exc
    fallback_llm.generate_content.assert_not_called()
    expected_calls = 1 + agent._SAME_PROVIDER_RETRY_ATTEMPTS
    assert primary_llm.generate_content.call_count == expected_calls
    failures = [c.args[0] for c in resilience.record_failure.call_args_list]
    assert failures == ["gemini"] * expected_calls


@pytest.mark.asyncio
async def test_transcript_locked_breaker_open_goes_terminal_no_retry(
    agent, primary_llm, fallback_llm
):
    """Locked transcript + primary breaker open → NO same-provider retry
    (pointless), NO record_failure (consequence of past failures), terminal
    TranscriptLockedError, fallback never called."""
    resilience = MagicMock(spec=ProviderResiliencePort)
    resilience.is_provider_open.side_effect = lambda name: name == "gemini"
    ctx = _make_ctx_with_resilience(primary_llm, resilience, fallback_llm)
    agent._set_execution_context(ctx)

    with patch("src.agents.base_agent.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        with pytest.raises(TranscriptLockedError) as exc_info:
            await agent._call_llm(_make_locked_request())

    assert isinstance(exc_info.value.cause, ProviderBreakerOpenError)
    primary_llm.generate_content.assert_not_called()  # short-circuited, no retry
    fallback_llm.generate_content.assert_not_called()
    resilience.record_failure.assert_not_called()
    mock_sleep.assert_not_called()  # retry loop never entered


@pytest.mark.asyncio
async def test_transcript_locked_via_raw_content_only(agent, primary_llm, fallback_llm):
    """raw_content present, NO tool parts → still locked (thinking-replay guard).
    Validates the OR-clause: FAILOVER error → same-provider path, no failover."""
    primary_llm.generate_content = AsyncMock(
        side_effect=LLMServerError("529", http_status=529)
    )
    ctx = _make_execution_context(primary_llm, fallback_llm)
    agent._set_execution_context(ctx)

    with patch("src.agents.base_agent.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(TranscriptLockedError):
            await agent._call_llm(_make_raw_content_request())

    fallback_llm.generate_content.assert_not_called()


@pytest.mark.asyncio
async def test_unlocked_clean_request_still_failovers(agent, primary_llm, fallback_llm):
    """Regression: a clean (unlocked) request still cross-provider-fails-over on
    LLMServerError — the invariant gates ONLY locked transcripts."""
    primary_llm.generate_content = AsyncMock(
        side_effect=LLMServerError("500", http_status=500)
    )
    ctx = _make_execution_context(primary_llm, fallback_llm)
    agent._set_execution_context(ctx)

    response = await agent._call_llm(_make_request())

    assert response.text == "fallback ok"
    fallback_llm.generate_content.assert_called_once()


@pytest.mark.asyncio
async def test_transcript_locked_success_runs_billing_and_span(
    agent, primary_llm, fallback_llm
):
    """When a same-provider retry succeeds, the shared success tail still runs:
    billing accumulation reflects the recovered response's tokens (guards the
    fall-through past the locked branch)."""
    primary_llm.generate_content = AsyncMock(side_effect=[
        LLMServerError("529", http_status=529),
        _make_llm_response("recovered"),
    ])
    ctx = _make_execution_context(primary_llm, fallback_llm)
    agent._set_execution_context(ctx)

    tokens_before = agent._billing_prompt_tokens

    with patch("src.agents.base_agent.asyncio.sleep", new=AsyncMock()):
        response = await agent._call_llm(_make_locked_request())

    assert response.text == "recovered"
    # _make_llm_response carries prompt_tokens=5 → billing tail ran on retry success
    assert agent._billing_prompt_tokens == tokens_before + 5
