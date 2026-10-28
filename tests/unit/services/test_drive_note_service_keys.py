"""The note document's `keys` (DRIVE_FILE_NOTES_RFC §3.1, §5.4; plan part B): every owner write stores the note's keys
(`drive_keys.note_keys`) and the snapshot name's name keys, with `keys_version` "{KEYS_VERSION}.{NOTE_KEYS_VERSION}";
a clear and an undo to the empty triple store none. They feed only the deleted-files search (part C)."""
from __future__ import annotations

from src.domain.drive_index import KEYS_VERSION
from src.domain.drive_keys import name_keys_v2, note_keys
from src.domain.drive_note import NOTE_KEYS_VERSION

from .test_drive_note_service import ACC, CHAT, NEXT_TURN, U, Env

VERSION = f"{KEYS_VERSION}.{NOTE_KEYS_VERSION}"


async def test_set_stores_the_note_keys_and_the_name_keys():
    env = Env()
    await env.service.set(U, ACC, "drive:a", "the contractor's invoice", "contract", ["home"], CHAT)
    note = await env.note("a")
    assert set(note_keys("the contractor's invoice", "contract", ("home",))) <= set(note.keys)
    assert set(name_keys_v2("a.pdf")) <= set(note.keys) and note.keys_version == VERSION


async def test_an_edit_by_the_note_ref_re_keys():
    env = Env()
    await env.service.set(U, ACC, "drive:a", "the contractor's invoice", provenance=CHAT)
    note = await env.note("a")
    await env.service.set(U, ACC, note.ref, "Pinturas Exemplo's invoice 2026/078", provenance=NEXT_TURN)
    edited = await env.note("a")
    assert set(note_keys("Pinturas Exemplo's invoice 2026/078")) <= set(edited.keys)
    assert not set(note_keys("the contractor's invoice")) - set(note_keys("Pinturas Exemplo's invoice 2026/078")) & set(edited.keys)


async def test_clear_and_undo_keep_the_keys_in_step():
    env = Env()
    await env.service.set(U, ACC, "drive:a", "the contractor's invoice", provenance=CHAT)
    await env.service.clear(U, ACC, "drive:a", NEXT_TURN)
    cleared = await env.note("a")
    assert cleared.keys == [] and cleared.keys_version == ""
    await env.service.undo(U, ACC, "drive:a", CHAT)
    restored = await env.note("a")
    assert set(note_keys("the contractor's invoice")) <= set(restored.keys) and restored.keys_version == VERSION
