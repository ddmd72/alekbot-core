"""Engine runners declare their own provider's native formats on a context copy, per attempt (U2 P2-3)."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents import domain_researcher_agent as DR_MODULE
from src.agents import tutor_agent as TUTOR_MODULE
from src.agents.core import quick_response_agent as QUICK_MODULE
from src.agents.core.smart_response_agent import SmartResponseAgent
from src.agents.domain_researcher_agent import DomainResearcherAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage
from src.domain.exceptions import TranscriptLockedError
from src.domain.native_format import NATIVE_FORMATS_KEY, NativeFormat, from_plain, to_plain
from src.domain.user import PerformanceTier
from src.infrastructure.delegation_engine import DelegationEngine, DelegationResult
from src.ports.llm_port import AgentExecutionContext, LLMPort, ProviderCapabilities
from tests.support.agent_answer_fakes import FakeEngine, result
from tests.unit.agents.core.test_quick_response_agent import (  # noqa: F401  (fixtures)
    quick_agent_config, mock_llm_port, mock_session_store, mock_prompt_builder, quick_agent,
    create_query_message,
)
from tests.unit.agents.core.test_smart_turn_clock import _make_ctx, _make_message, _make_smart
from tests.unit.agents.test_tutor_agent import (  # noqa: F401  (fixtures)
    mock_prompt_builder as tutor_prompt_builder, mock_assembler, _bound_message,
)
from tests.unit.agents.test_tutor_deliver_response import _make_tutor

PDF = NativeFormat("application/pdf", 10_000_000, 100)
PNG = NativeFormat("image/png", 4_000_000, 1)


def _declared(kwargs):
    return kwargs["context"][NATIVE_FORMATS_KEY]


async def test_quick_declares_its_provider_on_a_context_copy(quick_agent, monkeypatch):
    quick_agent.llm.native_formats = MagicMock(return_value=(PDF, PNG))
    engine = FakeEngine(result(args={"response": "x"}))
    monkeypatch.setattr(QUICK_MODULE, "DelegationEngine", engine)
    message = create_query_message("hello")
    message.context[NATIVE_FORMATS_KEY] = [{"mime": "stale", "max_bytes": 1, "max_pages": 1}]
    await quick_agent.execute(message)
    assert from_plain(_declared(engine.last_kwargs)) == (PDF, PNG)
    quick_agent.llm.native_formats.assert_called_once_with(quick_agent.model_name)
    assert message.context[NATIVE_FORMATS_KEY][0]["mime"] == "stale"   # the message's own context is untouched


async def test_quick_with_a_text_only_provider_declares_an_empty_list(quick_agent, monkeypatch):
    quick_agent.llm.native_formats = MagicMock(return_value=())
    engine = FakeEngine(result(args={"response": "x"}))
    monkeypatch.setattr(QUICK_MODULE, "DelegationEngine", engine)
    await quick_agent.execute(create_query_message("hello"))
    assert _declared(engine.last_kwargs) == []


async def test_tutor_declares_its_provider(tutor_prompt_builder, mock_assembler, monkeypatch):
    tutor = _make_tutor(tutor_prompt_builder, mock_assembler)
    tutor._llm.native_formats = MagicMock(return_value=(PDF,))
    engine = FakeEngine(result(args={"response": "x"}))
    monkeypatch.setattr(TUTOR_MODULE, "DelegationEngine", engine)
    await tutor.execute(_bound_message())
    assert from_plain(_declared(engine.last_kwargs)) == (PDF,)
    tutor._llm.native_formats.assert_called_once_with(tutor.model_name)


async def test_domain_researcher_declares_its_provider(monkeypatch):
    provider = MagicMock(spec=LLMPort)
    provider.native_formats.return_value = (PDF, PNG)
    ctx = AgentExecutionContext(
        agent_type="domain_researcher", provider=provider, model_name="m", tier=PerformanceTier.BALANCED,
        capabilities=ProviderCapabilities(), provider_name="stub", resilience_port=InMemoryProviderResilience(),
    )
    builder = MagicMock()
    builder.build_for_agent = AsyncMock(return_value="SYSTEM")
    agent = DomainResearcherAgent(
        AgentConfig(agent_id="domain_researcher_x", agent_type="domain_researcher", timeout_ms=60000,
                    capabilities=["domain_research"]),
        ctx, prompt_builder=builder, user_id="user-1")
    agent.coordinator = MagicMock()
    agent.coordinator.get_available_intents_for.return_value = [
        {"name": "search_web", "description": "Search.", "context_schema": {}}]
    engine = FakeEngine(result(text="done"))
    monkeypatch.setattr(DR_MODULE, "DelegationEngine", engine)
    message = AgentMessage.create(
        sender="coordinator", recipient=agent.agent_id, intent=AgentIntent.QUERY,
        payload={"query": "q"}, context={"account_id": "acc-1", "user_id": "user-1"})
    response = await agent.execute(message)
    assert response.status.value == "success"
    assert from_plain(_declared(engine.last_kwargs)) == (PDF, PNG)
    provider.native_formats.assert_called_once_with("m")


async def _smart_run_recording(agent, message, declared_by_attempt, lock_first):
    calls = []

    async def spy(self, *, call_llm, base_request, **kwargs):
        calls.append(kwargs["context"])
        if lock_first and len(calls) == 1:
            raise TranscriptLockedError(provider_name="first", cause=Exception("x"), turn=2)
        return DelegationResult(text="ok", total_tokens=0)

    from unittest.mock import patch
    with patch.object(DelegationEngine, "execute", spy):
        await agent.execute(message)
    return calls


async def test_smart_declares_its_attempt_provider_and_a_rotation_redeclares():
    first = _make_ctx(model_name="model-a")
    first.provider.native_formats.return_value = (PDF,)
    first.provider_name = "first"
    second = _make_ctx(model_name="model-b")
    second.provider.native_formats.return_value = (PNG,)
    second.provider_name = "second"
    agent = _make_smart(first)
    message = _make_message()
    agent.resolver.next_provider_override = MagicMock(
        return_value=MagicMock(execution_context=second, thinking_effort="medium"))

    calls = await _smart_run_recording(agent, message, None, lock_first=True)

    assert [from_plain(c[NATIVE_FORMATS_KEY]) for c in calls] == [(PDF,), (PNG,)]
    first.provider.native_formats.assert_called_once_with("model-a")
    second.provider.native_formats.assert_called_once_with("model-b")
    assert NATIVE_FORMATS_KEY not in message.context


async def test_smart_overwrites_a_key_already_on_the_message():
    ctx = _make_ctx()
    ctx.provider.native_formats.return_value = ()
    agent = _make_smart(ctx)
    message = _make_message()
    message.context[NATIVE_FORMATS_KEY] = to_plain((PDF,))
    calls = await _smart_run_recording(agent, message, None, lock_first=False)
    assert calls[0][NATIVE_FORMATS_KEY] == []
    assert message.context[NATIVE_FORMATS_KEY] == to_plain((PDF,))
