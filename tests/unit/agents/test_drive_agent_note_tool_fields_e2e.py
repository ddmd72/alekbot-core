"""P14 card for the drive specialist's note tools: every declared field, end to end (TEST_COVERAGE_SYSTEMIC_RFC §5 P14;
DRIVE_SPECIALIST_RFC §9.1, §9.2; design §4.3, §5).

A scripted model drives a real `DriveAgent` on an owner turn: its note tools are the real `DriveNoteToolRun` handlers over
the real `DriveNoteService` and the in-memory note store (`Env`: a drive fake with one file `a`, the embedding, security
and index ports stubbed). Assertions are on the stored note (text, type, areas, state, version), on the stored erase
request, on what the specialist's model was told, and on what the answer carries (the receipt, the history line).

Every declared field of the `drive_notes` surface is sent by a scripted call (module constants; the last test checks them
against the declarations). Each test ends with the two script facts: no request found the script empty, and every
scripted answer was consumed. Synthetic data only: the bathroom-repair invoice.
"""
from typing import Any, Dict, List, Sequence, Tuple
from unittest.mock import MagicMock

from src.agents.drive_agent import DriveAgent
from src.domain import drive_note_tool_texts as nt
from src.domain import drive_tool_texts as tt
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse
from src.domain.drive_note import DRIVE_NOTE_ERASE_DELIVERY, DRIVE_NOTE_RECEIPT_DELIVERY, NoteProvenance
from src.domain.turn_identity import TURN_ID_CONTEXT_KEY
from src.infrastructure.agent_config import DRIVE
from src.infrastructure.drive_note_tools import DRIVE_NOTE_CONTEXT_KEY
from src.infrastructure.drive_operations import DriveOperations
from src.services.user_drive_service import UserDriveService
from tests.support.scripted_llm import ScriptedLLM, execution_context, tool_results, tools
from tests.support.tool_e2e import StubPromptBuilder
from tests.support.tool_fields import unused
from tests.unit.services.test_drive_note_service import ACC, U, Env

REPAIR = "the bathroom repair invoice from Reformas Exemplo"
PATH = "Docs/a.pdf"
SEED = NoteProvenance(origin="chat", session_id="s0", turn_id="t0")      # an earlier turn: no same-turn guard applies
OWNER = {"user_id": U, "account_id": ACC, "interactive_delivery": True, "session_id": "s1", TURN_ID_CONTEXT_KEY: "t1"}

# --- the specialist's scripted note-tool calls (the drive_notes surface) ------------------------------------------------
CREATE = {"item_ref": "drive:a1:a", "text": REPAIR, "doc_type": "contract", "life_areas": ["home"]}
ADD = {"item_ref": "drive:a1:a", "text": "painting invoice from Pinturas Exemplo"}
GET = {"item_ref": "drive:a1:a"}
REPLACE = {"item_ref": "drive:a1:a", "text": "the final bathroom repair invoice"}
SET_TYPE = {"item_ref": "drive:a1:a", "doc_type": "invoice"}
CLEAR_TYPE = {"item_ref": "drive:a1:a"}
ADD_AREAS = {"item_ref": "drive:a1:a", "life_areas": ["finance"]}
REMOVE_AREAS = {"item_ref": "drive:a1:a", "life_areas": ["home"]}
DELETE = {"item_ref": "drive:a1:a"}
UNDO = {"item_ref": "drive:a1:a"}
ERASE = {"item_ref": "drive:a1:a"}
REPORT = {"note": "Done."}


class World:
    def __init__(self) -> None:
        self.env = Env()
        drive_service = UserDriveService(self.env.drive, self.env.connections)
        self.operations = DriveOperations(conversion_service=MagicMock(), drive_service=drive_service,
                                          drive_notes=self.env.service)

    async def seed(self, *, text: str = REPAIR, doc_type: str = "contract", life_areas: Sequence[str] = ("home",)) -> None:
        """A note already on the file."""
        await self.env.service.set(U, ACC, "drive:a1:a", text, doc_type, list(life_areas), SEED)

    async def note(self):
        return await self.env.note("a")

    async def run(self, specialist: ScriptedLLM, **context: Any) -> AgentResponse:
        agent = DriveAgent(
            AgentConfig(agent_id=f"drive_agent_{U}", agent_type="drive", timeout_ms=DRIVE.timeout_ms),
            execution_context=execution_context(specialist, "drive"), operations=self.operations,
            prompt_builder=StubPromptBuilder("DriveAgent prompt"), notes=self.env.service)
        ctx = {**OWNER, **context}
        return await agent.execute(AgentMessage.create(
            sender="coordinator", recipient=agent.agent_id, intent=AgentIntent.QUERY,
            payload={"query": "Keep the invoice on the file."}, context=ctx))


def specialist(*turns: Sequence[Tuple[str, dict]], report: dict = REPORT) -> ScriptedLLM:
    """The specialist's script: each turn's tool calls, then its report."""
    return ScriptedLLM([*(tools(*turn) for turn in turns), tools((tt.REPORT_TOOL, report))])


def told(model: ScriptedLLM) -> List[str]:
    """What the specialist's model read back from its tools, in order."""
    return [str(r) for _, r in tool_results(model)]


def consumed(*llms: ScriptedLLM) -> bool:
    """The two script facts: no request found the script empty, and every scripted answer was used."""
    return all(llm.unanswered == 0 and llm.pending() == 0 for llm in llms)


def receipts(response: AgentResponse) -> List[Dict[str, Any]]:
    return [i.data for i in response.delivery_items if i.type == DRIVE_NOTE_RECEIPT_DELIVERY]


def history_line(version: int) -> List[str]:
    return [f"[Drive note: {PATH} ref=drive:a1:a v{version}]"]


# --------------------------------------------------------------------------------------------------------- tests

async def test_create_note_stores_the_text_the_type_and_the_areas_on_the_file():
    world = World()
    model = specialist([(nt.CREATE_NOTE, CREATE)])

    response = await world.run(model)

    note = await world.note()
    assert (note.text, note.doc_type, note.life_areas, note.state, note.current_version) == (
        REPAIR, "contract", ["home"], "active", 1)
    assert receipts(response)[0]["text"] == REPAIR and receipts(response)[0]["version"] == 1   # the system posts it
    assert response.history_context[DRIVE_NOTE_CONTEXT_KEY] == history_line(1)
    (seen,) = told(model)
    assert "Note saved on" in seen and REPAIR not in seen        # the model is not told the words (Δ8)
    assert consumed(model)


async def test_add_to_note_appends_the_words_to_the_stored_text():
    world = World()
    await world.seed()
    model = specialist([(nt.ADD_TO_NOTE, ADD)])

    response = await world.run(model)

    note = await world.note()
    assert note.text == f"{REPAIR} painting invoice from Pinturas Exemplo"
    assert (note.doc_type, note.life_areas, note.current_version) == ("contract", ["home"], 2)
    assert response.history_context[DRIVE_NOTE_CONTEXT_KEY] == history_line(2)
    assert consumed(model)


async def test_replace_note_text_after_a_read_replaces_the_stored_text_and_keeps_type_and_areas():
    world = World()
    await world.seed()
    model = specialist([(nt.GET_NOTE, GET)], [(nt.REPLACE_NOTE_TEXT, REPLACE)])

    await world.run(model)

    note = await world.note()
    assert note.text == "the final bathroom repair invoice"
    assert (note.doc_type, note.life_areas, note.current_version) == ("contract", ["home"], 2)
    assert REPAIR in told(model)[0]                                  # get_note shows the words the replace overwrites
    assert consumed(model)


async def test_replace_note_text_without_a_read_is_refused_and_the_note_stays():
    world = World()
    await world.seed()
    model = specialist([(nt.REPLACE_NOTE_TEXT, REPLACE)])

    await world.run(model)

    note = await world.note()
    assert (note.text, note.current_version) == (REPAIR, 1)
    assert "get_note first" in told(model)[0] and "Nothing was stored." in told(model)[0]
    assert consumed(model)


async def test_set_note_type_changes_the_stored_type_and_keeps_the_words():
    world = World()
    await world.seed()
    model = specialist([(nt.SET_NOTE_TYPE, SET_TYPE)])

    await world.run(model)

    note = await world.note()
    assert (note.doc_type, note.text, note.life_areas, note.current_version) == ("invoice", REPAIR, ["home"], 2)
    assert consumed(model)


async def test_clear_note_type_empties_the_stored_type_and_keeps_the_words():
    world = World()
    await world.seed()
    model = specialist([(nt.CLEAR_NOTE_TYPE, CLEAR_TYPE)])

    await world.run(model)

    note = await world.note()
    assert (note.doc_type, note.text, note.life_areas, note.current_version) == ("", REPAIR, ["home"], 2)
    assert consumed(model)


async def test_add_life_areas_adds_to_the_stored_areas_in_order():
    world = World()
    await world.seed()
    model = specialist([(nt.ADD_LIFE_AREAS, ADD_AREAS)])

    await world.run(model)

    note = await world.note()
    assert (note.life_areas, note.text, note.doc_type, note.current_version) == (["home", "finance"], REPAIR,
                                                                                  "contract", 2)
    assert consumed(model)


async def test_remove_life_areas_removes_only_the_named_area():
    world = World()
    await world.seed(life_areas=("home", "finance"))
    model = specialist([(nt.REMOVE_LIFE_AREAS, REMOVE_AREAS)])

    await world.run(model)

    note = await world.note()
    assert (note.life_areas, note.text, note.current_version) == (["finance"], REPAIR, 2)
    assert consumed(model)


async def test_delete_note_clears_the_stored_note_and_undo_note_restores_it():
    world = World()
    await world.seed()
    cleared = specialist([(nt.DELETE_NOTE, DELETE)])

    await world.run(cleared)

    note = await world.note()
    assert note.state == "cleared" and note.text == ""
    assert consumed(cleared)

    restored = specialist([(nt.UNDO_NOTE, UNDO)])
    response = await world.run(restored, **{TURN_ID_CONTEXT_KEY: "t2"})

    note = await world.note()
    assert (note.state, note.text, note.doc_type, note.life_areas) == ("active", REPAIR, "contract", ["home"])
    assert receipts(response)[0]["action"] == "restore"
    assert consumed(restored)


async def test_erase_note_stores_a_request_and_a_code_for_the_owner_and_erases_nothing():
    world = World()
    await world.seed()
    model = specialist([(nt.ERASE_NOTE, ERASE)])

    response = await world.run(model)

    note = await world.note()
    assert (note.text, note.current_version, note.state) == (REPAIR, 1, "active")     # nothing is erased by that call
    assert len(world.env.store.data) == 1                                          # the pending request, by its code
    (item,) = [i for i in response.delivery_items if i.type == DRIVE_NOTE_ERASE_DELIVERY]
    code = item.data["command"].split()[-1]
    assert any(code in key for key in world.env.store.data)                       # the code the item carries
    assert all(code not in line for line in told(model)) and code not in response.result
    assert consumed(model)


async def test_get_note_shows_the_stored_words_as_data_and_writes_nothing():
    world = World()
    await world.seed()
    model = specialist([(nt.GET_NOTE, GET)])

    response = await world.run(model)

    (seen,) = told(model)
    assert REPAIR in seen and "contract" in seen
    assert (await world.note()).current_version == 1
    assert response.delivery_items == [] and not response.history_context
    assert consumed(model)


async def test_a_create_over_a_different_note_is_refused_and_the_stored_note_stays():
    world = World()
    await world.seed(text="the painting invoice from Pinturas Exemplo")
    model = specialist([(nt.CREATE_NOTE, CREATE)])

    response = await world.run(model)

    note = await world.note()
    assert (note.text, note.current_version) == ("the painting invoice from Pinturas Exemplo", 1)
    assert "already has a note" in told(model)[0] and "Nothing was stored." in told(model)[0]
    assert response.delivery_items == []
    assert consumed(model)


async def test_a_writer_off_an_owner_turn_is_not_served_and_stores_nothing():
    world = World()
    model = specialist([(nt.CREATE_NOTE, CREATE)])

    await world.run(model, interactive_delivery=None, run_origin="background:fire_due_reminders")

    assert await world.note() is None
    assert consumed(model)


# ------------------------------------------------------------------------------------------------ declared fields

def test_the_scripted_calls_use_every_declared_field():
    assert unused("drive_notes", nt.CREATE_NOTE, [CREATE]) == []
    assert unused("drive_notes", nt.ADD_TO_NOTE, [ADD]) == []
    assert unused("drive_notes", nt.GET_NOTE, [GET]) == []
    assert unused("drive_notes", nt.REPLACE_NOTE_TEXT, [REPLACE]) == []
    assert unused("drive_notes", nt.SET_NOTE_TYPE, [SET_TYPE]) == []
    assert unused("drive_notes", nt.CLEAR_NOTE_TYPE, [CLEAR_TYPE]) == []
    assert unused("drive_notes", nt.ADD_LIFE_AREAS, [ADD_AREAS]) == []
    assert unused("drive_notes", nt.REMOVE_LIFE_AREAS, [REMOVE_AREAS]) == []
    assert unused("drive_notes", nt.DELETE_NOTE, [DELETE]) == []
    assert unused("drive_notes", nt.UNDO_NOTE, [UNDO]) == []
    assert unused("drive_notes", nt.ERASE_NOTE, [ERASE]) == []
