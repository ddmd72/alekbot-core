"""Router prefetch on the scored fact search: the slots call runs beside triage; triage's `none` drops it; the search
runs on the slots query with the owner's location and the baseline excluded; any failure leaves the turn without a
prefetch (fact profile RFC §5.4)."""
import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.core.router_agent import RouterAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse
from src.domain.entities import FactEntity, FactType
from src.domain.fact_query import FactQuery
from src.ports.fact_search_port import FactSearchPort

LOCATION = "Springfield, Portugal"
QUESTION = "When is the car service?"
TODAY = date(2026, 11, 10)


class _Slots:
    def __init__(self, fail=False, block=False):
        self.fail, self.block = fail, block
        self.started, self.cancelled, self.calls = asyncio.Event(), False, []

    async def query_for_messages(self, messages, *, path, account_id, user_id):
        self.calls.append((path, account_id, user_id))
        self.started.set()
        if self.block:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if self.fail:
            raise RuntimeError("slots exploded")
        last = messages[-1].parts[0].text
        return FactQuery(today=TODAY, subject="car service", path=path, message=last)


class _Search(FactSearchPort):
    def __init__(self, facts=(), fail=False):
        self.facts, self.fail, self.calls = list(facts), fail, []

    async def search(self, account_id, query, *, exclude=frozenset()):
        self.calls.append((account_id, query, set(exclude)))
        if self.fail:
            raise RuntimeError("search exploded")
        return self.facts

    async def search_phrases(self, account_id, queries):
        raise AssertionError("the Router searches one query")


def _fact(text):
    return FactEntity(account_id="acc-1", created_by_user_id="usr-1", lineage_id="lin", text=text, type=FactType.STATE)


def _router(slots, search, search_intent="topic", on_triage=None):
    coordinator = MagicMock()
    coordinator.route_message = AsyncMock(return_value=AgentResponse.success(
        task_id="t", agent_id="smart_agent", result="ok", confidence=1.0))
    repository = MagicMock()
    repository.get_biographical_context_cached = AsyncMock(
        return_value=[{"id": "f-base", "text": "Alex lives in Springfield"}])
    router = RouterAgent(
        config=AgentConfig(agent_id="router_agent", agent_type="router"),
        coordinator=coordinator, smart_agent_id="smart_agent", user_id="usr-1",
        repository=repository, fact_search=search, slots=slots, user_location=LOCATION,
    )

    async def triage(text, message, history=None):
        if on_triage:
            await on_triage()
        return {"needs_memory_search": False, "search_intent": search_intent,
                "metadata": {"user_tone": "casual", "task_complexity": "info_search"}}

    router._classify_request_with_fallback = triage  # type: ignore[method-assign]
    return router, coordinator


def _message():
    return AgentMessage.create(
        sender="handler", recipient="router_agent", intent=AgentIntent.QUERY,
        payload={"text": QUESTION}, context={"user_id": "usr-1", "account_id": "acc-1", "session_id": "s"})


async def test_slots_start_before_triage_returns():
    slots, search = _Slots(), _Search()

    async def triage_waits_for_slots():
        await asyncio.wait_for(slots.started.wait(), 1)

    router, _ = _router(slots, search, on_triage=triage_waits_for_slots)
    await router.execute(_message())
    assert slots.calls == [("router", "acc-1", "usr-1")]


async def test_search_intent_none_cancels_the_slots_and_never_searches():
    slots, search = _Slots(block=True), _Search()
    router, coordinator = _router(
        slots, search, search_intent="none", on_triage=lambda: asyncio.wait_for(slots.started.wait(), 1))
    await router.execute(_message())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert slots.cancelled
    assert search.calls == []
    assert coordinator.route_message.call_args.args[0].context["enriched_context"] is None


async def test_one_search_on_the_slots_query_reaches_the_routed_message():
    slots, search = _Slots(), _Search([_fact("Car service on 2026-11-05")])
    router, coordinator = _router(slots, search)
    await router.execute(_message())
    assert len(search.calls) == 1
    account_id, query, exclude = search.calls[0]
    assert account_id == "acc-1"
    assert query.subject == "car service"
    assert query.home == LOCATION
    assert query.message == QUESTION
    assert exclude == {"f-base"}
    enriched = coordinator.route_message.call_args.args[0].context["enriched_context"]
    assert [f["content"] for f in enriched["facts"]] == ["Car service on 2026-11-05"]


@pytest.mark.parametrize("slots,search", [(_Slots(fail=True), _Search()), (_Slots(), _Search(fail=True))])
async def test_a_failure_means_no_prefetch_and_the_turn_continues(slots, search):
    router, coordinator = _router(slots, search)
    with patch("src.agents.core.router_agent.logger") as log:
        await router.execute(_message())
    assert log.error.called
    coordinator.route_message.assert_awaited_once()
    assert coordinator.route_message.call_args.args[0].context["enriched_context"] is None


async def test_no_account_means_no_prefetch():
    slots, search = _Slots(), _Search()
    router, coordinator = _router(slots, search)
    message = AgentMessage.create(
        sender="handler", recipient="router_agent", intent=AgentIntent.QUERY,
        payload={"text": QUESTION}, context={"user_id": "usr-1", "session_id": "s"})
    await router.execute(message)
    assert slots.calls == [] and search.calls == []
    coordinator.route_message.assert_awaited_once()
