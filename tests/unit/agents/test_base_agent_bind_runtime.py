"""BaseAgent.bind_runtime: the composition root's one call that hands an agent its coordinator, content store and quota
service (composition-root assessment D4). What it binds is what billing, capture and delegation read later; an agent
never bound runs as before, with all three off."""
import dataclasses
from typing import List, Tuple
from unittest.mock import AsyncMock

import pytest

from src.agents.agent_runtime import AgentRuntime
from src.agents.base_agent import BaseAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentResponse
from src.domain.llm import LLMRequest, LLMResponse, Message, MessagePart, UsageMetadata
from src.ports.prompt_content_store import PromptContentStore
from src.ports.quota_service import QuotaService


class _Agent(BaseAgent):
    async def can_handle(self, message: AgentMessage) -> bool:  # pragma: no cover
        return True

    async def execute(self, message: AgentMessage) -> AgentResponse:  # pragma: no cover
        raise NotImplementedError


class _Quota(QuotaService):
    def __init__(self) -> None:
        self.usage: List[Tuple[str, int]] = []

    async def record_usage(self, account_id: str, model: str, tokens: int, cost: float) -> None:
        self.usage.append((account_id, tokens))


class _Store(PromptContentStore):
    def __init__(self) -> None:
        self.turns: List[Tuple[str, str, str]] = []

    async def record_turn(self, *, request, response, agent_id, agent_type, account_id, turn, latency_ms,
                          provider="") -> None:
        self.turns.append((agent_id, account_id, response.text))

    async def record_dr_result(self, **kwargs) -> None:  # pragma: no cover
        raise NotImplementedError

    async def flush(self, timeout_s: float = 10.0) -> None:  # pragma: no cover
        return None


def _agent() -> _Agent:
    agent = _Agent(config=AgentConfig(agent_id="stub_user-1", agent_type="stub"))
    agent.llm = AsyncMock()
    agent.llm.generate_content = AsyncMock(return_value=LLMResponse(
        text="hi there", usage_metadata=UsageMetadata(prompt_tokens=10, completion_tokens=5, total_tokens=15)))
    return agent


def _request() -> LLMRequest:
    return LLMRequest(model_name="gemini-flash-latest", system_instruction="you are a test",
                      messages=[Message(role="user", parts=[MessagePart(text="hello")])])


async def _one_billed_call(agent: _Agent) -> LLMResponse:
    with agent._execution_billing_scope("acct-1"):
        response = await agent._call_llm(_request())
        await agent._flush_billing()
    return response


async def test_a_bound_agent_bills_captures_and_holds_the_coordinator():
    agent, quota, store, coordinator = _agent(), _Quota(), _Store(), object()

    agent.bind_runtime(AgentRuntime(coordinator=coordinator, prompt_content_store=store, quota_service=quota))
    response = await _one_billed_call(agent)

    assert response.text == "hi there"
    assert agent.coordinator is coordinator
    assert store.turns == [("stub_user-1", "acct-1", "hi there")]
    assert quota.usage == [("acct-1", 15)]


async def test_an_agent_never_bound_answers_with_billing_capture_and_coordinator_off():
    agent = _agent()

    response = await _one_billed_call(agent)

    assert response.text == "hi there"
    assert agent.coordinator is None
    assert agent._prompt_content_store is None and agent._quota_service is None


async def test_binding_an_empty_runtime_turns_every_feature_off_again():
    agent, quota, store = _agent(), _Quota(), _Store()
    agent.bind_runtime(AgentRuntime(coordinator=object(), prompt_content_store=store, quota_service=quota))

    agent.bind_runtime(AgentRuntime())
    await _one_billed_call(agent)

    assert agent.coordinator is None
    assert store.turns == [] and quota.usage == []


def test_the_runtime_is_frozen_and_empty_by_default():
    runtime = AgentRuntime()

    assert (runtime.coordinator, runtime.prompt_content_store, runtime.quota_service) == (None, None, None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        runtime.quota_service = _Quota()  # type: ignore[misc]
