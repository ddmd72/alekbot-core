"""The drive specialist's note tools (DRIVE_SPECIALIST_RFC §9.1, §9.2, §9.3 A4; design §4.3, Δ5, Δ6, Δ8, Δ10;
DRIVE_FILE_NOTES_RFC §4.5, §4.6, §4.8). One handler, one service call; every call recorded in the run's collector; the
writers gated by the owner turn; a cut write lands and posts its receipt late. Synthetic data only."""
import asyncio
from typing import Any, Dict, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain import drive_answer_texts as t
from src.domain import drive_note_tool_texts as nt
from src.domain.drive_answer import compose_answer
from src.domain.drive_index import DriveIndexStoreUnavailable
from src.domain.drive_labels import DOC_TYPES, LIFE_DOMAINS
from src.domain.drive_note import (
    DRIVE_NOTE_ERASE_DELIVERY, DRIVE_NOTE_RECEIPT_DELIVERY, NOTE_WRITES_PER_TURN, STATUS_ARCHIVED,
    STATUS_INDEXING_OFF, STATUS_NO_CARD, STATUS_NOT_INDEXED, STATUS_OTHER_DRIVE, STATUS_SOON, DriveNoteRefused,
    EraseRequestReceipt, NoteContent, NoteProvenance, NoteReceipt, NoteRefusal,
)
from src.domain.drive_note_change import NoteChange
from src.domain.drive_note_view import NoteView
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_pass_through import PassThroughCollector
from src.domain.drive_report import DriveReport
from src.domain.llm import ToolCall
from src.domain.turn_identity import TURN_ID_CONTEXT_KEY
from src.infrastructure import drive_note_tools as module
from src.infrastructure.drive_note_tools import DriveNoteSession, DriveNoteToolRun, note_provenance
from src.infrastructure.drive_operations import DriveOperations
from src.infrastructure.drive_tools import DriveToolRun
from src.services.drive_note_service import DriveNoteService
from src.services.user_notification_service import UserNotificationService
from tests.unit.services.test_drive_note_service import ACC, T, U, Env

OWNER = {"user_id": U, "account_id": ACC, "interactive_delivery": True, "session_id": "s1",
         TURN_ID_CONTEXT_KEY: "t1"}
REPAIR = "the bathroom repair invoice from Reformas Exemplo"
PATH = "Home/bathroom-invoice.pdf"


class Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _receipt(action: str = "set", *, text: str = REPAIR, version: int = 1, noop: bool = False, status: str = "",
             note_id: str = "n1", ref: str = "drive:a", redo: bool = False, doc_type: str = "") -> NoteReceipt:
    return NoteReceipt(action=action, note_id=note_id, ref=ref, path=PATH if note_id else "", version=version,
                       text=text, doc_type=doc_type, status=status, state="active", noop=noop, redo=redo)


def _view(version: int = 1, state: str = "active", text: str = REPAIR) -> NoteView:
    return NoteView(note_id="n1", ref="drive:a", note_ref="drive_note:n1", path=PATH, version=version, state=state,
                    text=text, doc_type="contract", life_areas=("home",))


def _ops(notification: Any = None) -> DriveOperations:
    return DriveOperations(conversion_service=MagicMock(), notification=notification)


def _service() -> AsyncMock:
    service = AsyncMock(spec=DriveNoteService)
    service.create.return_value = _receipt()
    service.update.return_value = _receipt(version=2)
    service.clear.return_value = _receipt("clear", text="", version=2)
    service.undo.return_value = _receipt("restore", version=3)
    service.get.return_value = _view()
    service.request_erase.return_value = EraseRequestReceipt(scope="one", code="c9f3a1", note_count=1, path=PATH,
                                                             version_count=3)
    return service


def _tools(service: Any, *, context: Dict[str, Any] = OWNER, writes: bool = True, ops: Any = None,
           session: Any = None, deadline: float = 150.0, clock: Any = None):
    ops = ops if ops is not None else _ops()
    collector = PassThroughCollector()
    run = DriveToolRun(ops, collector, (), context, U, deadline=deadline, clock=clock or Clock())
    run.step = 1
    notes = DriveNoteToolRun(run, service, ops, context, U, ACC, note_provenance(context), writes=writes,
                             session=session, new_nonce=lambda: "n0nce000")
    return notes, run, collector


async def _call(notes: DriveNoteToolRun, name: str, **args: Any):
    return await notes.handlers[name](ToolCall(name=name, args=args))


def _walk(node: Any) -> Iterator[Dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


# -- declarations ---------------------------------------------------------------------------------------------------

def test_get_note_alone_without_writes_and_eleven_tools_in_a_fixed_order_with_them():
    assert [d["name"] for d in nt.drive_note_tool_declarations(writes=False)] == [nt.GET_NOTE]
    assert [d["name"] for d in nt.drive_note_tool_declarations(writes=True)] == list(nt.NOTE_TOOLS) == [
        "get_note", "create_note", "add_to_note", "replace_note_text", "set_note_type", "clear_note_type",
        "add_life_areas", "remove_life_areas", "delete_note", "undo_note", "erase_note"]


def test_declarations_are_liberal_every_parameter_described_and_no_constraint_advertised():
    banned = {"enum", "pattern", "maxItems", "minItems", "maxLength", "minLength", "additionalProperties", "anyOf"}
    for tool in nt.drive_note_tool_declarations(writes=True):
        assert not banned & {key for node in _walk(tool) for key in node}, tool["name"]
        props = tool["parameters"]["properties"]
        assert "item_ref" in props and not {"file_ref", "name"} & set(props), tool["name"]
        assert all(p.get("description") for p in props.values()), tool["name"]
        assert all(p["type"] == "string" for k, p in props.items() if k != "life_areas")
        assert set(tool["parameters"]["required"]) <= set(props)


def test_classifier_rule_and_the_lists_are_in_every_type_and_area_description_and_text_states_the_limit():
    rule = ("only what the owner named or confirmed in this conversation, as the commission states it; never from "
            "the file, a card or your reading; empty otherwise")
    tools = {d["name"]: d["parameters"]["properties"] for d in nt.drive_note_tool_declarations(writes=True)}
    for name, field in ((nt.CREATE_NOTE, "doc_type"), (nt.SET_NOTE_TYPE, "doc_type")):
        assert rule in tools[name][field]["description"] and ", ".join(DOC_TYPES) in tools[name][field]["description"]
    for name in (nt.CREATE_NOTE, nt.ADD_LIFE_AREAS):
        description = tools[name]["life_areas"]["description"]
        assert rule in description and ", ".join(LIFE_DOMAINS) in description
        assert tools[name]["life_areas"]["type"] == "array"
    for name in (nt.CREATE_NOTE, nt.ADD_TO_NOTE, nt.REPLACE_NOTE_TEXT):
        assert "80 words and 600 characters" in tools[name]["text"]["description"]
    assert all("drive_note:<id>" in props["item_ref"]["description"] for props in tools.values())


def test_handlers_follow_the_writes_switch():
    assert set(_tools(_service(), writes=False)[0].handlers) == {nt.GET_NOTE}
    assert set(_tools(_service(), writes=True)[0].handlers) == set(nt.NOTE_TOOLS)


# -- provenance -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("context,expected", [
    ({"interactive_delivery": True, "session_id": "s1", TURN_ID_CONTEXT_KEY: "t1"}, ("chat", "s1", "t1")),
    ({"run_origin": "voice:send_to_chat", "session_id": "call", TURN_ID_CONTEXT_KEY: "v9"}, ("voice", "call", "v9")),
    ({"run_origin": "voice:ask_alek", "session_id": "call", TURN_ID_CONTEXT_KEY: "v8"}, ("voice", "call", "v8")),
    ({"run_origin": "voice:unknown", "session_id": "s2", TURN_ID_CONTEXT_KEY: "t2"}, ("chat", "s2", "t2")),
    ({"interactive_delivery": True, "session_id": "s1"}, ("chat", "s1", "")),
], ids=["chat", "send_to_chat", "ask_alek", "unknown_voice_origin_counts_as_chat", "no_turn_id"])
def test_note_provenance(context, expected):
    provenance = note_provenance(context)
    assert (provenance.origin, provenance.session_id, provenance.turn_id) == expected


# -- one tool, one service call -----------------------------------------------------------------------------------

PROV = NoteProvenance(origin="chat", session_id="s1", turn_id="t1")


@pytest.mark.parametrize("tool,args,method,expected", [
    (nt.CREATE_NOTE, {"item_ref": "drive:a", "text": REPAIR, "doc_type": "contract", "life_areas": ["home"]},
     "create", ((U, ACC, "drive:a", NoteContent(REPAIR, "contract", ("home",)), PROV), {})),
    (nt.ADD_TO_NOTE, {"item_ref": "drive:a", "text": "second invoice"}, "update",
     ((U, ACC, "drive:a", NoteChange.append_text("second invoice"), PROV), {"expected_version": None})),
    (nt.SET_NOTE_TYPE, {"item_ref": "drive:a", "doc_type": "invoice"}, "update",
     ((U, ACC, "drive:a", NoteChange.set_type("invoice"), PROV), {"expected_version": None})),
    (nt.CLEAR_NOTE_TYPE, {"item_ref": "drive:a"}, "update",
     ((U, ACC, "drive:a", NoteChange.clear_type(), PROV), {"expected_version": None})),
    (nt.ADD_LIFE_AREAS, {"item_ref": "drive:a", "life_areas": "home, finance"}, "update",
     ((U, ACC, "drive:a", NoteChange.add_areas(["home", "finance"]), PROV), {"expected_version": None})),
    (nt.REMOVE_LIFE_AREAS, {"item_ref": "drive_note:n1", "life_areas": ["home"]}, "update",
     ((U, ACC, "drive_note:n1", NoteChange.remove_areas(["home"]), PROV), {"expected_version": None})),
    (nt.DELETE_NOTE, {"item_ref": "drive:a"}, "clear", ((U, ACC, "drive:a", PROV), {})),
    (nt.UNDO_NOTE, {"item_ref": "drive:a"}, "undo", ((U, ACC, "drive:a", PROV), {})),
    (nt.ERASE_NOTE, {"item_ref": "drive:a"}, "request_erase", ((U, "drive:a"), {})),
    (nt.GET_NOTE, {"item_ref": " drive:a "}, "get", ((U, "drive:a"), {})),
], ids=lambda v: v if isinstance(v, str) else "")
async def test_each_tool_makes_exactly_one_service_call_with_its_mapped_arguments(tool, args, method, expected):
    service = _service()
    notes, _, collector = _tools(service)
    await _call(notes, tool, **args)
    assert [c[0] for c in service.mock_calls] == [method]
    call = getattr(service, method).await_args
    assert (call.args, call.kwargs) == expected
    assert len(collector.results) == 1


async def test_replace_note_text_passes_the_version_get_note_read():
    service = _service()
    service.get.return_value = _view(version=4)
    notes, _, _ = _tools(service)
    await _call(notes, nt.GET_NOTE, item_ref="drive:a")
    await _call(notes, nt.REPLACE_NOTE_TEXT, item_ref="drive_note:n1", text="the final bathroom repair invoice")
    call = service.update.await_args
    assert call.args[3] == NoteChange.replace_text("the final bathroom repair invoice")
    assert call.kwargs == {"expected_version": 4}      # read by its drive: ref, replaced by its note ref (aliases)


# -- an empty or omitted field never writes ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool,args", [
    (nt.CREATE_NOTE, {"item_ref": "drive:a"}),
    (nt.CREATE_NOTE, {"item_ref": "drive:a", "text": "", "doc_type": "", "life_areas": []}),
    (nt.ADD_TO_NOTE, {"item_ref": "drive:a"}),
    (nt.ADD_TO_NOTE, {"item_ref": "drive:a", "text": "   "}),
    (nt.REPLACE_NOTE_TEXT, {"item_ref": "drive:a", "text": ""}),
    (nt.SET_NOTE_TYPE, {"item_ref": "drive:a", "doc_type": ""}),
    (nt.ADD_LIFE_AREAS, {"item_ref": "drive:a", "life_areas": []}),
    (nt.REMOVE_LIFE_AREAS, {"item_ref": "drive:a", "life_areas": ""}),
    (nt.CREATE_NOTE, {"item_ref": "drive:a", "text": "x " * 81}),
    (nt.SET_NOTE_TYPE, {"item_ref": "drive:a", "doc_type": "spaceship"}),
], ids=["create_no_fields", "create_all_empty", "add_no_text", "add_blank_text", "replace_empty_is_refused_D5",
        "set_type_empty", "add_areas_empty", "remove_areas_empty", "create_too_long", "unknown_type"])
async def test_an_empty_or_invalid_field_is_refused_before_any_service_call(tool, args):
    service = _service()
    notes, _, collector = _tools(service, session=DriveNoteSession())
    result = await _call(notes, tool, **args)
    assert service.mock_calls == []
    outcome = collector.results[0].outcome
    assert (outcome.cls, outcome.success, outcome.delivery, outcome.note_context) == (
        OutcomeClass.REQUEST_ERROR, False, (), ())
    assert outcome.text.endswith("Nothing was stored.") and outcome.text in result.result_str


@pytest.mark.parametrize("tool", list(nt.NOTE_TOOLS))
async def test_a_missing_item_ref_is_refused_before_any_service_call(tool):
    service = _service()
    notes, _, collector = _tools(service)
    await _call(notes, tool, text=REPAIR, doc_type="contract", life_areas=["home"])
    assert service.mock_calls == []
    assert collector.results[0].outcome.cls is OutcomeClass.REQUEST_ERROR
    assert collector.results[0].outcome.text.startswith("item_ref is required")


async def test_truncated_arguments_are_refused_without_a_service_call():
    service = _service()
    notes, _, collector = _tools(service)
    for tool in (nt.CREATE_NOTE, nt.GET_NOTE):
        await _call(notes, tool, item_ref="drive:a", _parse_error="truncated_json")
    assert service.mock_calls == []
    assert [r.outcome.text for r in collector.results] == [nt.TRUNCATED, nt.TRUNCATED]


# -- replace needs a read in this run, of the version now stored ------------------------------------------------------

async def test_replace_without_a_read_is_refused_before_any_service_call():
    service = _service()
    notes, _, collector = _tools(service)
    result = await _call(notes, nt.REPLACE_NOTE_TEXT, item_ref="drive:a", text="a new text")
    assert service.mock_calls == []
    assert collector.results[0].outcome.cls is OutcomeClass.REQUEST_ERROR
    assert nt.READ_FIRST in result.result_str


async def test_replace_after_the_note_changed_since_the_read_is_refused_and_nothing_is_written():
    env = Env()
    await env.service.set(U, ACC, "drive:a", REPAIR, provenance=PROV)
    notes, _, collector = _tools(env.service)
    await _call(notes, nt.GET_NOTE, item_ref="drive:a")
    await env.service.update(U, ACC, "drive:a", NoteChange.set_type("invoice"), PROV)      # another writer: v2
    await _call(notes, nt.REPLACE_NOTE_TEXT, item_ref="drive:a", text="a new text")
    outcome = collector.results[-1].outcome
    assert outcome.cls is OutcomeClass.REQUEST_ERROR and "changed since you read it" in outcome.text
    assert (await env.note()).text == REPAIR and (await env.note()).current_version == 2


async def test_get_note_and_replace_note_text_in_one_batch_run_in_emission_order():
    env = Env()
    await env.service.set(U, ACC, "drive:a", REPAIR, provenance=PROV)
    notes, _, collector = _tools(env.service)
    await asyncio.gather(
        notes.handlers[nt.GET_NOTE](ToolCall(name=nt.GET_NOTE, args={"item_ref": "drive:a"})),
        notes.handlers[nt.REPLACE_NOTE_TEXT](ToolCall(name=nt.REPLACE_NOTE_TEXT,
                                                      args={"item_ref": "drive:a", "text": "the bathroom repair, final"})))
    assert [r.outcome.op for r in collector.results] == [DriveOp.NOTE_READ, DriveOp.NOTE_WRITE]
    assert collector.results[1].outcome.cls is OutcomeClass.OK
    assert (await env.note()).text == "the bathroom repair, final"


async def test_a_replace_emitted_before_its_read_in_the_same_batch_is_refused():
    env = Env()
    await env.service.set(U, ACC, "drive:a", REPAIR, provenance=PROV)
    notes, _, collector = _tools(env.service)
    await asyncio.gather(
        notes.handlers[nt.REPLACE_NOTE_TEXT](ToolCall(name=nt.REPLACE_NOTE_TEXT,
                                                      args={"item_ref": "drive:a", "text": "the bathroom repair, final"})),
        notes.handlers[nt.GET_NOTE](ToolCall(name=nt.GET_NOTE, args={"item_ref": "drive:a"})))
    assert collector.results[0].outcome.text == nt.READ_FIRST
    assert (await env.note()).text == REPAIR


# -- additive kinds create a note -------------------------------------------------------------------------------------

@pytest.mark.parametrize("tool,args,expected", [
    (nt.ADD_TO_NOTE, {"text": REPAIR}, NoteContent(REPAIR, "", ())),
    (nt.SET_NOTE_TYPE, {"doc_type": "contract"}, NoteContent("", "contract", ())),
    (nt.ADD_LIFE_AREAS, {"life_areas": ["home"]}, NoteContent("", "", ("home",))),
], ids=["add_to_note", "set_note_type", "add_life_areas"])
async def test_an_additive_tool_on_a_file_with_no_note_creates_it(tool, args, expected):
    env = Env()
    notes, _, collector = _tools(env.service)
    await _call(notes, tool, item_ref="drive:a", **args)
    note = await env.note()
    assert note.content == expected and note.current_version == 1
    outcome = collector.results[0].outcome
    assert outcome.cls is OutcomeClass.OK and outcome.note_context == ("[Drive note: Docs/a.pdf ref=drive:a v1]",)


@pytest.mark.parametrize("tool,args", [
    (nt.CLEAR_NOTE_TYPE, {}), (nt.REMOVE_LIFE_AREAS, {"life_areas": ["home"]}),
    (nt.REPLACE_NOTE_TEXT, {"text": "words"}),
], ids=["clear_note_type", "remove_life_areas", "replace_note_text"])
async def test_a_non_additive_tool_on_a_file_with_no_note_is_not_found(tool, args):
    env = Env()
    session = DriveNoteSession()
    session.reads["drive:a"] = 0          # a read that found no note
    notes, _, collector = _tools(env.service, session=session)
    await _call(notes, tool, item_ref="drive:a", **args)
    outcome = collector.results[0].outcome
    assert (outcome.cls, outcome.delivery) == (OutcomeClass.NOT_FOUND, ())
    assert outcome.text.endswith("Nothing was stored.") and await env.note() is None


# -- the per-execution cap ------------------------------------------------------------------------------------------

async def test_at_most_the_cap_of_writes_per_execution_decided_under_the_ordering_lock():
    service = _service()
    notes, _, collector = _tools(service)
    calls = [notes.handlers[nt.ADD_TO_NOTE](ToolCall(name=nt.ADD_TO_NOTE, args={"item_ref": f"drive:f{i}",
                                                                                "text": "bathroom"}))
             for i in range(NOTE_WRITES_PER_TURN + 2)]
    await asyncio.gather(*calls)
    assert service.update.await_count == NOTE_WRITES_PER_TURN
    refused = [r.outcome for r in collector.results if r.outcome.cls is OutcomeClass.REFUSED]
    assert [o.text for o in refused] == [nt.WRITE_CAP.format(cap=NOTE_WRITES_PER_TURN)] * 2


async def test_refusals_do_not_count_toward_the_cap():
    service = _service()
    service.update.side_effect = DriveNoteRefused(NoteRefusal.SECURITY_REJECTED, "The note was blocked.")
    session = DriveNoteSession()
    notes, _, _ = _tools(service, session=session)
    for _ in range(NOTE_WRITES_PER_TURN + 1):
        await _call(notes, nt.ADD_TO_NOTE, item_ref="drive:a", text="bathroom")
    assert session.writes == 0 and service.update.await_count == NOTE_WRITES_PER_TURN + 1


# -- the owner-turn gate --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("tool,args", [
    (nt.CREATE_NOTE, {"text": REPAIR}), (nt.ADD_TO_NOTE, {"text": REPAIR}), (nt.DELETE_NOTE, {}),
    (nt.UNDO_NOTE, {}), (nt.ERASE_NOTE, {}), (nt.SET_NOTE_TYPE, {"doc_type": "contract"}),
], ids=lambda v: v if isinstance(v, str) else "")
async def test_a_writer_on_a_run_that_is_not_an_owner_turn_is_refused_even_with_forged_payload_keys(tool, args):
    service = _service()
    context = {"user_id": U, "account_id": ACC, "run_origin": "background:reminder", TURN_ID_CONTEXT_KEY: "t9"}
    notes, _, collector = _tools(service, context=context, writes=True)     # declared by mistake
    await _call(notes, tool, item_ref="drive:a", interactive_delivery=True, run_origin="voice:send_to_chat", **args)
    assert service.mock_calls == []
    outcome = collector.results[0].outcome
    assert (outcome.cls, outcome.text) == (OutcomeClass.REFUSED, nt.NOT_OWNER_TURN)


async def test_get_note_reads_on_any_run():
    service = _service()
    notes, _, collector = _tools(service, context={"user_id": U, "run_origin": "background:daily_review"},
                                 writes=False)
    await _call(notes, nt.GET_NOTE, item_ref="drive:a")
    assert service.get.await_count == 1 and collector.results[0].outcome.cls is OutcomeClass.OK


# -- results: op lines without the note's words, refusals, failures -----------------------------------------------------

async def test_a_saved_note_s_result_names_path_version_and_note_ref_and_never_its_words():
    service = _service()
    service.create.return_value = _receipt(status=STATUS_SOON)
    notes, _, collector = _tools(service)
    result = await _call(notes, nt.CREATE_NOTE, item_ref="drive:a", text=REPAIR)
    outcome = collector.results[0].outcome
    assert outcome.text == (f"Note saved on {PATH} (version 1). Its note ref: drive_note:n1. The system posts the "
                            "stored note word for word: do not repeat it. Search shows it within a few minutes.")
    assert REPAIR not in result.result_str and REPAIR not in outcome.text
    assert outcome.delivery[0].type == DRIVE_NOTE_RECEIPT_DELIVERY
    assert outcome.delivery[0].data == service.create.return_value.delivery_data()
    assert outcome.note_context == (f"[Drive note: {PATH} ref=drive:a v1]",)
    assert result.delivery_items == [] and result.history_context is None   # both ride the collector only (Δ7)


@pytest.mark.parametrize("status,sentence", list(nt.STATUS_SENTENCES.items()))
def test_each_status_sentence_closes_the_saved_line(status, sentence):
    assert nt.note_receipt_line(_receipt(status=status)).endswith(sentence)


def test_the_six_status_sentences_are_the_known_statuses():
    assert set(nt.STATUS_SENTENCES) == {STATUS_SOON, STATUS_INDEXING_OFF, STATUS_NOT_INDEXED, STATUS_NO_CARD,
                                        STATUS_ARCHIVED, STATUS_OTHER_DRIVE}


@pytest.mark.parametrize("receipt,expected", [
    (_receipt("clear", text="", version=2), f"Note removed from {PATH}; its history is kept and undo can bring it "
                                            "back. Its note ref: drive_note:n1. The system posts a receipt to the owner."),
    (_receipt("clear", note_id="", text="", version=0, noop=True), "There was no note on drive:a; nothing was changed."),
    (_receipt("restore", version=3), f"Undone: the previous note is back on {PATH} (version 3). Its note ref: "
                                     "drive_note:n1. The system posts the note word for word: do not repeat it."),
    (_receipt("restore", version=4, redo=True), f"Redone: the undone change is back on {PATH} (version 4). Its note "
                                                "ref: drive_note:n1. The system posts the note word for word: do not "
                                                "repeat it."),
    (_receipt("restore", text="", version=2), f"Undone: there was no note on {PATH} before, so the note is removed "
                                              "(version 2). Its note ref: drive_note:n1. The system posts a receipt "
                                              "to the owner."),
    (_receipt("restore", version=3, noop=True), f"The last change of the note on {PATH} was already undone in this "
                                                "turn; nothing was written."),
    (_receipt(version=2, noop=True), f"The note on {PATH} already says exactly this (version 2); nothing was "
                                     "written. Its note ref: drive_note:n1. The system posts it word for word: do not "
                                     "repeat it."),
], ids=["clear", "clear_noop", "undo", "redo", "undo_to_nothing", "undo_rerun", "set_noop"])
def test_receipt_lines_carry_no_note_words(receipt, expected):
    line = nt.note_receipt_line(receipt)
    assert line == expected and REPAIR not in line


async def test_a_noop_clear_with_no_note_has_no_history_line_but_still_delivers_its_receipt():
    env = Env()
    notes, _, collector = _tools(env.service)
    await _call(notes, nt.DELETE_NOTE, item_ref="drive:a")
    outcome = collector.results[0].outcome
    assert outcome.note_context == () and [d.type for d in outcome.delivery] == [DRIVE_NOTE_RECEIPT_DELIVERY]
    assert outcome.text == "There was no note on drive:a; nothing was changed."


@pytest.mark.parametrize("reason,cls", [
    (NoteRefusal.NOT_CONNECTED, OutcomeClass.NOT_CONNECTED), (NoteRefusal.NOT_ON_DRIVE, OutcomeClass.NOT_FOUND),
    (NoteRefusal.NO_NOTE, OutcomeClass.NOT_FOUND), (NoteRefusal.CANNOT_OPEN, OutcomeClass.REFUSED),
    (NoteRefusal.SECURITY_REJECTED, OutcomeClass.REFUSED), (NoteRefusal.NOTE_LIMIT, OutcomeClass.REFUSED),
    (NoteRefusal.TOO_LONG, OutcomeClass.REQUEST_ERROR), (NoteRefusal.NOTE_EXISTS, OutcomeClass.REQUEST_ERROR),
    (NoteRefusal.EMPTY_CHANGE, OutcomeClass.REQUEST_ERROR), (NoteRefusal.CHANGED_SINCE_READ, OutcomeClass.REQUEST_ERROR),
    (NoteRefusal.TOO_MANY_LIFE_AREAS, OutcomeClass.REQUEST_ERROR), (NoteRefusal.INVALID_REF, OutcomeClass.REQUEST_ERROR),
    (NoteRefusal.AMBIGUOUS_NOTE, OutcomeClass.REQUEST_ERROR), (NoteRefusal.CONFLICT, OutcomeClass.FAILED),
    (NoteRefusal.DRIVE_UNAVAILABLE, OutcomeClass.FAILED), (NoteRefusal.HISTORY_MISSING, OutcomeClass.FAILED),
    (NoteRefusal.ERASE_UNAVAILABLE, OutcomeClass.FAILED),
])
async def test_a_refusal_maps_to_its_class_with_its_message_and_nothing_stored(reason, cls):
    service = _service()
    service.create.side_effect = DriveNoteRefused(reason, "The refusal's own words.")
    session = DriveNoteSession()
    notes, _, collector = _tools(service, session=session)
    await _call(notes, nt.CREATE_NOTE, item_ref="drive:a", text=REPAIR)
    outcome = collector.results[0].outcome
    assert (outcome.cls, outcome.success, outcome.text, outcome.delivery, outcome.note_context) == (
        cls, False, "The refusal's own words. Nothing was stored.", (), ())
    assert session.writes == 0


async def test_a_store_outage_says_try_again():
    service = _service()
    service.undo.side_effect = DriveIndexStoreUnavailable("down")
    notes, _, collector = _tools(service)
    await _call(notes, nt.UNDO_NOTE, item_ref="drive:a")
    outcome = collector.results[0].outcome
    assert outcome.cls is OutcomeClass.FAILED and "Try again" in outcome.text


async def test_an_unexpected_error_logs_only_its_type_never_the_note_and_without_a_traceback(monkeypatch):
    log = MagicMock()
    monkeypatch.setattr(module, "logger", log)
    service = _service()
    service.update.side_effect = ValueError(f"document field text={REPAIR!r} is invalid")
    notes, _, collector = _tools(service)
    await _call(notes, nt.ADD_TO_NOTE, item_ref="drive:a", text=REPAIR)
    assert collector.results[0].outcome.text == nt.UNEXPECTED
    error = log.error.call_args
    assert "ValueError" in error.args[0] and REPAIR not in error.args[0] and "exc_info" not in error.kwargs
    every = " ".join(str(c) for c in log.mock_calls)
    assert REPAIR not in every


# -- get_note --------------------------------------------------------------------------------------------------------

async def test_get_note_shows_the_note_as_a_card_does_inside_an_untrusted_wrapper():
    service = _service()
    notes, _, collector = _tools(service)
    result = await _call(notes, nt.GET_NOTE, item_ref="drive:a")
    outcome = collector.results[0].outcome
    assert (outcome.op, outcome.cls, outcome.facts.owner_note, collector.results[0].kind) == (
        DriveOp.NOTE_READ, OutcomeClass.OK, True, t.KIND_NOTE)
    assert outcome.text == (
        f"The owner's note on {PATH} (drive:a, note ref drive_note:n1, version 1):\n"
        f'<drive_document nonce="n0nce000" ref="drive:a" path="{PATH}">\n'
        f"noted by the owner: «{REPAIR}» (type contract; life areas home)\n"
        '</drive_document nonce="n0nce000">')
    assert outcome.text in result.result_str     # the specialist needs the words to replace them


async def test_get_note_on_a_cleared_an_archived_and_no_note():
    service = _service()
    notes, _, collector = _tools(service)
    service.get.return_value = _view(version=3, state="cleared", text="")
    await _call(notes, nt.GET_NOTE, item_ref="drive:a")
    service.get.return_value = _view(version=2, state="archived")
    await _call(notes, nt.GET_NOTE, item_ref="drive_note:n1")
    service.get.return_value = None
    await _call(notes, nt.GET_NOTE, item_ref="drive:b")
    cleared, archived, none = (r.outcome for r in collector.results)
    assert cleared.text == (f"The owner's note on {PATH} (drive:a, note ref drive_note:n1) was deleted (version 3); "
                            "undo_note brings it back.")
    assert not cleared.facts.owner_note
    assert "the file is gone from the drive; change this note by its note ref" in archived.text
    assert (none.cls, none.text) == (OutcomeClass.NOT_FOUND, "There is no note on drive:b.")


def test_the_session_remembers_a_read_under_every_alias():
    session = DriveNoteSession()
    session.remember("drive:a", _view(version=5))
    session.remember("drive:b", None)
    assert session.reads == {"drive:a": 5, "drive_note:n1": 5, "drive:b": 0}
    assert session.read_version("drive:c") is None


# -- erase: the code only in the delivery item ------------------------------------------------------------------------

async def test_the_erase_code_is_only_in_the_delivery_item_never_in_a_result_or_the_answer():
    env = Env()
    await env.service.set(U, ACC, "drive:a", REPAIR, provenance=PROV)
    await env.service.update(U, ACC, "drive:a", NoteChange.set_type("contract"), PROV)
    notes, _, collector = _tools(env.service)
    result = await _call(notes, nt.ERASE_NOTE, item_ref="drive:a")
    outcome = collector.results[0].outcome
    (item,) = outcome.delivery
    assert item.type == DRIVE_NOTE_ERASE_DELIVERY
    assert (item.data["command"], item.data["version_count"]) == ("$drive-notes erase c0000", 2)
    assert outcome.op is DriveOp.NOTE_ERASE and outcome.note_context == ()
    answer = compose_answer(collector, DriveReport(note="Asked for the erase code."))
    assert "c0000" not in result.result_str and "c0000" not in outcome.text and "c0000" not in answer.text
    assert answer.delivery_items == [item]


# -- a cut write still lands, and its receipt is posted late (Δ10) ----------------------------------------------------

async def test_a_write_cut_after_it_started_still_lands_and_posts_exactly_one_late_receipt():
    gate = asyncio.Event()
    service = _service()

    async def slow_create(*args, **kwargs):
        await gate.wait()
        return _receipt(status=STATUS_SOON)
    service.create.side_effect = slow_create
    notification = AsyncMock(spec=UserNotificationService)
    ops = _ops(notification)
    session = DriveNoteSession()
    notes, run, collector = _tools(service, ops=ops, session=session)
    call = asyncio.ensure_future(_call(notes, nt.CREATE_NOTE, item_ref="drive:a", text=REPAIR))
    await asyncio.sleep(0.01)
    call.cancel()                                          # the soft deadline cancels the engine
    with pytest.raises(asyncio.CancelledError):
        await call
    assert not run._order.locked() and len(ops._shielded) == 1    # the lock is free; DriveOperations holds the write
    (cut,) = collector.results
    assert (cut.outcome.cls, cut.outcome.text, cut.outcome.delivery) == (
        OutcomeClass.STILL_RUNNING, nt.NOTE_STILL_RUNNING, ())
    answer = compose_answer(collector, DriveReport(), cut=True)
    assert answer.delivery_items == [] and answer.status.value == "still_running"
    notification.notify_drive_note_receipt.assert_not_awaited()
    gate.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert service.create.await_count == 1 and session.writes == 1     # the write landed and counted
    notification.notify_drive_note_receipt.assert_awaited_once()
    user_id, account_id, item = notification.notify_drive_note_receipt.await_args.args
    assert (user_id, account_id, item.type) == (U, ACC, DRIVE_NOTE_RECEIPT_DELIVERY)
    assert item.data == _receipt(status=STATUS_SOON).delivery_data()
    assert not ops._shielded and not ops._notices


async def test_a_write_that_lands_before_the_answer_rides_the_answer_only():
    notification = AsyncMock(spec=UserNotificationService)
    ops = _ops(notification)
    service = _service()
    notes, _, collector = _tools(service, ops=ops)
    await _call(notes, nt.CREATE_NOTE, item_ref="drive:a", text=REPAIR)
    for _ in range(5):
        await asyncio.sleep(0)
    answer = compose_answer(collector, DriveReport())
    assert [d.type for d in answer.delivery_items] == [DRIVE_NOTE_RECEIPT_DELIVERY]
    notification.notify_drive_note_receipt.assert_not_awaited()


async def test_a_cut_write_that_is_refused_posts_nothing_late():
    gate = asyncio.Event()
    service = _service()

    async def refused(*args, **kwargs):
        await gate.wait()
        raise DriveNoteRefused(NoteRefusal.NOTE_EXISTS, "The file already has a note.")
    service.create.side_effect = refused
    notification = AsyncMock(spec=UserNotificationService)
    notes, _, _ = _tools(service, ops=_ops(notification))
    call = asyncio.ensure_future(_call(notes, nt.CREATE_NOTE, item_ref="drive:a", text=REPAIR))
    await asyncio.sleep(0.01)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    gate.set()
    for _ in range(5):
        await asyncio.sleep(0)
    notification.notify_drive_note_receipt.assert_not_awaited()


async def test_a_late_receipt_without_a_notification_service_or_with_a_failing_one_never_raises():
    _ops(None).post_late_note_receipt(U, ACC, MagicMock(type=DRIVE_NOTE_RECEIPT_DELIVERY))
    notification = AsyncMock(spec=UserNotificationService)
    notification.notify_drive_note_receipt.side_effect = RuntimeError("chat down")
    ops = _ops(notification)
    ops.post_late_note_receipt(U, ACC, MagicMock(type=DRIVE_NOTE_RECEIPT_DELIVERY))
    for _ in range(3):
        await asyncio.sleep(0)
    notification.notify_drive_note_receipt.assert_awaited_once()
    assert not ops._notices


async def test_a_call_after_the_deadline_is_not_started():
    service = _service()
    notes, _, collector = _tools(service, deadline=10.0, clock=Clock(11.0))
    await _call(notes, nt.ADD_TO_NOTE, item_ref="drive:a", text=REPAIR)
    await _call(notes, nt.GET_NOTE, item_ref="drive:a")
    assert service.mock_calls == []
    assert [r.outcome.text for r in collector.results] == [t.NOT_STARTED, t.NOT_STARTED]


# -- end to end on the in-memory note store ---------------------------------------------------------------------------

async def test_end_to_end_create_normalizes_resolves_the_alias_reports_not_indexed_then_undo_restores():
    env = Env()
    notes, _, collector = _tools(env.service)
    await _call(notes, nt.CREATE_NOTE, item_ref="drive:a", text=f"  {REPAIR}\n  base   852,40 ",
                doc_type="договор")
    stored = await env.note()
    assert (stored.text, stored.doc_type) == (f"{REPAIR} base 852,40", "contract")
    first = collector.results[0].outcome
    assert first.text.endswith(nt.STATUS_SENTENCES[STATUS_NOT_INDEXED])
    assert first.delivery[0].data["text"] == f"{REPAIR} base 852,40"
    await _call(notes, nt.ADD_TO_NOTE, item_ref="drive:a", text="and IVA 196,05")
    assert (await env.note()).text == f"{REPAIR} base 852,40 and IVA 196,05"
    await _call(notes, nt.UNDO_NOTE, item_ref="drive:a")
    restored = await env.note()
    assert (restored.text, restored.doc_type, restored.current_version) == (
        f"{REPAIR} base 852,40", "contract", 3)
    assert [r.outcome.cls for r in collector.results] == [OutcomeClass.OK] * 3
    assert all(REPAIR not in r.outcome.text for r in collector.results)


async def test_an_archived_note_is_changed_by_its_note_ref_from_the_store_alone():
    env = Env()
    receipt = await env.service.set(U, ACC, "drive:a", REPAIR, provenance=PROV)
    await env.notes.archive(U, receipt.note_id, archived_at=T, last_seen_at=T)
    env.drive.connection.side_effect = RuntimeError("the drive is not reachable")
    graph = env.graph_calls()
    notes, _, collector = _tools(env.service)
    await _call(notes, nt.ADD_LIFE_AREAS, item_ref=receipt.note_ref, life_areas=["home"])
    outcome = collector.results[0].outcome
    assert outcome.cls is OutcomeClass.OK and outcome.text.endswith(nt.STATUS_SENTENCES[STATUS_ARCHIVED])
    assert env.graph_calls() == graph                                   # no drive call
    note = await env.notes.get(U, receipt.note_id)
    assert (note.state, note.life_areas, note.text) == ("archived", ["home"], REPAIR)


async def test_a_get_note_queued_behind_a_write_that_ran_past_the_deadline_is_not_started():
    clock = Clock(0.0)
    service = _service()

    async def slow_update(*args, **kwargs):
        clock.now = 200.0
        return _receipt(version=2)
    service.update.side_effect = slow_update
    notes, _, collector = _tools(service, deadline=150.0, clock=clock)
    await asyncio.gather(_call(notes, nt.ADD_TO_NOTE, item_ref="drive:a", text="and the gate"),
                         _call(notes, nt.GET_NOTE, item_ref="drive:a"))
    assert service.get.await_count == 0
    assert [r.outcome.text for r in collector.results][1] == t.NOT_STARTED


async def test_a_failing_get_note_is_a_failed_read_with_nothing_remembered():
    service = _service()
    service.get.side_effect = DriveIndexStoreUnavailable("down")
    session = DriveNoteSession()
    notes, _, collector = _tools(service, session=session)
    await _call(notes, nt.GET_NOTE, item_ref="drive:a")
    assert collector.results[0].outcome.cls is OutcomeClass.FAILED and session.reads == {}


def test_a_stub_s_empty_refs_are_not_remembered():
    session = DriveNoteSession()
    session.remember("drive_note:n1", NoteView(note_id="n1", ref="", note_ref="drive_note:n1", path="", version=2,
                                               state="active"))
    assert session.reads == {"drive_note:n1": 2}


async def test_a_cut_write_whose_task_is_cancelled_posts_nothing():
    notification = AsyncMock(spec=UserNotificationService)
    notes, _, _ = _tools(_service(), ops=_ops(notification))
    task = asyncio.ensure_future(asyncio.sleep(10))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    notes._post_late(task)
    notification.notify_drive_note_receipt.assert_not_awaited()
