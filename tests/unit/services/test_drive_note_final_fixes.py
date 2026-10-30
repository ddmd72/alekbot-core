"""The final fix batch's smaller rulings (`.superpowers/sdd/drive-notes/final-fix-rulings.md`), one test or more each:
D-I2 the nudge cap and no erase-all nudge; D-I3 the drive id falls back to the sync state's; C-I3 the CONFLICT detail
carries the scope; C-M4 / C-M5 refusal texts; C-M7 an unreadable connection is not "another drive"; L-M1 an erase with
indexing off keeps the stub while a record holds the copy; L-M3 a `drive:` ref never acts on another drive's note while
a different one is connected; L-M2 a deleted note is found by its last known name; C-M11 a deleted entry's date is the
last time the file was seen; D-M1 an unknown round-end check ages out; D-M2 no
round end without an item call; D-M3 a refused note embedding backs off alone; D-M5 the `note_checks` exemption; the
texts of C-I3, C-I5, C-M9. Synthetic data only."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.domain.drive_index import DriveIndexRecord, DriveSyncState
from src.domain.drive_index_params import ITEM_CALLS_PER_TICK
from src.domain.drive_keys import query_concepts
from src.domain.drive_note import (
    STATUS_OTHER_DRIVE, DriveNoteRefused, NoteBinding, NoteRefusal, NoteSnapshot, NoteState, binding_key,
)
from src.domain.drive_note_check import WHY_GONE, NoteCheck
from src.domain.drive_note_lifecycle import NOTE_CHECK_UNKNOWN_MAX_AGE
from src.domain.drive_note_render import search_keys
from src.domain.exceptions import LLMClientError
from src.domain.user_drive import DriveConnection
from src.locales import en, es, fr
from src.domain.ui_messages import UIMessage

from .test_drive_note_service import ACC, CHAT, TEXT, U, Env, _Clock, _FlakyErase, refusal

ROOT = Path(__file__).resolve().parents[3]


# -- D-I2: the nudge ---------------------------------------------------------------------------------------------------

async def test_a_nudge_carries_at_most_item_calls_per_tick_ids():
    env = Env()
    await env.service._nudge(U, [f"i{n}" for n in range(ITEM_CALLS_PER_TICK + 15)])
    assert len(env.nudged()[-1]) == ITEM_CALLS_PER_TICK


async def test_an_erase_all_nudges_nothing_and_a_one_note_erase_nudges_its_item():
    env = Env()
    for item in ("a", "b"):
        await env.service.set(U, ACC, f"drive:{item}", TEXT, provenance=CHAT)
    one = await env.service.request_erase(U, "drive:a")
    before = len(env.nudged())
    assert await env.service.erase(U, one.code) == 1
    assert env.nudged()[before:] == [["a"]]
    every = await env.service.request_erase_all(U)
    before = len(env.nudged())
    assert await env.service.erase(U, every.code) == 1
    assert env.nudged()[before:] == []


# -- D-I3: the drive id ------------------------------------------------------------------------------------------------

async def test_an_empty_credential_drive_id_falls_back_to_the_sync_states():
    env = Env()
    env.drive.connection.return_value = DriveConnection(connected=True, drive_id="")
    env.index.get_sync_state.return_value = DriveSyncState(user_id=U, drive_id="drive-1")
    receipt = await env.service.set(U, ACC, "drive:a", TEXT, provenance=CHAT)
    note = await env.notes.find_bound(U, binding_key("onedrive", "drive-1", "a"))
    assert note is not None and note.note_id == receipt.note_id


async def test_no_drive_id_anywhere_is_still_not_connected():
    env = Env()
    env.drive.connection.return_value = DriveConnection(connected=True, drive_id="")
    env.index.get_sync_state.return_value = None
    with pytest.raises(DriveNoteRefused) as info:
        await env.service.set(U, ACC, "drive:a", TEXT, provenance=CHAT)
    assert refusal(info) == NoteRefusal.NOT_CONNECTED


# -- C-I3: the scope in the CONFLICT detail ----------------------------------------------------------------------------

async def test_a_partial_erase_all_reports_its_scope():
    env = Env(notes=_FlakyErase(clock=_Clock()))
    receipts = [await env.service.set(U, ACC, f"drive:{item}", TEXT, provenance=CHAT) for item in ("a", "b")]
    env.notes.stuck = receipts[1].note_id
    request = await env.service.request_erase_all(U)
    with pytest.raises(DriveNoteRefused) as info:
        await env.service.erase(U, request.code)
    assert refusal(info) == NoteRefusal.CONFLICT and info.value.detail["scope"] == "all"


# -- C-M4, C-M5: refusal texts -----------------------------------------------------------------------------------------

async def test_a_gone_file_refusal_points_to_the_archived_note():
    env = Env(missing={"gone"})
    with pytest.raises(DriveNoteRefused) as info:
        await env.service.set(U, ACC, "drive:gone", TEXT, provenance=CHAT)
    assert refusal(info) == NoteRefusal.NOT_ON_DRIVE
    assert "that note is archived" in str(info.value) and "drive_note:" in str(info.value)


async def test_an_unopenable_file_refusal_asks_for_a_copy_in_a_new_folder():
    rec = DriveIndexRecord(user_id=U, item_id="x", parent_id="r", name="x.pdf",
                           inaccessible_since=_Clock()())
    env = Env(records={"x": rec})
    with pytest.raises(DriveNoteRefused) as info:
        await env.service.set(U, ACC, "drive:x", TEXT, provenance=CHAT)
    assert refusal(info) == NoteRefusal.CANNOT_OPEN
    assert "a new folder they create inside the bot's folder" in str(info.value)


# -- C-M7: an unreadable connection is not "another drive" -------------------------------------------------------------

async def test_an_unreadable_connection_does_not_say_other_drive():
    env = Env()
    first = await env.service.set(U, ACC, "drive:a", TEXT, provenance=CHAT)
    env.drive.connection.side_effect = RuntimeError("graph down")
    receipt = await env.service.set(U, ACC, f"drive_note:{first.note_id}", "new words", provenance=CHAT)
    assert receipt.status != STATUS_OTHER_DRIVE


# -- L-M1: erase with indexing off -------------------------------------------------------------------------------------

async def test_with_indexing_off_a_stub_waits_while_a_record_holds_the_copy():
    env = Env(indexing=False)
    receipt = await env.service.set(U, ACC, "drive:a", TEXT, provenance=CHAT)
    await env.service.set(U, ACC, "drive:b", "two", provenance=CHAT)
    held = (await env.notes.find_bound(U, binding_key("onedrive", "drive-1", "a"))).note_id
    env.records["a"] = DriveIndexRecord(user_id=U, item_id="a", parent_id="r", name="a.pdf", owner_note_id=held)
    request = await env.service.request_erase_all(U)
    assert await env.service.erase(U, request.code) == 2
    stub = await env.notes.get(U, receipt.note_id)
    assert stub is not None and stub.erase_pending                     # the indexer clears the copy first
    other = await env.notes.find_bound(U, binding_key("onedrive", "drive-1", "b"))
    assert other is None                                                # no copy anywhere: deleted at once


# -- L-M3: a drive: ref and another drive's note ----------------------------------------------------------------------

async def test_a_drive_ref_to_a_note_on_another_drive_is_refused_with_its_own_ref():
    env = Env()
    receipt = await env.service.set(U, ACC, "drive:a", TEXT, provenance=CHAT)
    env.drive.connection.return_value = DriveConnection(connected=True, drive_id="drive-2")
    with pytest.raises(DriveNoteRefused) as info:
        await env.service.clear(U, ACC, "drive:a", CHAT)
    assert refusal(info) == NoteRefusal.NOT_ON_DRIVE and f"drive_note:{receipt.note_id}" in str(info.value)
    assert (await env.notes.get(U, receipt.note_id)).state == NoteState.ACTIVE.value
    cleared = await env.service.clear(U, ACC, f"drive_note:{receipt.note_id}", CHAT)   # its own ref still works
    assert cleared.action == "clear"


# -- L-M2, C-M11: the deleted-files source -----------------------------------------------------------------------------

def _archived(name_then: str, name_now: str):
    from src.domain.drive_note import DriveNote
    from src.domain.drive_note_merge import note_store_keys
    note = DriveNote(note_id="n1", user_id=U, state=NoteState.ARCHIVED, current_version=1, text="bathroom invoice",
                     binding=NoteBinding(provider_key="onedrive", drive_id="drive-1", item_id="i1"),
                     snapshot=NoteSnapshot(name=name_now, path=name_now))
    keys, version = note_store_keys(note.content, name_then)
    return note.model_copy(update={"keys": keys, "keys_version": version})


def test_a_renamed_then_deleted_file_is_found_by_its_last_known_name():
    keys = search_keys(_archived("draft.pdf", "lease2024.pdf"))
    concept = query_concepts("lease2024").concepts[0]
    assert any(k in keys for k in (*concept.about_keys, *concept.word_keys))


async def test_a_deleted_entry_shows_the_day_the_file_was_last_seen():
    from src.adapters.in_memory_drive_note_repo import InMemoryDriveNoteRepository
    from src.services.drive_note_deleted_search import DriveNoteDeletedSearch
    from datetime import datetime, timezone
    notes = InMemoryDriveNoteRepository()
    note = _archived("a.pdf", "a.pdf").model_copy(update={
        "last_seen_at": datetime(2026, 9, 3, tzinfo=timezone.utc),
        "archived_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 10, 1, tzinfo=timezone.utc)})
    notes.notes[(U, note.note_id)] = note
    source = DriveNoteDeletedSearch(notes, AsyncMock(), embedding_space="s")
    hits, _ = await source(U, query_concepts("bathroom").concepts, DriveConnection(connected=True, drive_id="drive-1"))
    assert [h.day for h in hits] == ["2026-09-03"]


# -- D-M1, D-M2: the round end ------------------------------------------------------------------------------------------

async def test_an_unknown_check_older_than_the_limit_is_dropped():
    from .drive_note_world import NotesWorld
    w = NotesWorld()
    a = w.drive.add("a.pdf")
    await w.settle()
    nid = w.note_on(a)
    w.drive.delete(a)
    w.drive.get_change = AsyncMock(side_effect=OSError("graph flaky"))   # every read says nothing
    await w.settle()
    assert [c.note_id for c in (await w.state()).note_checks] == [nid]
    w.clock.advance((NOTE_CHECK_UNKNOWN_MAX_AGE + timedelta(hours=1)).total_seconds())
    await w.tick()
    assert (await w.state()).note_checks == ()
    assert w.note(nid).state == NoteState.ACTIVE.value                  # never archived on a guess: the audit decides


async def test_no_round_end_or_audit_without_an_item_call():
    from src.services import drive_indexing_service as svc_mod
    from .drive_note_world import NotesWorld
    w = NotesWorld()
    w.lifecycle.round_end = AsyncMock()
    w.lifecycle.audit = AsyncMock()
    budget = SimpleNamespace(item_call_allowed=lambda: False, item_calls=0)
    token = svc_mod._TICK_NOTES.set(svc_mod._TickNotes(drive_id="d1", lifecycle=True))
    try:
        state = DriveSyncState(user_id="u", connection_id="u", drive_id="d1", note_checks=(NoteCheck("n", "i", WHY_GONE, None),))
        summary: dict = {}
        assert await w.svc._notes_round_end("u", state, budget, summary) == state
    finally:
        svc_mod._TICK_NOTES.reset(token)
    w.lifecycle.round_end.assert_not_awaited()
    w.lifecycle.audit.assert_not_awaited()
    assert summary == {"note_round_end_skipped": 1}


# -- D-M3: a refused note embedding -----------------------------------------------------------------------------------

async def test_a_refused_note_embedding_backs_off_alone():
    from .test_drive_indexing_notes import World
    w = World()
    a, b = w.drive.add("a.pdf"), w.drive.add("b.pdf")
    await w.settle()
    first = await w.put_note(a, "refused words")
    second = await w.put_note(b, "fine words")
    for note in (first, second):
        w.notes.notes[("u", note.note_id)] = note.model_copy(update={"embedding_pending": True, "note_vector": None})
    real = w.emb.get_embedding

    async def embed(text, task_type="RETRIEVAL_DOCUMENT"):
        if "refused words" in text:
            raise LLMClientError("bad input", http_status=400)
        return await real(text, task_type)

    w.emb.get_embedding = embed
    w.clock.advance(1)
    await w.tick()
    assert (await w.notes.get("u", second.note_id)).embedding_pending is False   # not blocked behind the refusal
    assert (await w.notes.get("u", first.note_id)).embedding_pending is True
    from src.domain.drive_note import note_embedding_text
    held = (await w.notes.get("u", first.note_id))
    note_text = note_embedding_text(held.content, held.snapshot.name if held.snapshot else "")
    calls = []
    w.emb.get_embedding = lambda text, task_type="RETRIEVAL_DOCUMENT": calls.append(text) or real(text, task_type)
    w.clock.advance(600)
    await w.tick()
    assert note_text not in calls                                                # held, not retried every tick


# -- D-M5: the field exemption ------------------------------------------------------------------------------------------

def test_note_checks_on_the_sync_state_is_exempt_from_single_field_indexes():
    entries = json.loads((ROOT / "config" / "firestore.indexes.json").read_text())
    assert {"collectionGroup": "development_drive_sync_state", "fieldPath": "note_checks", "indexes": []} in entries


# -- texts: C-I3, C-I5, C-M9 -------------------------------------------------------------------------------------------

def test_erase_all_is_named_in_the_capabilities():
    from src.utils import capabilities
    text = Path(capabilities.__file__).read_text()
    assert "to erase all of them with their history, type `$drive-notes erase-all`" in text
    assert "- `$drive-notes erase-all` — erase all your drive notes" in text


@pytest.mark.parametrize("module", [en, es, fr], ids=["en", "es", "fr"])
def test_one_wording_per_language_for_drive_notes(module):
    strings = module.UI_STRINGS
    if module is en:
        assert "file notes" not in strings[UIMessage.DRIVE_NOTES_USAGE.value]
        assert strings[UIMessage.DRIVE_NOTES_NONE.value] == "You have no drive notes."
    if module is fr:
        assert strings[UIMessage.DRIVE_NOTES_NO_SUCH_REQUEST.value].startswith(
            "❌ Cette demande d'effacement n'existe pas : ")
    if module is es:
        assert "para borrarla para siempre" in strings[UIMessage.DRIVE_NOTE_CLEARED.value]
    assert "{erased}" in strings[UIMessage.DRIVE_NOTE_ERASE_ALL_PARTIAL.value]
