"""Smart declares annotate_drive_file only on an owner turn (DRIVE_FILE_NOTES_RFC §4.2, §4.5 layer 1, D5 as amended by
D18, N8): with the service, the DRIVE_NOTES_ENABLED switch, a user, and either `interactive_delivery` or a `run_origin`
of exactly voice:ask_alek / voice:tell_alek. Outside `if skills:`. Never on notify (`background:…`), reminders, the daily
review, /worker or an unknown voice origin; draft_skill and propose_memory_facts stay chat-only. The handler gets the
turn id the entry point fixed, the same on a provider-rotation re-run."""
from dataclasses import dataclass, field
from typing import List
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.core.smart_response_agent import SmartResponseAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.drive_note import DRIVE_NOTE_RECEIPT_DELIVERY, NoteProvenance, NoteReceipt
from src.domain.exceptions import LLMError, TranscriptLockedError
from src.domain.skill import Skill
from src.domain.user import PerformanceTier, UserBotConfig
from src.infrastructure.task_execution_resolver import ExecutionOverride, TaskExecutionResolver
from src.ports.llm_port import LLMPort, LLMResponse, Message, MessagePart, ProviderCapabilities, ToolCall, UsageMetadata
from src.services.agent_context_builder import AgentExecutionContext
from src.services.drive_note_service import DriveNoteService

TOOL = "annotate_drive_file"
NOTES_SKILL = Skill(name="drive-file-notes", description="Use before annotate_drive_file.", body="1. Ask.", version=1)


@dataclass
class _Session:
    history: List[Message] = field(default_factory=list)


def _resp(text="", tool_calls=None):
    return LLMResponse(text=text, tool_calls=tool_calls or [],
                       usage_metadata=UsageMetadata(prompt_tokens=1, completion_tokens=1, total_tokens=2))


def _ctx(llm):
    return AgentExecutionContext(agent_type="smart", provider=llm, model_name="m", tier=PerformanceTier.BALANCED,
                                 capabilities=ProviderCapabilities(), resilience_port=InMemoryProviderResilience())


def _agent(note_service, *, skills=(NOTES_SKILL,), enabled=True, user_id="u1", memory=True, resolver=None):
    llm = MagicMock(spec=LLMPort)
    store = MagicMock()
    store.load_session = AsyncMock(return_value=_Session([]))
    builder = MagicMock()
    builder.build_for_agent = AsyncMock(return_value="SYSTEM_PROMPT")
    coordinator = MagicMock()
    coordinator.get_available_intents_for = MagicMock(return_value=[])
    if resolver is None:
        resolver = MagicMock(spec=TaskExecutionResolver)
        resolver.resolve.return_value = None
    skill_service = MagicMock()
    skill_service.list_skills = AsyncMock(return_value=list(skills))
    memory_service = MagicMock() if memory else None
    agent = SmartResponseAgent(
        config=AgentConfig(agent_id="smart_response_agent_u1", agent_type="smart_response", llm_model="m",
                           timeout_ms=60000, metadata={"user_id": user_id}),
        execution_context=_ctx(llm), session_store=store, prompt_builder=builder, resolver=resolver,
        user_config=UserBotConfig(), coordinator=coordinator, skill_service=skill_service,
        memory_fact_service=memory_service, drive_note_service=note_service, drive_notes_enabled=enabled,
    )
    return agent, llm


def _message(**context):
    base = {"session_id": "u1:C1", "user_id": "u1", "account_id": "a1",
            "current_message_parts": [MessagePart(text="this is Petya's estimate")]}
    base.update(context)
    return AgentMessage.create(sender="router", recipient="smart_response_agent_u1", intent=AgentIntent.QUERY,
                               payload={"text": "this is Petya's estimate"}, context=base)


def _service():
    s = AsyncMock(spec=DriveNoteService)
    s.set.return_value = NoteReceipt(action="set", note_id="n1", ref="drive:a", path="Docs/a.pdf", version=1,
                                     text="Petya's estimate", status="soon")
    s.undo.return_value = NoteReceipt(action="restore", note_id="n1", ref="drive:a", path="Docs/a.pdf", version=2)
    return s


def _tools(llm, call=0):
    return [t["name"] for t in llm.generate_content.await_args_list[call].kwargs["request"].tools]


@pytest.mark.parametrize("context", [
    {"interactive_delivery": True},
    {"run_origin": "voice:ask_alek"},
    {"run_origin": "voice:tell_alek"},
], ids=["chat", "ask_alek", "tell_alek"])
async def test_declared_on_an_owner_turn(context):
    agent, llm = _agent(_service())
    llm.generate_content = AsyncMock(return_value=_resp("ok"))
    await agent.execute(_message(**context))
    assert TOOL in _tools(llm)


@pytest.mark.parametrize("context", [
    {"run_origin": "background:reminder:n1"},
    {"run_origin": "background:daily_email_review"},
    {"run_origin": "background:unknown"},
    {},
    {"run_origin": "voice:something_new"},
    {"run_origin": "voice:"},
    {"run_origin": ["voice:ask_alek"]},
], ids=["notify-reminder", "daily-review", "worker", "no-origin", "unknown-voice", "voice-prefix", "not-a-string"])
async def test_absent_on_every_other_run(context):
    agent, llm = _agent(_service())
    llm.generate_content = AsyncMock(return_value=_resp("ok"))
    await agent.execute(_message(**context))
    assert TOOL not in _tools(llm)


@pytest.mark.parametrize("kwargs", [
    {"note_service": None}, {"enabled": False}, {"user_id": None},
], ids=["no-service", "switch-off", "no-user"])
async def test_absent_without_the_service_the_switch_or_a_user(kwargs):
    note_service = kwargs.pop("note_service", _service())
    agent, llm = _agent(note_service, **kwargs)
    llm.generate_content = AsyncMock(return_value=_resp("ok"))
    message = _message(interactive_delivery=True)
    if kwargs.get("user_id", "u1") is None:
        message.context["user_id"] = None
    await agent.execute(message)
    assert TOOL not in _tools(llm)


async def test_declared_outside_if_skills_and_fails_closed_with_no_skills():
    service = _service()
    agent, llm = _agent(service, skills=(), memory=False)
    llm.generate_content = AsyncMock(side_effect=[
        _resp(tool_calls=[ToolCall(name=TOOL, args={"file_ref": "drive:a", "note": "x"})]), _resp("ok")])
    await agent.execute(_message(interactive_delivery=True))
    assert _tools(llm) == ["delegate_to_specialist", TOOL]
    service.set.assert_not_called()
    second = llm.generate_content.await_args_list[1].kwargs["request"]
    assert "notes are unavailable right now" in second.messages[-1].model_dump_json()


async def test_on_a_call_draft_skill_and_propose_memory_facts_stay_absent():
    agent, llm = _agent(_service())
    agent.skill_service.draft = AsyncMock()
    llm.generate_content = AsyncMock(return_value=_resp("ok"))
    await agent.execute(_message(run_origin="voice:tell_alek"))
    names = _tools(llm)
    assert TOOL in names and "draft_skill" not in names and "propose_memory_facts" not in names


async def test_a_call_is_gated_by_the_skill_and_runs_with_the_fixed_turn_id_after_use_skill():
    service = _service()
    agent, llm = _agent(service)
    llm.generate_content = AsyncMock(side_effect=[
        _resp(tool_calls=[ToolCall(name="use_skill", args={"name": "drive-file-notes"})]),
        _resp(tool_calls=[ToolCall(name=TOOL, args={"file_ref": "drive:a", "note": "Petya's estimate"})]),
        _resp("Noted."),
    ])
    response = await agent.execute(_message(interactive_delivery=True, turn_id="slack:Ev9"))
    assert response.status == AgentStatus.SUCCESS
    service.set.assert_awaited_once_with("u1", "a1", "drive:a", "Petya's estimate", "", [],
                                         NoteProvenance(origin="chat", session_id="u1:C1", turn_id="slack:Ev9"))
    [item] = [d for d in response.delivery_items if d.type == DRIVE_NOTE_RECEIPT_DELIVERY]
    assert item.data["text"] == "Petya's estimate"
    assert response.metadata["drive_note_context"] == ["[Drive note: Docs/a.pdf ref=drive:a v1]"]


async def test_a_voice_turn_writes_with_origin_voice():
    service = _service()
    agent, llm = _agent(service)
    llm.generate_content = AsyncMock(side_effect=[
        _resp(tool_calls=[ToolCall(name="use_skill", args={"name": "drive-file-notes"})]),
        _resp(tool_calls=[ToolCall(name=TOOL, args={"file_ref": "drive:a", "note": "x"})]),
        _resp("Noted."),
    ])
    await agent.execute(_message(run_origin="voice:ask_alek", turn_id="voice:abc"))
    assert service.set.await_args.args[6] == NoteProvenance(origin="voice", session_id="u1:C1", turn_id="voice:abc")


async def test_a_provider_rotation_re_run_sees_the_same_turn_id():
    service = _service()
    other_llm = MagicMock(spec=LLMPort)
    resolver = MagicMock(spec=TaskExecutionResolver)
    resolver.resolve.return_value = None
    resolver.next_provider_override.return_value = ExecutionOverride(
        execution_context=AgentExecutionContext(agent_type="smart", provider=other_llm, model_name="m2",
                                                tier=PerformanceTier.BALANCED, capabilities=ProviderCapabilities(),
                                                resilience_port=InMemoryProviderResilience(),
                                                provider_name="other"),
        thinking_effort=None)
    agent, llm = _agent(service, resolver=resolver)
    undo_call = _resp(tool_calls=[ToolCall(name=TOOL, args={"file_ref": "drive:a", "undo": True})])
    use_skill = _resp(tool_calls=[ToolCall(name="use_skill", args={"name": "drive-file-notes"})])
    llm.generate_content = AsyncMock(side_effect=[use_skill, undo_call, TranscriptLockedError(provider_name="first", cause=LLMError("529"), turn=2)])
    other_llm.generate_content = AsyncMock(side_effect=[use_skill, undo_call, _resp("Undone.")])
    await agent.execute(_message(interactive_delivery=True, turn_id="slack:Ev9"))
    turn_ids = [c.args[3].turn_id for c in service.undo.await_args_list]
    assert turn_ids == ["slack:Ev9", "slack:Ev9"]


async def test_pinned_residual_a_skill_loaded_in_the_same_batch_opens_the_gate():
    """Plan part A §6 / RFC §0.4 delta (review I4, option a): `use_skill` and `annotate_drive_file` emitted in ONE batch
    — the note's arguments were written before the model read the skill — still write the note, because use_skill
    records the load before the note call checks the gate. Accepted: the tool description repeats the said-or-confirmed
    rule and the service's security check stays. A change to this behaviour must change this test on purpose."""
    service = _service()
    agent, llm = _agent(service)
    llm.generate_content = AsyncMock(side_effect=[
        _resp(tool_calls=[ToolCall(name="use_skill", args={"name": "drive-file-notes"}),
                          ToolCall(name=TOOL, args={"file_ref": "drive:a", "note": "Petya's estimate"})]),
        _resp("Noted."),
    ])
    await agent.execute(_message(interactive_delivery=True, turn_id="slack:Ev9"))
    service.set.assert_awaited_once()
