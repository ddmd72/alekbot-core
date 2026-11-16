"""Mutation-survivor kills for src.agents.core.router_agent (keys ea4b34c9, 1fcf455c, 386d02e8, c9d9be7d)."""
import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.core.router_agent import create_router_agent
from src.domain.agent import AgentIntent, AgentMessage
from src.domain.fact_query import FactQuery
from src.domain.request_context import RequestContext
from src.ports.fact_search_port import FactSearchPort
from src.ports.repository import FactRepository


def _message(text: str = "hello") -> AgentMessage:
    return AgentMessage.create(
        sender="conversation_handler",
        recipient="router_agent",
        intent=AgentIntent.QUERY,
        payload={"text": text},
        context={"user_id": "user-1", "session_id": "session-1"},
    )


class _RecordingSearch(FactSearchPort):
    def __init__(self):
        self.queries = []

    async def search(self, account_id, query, *, exclude=frozenset()):
        self.queries.append(query)
        return []

    async def search_phrases(self, account_id, queries):
        raise AssertionError("unused")


class _BlockingSlots:
    """Slots read that never finishes until cancelled; records the cancellation."""

    def __init__(self):
        self.started = asyncio.Event()
        self.cancelled = False

    async def query_for_messages(self, messages, *, path, account_id, user_id):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


async def test_triage_failure_propagates_and_cancels_prefetch(monkeypatch):
    """ea4b34c9: a failing triage re-raises (never returns None) and the prefetch task is cancelled."""
    slots = _BlockingSlots()
    agent = create_router_agent(fact_search=_RecordingSearch(), slots=slots, user_id="user-1")

    async def boom(*args, **kwargs):
        await slots.started.wait()
        raise RuntimeError("triage down")

    monkeypatch.setattr(agent, "_classify_request_with_fallback", boom)
    with RequestContext(user_id="user-1", account_id="acc-1"):
        with pytest.raises(RuntimeError, match="triage down"):
            await agent.execute(_message())
    await asyncio.sleep(0)
    assert slots.cancelled is True


async def test_biographical_failure_still_runs_the_search():
    """1fcf455c: a failing baseline read is swallowed; the scored search still runs and yields a context."""
    repo = MagicMock(spec=FactRepository)
    repo.get_biographical_context_cached = AsyncMock(side_effect=RuntimeError("store down"))
    search = _RecordingSearch()
    agent = create_router_agent(
        repository=repo, fact_search=search, slots=_BlockingSlots(), user_id="user-1",
    )
    task = asyncio.get_running_loop().create_future()
    task.set_result(FactQuery(today=date(2026, 11, 10), subject="s", path="router", message="s"))
    with RequestContext(user_id="user-1", account_id="acc-1"):
        context = await agent._finish_prefetch(task, _message())  # type: ignore[arg-type]
    assert len(search.queries) == 1
    assert context is not None


def test_factory_forwards_user_location():
    """386d02e8: the factory's user_location reaches the built agent."""
    assert create_router_agent(user_location="Springfield, Portugal").user_location == "Springfield, Portugal"


def test_factory_forwards_prefetch_limit():
    """c9d9be7d: the factory's prefetch_limit reaches the built agent."""
    assert create_router_agent(prefetch_limit=3).prefetch_limit == 3
