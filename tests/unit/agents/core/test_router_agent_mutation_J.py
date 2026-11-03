"""Mutation survivors of src/agents/core/router_agent.py (group J): routed context, triage fallback, parse errors, factory wiring."""
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.core.router_agent import RouterAgent, create_router_agent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse
from src.domain.agent_note import AgentNote
from src.domain.llm import Message, MessagePart
from src.domain.session import SessionState
from src.domain.user import PerformanceTier
from src.ports.agent_note_port import AgentNotePort
from src.ports.llm_port import AgentExecutionContext, LLMPort, ProviderCapabilities
from src.ports.prompt_builder_port import PromptBuilderPort
from src.ports.session_store import SessionStore


def _query(text: str, session_id: str = "session456") -> AgentMessage:
    return AgentMessage.create(
        sender="conversation_handler", recipient="router_agent", intent=AgentIntent.QUERY,
        payload={"text": text}, context={"user_id": "user123", "session_id": session_id},
    )


def _ec(llm) -> AgentExecutionContext:
    return AgentExecutionContext(
        agent_type="router", provider=llm, model_name="gemini-flash", tier=PerformanceTier.ECO,
        capabilities=ProviderCapabilities(), resilience_port=InMemoryProviderResilience(),
    )


def _config() -> AgentConfig:
    return AgentConfig(agent_id="router_agent", agent_type="router", llm_model=None, timeout_ms=None,
                       capabilities=["classification", "routing"])


def _llm(text: str = "{}"):
    llm = MagicMock(spec=LLMPort)
    llm.generate_content = AsyncMock(return_value=MagicMock(text=text))
    return llm


def _pb():
    pb = AsyncMock(spec=PromptBuilderPort)
    pb.build_for_agent.return_value = "You are a triage router."
    return pb


class _HistoryStore(SessionStore):
    def __init__(self, history):
        self._state = SessionState(history=history)

    async def load_session(self, session_id):
        return self._state

    async def save_session(self, session_id, state): ...
    async def append_message(self, session_id, message, owner_id=None): ...
    async def append_messages_batch(self, *a, **k): ...
    async def get_latest_session_id(self, owner_id): return None


async def test_routed_message_carries_active_notes_in_context():
    note = AgentNote(note_id="n1", user_id="user123", text="Buy milk", instruction="Go to the shop",
                     created_at=datetime(2026, 10, 19, tzinfo=timezone.utc),
                     due=datetime(2026, 10, 21, 6, tzinfo=timezone.utc))
    notes = AsyncMock(spec=AgentNotePort)
    notes.list_active_notes = AsyncMock(return_value=[note])
    coordinator = MagicMock()
    coordinator.route_message = AsyncMock(
        return_value=AgentResponse.success(task_id="t", agent_id="smart", result="ok", confidence=1.0))
    agent = RouterAgent(config=_config(), coordinator=coordinator, quick_agent_id="quick_agent",
                        smart_agent_id="smart_agent", user_id="user123", notes_port=notes)

    await agent.execute(_query("hello"))

    routed = coordinator.route_message.call_args.args[0]
    assert [n["note_id"] for n in routed.context["agent_notes"]] == ["n1"]
    assert routed.context["agent_notes"][0]["text"] == "Buy milk"


async def test_failed_llm_triage_returns_the_rule_based_classification():
    llm = _llm()
    llm.generate_content.side_effect = RuntimeError("provider down")
    agent = RouterAgent(config=_config(), execution_context=_ec(llm), prompt_builder=_pb())

    result = await agent._classify_request_with_fallback("hello", _query("hello"), None)

    assert result == agent._classify_request("hello")
    assert result


async def test_triage_without_history_sends_the_current_text():
    llm = _llm('{"metadata": {}}')
    agent = RouterAgent(config=_config(), execution_context=_ec(llm), prompt_builder=_pb())

    await agent._classify_with_llm("what is the weather", _query("what is the weather"), None)

    request = llm.generate_content.call_args.kwargs["request"]
    assert [m.parts[0].text for m in request.messages] == ["what is the weather"]


async def test_malformed_triage_json_raises_decode_error():
    agent = RouterAgent(config=_config(), execution_context=_ec(_llm("{broken: json}")), prompt_builder=_pb())

    with pytest.raises(json.JSONDecodeError):
        await agent._classify_with_llm("hi", _query("hi"), None)


async def test_factory_wires_session_store_into_triage_history():
    store = _HistoryStore([Message(role="user", parts=[MessagePart(text="earlier question")]),
                           Message(role="model", parts=[MessagePart(text="earlier answer")])])
    llm = _llm('{"metadata": {}}')
    agent = create_router_agent(execution_context=_ec(llm), prompt_builder=_pb(), session_store=store,
                                user_id="user123")

    assert agent.session_store is store
    await agent.execute(_query("follow up"))

    request = llm.generate_content.call_args.kwargs["request"]
    assert any("earlier question" in p.text for m in request.messages for p in m.parts)
