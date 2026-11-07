"""Failover and breaker log lines name the agent and the providers in their message text.

Cloud Logging serialises the message only (``extra`` never reaches it), so a reader can answer
"which agent failed over, from which provider to which" only if the text carries it.
Assertions check the presence of tokens, not the full wording.
"""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.domain.exceptions import BothProvidersUnavailableError, LLMUnavailableError
from src.ports.llm_port import LLMPort
from src.ports.provider_resilience_port import ProviderResiliencePort
from tests.unit.agents.core.test_base_agent_fallback import (
    _MinimalAgent,
    _make_config,
    _make_ctx_with_resilience,
    _make_execution_context,
    _make_llm_response,
    _make_request,
)


def _llm(**kw) -> MagicMock:
    llm = MagicMock(spec=LLMPort)
    llm.generate_content = AsyncMock(**kw)
    return llm


def _lines(caplog, event: str) -> list:
    return [r.getMessage() for r in caplog.records if event in r.getMessage()]


def _assert_labelled(line: str) -> None:
    assert "agent=quick" in line
    assert "gemini" in line and "claude" in line


@pytest.mark.asyncio
async def test_fallback_and_ok_lines_name_agent_and_providers(caplog):
    primary = _llm(side_effect=LLMUnavailableError("down", http_status=503))
    fallback = _llm(return_value=_make_llm_response("fb"))
    agent = _MinimalAgent(_make_config(), primary)
    agent._set_execution_context(_make_execution_context(primary, fallback))
    with caplog.at_level(logging.INFO):
        await agent._call_llm(_make_request())
    for event in ("llm_fallback ", "llm_fallback_ok"):
        (line,) = _lines(caplog, event)
        _assert_labelled(line)


@pytest.mark.asyncio
async def test_fallback_failed_line_names_agent_and_providers(caplog):
    primary = _llm(side_effect=LLMUnavailableError("down", http_status=503))
    fallback = _llm(side_effect=LLMUnavailableError("down too", http_status=503))
    agent = _MinimalAgent(_make_config(), primary)
    agent._set_execution_context(_make_execution_context(primary, fallback))
    with caplog.at_level(logging.INFO), pytest.raises(BothProvidersUnavailableError):
        await agent._call_llm(_make_request())
    (line,) = _lines(caplog, "llm_fallback_failed")
    _assert_labelled(line)


@pytest.mark.asyncio
async def test_no_fallback_line_names_agent_and_primary(caplog):
    primary = _llm(side_effect=LLMUnavailableError("down", http_status=503))
    agent = _MinimalAgent(_make_config(), primary)
    agent._set_execution_context(_make_execution_context(primary, None))
    with caplog.at_level(logging.INFO), pytest.raises(BothProvidersUnavailableError):
        await agent._call_llm(_make_request())
    (line,) = _lines(caplog, "llm_no_fallback")
    assert "agent=quick" in line and "gemini" in line


@pytest.mark.asyncio
async def test_both_open_line_names_agent_and_providers(caplog):
    primary = _llm(side_effect=LLMUnavailableError("down", http_status=503))
    fallback = _llm(return_value=_make_llm_response())
    resilience = MagicMock(spec=ProviderResiliencePort)
    resilience.is_provider_open.side_effect = lambda name: name == "claude"
    agent = _MinimalAgent(_make_config(), primary)
    agent._set_execution_context(_make_ctx_with_resilience(primary, resilience, fallback))
    with caplog.at_level(logging.INFO), pytest.raises(BothProvidersUnavailableError):
        await agent._call_llm(_make_request())
    (line,) = _lines(caplog, "llm_both_open")
    _assert_labelled(line)


@pytest.mark.asyncio
async def test_breaker_open_line_names_the_tripping_agent(caplog):
    resilience = InMemoryProviderResilience(failure_threshold=2)
    primary = _llm(side_effect=LLMUnavailableError("down", http_status=503))
    fallback = _llm(return_value=_make_llm_response())
    agent = _MinimalAgent(_make_config(), primary)
    agent._set_execution_context(_make_ctx_with_resilience(primary, resilience, fallback))
    with caplog.at_level(logging.INFO):
        await agent._call_llm(_make_request())
        await agent._call_llm(_make_request())
    (line,) = _lines(caplog, "provider_breaker_open")
    assert "provider=gemini" in line and "agent=quick" in line


@pytest.mark.asyncio
async def test_prompt_content_after_failover_carries_served_model_and_provider():
    primary = _llm(side_effect=LLMUnavailableError("down", http_status=503))
    fallback = _llm(return_value=_make_llm_response("fb"))
    store = MagicMock()
    store.record_turn = AsyncMock()
    agent = _MinimalAgent(_make_config(), primary)
    agent._prompt_content_store = store
    agent._set_execution_context(_make_execution_context(primary, fallback))
    await agent._call_llm(_make_request())
    kwargs = store.record_turn.call_args.kwargs
    assert kwargs["request"].model_name == "claude-sonnet-4-6"
    assert kwargs["provider"] == "claude"


@pytest.mark.asyncio
async def test_prompt_content_without_failover_keeps_requested_model():
    primary = _llm(return_value=_make_llm_response())
    store = MagicMock()
    store.record_turn = AsyncMock()
    agent = _MinimalAgent(_make_config(), primary)
    agent._prompt_content_store = store
    agent._set_execution_context(_make_execution_context(primary, None))
    await agent._call_llm(_make_request())
    kwargs = store.record_turn.call_args.kwargs
    assert kwargs["request"].model_name == "gemini-flash"
    assert kwargs["provider"] == "gemini"
