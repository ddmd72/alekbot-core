"""BaseAgent._call_llm: a non-failover client error raised by the FALLBACK carries the provider that raised it
(USER_DRIVE_INDEXING_RFC §25.4.4, review N7). It is an `LLMFallbackClientError(LLMClientError)`: existing callers still
see an `LLMClientError` with its status, the drive indexer can tell the fallback's refusal from the primary's. The
primary's own client errors, an account error on the fallback and the failover errors are unchanged."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.base_agent import BaseAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentResponse
from src.domain.exceptions import (
    BothProvidersUnavailableError, LLMAccountError, LLMClientError, LLMFallbackClientError, LLMServerError,
    LLMUnavailableError,
)
from src.domain.user import PerformanceTier
from src.ports.llm_port import (
    AgentExecutionContext, LLMPort, LLMRequest, LLMResponse, Message, MessagePart, ProviderCapabilities,
    UsageMetadata,
)


class _Agent(BaseAgent):
    def __init__(self, llm: LLMPort) -> None:
        super().__init__(AgentConfig(agent_id="a", agent_type="drive_profiler"))
        self.llm = llm

    async def can_handle(self, message: AgentMessage) -> bool:
        return True

    async def execute(self, message: AgentMessage) -> AgentResponse:
        raise NotImplementedError


def _llm(effect=None):
    llm = MagicMock(spec=LLMPort)
    llm.generate_content = AsyncMock(
        side_effect=effect,
        return_value=LLMResponse(text="ok", usage_metadata=UsageMetadata(prompt_tokens=1, completion_tokens=1,
                                                                         total_tokens=2)))
    return llm


def _agent(primary, fallback):
    agent = _Agent(primary)
    agent._set_execution_context(AgentExecutionContext(
        agent_type="drive_profiler", provider=primary, model_name="gpt-6-luna", tier=PerformanceTier.ECO,
        capabilities=ProviderCapabilities(), provider_name="openai", fallback_provider=fallback,
        fallback_model_name="gemini-3.8-flash" if fallback else None,
        fallback_provider_name="gemini" if fallback else None, resilience_port=InMemoryProviderResilience()))
    return agent


_REQUEST = LLMRequest(model_name="gpt-6-luna", system_instruction="s",
                      messages=[Message(role="user", parts=[MessagePart(text="hi")])])


def test_it_is_a_client_error_carrying_the_provider():
    exc = LLMFallbackClientError("bad", http_status=400, provider_name="gemini")
    assert isinstance(exc, LLMClientError)
    assert exc.http_status == 400 and exc.provider_name == "gemini"


@pytest.mark.parametrize("status", [400, 413, 422, None])
async def test_a_client_error_of_the_fallback_is_wrapped_with_its_provider(status):
    cause = LLMClientError("refused", http_status=status)
    agent = _agent(_llm(LLMUnavailableError("503", http_status=503)), _llm(cause))
    with pytest.raises(LLMFallbackClientError) as caught:
        await agent._call_llm(_REQUEST)
    assert caught.value.provider_name == "gemini"
    assert caught.value.http_status == status
    assert caught.value.__cause__ is cause


async def test_existing_callers_still_catch_an_llm_client_error():
    agent = _agent(_llm(LLMUnavailableError("503", http_status=503)), _llm(LLMClientError("x", http_status=400)))
    with pytest.raises(LLMClientError):
        await agent._call_llm(_REQUEST)


async def test_an_account_error_of_the_fallback_is_not_wrapped():
    """An account-level 4xx is an outage for everyone, not a refusal of this input: it keeps its own type."""
    cause = LLMAccountError("billing", http_status=400)
    agent = _agent(_llm(LLMUnavailableError("503", http_status=503)), _llm(cause))
    with pytest.raises(LLMAccountError) as caught:
        await agent._call_llm(_REQUEST)
    assert caught.value is cause and not isinstance(caught.value, LLMFallbackClientError)


async def test_the_primarys_own_client_error_is_unchanged():
    cause = LLMClientError("refused", http_status=400)
    fallback = _llm()
    agent = _agent(_llm(cause), fallback)
    with pytest.raises(LLMClientError) as caught:
        await agent._call_llm(_REQUEST)
    assert caught.value is cause
    fallback.generate_content.assert_not_called()


async def test_a_failover_error_of_the_fallback_is_still_both_unavailable():
    agent = _agent(_llm(LLMUnavailableError("503", http_status=503)), _llm(LLMServerError("500", http_status=500)))
    with pytest.raises(BothProvidersUnavailableError):
        await agent._call_llm(_REQUEST)


async def test_the_fallback_breaker_counts_a_wrapped_refusal_as_no_failure():
    """A refusal is about the input, not the fallback's health: the breaker is left alone, as for the primary."""
    agent = _agent(_llm(LLMUnavailableError("503", http_status=503)), _llm(LLMClientError("x", http_status=400)))
    resilience = agent._agent_execution_context.resilience_port
    with pytest.raises(LLMFallbackClientError):
        await agent._call_llm(_REQUEST)
    assert not resilience.is_provider_open("gemini")
