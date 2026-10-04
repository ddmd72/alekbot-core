from dataclasses import dataclass, field
from typing import List
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.core.smart_response_agent import SmartResponseAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.skill import Skill
from src.domain.user import PerformanceTier, UserBotConfig
from src.infrastructure.task_execution_resolver import TaskExecutionResolver
from src.ports.llm_port import (
    LLMPort, LLMResponse, Message, MessagePart, ProviderCapabilities, ToolCall, UsageMetadata,
)
from src.services.agent_context_builder import AgentExecutionContext

SKILL = Skill(name="flight-status", description="Use when a flight is asked about.", body="1. Open.", version=1)


@dataclass
class _Session:
    history: List[Message] = field(default_factory=list)


def _resp(text="", tool_calls=None):
    return LLMResponse(text=text, tool_calls=tool_calls or [],
                       usage_metadata=UsageMetadata(prompt_tokens=1, completion_tokens=1, total_tokens=2))


def _agent(skill_service, history=None):
    llm = MagicMock(spec=LLMPort)
    store = MagicMock()
    store.load_session = AsyncMock(return_value=_Session(history or []))
    builder = MagicMock()
    builder.build_for_agent = AsyncMock(return_value="SYSTEM_PROMPT")
    coordinator = MagicMock()
    coordinator.get_available_intents_for = MagicMock(return_value=[])
    resolver = MagicMock(spec=TaskExecutionResolver)
    resolver.resolve.return_value = None
    ctx = AgentExecutionContext(agent_type="smart", provider=llm, model_name="m",
                                tier=PerformanceTier.BALANCED, capabilities=ProviderCapabilities(),
                                resilience_port=InMemoryProviderResilience())
    agent = SmartResponseAgent(
        config=AgentConfig(agent_id="smart_response_agent_u1", agent_type="smart_response",
                           llm_model="m", timeout_ms=60000, metadata={"user_id": "u1"}),
        execution_context=ctx, session_store=store, prompt_builder=builder, resolver=resolver,
        user_config=UserBotConfig(), coordinator=coordinator, skill_service=skill_service,
    )
    return agent, llm, builder


def _message():
    return AgentMessage.create(sender="router", recipient="smart_response_agent_u1", intent=AgentIntent.QUERY,
                               payload={"text": "status of IB123?"},
                               context={"session_id": "u1:C1", "user_id": "u1", "account_id": "a1",
                                        "current_message_parts": [MessagePart(text="status of IB123?")]})


def _service(skills):
    s = MagicMock()
    s.list_skills = AsyncMock(return_value=skills)
    return s


async def test_catalog_and_tool_offered_when_user_has_skills():
    agent, llm, builder = _agent(_service([SKILL]))
    llm.generate_content = AsyncMock(return_value=_resp("answer"))

    await agent.execute(_message())

    catalog = builder.build_for_agent.await_args.kwargs["skills_catalog"]
    assert "flight-status — Use when a flight is asked about." in catalog
    tools = llm.generate_content.await_args.kwargs["request"].tools
    assert [t["name"] for t in tools] == ["delegate_to_specialist", "use_skill"]


async def test_no_skills_no_catalog_no_tool():
    agent, llm, builder = _agent(_service([]))
    llm.generate_content = AsyncMock(return_value=_resp("answer"))

    await agent.execute(_message())

    assert builder.build_for_agent.await_args.kwargs["skills_catalog"] is None
    assert [t["name"] for t in llm.generate_content.await_args.kwargs["request"].tools] == ["delegate_to_specialist"]


async def test_skill_store_failure_degrades_to_no_skills():
    service = MagicMock()
    service.list_skills = AsyncMock(side_effect=RuntimeError("firestore down"))
    agent, llm, builder = _agent(service)
    llm.generate_content = AsyncMock(return_value=_resp("answer"))

    response = await agent.execute(_message())

    assert response.status == AgentStatus.SUCCESS
    assert builder.build_for_agent.await_args.kwargs["skills_catalog"] is None


async def test_use_skill_loads_body_and_surfaces_skill_context():
    agent, llm, _ = _agent(_service([SKILL]))
    llm.generate_content = AsyncMock(side_effect=[
        _resp(tool_calls=[ToolCall(name="use_skill", args={"name": "flight-status"})]),
        _resp("Gate B12, on time."),
    ])

    response = await agent.execute(_message())

    assert response.status == AgentStatus.SUCCESS
    assert response.metadata["skill_context"] == [{"name": "flight-status", "version": 1, "body": "1. Open."}]
    second = llm.generate_content.await_args_list[1].kwargs["request"]
    assert "1. Open." in str(second.messages[-1].parts[-1].tool_response)


async def test_body_visible_in_recent_history_is_not_reloaded():
    history = [
        Message(role="user", parts=[MessagePart(text="status of IB123?")]),
        Message(role="model", parts=[MessagePart(text="short", full_text='answer\n\n[Skill "flight-status" v1]\n1. Open.')]),
    ]
    agent, llm, _ = _agent(_service([SKILL]), history=history)
    llm.generate_content = AsyncMock(side_effect=[
        _resp(tool_calls=[ToolCall(name="use_skill", args={"name": "flight-status"})]),
        _resp("done"),
    ])

    response = await agent.execute(_message())

    assert "skill_context" not in response.metadata
    second = llm.generate_content.await_args_list[1].kwargs["request"]
    assert "already in your context" in str(second.messages[-1].parts[-1].tool_response)
