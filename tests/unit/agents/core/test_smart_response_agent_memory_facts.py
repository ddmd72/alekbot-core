from dataclasses import dataclass, field
from typing import List
from unittest.mock import AsyncMock, MagicMock

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.core.smart_response_agent import SmartResponseAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.memory_fact import MEMORY_PREVIEW_DELIVERY, MemoryFactDraft, ProposedFact
from src.domain.skill import Skill
from src.domain.user import PerformanceTier, UserBotConfig
from src.infrastructure.task_execution_resolver import TaskExecutionResolver
from src.ports.llm_port import (
    LLMPort, LLMResponse, Message, MessagePart, ProviderCapabilities, ToolCall, UsageMetadata,
)
from src.services.agent_context_builder import AgentExecutionContext

FACT = {"text": "User's passport expires on 2031-03-04.", "source": "passport.pdf"}


@dataclass
class _Session:
    history: List[Message] = field(default_factory=list)


def _resp(text="", tool_calls=None):
    return LLMResponse(text=text, tool_calls=tool_calls or [],
                       usage_metadata=UsageMetadata(prompt_tokens=1, completion_tokens=1, total_tokens=2))


def _agent(memory_fact_service, skills=()):
    llm = MagicMock(spec=LLMPort)
    store = MagicMock()
    store.load_session = AsyncMock(return_value=_Session([]))
    builder = MagicMock()
    builder.build_for_agent = AsyncMock(return_value="SYSTEM_PROMPT")
    coordinator = MagicMock()
    coordinator.get_available_intents_for = MagicMock(return_value=[])
    resolver = MagicMock(spec=TaskExecutionResolver)
    resolver.resolve.return_value = None
    skill_service = MagicMock()
    skill_service.list_skills = AsyncMock(return_value=list(skills))
    ctx = AgentExecutionContext(agent_type="smart", provider=llm, model_name="m",
                                tier=PerformanceTier.BALANCED, capabilities=ProviderCapabilities(),
                                resilience_port=InMemoryProviderResilience())
    agent = SmartResponseAgent(
        config=AgentConfig(agent_id="smart_response_agent_u1", agent_type="smart_response",
                           llm_model="m", timeout_ms=60000, metadata={"user_id": "u1"}),
        execution_context=ctx, session_store=store, prompt_builder=builder, resolver=resolver,
        user_config=UserBotConfig(), coordinator=coordinator, skill_service=skill_service,
        memory_fact_service=memory_fact_service,
    )
    return agent, llm


def _message(interactive_delivery=True):
    context = {"session_id": "u1:C1", "user_id": "u1", "account_id": "a1",
               "current_message_parts": [MessagePart(text="remember the data from this scan")]}
    if interactive_delivery:
        context["interactive_delivery"] = True
    return AgentMessage.create(sender="router", recipient="smart_response_agent_u1", intent=AgentIntent.QUERY,
                               payload={"text": "remember the data from this scan"}, context=context)


def _service(code="ab12"):
    s = MagicMock()
    s.propose = AsyncMock(return_value=code)
    return s


def _tool_names(llm):
    return [t["name"] for t in llm.generate_content.await_args_list[0].kwargs["request"].tools]


async def test_tool_offered_on_an_interactive_turn():
    agent, llm = _agent(_service())
    llm.generate_content = AsyncMock(return_value=_resp("answer"))

    await agent.execute(_message(interactive_delivery=True))

    assert "propose_memory_facts" in _tool_names(llm)


async def test_tool_offered_even_when_the_skill_store_returns_nothing():
    agent, llm = _agent(_service(), skills=())
    llm.generate_content = AsyncMock(return_value=_resp("answer"))

    await agent.execute(_message(interactive_delivery=True))

    assert _tool_names(llm) == ["delegate_to_specialist", "propose_memory_facts"]


async def test_tool_absent_without_interactive_delivery():
    skill = Skill(name="flight-status", description="Use when a flight is asked about.", body="1. Open.", version=1)
    agent, llm = _agent(_service(), skills=[skill])
    llm.generate_content = AsyncMock(return_value=_resp("answer"))

    await agent.execute(_message(interactive_delivery=False))

    assert "propose_memory_facts" not in _tool_names(llm)


async def test_tool_absent_without_the_service():
    agent, llm = _agent(None)
    llm.generate_content = AsyncMock(return_value=_resp("answer"))

    await agent.execute(_message(interactive_delivery=True))

    assert "propose_memory_facts" not in _tool_names(llm)


async def test_call_routes_to_service_with_user_and_origin_session_and_surfaces_the_preview():
    service = _service(code="9f3e")
    agent, llm = _agent(service)
    llm.generate_content = AsyncMock(side_effect=[
        _resp(tool_calls=[ToolCall(name="propose_memory_facts", args={"facts": [FACT]})]),
        _resp("Paste the command to save them."),
    ])

    response = await agent.execute(_message(interactive_delivery=True))

    assert response.status == AgentStatus.SUCCESS
    service.propose.assert_awaited_once_with(
        "u1", "u1:C1", MemoryFactDraft(facts=[ProposedFact(**FACT)]),
    )
    [item] = [d for d in response.delivery_items if d.type == MEMORY_PREVIEW_DELIVERY]
    assert item.data["command"] == "$memory save 9f3e"
    for call in llm.generate_content.await_args_list:
        request = call.kwargs["request"]
        sent = "\n".join([request.system_instruction or ""]
                         + [p.model_dump_json() for m in request.messages for p in m.parts])
        assert "9f3e" not in sent
