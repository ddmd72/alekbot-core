"""The append's re-run guard in DriveNoteService.update (U2 arbiter log "U2-4 follow-up A"; mirrors undo's I3 guard,
DRIVE_FILE_NOTES_RFC §4.4): a provider-rotation re-run re-delegates with the same turn id, so the same append in the
same session and turn writes nothing the second time. On the in-memory note store. Synthetic data only."""
import pytest

from src.domain.drive_note import NoteProvenance
from src.domain.drive_note_change import NoteChange

from .test_drive_note_service import ACC, U, Env

REPAIR = "the bathroom repair invoice from Reformas Exemplo"
TURN = NoteProvenance(origin="chat", session_id="s1", turn_id="t5")
NEXT = NoteProvenance(origin="chat", session_id="s1", turn_id="t6")
OTHER_SESSION = NoteProvenance(origin="voice", session_id="call", turn_id="t5")


@pytest.fixture
def env():
    return Env()


async def _appended(env, provenance=TURN):
    await env.service.set(U, ACC, "drive:a1:a", REPAIR, provenance=NoteProvenance(session_id="s1", turn_id="t1"))
    return await env.service.update(U, ACC, "drive:a1:a", NoteChange.append_text("with IVA"), provenance)


async def test_the_same_append_re_run_in_the_same_turn_writes_nothing(env):
    first = await _appended(env)
    again = await env.service.update(U, ACC, "drive:a1:a", NoteChange.append_text("with IVA"), TURN)
    note = await env.note()
    assert (note.text, note.current_version) == (f"{REPAIR} with IVA", 2)
    assert (again.noop, again.version, again.text, again.note_id) == (True, 2, f"{REPAIR} with IVA", first.note_id)
    assert len(await env.notes.list_versions(U, first.note_id)) == 2


async def test_the_re_run_of_an_append_that_created_the_note_writes_nothing(env):
    await env.service.update(U, ACC, "drive:a1:a", NoteChange.append_text(REPAIR), TURN)
    again = await env.service.update(U, ACC, "drive:a1:a", NoteChange.append_text(REPAIR), TURN)
    note = await env.note()
    assert (note.text, note.current_version, again.noop) == (REPAIR, 1, True)


@pytest.mark.parametrize("provenance", [NEXT, OTHER_SESSION, NoteProvenance(session_id="s1", turn_id="")],
                         ids=["next_turn", "same_turn_id_other_session", "no_turn_id"])
async def test_an_append_in_another_turn_or_session_or_without_a_turn_id_appends_again(env, provenance):
    if not provenance.turn_id:
        await _appended(env, provenance)
    else:
        await _appended(env)
    await env.service.update(U, ACC, "drive:a1:a", NoteChange.append_text("with IVA"), provenance)
    assert (await env.note()).text == f"{REPAIR} with IVA with IVA"


async def test_a_different_append_in_the_same_turn_is_written(env):
    await _appended(env)
    await env.service.update(U, ACC, "drive:a1:a", NoteChange.append_text("and the painting"), TURN)
    note = await env.note()
    assert (note.text, note.current_version) == (f"{REPAIR} with IVA and the painting", 3)


async def test_the_guard_is_only_for_appends(env):
    await env.service.set(U, ACC, "drive:a1:a", REPAIR, "contract", provenance=TURN)
    await env.service.update(U, ACC, "drive:a1:a", NoteChange.add_areas(["home"]), TURN)
    note = await env.note()
    assert (note.life_areas, note.current_version) == (["home"], 2)
