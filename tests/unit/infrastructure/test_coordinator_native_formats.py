"""The coordinator keeps the native-format declaration only for the opening intents (U2 P2-4, G3)."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.agent import AgentResponse
from src.domain.native_format import NATIVE_FORMATS_KEY
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import NATIVE_OPENING_INTENTS

DECLARATION = [{"mime": "application/pdf", "max_bytes": 1000, "max_pages": 5}]
NON_OPENING = ["search_web", "tutor_chat", "open_file_x"]


def _context():
    return {"user_id": "user-1", "account_id": "acc-1", NATIVE_FORMATS_KEY: list(DECLARATION), "params": {}}


def _sync_coordinator():
    coordinator = AgentCoordinator()
    coordinator.route_message = AsyncMock(return_value=AgentResponse.success(task_id="t", agent_id="a", result="r"))
    return coordinator


@pytest.mark.parametrize("intent", sorted(NATIVE_OPENING_INTENTS))
async def test_an_opening_intent_keeps_the_key_on_its_message(intent):
    coordinator = _sync_coordinator()
    context = _context()
    await coordinator._execute_sync("drive_agent", intent, "q", context)
    sent = coordinator.route_message.call_args.args[0]
    assert sent.context[NATIVE_FORMATS_KEY] == DECLARATION


@pytest.mark.parametrize("intent", NON_OPENING)
async def test_another_intent_never_sees_the_key(intent):
    coordinator = _sync_coordinator()
    context = _context()
    await coordinator._execute_sync("web_search_agent", intent, "q", context)
    sent = coordinator.route_message.call_args.args[0]
    assert NATIVE_FORMATS_KEY not in sent.context
    assert context[NATIVE_FORMATS_KEY] == DECLARATION   # the caller's own context is not mutated


def _async_coordinator():
    queue = MagicMock()
    queue.enqueue_agent_task = AsyncMock(return_value="task-1")
    return AgentCoordinator(task_queue=queue), queue


@pytest.mark.parametrize("intent", NON_OPENING)
async def test_a_cloud_task_body_never_carries_the_key(intent):
    coordinator, queue = _async_coordinator()
    context = _context()
    await coordinator._execute_async("deep_research_agent", intent, "q", context)
    assert NATIVE_FORMATS_KEY not in queue.enqueue_agent_task.call_args.kwargs["context"]
    assert context[NATIVE_FORMATS_KEY] == DECLARATION


@pytest.mark.parametrize("intent", sorted(NATIVE_OPENING_INTENTS))
async def test_an_opening_intent_enqueued_keeps_the_key(intent):
    coordinator, queue = _async_coordinator()
    await coordinator._execute_async("drive_agent", intent, "q", _context())
    assert queue.enqueue_agent_task.call_args.kwargs["context"][NATIVE_FORMATS_KEY] == DECLARATION


async def test_a_context_without_the_key_passes_through_unchanged():
    coordinator = _sync_coordinator()
    await coordinator._execute_sync("web_search_agent", "search_web", "q", {"user_id": "u"})
    assert coordinator.route_message.call_args.args[0].context == {"user_id": "u"}
