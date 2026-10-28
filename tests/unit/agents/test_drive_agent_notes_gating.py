"""The drive specialist declares and serves the note tools by turn (DRIVE_SPECIALIST_RFC §9.1; design §4.4, Δ6, Δ7,
Δ12; DRIVE_FILE_NOTES_RFC §4.2, §4.5 layer 1, D5 as amended by D18, N8).

The six cases of the retired Smart gate, re-homed on `DriveAgent`: `get_note` whenever the service is present, the switch
is on and there is a user; the ten writers only on an owner turn (a chat message, or a call with Lelik whose
`run_origin` is exactly voice:ask_alek / voice:tell_alek); never on a reminder, the daily review, /worker or an unknown
origin. A writer's provenance carries the origin, the session and the turn id the entry point fixed; the receipt and the
history line come out of the answer; a provider-rotation re-run keeps the turn id. Synthetic data only."""
import inspect
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.core.quick_response_agent import QuickResponseAgent
from src.agents.core.smart_response_agent import SmartResponseAgent
from src.agents.drive_agent import DriveAgent
from src.composition.user_agent_factory import UserAgentFactory, _UserContext
from src.domain import drive_note_tool_texts as nt
from src.domain import drive_tool_texts as tt
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse
from src.domain.drive_note import (
    DRIVE_NOTE_RECEIPT_DELIVERY, NoteContent, NoteProvenance, NoteReceipt)
from src.domain.llm import PROMPT_CACHE_BOUNDARY, LLMRequest
from src.domain.turn_identity import TURN_ID_CONTEXT_KEY
from src.domain.user import UserBotConfig
from src.domain.user_drive import DriveConnection
from src.infrastructure.agent_config import DRIVE
from src.infrastructure.drive_operations import DriveOperations
from src.infrastructure.drive_note_tools import DRIVE_NOTE_CONTEXT_KEY
from src.services.drive_note_service import DriveNoteService
from src.services.user_drive_service import UserDriveService
from tests.support.scripted_llm import ScriptedLLM, calls, execution_context, script
from tests.support.tool_e2e import StubPromptBuilder
from tests.unit.services.test_drive_note_service import ACC, U, Env

REPAIR = "the bathroom repair invoice from Reformas Exemplo"
PATH = "Home/bathroom-invoice.pdf"
CREATE = {"item_ref": "drive:a", "text": REPAIR}
REPORT = {"note": "Done."}
NOTE_WRITERS = [name for name in nt.NOTE_TOOLS if name != nt.GET_NOTE]

CHAT = {"interactive_delivery": True}
CALL_ASK = {"run_origin": "voice:ask_alek"}
CALL_TELL = {"run_origin": "voice:tell_alek"}


def _receipt(version: int = 1) -> NoteReceipt:
    return NoteReceipt(action="create", note_id="n1", ref="drive:a", path=PATH, version=version, text=REPAIR,
                       status="not_indexed", state="active")


def _service() -> AsyncMock:
    service = AsyncMock(spec=DriveNoteService)
    service.create.return_value = _receipt()
    return service


def _operations() -> DriveOperations:
    drive = AsyncMock(spec=UserDriveService)
    drive.connection.return_value = DriveConnection(connected=True, drive_id="d1")
    return DriveOperations(conversion_service=MagicMock(), drive_service=drive)


def _agent(notes: Optional[Any], *, enabled: bool = True) -> DriveAgent:
    prompts = StubPromptBuilder(f"DriveAgent prompt\n\n{PROMPT_CACHE_BOUNDARY}")
    return DriveAgent(AgentConfig(agent_id="drive_agent_u", agent_type="drive", timeout_ms=DRIVE.timeout_ms),
                      execution_context=execution_context(ScriptedLLM([]), "drive"), operations=_operations(),
                      prompt_builder=prompts, notes=notes, notes_enabled=enabled)


def _message(**context: Any) -> AgentMessage:
    ctx: Dict[str, Any] = {"user_id": U, "account_id": ACC, "session_id": "s1", TURN_ID_CONTEXT_KEY: "t1"}
    ctx.update(context)
    ctx = {k: v for k, v in ctx.items() if v is not None}
    return AgentMessage.create(sender="coordinator", recipient="drive_agent_u", intent=AgentIntent.QUERY,
                               payload={"query": "Keep the invoice on the file."}, context=ctx)


async def _run(agent: DriveAgent, *turns: Tuple[Tuple[str, dict], ...], message: Optional[AgentMessage] = None,
               **context: Any) -> Tuple[AgentResponse, List[LLMRequest]]:
    agent._call_llm, seen = script(*[calls(*turn) for turn in turns], calls((tt.REPORT_TOOL, REPORT)))
    response = await agent.execute(message if message is not None else _message(**context))
    return response, seen


def _declared(seen: List[LLMRequest]) -> List[str]:
    return [tool["name"] for tool in seen[0].tools]


# -- 1. declared on an owner turn -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("context", [CHAT, CALL_ASK, CALL_TELL], ids=["chat", "ask_alek", "tell_alek"])
async def test_the_eleven_note_tools_are_declared_and_served_on_an_owner_turn(context):
    service = _service()
    response, seen = await _run(_agent(service), ((nt.CREATE_NOTE, CREATE),), **context)
    names = _declared(seen)
    assert names[-1] == tt.REPORT_TOOL and names[-1 - len(nt.NOTE_TOOLS):-1] == list(nt.NOTE_TOOLS)   # Δ16 order
    service.create.assert_awaited_once()
    assert response.history_context[DRIVE_NOTE_CONTEXT_KEY] == [f"[Drive note: {PATH} ref=drive:a v1]"]


# -- 2. absent on every other run (get_note stays: it reads, §9.1 / Δ6) -----------------------------------------------------

@pytest.mark.parametrize("context", [
    {"run_origin": "background:fire_due_reminders"}, {"run_origin": "background:daily_email_review"},
    {"run_origin": "background:unknown"}, {}, {"run_origin": "voice:unknown"}, {"run_origin": "voice:"},
    {"run_origin": 7}, {"run_origin": ["voice:ask_alek"]},
], ids=["reminder", "daily_review", "background_unknown", "no_origin", "unknown_voice", "bare_voice", "int_origin",
        "list_origin"])
async def test_no_writer_is_declared_or_served_off_an_owner_turn_but_get_note_is(context):
    service = _service()
    _, seen = await _run(_agent(service), ((nt.CREATE_NOTE, CREATE),), **context)
    names = _declared(seen)
    assert nt.GET_NOTE in names and not set(NOTE_WRITERS) & set(names)
    service.create.assert_not_awaited()
    assert service.mock_calls == []                       # the refused call reached no service at all


# -- 3. absent without the service, the switch or a user ----------------------------------------------------------------------

async def test_no_note_tool_without_the_service():
    _, seen = await _run(_agent(None), **CHAT)
    assert not set(nt.NOTE_TOOLS) & set(_declared(seen))


async def test_no_note_tool_with_the_switch_off():
    service = _service()
    _, seen = await _run(_agent(service, enabled=False), ((nt.CREATE_NOTE, CREATE),), **CHAT)
    assert not set(nt.NOTE_TOOLS) & set(_declared(seen))
    assert service.mock_calls == []


async def test_no_note_tool_without_a_user():
    service = _service()
    _, seen = await _run(_agent(service), user_id=None, **CHAT)
    assert not set(nt.NOTE_TOOLS) & set(_declared(seen))
    assert service.mock_calls == []


# -- 4. a call's write has provenance origin voice -------------------------------------------------------------------------

async def test_a_calls_write_carries_origin_voice_the_session_and_the_turn_id():
    service = _service()
    await _run(_agent(service), ((nt.CREATE_NOTE, CREATE),), session_id="call-1", **{TURN_ID_CONTEXT_KEY: "voice:v7"},
               **CALL_TELL)
    (call,) = service.create.await_args_list
    assert call.args[4] == NoteProvenance(origin="voice", session_id="call-1", turn_id="voice:v7")
    assert call.args[3] == NoteContent(REPAIR)


# -- 5. a chat write: the fixed turn id, the receipt, the history line -------------------------------------------------------

async def test_a_chat_write_runs_with_the_fixed_turn_id_and_the_answer_carries_receipt_and_history_line():
    service = _service()
    response, _ = await _run(_agent(service), ((nt.CREATE_NOTE, CREATE),), session_id="s1",
                             **{TURN_ID_CONTEXT_KEY: "slack:Ev1"}, **CHAT)
    assert service.create.await_args.args[4] == NoteProvenance(origin="chat", session_id="s1", turn_id="slack:Ev1")
    assert [(i.type, i.data) for i in response.delivery_items] == [
        (DRIVE_NOTE_RECEIPT_DELIVERY, _receipt().delivery_data())]
    assert response.history_context == {DRIVE_NOTE_CONTEXT_KEY: [f"[Drive note: {PATH} ref=drive:a v1]"]}
    assert REPAIR not in response.result                   # the system posts the stored note; the answer repeats none


# -- 6. a provider-rotation re-run keeps the same turn id on both attempts ------------------------------------------------------

async def test_a_rerun_of_the_same_turn_carries_the_same_turn_id_on_both_attempts():
    service = _service()
    message = _message(**{TURN_ID_CONTEXT_KEY: "slack:Ev2"}, **CHAT)
    await _run(_agent(service), ((nt.CREATE_NOTE, CREATE),), message=message)
    await _run(_agent(service), ((nt.CREATE_NOTE, CREATE),), message=message)
    turn_ids = [call.args[4].turn_id for call in service.create.await_args_list]
    assert turn_ids == ["slack:Ev2", "slack:Ev2"]


async def test_a_rerun_of_an_append_writes_the_words_once():
    env = Env()
    await env.service.set(U, ACC, "drive:a", REPAIR, "", [], NoteProvenance(origin="chat", session_id="s0", turn_id="t0"))
    message = _message(**{TURN_ID_CONTEXT_KEY: "slack:Ev3"}, **CHAT)
    add = ((nt.ADD_TO_NOTE, {"item_ref": "drive:a", "text": "second invoice"}),)
    for _ in range(2):
        await _run(_agent(env.service), add, message=message)
    note = await env.note("a")
    assert note.text == f"{REPAIR} second invoice" and note.current_version == 2


# -- Δ7: a note tool beside the report still leaves its history line and receipt ---------------------------------------------------

async def test_a_note_tool_in_the_batch_of_the_report_still_leaves_its_history_line_and_receipt():
    service = _service()
    agent = _agent(service)
    agent._call_llm, _ = script(calls((nt.CREATE_NOTE, CREATE), (tt.REPORT_TOOL, REPORT)))
    response = await agent.execute(_message(**CHAT))
    service.create.assert_awaited_once()
    assert response.history_context[DRIVE_NOTE_CONTEXT_KEY] == [f"[Drive note: {PATH} ref=drive:a v1]"]
    assert [i.type for i in response.delivery_items] == [DRIVE_NOTE_RECEIPT_DELIVERY]


# -- composition ---------------------------------------------------------------------------------------------------------------------

def _factory(**over: Any) -> SimpleNamespace:
    context_builder = MagicMock()
    context_builder.build.return_value = MagicMock(provider=MagicMock(), model_name="claude-sonnet-5-5")
    fields = dict(context_builder=context_builder, file_conversion_service=MagicMock(), file_storage=MagicMock(),
                  notification_service=MagicMock(), user_drive_service=MagicMock(), localization=MagicMock(),
                  language_service=MagicMock(), drive_search_service=MagicMock(), notes_provider=None,
                  drive_note_service=None, drive_notes_enabled=True, alert_sink=None)
    fields.update(over)
    fake = SimpleNamespace(**fields)
    fake._drive_operations = lambda ctx: UserAgentFactory._drive_operations(fake, ctx)
    return fake


def _ctx() -> _UserContext:
    profile = MagicMock()
    profile.config = UserBotConfig()
    return _UserContext(user_profile=profile, prompt_builder=AsyncMock(name="prompt_builder"))


def test_the_factory_gives_the_specialist_the_service_and_the_switch():
    service = _service()
    on = UserAgentFactory._build_drive(_factory(drive_note_service=service), "u", _ctx())
    off = UserAgentFactory._build_drive(_factory(drive_note_service=service, drive_notes_enabled=False), "u", _ctx())
    bare = UserAgentFactory._build_drive(_factory(), "u", _ctx())
    assert (on._notes, on._notes_enabled) == (service, True)
    assert (off._notes, off._notes_enabled) == (service, False)
    assert bare._notes is None


def test_the_operations_the_factory_builds_read_the_same_service_for_the_open_header():
    service = _service()
    agent = UserAgentFactory._build_drive(_factory(drive_note_service=service), "u", _ctx())
    assert agent.operations._drive_notes is service


def test_quick_and_smart_take_no_note_service():
    for cls in (QuickResponseAgent, SmartResponseAgent):
        assert not [p for p in inspect.signature(cls.__init__).parameters if "drive_note" in p], cls
