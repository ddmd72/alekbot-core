"""Harness safety for the notes (DRIVE_SPECIALIST_RFC §12, §9): in the drive evals no note write ever runs. The note tools
reach the owner's notes through `DriveNoteService`, which `DryRunDriveOperations` does not cover (it stubs only
`DriveOperations.MUTATION_METHODS`), so `DryRunDriveNotes` stubs every write of the class, every instance, and refuses to
install while a public method is unclassified. The agent-level test is the hard gate before any owner run: a specialist
run that scripts `create_note` under the dry run writes nothing to the store."""
import pytest

from scripts.validation.drive_eval.dry_run_drive import DriveOpLog, DryRunDriveOperations, public_coroutines
from scripts.validation.drive_eval.dry_run_drive_notes import (
    READ_METHODS, STUBS, WRITE_METHODS, DryRunDriveNotes, DryRunNotesError)
from src.domain.drive_note import ERASE_SCOPE_ALL, ERASE_SCOPE_ONE, NoteContent, NoteProvenance
from src.domain.drive_note_change import NoteChange
from src.domain import drive_note_tool_texts as nt
from src.services.drive_note_service import DriveNoteService
from tests.support.scripted_llm import tool_results
from tests.unit.agents.test_drive_agent_note_tool_fields_e2e import CREATE, REPAIR, World, specialist
from tests.unit.services.test_drive_note_service import ACC, U, Env

PROV = NoteProvenance(origin="chat", session_id="s1", turn_id="t1")
# method: (positional arguments after user_id [and account_id], keywords)
CALLS = {
    "set": ((ACC, "drive:a1:a", REPAIR), {"doc_type": "contract"}),
    "create": ((ACC, "drive:a1:a", NoteContent(REPAIR)), {}),
    "update": ((ACC, "drive:a1:a", NoteChange.append_text("second invoice")), {}),
    "clear": ((ACC, "drive:a1:a"), {}),
    "undo": ((ACC, "drive:a1:a"), {}),
    "request_erase": (("drive:a1:a",), {}),
    "request_erase_all": ((), {}),
    "erase": (("c0000",), {}),
}


def _untouched(env: Env) -> bool:
    return (env.drive.mock_calls == [] and env.index.mock_calls == [] and env.embeddings.mock_calls == []
            and env.queue.mock_calls == [] and env.store.data == {} and env.users.mock_calls == [])


def _nothing_written(env: Env) -> bool:
    """The agent's own preflight reads the drive connection, so only what a write would touch is checked."""
    return env.embeddings.mock_calls == [] and env.queue.mock_calls == [] and env.store.data == {}


def test_every_public_note_service_method_is_classified_read_or_write():
    assert set(public_coroutines(DriveNoteService)) == READ_METHODS | WRITE_METHODS
    assert not READ_METHODS & WRITE_METHODS and WRITE_METHODS == set(STUBS) == set(CALLS)


def test_a_new_unclassified_method_refuses_the_install():
    class Wider(DriveNoteService):
        async def purge_everything(self, user_id: str) -> None: ...

    with pytest.raises(DryRunNotesError, match="purge_everything"):
        DryRunDriveNotes(DriveOpLog(), Wider).install()


@pytest.mark.parametrize("method", sorted(CALLS))
async def test_a_note_write_under_the_dry_run_reaches_no_port_and_stores_nothing(method):
    env = Env()
    args, kwargs = CALLS[method]
    log = DriveOpLog()
    with DryRunDriveNotes(log):
        if method in ("request_erase", "request_erase_all", "erase"):
            out = await getattr(env.service, method)(U, *args, **kwargs)
        else:
            out = await getattr(env.service, method)(U, *args, provenance=PROV, **kwargs)
    assert _untouched(env) and await env.note() is None
    assert [(e["op"], e["stubbed"], e["outcome"]) for e in log.entries] == [(f"note_{method}", True, "ok")]
    if method == "request_erase":
        assert out.scope == ERASE_SCOPE_ONE
    if method == "request_erase_all":
        assert out.scope == ERASE_SCOPE_ALL
    if method == "erase":
        assert out == 0


@pytest.mark.parametrize("method", ["set", "create", "update"])
async def test_the_same_write_without_the_dry_run_reaches_the_store(method):
    """Proves the test above can fail: the stub, not the arguments, keeps the store untouched."""
    env = Env()
    args, kwargs = CALLS[method]
    await getattr(env.service, method)(U, *args, provenance=PROV, **kwargs)
    assert await env.note() is not None and not _untouched(env)


async def test_the_log_names_the_target_and_never_the_words():
    env = Env()
    log = DriveOpLog()
    with DryRunDriveNotes(log):
        await env.service.create(U, ACC, "drive:a1:a", NoteContent(REPAIR, "contract", ("home",)), PROV)
        await env.service.update(U, ACC, "drive:a1:a", NoteChange.append_text("second invoice"), PROV)
    assert log.entries[0]["args"] == {"ref": "drive:a1:a"}
    assert "second invoice" not in str(log.entries) and REPAIR not in str(log.entries)


async def test_reads_still_run_under_the_dry_run():
    env = Env()
    await env.service.set(U, ACC, "drive:a1:a", REPAIR, "", [], PROV)
    with DryRunDriveNotes(DriveOpLog()):
        view = await env.service.get(U, "drive:a1:a")
    assert view is not None and view.text == REPAIR


async def test_uninstall_restores_the_class_and_a_second_install_is_refused():
    original = DriveNoteService.create
    notes = DryRunDriveNotes(DriveOpLog())
    notes.install()
    with pytest.raises(DryRunNotesError):
        notes.install()
    assert DriveNoteService.create is not original
    notes.uninstall()
    assert DriveNoteService.create is original


async def test_a_dry_run_specialist_with_a_scripted_create_note_writes_nothing_to_the_store():
    """The hard gate: a real DriveAgent on an owner turn over the real note service, both dry runs installed."""
    world = World()
    log = DriveOpLog()
    model = specialist([(nt.CREATE_NOTE, CREATE)])

    with DryRunDriveOperations(log), DryRunDriveNotes(log):
        response = await world.run(model)

    assert await world.note() is None                       # the store holds no note
    assert _nothing_written(world.env)                      # no embedding, no nudge, no erase request
    assert [(e["op"], e["stubbed"]) for e in log.entries if e["op"] != "preflight"] == [("note_create", True)]
    assert "Note saved" in str(tool_results(model))          # the model was told it landed
    assert response.delivery_items                           # and the receipt shape came through, naming nothing real
