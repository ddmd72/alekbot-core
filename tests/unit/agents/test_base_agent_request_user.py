"""BaseAgent.process() opens the request user scope from message.context["user_id"].

The prompt-content row's user_id is read from the request-scoped telemetry context at record_turn time. Entry points
other than chat (Cloud Tasks, schedulers, background runners) set no such context, so process() sets it for the
execution and restores it after.
"""

import asyncio
from typing import List, Optional
from unittest.mock import AsyncMock

from src.agents.base_agent import BaseAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse
from src.domain.llm import LLMRequest, LLMResponse, Message, MessagePart, UsageMetadata
from src.domain.request_context import RequestContext
from src.utils.telemetry import get_request_context, request_user_scope


class _SpyStore:
    """Records the request user visible inside record_turn, as the BigQuery adapter reads it."""

    def __init__(self) -> None:
        self.seen: List[Optional[str]] = []
        self.accounts: List[Optional[str]] = []

    async def record_turn(self, **kwargs) -> None:
        self.seen.append(get_request_context()["user_id"])
        self.accounts.append(kwargs.get("account_id"))


class _LLM:
    async def generate_content(self, request, **kwargs):
        return LLMResponse(text="ok", usage_metadata=UsageMetadata(prompt_tokens=1, completion_tokens=1, total_tokens=2))


class _Agent(BaseAgent):
    def __init__(self, store: _SpyStore, fail: bool = False, pause: Optional[asyncio.Event] = None) -> None:
        super().__init__(config=AgentConfig(agent_id="stub_u", agent_type="stub"))
        self._prompt_content_store = store
        self.llm = _LLM()
        self._fail = fail
        self._pause = pause

    async def can_handle(self, message: AgentMessage) -> bool:
        return True

    async def execute(self, message: AgentMessage) -> AgentResponse:
        if self._pause is not None:
            await self._pause.wait()
        await self._call_llm(
            LLMRequest(model_name="m", system_instruction="s", messages=[Message(role="user", parts=[MessagePart(text="x")])])
        )
        if self._fail:
            raise ValueError("boom")
        return AgentResponse.success(task_id=message.task_id, agent_id=self.agent_id, result={})


def _message(user_id: Optional[str], account_id: Optional[str] = "account-a") -> AgentMessage:
    context = {"account_id": account_id} if account_id else {}
    if user_id is not None:
        context["user_id"] = user_id
    return AgentMessage.create(sender="t", recipient="stub_u", intent=AgentIntent.QUERY, payload={}, context=context)


async def test_record_turn_sees_the_message_user_and_context_is_restored():
    store = _SpyStore()
    assert get_request_context()["user_id"] is None

    await _Agent(store).process(_message("user-a"))

    assert store.seen == ["user-a"]
    assert get_request_context()["user_id"] is None


async def test_outer_user_is_restored_after_an_inline_specialist():
    store = _SpyStore()
    with request_user_scope("user-outer"):
        await _Agent(store).process(_message("user-a"))
        assert get_request_context()["user_id"] == "user-outer"
    assert store.seen == ["user-a"]


async def test_context_is_restored_when_the_execution_raises():
    store = _SpyStore()
    with request_user_scope("user-outer"):
        await _Agent(store, fail=True).process(_message("user-a"))
        assert get_request_context()["user_id"] == "user-outer"
    assert store.seen == ["user-a"]


async def test_message_without_user_leaves_the_outer_user():
    store = _SpyStore()
    with request_user_scope("user-outer"):
        await _Agent(store).process(_message(None))
        assert get_request_context()["user_id"] == "user-outer"
    assert store.seen == ["user-outer"]


async def test_message_without_user_and_no_outer_records_none():
    store = _SpyStore()
    await _Agent(store).process(_message(None))
    assert store.seen == [None]


async def test_concurrent_executions_each_record_their_own_user():
    store = _SpyStore()
    gate = asyncio.Event()
    agent = _Agent(store, pause=gate)

    tasks = [asyncio.create_task(agent.process(_message(u))) for u in ("user-a", "user-b")]
    await asyncio.sleep(0.01)
    gate.set()
    await asyncio.gather(*tasks)

    assert sorted(store.seen) == ["user-a", "user-b"]


def test_request_user_scope_with_falsy_user_changes_nothing():
    with request_user_scope("user-outer"):
        with request_user_scope(None):
            assert get_request_context()["user_id"] == "user-outer"
        with request_user_scope(""):
            assert get_request_context()["user_id"] == "user-outer"


def _billed_agent(store: _SpyStore) -> _Agent:
    agent = _Agent(store)
    agent._quota_service = AsyncMock()
    return agent


async def test_account_comes_from_the_request_context_when_the_message_names_none():
    store = _SpyStore()
    agent = _billed_agent(store)

    async with RequestContext(user_id="user-a", account_id="account-a"):
        await agent.process(_message("user-a", account_id=None))

    agent._quota_service.record_usage.assert_awaited_once()
    assert agent._quota_service.record_usage.await_args.kwargs["account_id"] == "account-a"
    assert store.accounts == ["account-a"]


async def test_the_message_account_wins_over_the_request_context():
    store = _SpyStore()
    agent = _billed_agent(store)

    async with RequestContext(user_id="user-a", account_id="account-ctx"):
        await agent.process(_message("user-a", account_id="account-msg"))

    assert agent._quota_service.record_usage.await_args.kwargs["account_id"] == "account-msg"
    assert store.accounts == ["account-msg"]


async def test_no_account_anywhere_means_no_billing_and_no_error():
    store = _SpyStore()
    agent = _billed_agent(store)

    response = await agent.process(_message("user-a", account_id=None))

    assert response.status == "success"
    agent._quota_service.record_usage.assert_not_awaited()
    assert store.accounts == [None]


async def test_a_request_context_with_only_a_user_is_not_billed_as_an_account():
    store = _SpyStore()
    agent = _billed_agent(store)

    async with RequestContext(user_id="user-a"):
        await agent.process(_message("user-a", account_id=None))

    agent._quota_service.record_usage.assert_not_awaited()
