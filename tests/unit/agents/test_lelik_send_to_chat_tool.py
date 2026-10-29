"""Lelik's session has two tools: delegate_to_specialist (a worker) and send_to_chat (a channel, the caller's chat).
VOICE_COMPANION_RFC §4.15.2, revised 2026-10-29: send_to_chat is never an intent of delegate_to_specialist, it runs
as the ASYNC errand through the Alek gateway; ask_alek stays SYNC.

Built over the real registry of ALL_DESCRIPTORS and the real coordinator; only the task queue is a recording fake.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.lelik_agent import LelikAgent
from src.domain.lelik_context import LelikContext
from src.domain.voice_errand import delegation_arguments
from src.domain.voice_provider_profile import VOICE_PROVIDER_PROFILES
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import ALEK, ALL_DESCRIPTORS, Intent, SMART_RESPONSE
from src.infrastructure.agent_registry import AgentRegistry, ExecutionMode


class _RecordingQueue:
    """Stores what would become a Cloud Task."""

    def __init__(self):
        self.tasks = []

    async def enqueue_agent_task(self, agent_id, intent, query, context, deadline_seconds=None, dedup_key=None):
        self.tasks.append({"agent_id": agent_id, "intent": intent, "query": query, "context": context,
                           "deadline_seconds": deadline_seconds})
        return f"task-{len(self.tasks)}"


def _registry():
    registry = AgentRegistry()
    for descriptor in ALL_DESCRIPTORS:
        registry.register(descriptor)
    return registry


def _lelik(registry=None, queue=None):
    prompt_builder = AsyncMock()
    prompt_builder.build_for_agent.return_value = "PROMPT"
    persona = AsyncMock()
    persona.assemble.return_value = LelikContext(biographical_facts=[], conversation_history=[])
    persona.primary_channel.return_value = None
    agent = LelikAgent(
        config=MagicMock(agent_id="lelik_agent_u1"), telephony=AsyncMock(),
        from_number="+1", status_callback_url="https://x/voice/status",
        prompt_builder=prompt_builder, persona=persona, notifications=AsyncMock(),
        voice_profile=VOICE_PROVIDER_PROFILES["xai"],
    )
    agent.coordinator = AgentCoordinator(registry=registry or _registry(), task_queue=queue, alert_sink=AsyncMock())
    return agent


async def _tools(agent):
    return {tool["name"]: tool for tool in (await agent.session_config(user_id="u1", account_id="a1"))["tools"]}


@pytest.mark.asyncio
async def test_the_session_declares_delegate_to_specialist_and_send_to_chat():
    tools = await _tools(_lelik())

    assert set(tools) == {"delegate_to_specialist", "send_to_chat"}


@pytest.mark.asyncio
async def test_send_to_chat_is_not_an_intent_of_delegate_to_specialist():
    delegate = (await _tools(_lelik()))["delegate_to_specialist"]["description"]

    assert "- ask_alek:" in delegate
    assert "send_to_chat" not in delegate
    assert "tell_alek" not in delegate


@pytest.mark.asyncio
async def test_send_to_chat_takes_one_required_request_and_carries_the_manifest_description():
    tool = (await _tools(_lelik()))["send_to_chat"]

    assert tool["description"] == ALEK.capability_descriptions[Intent.SEND_TO_CHAT]
    assert tool["parameters"]["type"] == "object"
    assert set(tool["parameters"]["properties"]) == {"request"}
    assert tool["parameters"]["properties"]["request"]["type"] == "string"
    assert tool["parameters"]["required"] == ["request"]


@pytest.mark.asyncio
async def test_without_the_alek_gateway_registered_there_is_no_send_to_chat():
    registry = AgentRegistry()
    for descriptor in ALL_DESCRIPTORS:
        if descriptor is not ALEK:
            registry.register(descriptor)

    assert "send_to_chat" not in await _tools(_lelik(registry=registry))


def test_send_to_chat_is_an_async_errand_and_ask_alek_stays_sync():
    registry = _registry()

    assert registry.get_execution_mode(Intent.SEND_TO_CHAT) is ExecutionMode.ASYNC
    assert registry.get_execution_mode(Intent.ASK_ALEK) is ExecutionMode.SYNC


def test_smart_is_never_offered_send_to_chat():
    smart = {i["name"] for i in _registry().get_available_intents_for(SMART_RESPONSE)}

    assert "send_to_chat" not in smart and "ask_alek" not in smart


@pytest.mark.asyncio
async def test_a_send_to_chat_call_becomes_a_cloud_task_for_the_alek_gateway():
    queue = _RecordingQueue()
    agent = _lelik(queue=queue)
    arguments = delegation_arguments("send_to_chat", {"request": "the garage rules for guests, as a file"})

    outcome = await agent.delegate_outcome("u1", "a1", arguments, call_context=[{"role": "user", "text": "send it"}])

    assert not outcome.failed
    [task] = queue.tasks
    assert task["agent_id"] == ALEK.agent_id and task["intent"] == "send_to_chat"
    assert task["query"].endswith("the garage rules for guests, as a file")
    assert task["context"]["user_id"] == "u1"
    assert task["deadline_seconds"] == ALEK.dispatch_deadline_s
