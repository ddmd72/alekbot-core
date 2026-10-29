"""Smart declares the workspace tool on every path where the service is configured, outside `if skills:`, and
labels every write with the run's origin and fire key (AGENT_WORKSPACE_RFC §7.1, §9, D2, D5)."""
import logging
from dataclasses import dataclass, field
from typing import List
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.core.smart_response_agent import SmartResponseAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.skill import Skill
from src.domain.user import PerformanceTier, UserBotConfig
from src.domain.workspace import WorkspaceResult
from src.infrastructure.task_execution_resolver import TaskExecutionResolver
from src.ports.llm_port import LLMPort, LLMResponse, Message, MessagePart, ProviderCapabilities, ToolCall, UsageMetadata
from src.services.agent_context_builder import AgentExecutionContext

F = "reminder:1729401234567@2026-10-20T06:00:00+00:00"
CHAT = {"interactive_delivery": True}
REMINDER = {"run_origin": "background:execute_reminder", "run_fire_key": F}
ERRAND = {"run_origin": "voice:send_to_chat"}


@dataclass
class _Session:
    history: List[Message] = field(default_factory=list)


def _resp(text="", tool_calls=None):
    return LLMResponse(text=text, tool_calls=tool_calls or [],
                       usage_metadata=UsageMetadata(prompt_tokens=1, completion_tokens=1, total_tokens=2))


def _agent(workspace_service, skills=(), list_skills=None):
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
    skill_service.list_skills = list_skills or AsyncMock(return_value=list(skills))
    ctx = AgentExecutionContext(agent_type="smart", provider=llm, model_name="m", tier=PerformanceTier.BALANCED,
                                capabilities=ProviderCapabilities(), resilience_port=InMemoryProviderResilience())
    agent = SmartResponseAgent(
        config=AgentConfig(agent_id="smart_response_agent_u1", agent_type="smart_response", llm_model="m",
                           timeout_ms=60000, metadata={"user_id": "u1"}),
        execution_context=ctx, session_store=store, prompt_builder=builder, resolver=resolver,
        user_config=UserBotConfig(), coordinator=coordinator, skill_service=skill_service,
        workspace_service=workspace_service,
    )
    return agent, llm


def _message(extra):
    context = {"session_id": "u1:C1", "user_id": "u1", "account_id": "a1",
               "current_message_parts": [MessagePart(text="where are we with the garden shed?")], **extra}
    return AgentMessage.create(sender="router", recipient="smart_response_agent_u1", intent=AgentIntent.QUERY,
                               payload={"text": "where are we with the garden shed?"}, context=context)


def _service():
    service = MagicMock()
    service.run = AsyncMock(return_value=WorkspaceResult(text="[workspace:projects/garden-shed.md · generation 101]"))
    return service


def _tool_names(llm):
    return [t["name"] for t in llm.generate_content.await_args_list[0].kwargs["request"].tools]


def _view_then_answer(llm):
    llm.generate_content = AsyncMock(side_effect=[
        _resp(tool_calls=[ToolCall(name="workspace", args={"command": "view", "path": "projects/garden-shed.md"})]),
        _resp("answer"),
    ])


@pytest.mark.parametrize("extra", [CHAT, REMINDER, ERRAND, {}])
async def test_declared_on_every_path(extra):
    agent, llm = _agent(_service())
    llm.generate_content = AsyncMock(return_value=_resp("answer"))
    await agent.execute(_message(extra))
    assert _tool_names(llm) == ["delegate_to_specialist", "workspace"]


async def test_declared_when_the_skill_store_fails():
    agent, llm = _agent(_service(), list_skills=AsyncMock(side_effect=RuntimeError("firestore down")))
    llm.generate_content = AsyncMock(return_value=_resp("answer"))
    await agent.execute(_message(CHAT))
    assert "workspace" in _tool_names(llm)


async def test_absent_without_the_service():
    agent, llm = _agent(None)
    llm.generate_content = AsyncMock(return_value=_resp("answer"))
    await agent.execute(_message(CHAT))
    assert "workspace" not in _tool_names(llm)


@pytest.mark.parametrize("extra,origin,fire", [
    (CHAT, "chat", None),
    (REMINDER, "background:execute_reminder", F),
    (ERRAND, "voice:send_to_chat", None),
    ({"run_origin": "voice:ask_alek"}, "voice:ask_alek", None),
    ({"run_origin": "background:daily_email_review"}, "background:daily_email_review", None),
])
async def test_origin_and_fire_reach_the_service(extra, origin, fire):
    service = _service()
    agent, llm = _agent(service)
    _view_then_answer(llm)
    response = await agent.execute(_message(extra))
    assert response.status == AgentStatus.SUCCESS
    call, command, args = service.run.await_args.args
    assert (call.user_id, call.origin, call.fire_key, command) == ("u1", origin, fire, "view")
    assert args["path"] == "projects/garden-shed.md"


async def test_missing_origin_falls_back_with_a_warning(caplog):
    service = _service()
    agent, llm = _agent(service)
    _view_then_answer(llm)
    with caplog.at_level(logging.WARNING):
        await agent.execute(_message({}))
    assert service.run.await_args.args[0].origin == "background:unknown"
    assert "run_origin missing" in caplog.text


async def test_references_carry_loaded_skills_and_reminders():
    service = _service()
    skill = Skill(name="morning-briefing", description="Use for the morning briefing.",
                  body="View `workspace:projects/morning-briefing.md` first.", version=1)
    notes = [{"note_id": "1", "text": "Morning briefing", "workspace_file": "projects/morning-briefing.md"}]
    agent, llm = _agent(service, skills=[skill])
    _view_then_answer(llm)
    await agent.execute(_message({**CHAT, "agent_notes": notes}))
    call = service.run.await_args.args[0]
    assert call.references.skills == (("morning-briefing", skill.body),)
    assert call.references.notes == tuple(notes)


async def test_the_result_reaches_the_model():
    agent, llm = _agent(_service())
    _view_then_answer(llm)
    await agent.execute(_message(CHAT))
    second = llm.generate_content.await_args_list[1].kwargs["request"]
    sent = "\n".join(p.model_dump_json() for m in second.messages for p in m.parts)
    assert "generation 101" in sent
