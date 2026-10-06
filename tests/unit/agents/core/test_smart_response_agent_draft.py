from dataclasses import dataclass, field
from typing import List
from unittest.mock import AsyncMock, MagicMock

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.core.smart_response_agent import SmartResponseAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.skill import SKILL_PREVIEW_DELIVERY, DraftResult, Skill
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


def _message(interactive_delivery=True):
    context = {"session_id": "u1:C1", "user_id": "u1", "account_id": "a1",
               "current_message_parts": [MessagePart(text="save this as a skill")]}
    if interactive_delivery:
        context["interactive_delivery"] = True
    return AgentMessage.create(sender="router", recipient="smart_response_agent_u1", intent=AgentIntent.QUERY,
                               payload={"text": "save this as a skill"}, context=context)


def _service(skills, code="ab12"):
    s = MagicMock()
    s.list_skills = AsyncMock(return_value=skills)
    drafted = Skill(name="flight-status", description="Use when a flight is asked about.", body="1. Open.")
    s.draft = AsyncMock(return_value=DraftResult(code=code, skill=drafted))
    return s


def _draft_tool_call():
    return ToolCall(name="draft_skill", args={
        "name": "flight-status", "description": "Use when a flight is asked about.", "body": "1. Open.",
    })


async def test_draft_skill_tool_offered_alongside_use_skill_when_interactive():
    agent, llm, _ = _agent(_service([SKILL]))
    llm.generate_content = AsyncMock(return_value=_resp("answer"))

    await agent.execute(_message(interactive_delivery=True))

    tools = llm.generate_content.await_args.kwargs["request"].tools
    assert [t["name"] for t in tools] == ["delegate_to_specialist", "use_skill", "draft_skill"]


async def test_draft_skill_tool_absent_without_interactive_delivery():
    agent, llm, _ = _agent(_service([SKILL]))
    llm.generate_content = AsyncMock(return_value=_resp("answer"))

    await agent.execute(_message(interactive_delivery=False))

    tools = llm.generate_content.await_args.kwargs["request"].tools
    assert [t["name"] for t in tools] == ["delegate_to_specialist", "use_skill"]
    assert "draft_skill" not in [t["name"] for t in tools]


async def test_draft_skill_call_routes_to_skill_service_with_user_id():
    service = _service([SKILL])
    agent, llm, _ = _agent(service)
    llm.generate_content = AsyncMock(side_effect=[
        _resp(tool_calls=[_draft_tool_call()]),
        _resp("Done — paste the command to save it."),
    ])

    response = await agent.execute(_message(interactive_delivery=True))

    assert response.status == AgentStatus.SUCCESS
    service.draft.assert_awaited_once()
    user_id, drafted_skill, changes = service.draft.await_args.args
    assert user_id == "u1"
    assert isinstance(drafted_skill, Skill)
    assert drafted_skill.name == "flight-status"
    assert changes == []


async def test_successful_draft_surfaces_skill_preview_delivery_item():
    agent, llm, _ = _agent(_service([SKILL]))
    llm.generate_content = AsyncMock(side_effect=[
        _resp(tool_calls=[_draft_tool_call()]),
        _resp("Done — paste the command to save it."),
    ])

    response = await agent.execute(_message(interactive_delivery=True))

    assert response.status == AgentStatus.SUCCESS
    preview_items = [d for d in response.delivery_items if d.type == SKILL_PREVIEW_DELIVERY]
    assert len(preview_items) == 1
    assert preview_items[0].data["name"] == "flight-status"
    assert preview_items[0].data["command"] == "$skill save ab12"


async def test_draft_skill_save_code_never_reaches_any_llm_request():
    """RFC §8 core invariant: the save code must never appear in text a model receives.

    Serialises every generate_content request across both turns — system_instruction,
    every message's every part (text/full_text/tool_call/tool_response) — and asserts the
    draft's save code string is absent from all of it, while the delivery item's command
    (never sent to any provider) does carry it.
    """
    agent, llm, _ = _agent(_service([SKILL], code="9f3e7c21"))
    llm.generate_content = AsyncMock(side_effect=[
        _resp(tool_calls=[_draft_tool_call()]),
        _resp("Done — paste the command to save it."),
    ])

    response = await agent.execute(_message(interactive_delivery=True))

    assert response.status == AgentStatus.SUCCESS
    code = "9f3e7c21"

    def _serialise_request(call) -> str:
        request = call.kwargs["request"]
        chunks = [request.system_instruction or ""]
        for message in request.messages:
            for part in message.parts:
                chunks.append(part.model_dump_json())
        return "\n".join(chunks)

    for call in llm.generate_content.await_args_list:
        serialised = _serialise_request(call)
        assert code not in serialised, f"save code leaked into a generate_content request: {serialised!r}"

    preview_items = [d for d in response.delivery_items if d.type == SKILL_PREVIEW_DELIVERY]
    assert len(preview_items) == 1
    assert code in preview_items[0].data["command"]
