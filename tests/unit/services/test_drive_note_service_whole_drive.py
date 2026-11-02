"""Whole-drive notes in DriveNoteService (MULTI_DRIVE_RFC §11.10, T-13 service half; slice 2b P-D): a note about the
entire drive is bound by `c:{connection_id}`, resolved from the drive's name (current or held former), written, undone,
cleared and erased like a file note, counted in the lineage cap, never searched and never nudged; the live item path
runs the READ access check before the item is read."""
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_mounts import RENAME_NOTICE, unknown_drive_line
from src.domain.drive_note import (
    NOTES_PER_USER_MAX, DriveNoteRefused, NoteRefusal, connection_binding_key, drive_note_connection_id, is_drive_note,
    item_binding_key,
)
from src.domain.drive_note_receipt import render_drive_note_erase, render_drive_note_receipt
from src.domain.drive_note_tool_texts import ONE_TARGET, erase_request_line, note_receipt_line
from src.domain.user_drive import DriveAccessRefused
from src.locales import en
from src.services.drive_note_service import DriveNoteService
from tests.unit.services.test_drive_note_service import (
    ACC, C1, CHAT, NEXT_TURN, TEXT, U, Env, identity, refusal, ref,
)

ACME_TEXT = "Acme work files: R&D, Tom's plan"
KEY = connection_binding_key(C1)


def strings(message):
    return en.UI_STRINGS[message.value]


async def _rename(env: Env, name: str) -> None:
    check = await env.connections.rename(U, C1, name)
    assert check.ok, check


@pytest.fixture
async def acme() -> Env:
    env = Env()
    await _rename(env, "Acme")
    return env


async def _drive_note(env: Env):
    return await env.notes.find_bound(U, KEY)


class TestWrittenByDriveName:
    async def test_a_note_by_whole_drive_name_is_bound_by_the_connection_key(self, acme):
        receipt = await acme.service.create(U, ACC, "", _content(ACME_TEXT), CHAT, whole_drive_name="Acme")

        note = await _drive_note(acme)
        assert note.match_keys == [KEY] and note.binding.connection_id == C1 and note.binding.item_id == ""
        assert (note.binding.ref_tag, note.state, note.current_version, note.text) == ("a1", "active", 1, ACME_TEXT)
        assert (note.snapshot.name, note.snapshot.is_folder) == ("Acme", True)
        assert is_drive_note(note) and drive_note_connection_id(note) == C1
        assert (receipt.path, receipt.drive, receipt.ref, receipt.status) == ("Acme", True, "", "")

    async def test_the_receipt_names_the_drive_in_chat_and_in_the_tool_result(self, acme):
        receipt = await acme.service.set(U, ACC, "", ACME_TEXT, provenance=CHAT, whole_drive_name="Acme")

        shown = render_drive_note_receipt(receipt.delivery_data(), strings)
        assert shown.split("\n")[0] == "📝 Note saved on drive Acme (version 1):"
        assert note_receipt_line(receipt).startswith("Note saved on drive Acme (version 1).")
        assert receipt.delivery_data()["drive"] is True

    async def test_a_file_receipt_does_not_say_drive(self, acme):
        receipt = await acme.service.set(U, ACC, ref("a"), TEXT, provenance=CHAT)

        assert receipt.drive is False and "drive" not in receipt.delivery_data()

    async def test_no_drive_call_no_index_read_no_embedding_no_nudge(self, acme):
        await acme.service.set(U, ACC, "", ACME_TEXT, provenance=CHAT, whole_drive_name="Acme")

        assert acme.graph_calls() == 0
        acme.index.get_records.assert_not_awaited()
        acme.embeddings.get_embedding.assert_not_awaited()
        assert acme.nudged() == []
        note = await _drive_note(acme)
        assert note.note_vector is None and note.embedding_pending is False

    async def test_get_reads_it_by_name_and_shows_the_drive_name_as_its_place(self, acme):
        await acme.service.set(U, ACC, "", ACME_TEXT, provenance=CHAT, whole_drive_name="Acme")

        view = await acme.service.get(U, "", whole_drive_name="acme")

        assert (view.text, view.path, view.version) == (ACME_TEXT, "Acme", 1)

    async def test_get_of_a_drive_with_no_note_is_none(self, acme):
        assert await acme.service.get(U, "", whole_drive_name="Acme") is None

    async def test_the_second_write_is_version_two_on_the_same_lineage(self, acme):
        await acme.service.set(U, ACC, "", ACME_TEXT, provenance=CHAT, whole_drive_name="Acme")
        first = await _drive_note(acme)

        await acme.service.set(U, ACC, "", "Family documents: Jamie's birth certificate", provenance=NEXT_TURN,
                               whole_drive_name="Acme")

        note = await _drive_note(acme)
        assert (note.note_id, note.current_version) == (first.note_id, 2)

    async def test_drive_note_texts_lists_it_by_connection(self, acme):
        await acme.service.set(U, ACC, "", ACME_TEXT, provenance=CHAT, whole_drive_name="Acme")

        assert await acme.service.drive_note_texts(U) == {C1: ACME_TEXT}


class TestRenamedDrive:
    async def test_a_rename_changes_nothing_stored_and_the_next_receipt_prints_the_new_name(self, acme):
        await acme.service.set(U, ACC, "", ACME_TEXT, provenance=CHAT, whole_drive_name="Acme")
        before = await _drive_note(acme)

        await _rename(acme, "Acme Hamburg")

        assert await _drive_note(acme) == before
        receipt = await acme.service.set(U, ACC, "", ACME_TEXT + " (Hamburg)", provenance=NEXT_TURN,
                                         whole_drive_name="Acme Hamburg")
        assert receipt.path == "Acme Hamburg"
        assert render_drive_note_receipt(receipt.delivery_data(), strings).startswith(
            "📝 Note saved on drive Acme Hamburg (version 2):")

    async def test_the_former_name_still_resolves_and_the_notice_is_available(self, acme):
        await acme.service.set(U, ACC, "", ACME_TEXT, provenance=CHAT, whole_drive_name="Acme")
        await _rename(acme, "Acme Hamburg")

        receipt = await acme.service.update(U, ACC, "", _append("Hamburg office"), NEXT_TURN, whole_drive_name="Acme")

        assert receipt.path == "Acme Hamburg" and receipt.version == 2
        assert await acme.service.drive_rename_notice(U, "Acme") == RENAME_NOTICE.format(
            former="Acme", current="Acme Hamburg")
        assert await acme.service.drive_rename_notice(U, "Acme Hamburg") == ""
        assert await acme.service.drive_rename_notice(U, "Nowhere") == ""


class TestUndoClearErase:
    async def test_undo_restores_the_previous_text_by_drive_name(self, acme):
        await acme.service.set(U, ACC, "", ACME_TEXT, provenance=CHAT, whole_drive_name="Acme")
        await acme.service.set(U, ACC, "", "Acme invoices", provenance=NEXT_TURN, whole_drive_name="Acme")

        receipt = await acme.service.undo(U, ACC, "", _turn("t3"),
                                          whole_drive_name="Acme")

        assert (await _drive_note(acme)).text == ACME_TEXT
        assert receipt.drive and receipt.path == "Acme"

    async def test_clear_keeps_the_history_and_says_drive(self, acme):
        await acme.service.set(U, ACC, "", ACME_TEXT, provenance=CHAT, whole_drive_name="Acme")

        receipt = await acme.service.clear(U, ACC, "", NEXT_TURN, whole_drive_name="Acme")

        note = await _drive_note(acme)
        assert (note.state, note.text) == ("cleared", "") and receipt.drive
        assert render_drive_note_receipt(receipt.delivery_data(), strings).startswith("🗑️ Note removed from drive Acme.")

    async def test_clear_with_no_note_is_a_noop_that_still_names_the_drive(self, acme):
        receipt = await acme.service.clear(U, ACC, "", CHAT, whole_drive_name="Acme")

        assert receipt.noop and receipt.drive and receipt.path == "Acme"
        assert render_drive_note_receipt(receipt.delivery_data(), strings) == (
            "There was no note on drive Acme; nothing changed.")

    async def test_an_erase_request_names_the_drive_and_redeeming_it_deletes_the_note_at_once(self, acme):
        await acme.service.set(U, ACC, "", ACME_TEXT, provenance=CHAT, whole_drive_name="Acme")
        note_id = (await _drive_note(acme)).note_id

        request = await acme.service.request_erase(U, "", whole_drive_name="Acme")

        assert request.drive and request.path == "Acme" and request.version_count == 1
        assert "drive Acme" in erase_request_line(request)
        line, _command = render_drive_note_erase(request.delivery_data(), strings)
        assert "drive Acme" in line
        assert await acme.service.erase(U, request.code) == 1
        assert await acme.notes.get(U, note_id) is None            # no record copy to clear: no stub waits
        assert acme.nudged() == []


class TestRefusals:
    async def test_an_unknown_drive_name_is_refused_with_the_drives_listed(self, acme):
        with pytest.raises(DriveNoteRefused) as info:
            await acme.service.set(U, ACC, "", ACME_TEXT, provenance=CHAT, whole_drive_name="Springfield")

        assert refusal(info) == NoteRefusal.INVALID_REF
        table = await acme.connections.mounts(U)
        assert str(info.value) == unknown_drive_line("Springfield", table)
        assert await _drive_note(acme) is None

    async def test_a_disconnected_drive_name_is_unknown(self, acme):
        acme.disconnect(C1)

        with pytest.raises(DriveNoteRefused) as info:
            await acme.service.get(U, "", whole_drive_name="Acme")

        assert refusal(info) == NoteRefusal.INVALID_REF

    async def test_both_a_ref_and_a_drive_name_are_refused(self, acme):
        with pytest.raises(DriveNoteRefused) as info:
            await acme.service.set(U, ACC, ref("a"), ACME_TEXT, provenance=CHAT, whole_drive_name="Acme")

        assert refusal(info) == NoteRefusal.INVALID_REF and str(info.value) == ONE_TARGET

    async def test_neither_is_the_old_invalid_ref_refusal(self, acme):
        with pytest.raises(DriveNoteRefused) as info:
            await acme.service.get(U, "")

        assert refusal(info) == NoteRefusal.INVALID_REF

    async def test_a_disconnected_connection_with_a_stale_target_refuses_the_write(self, acme):
        target = await acme.service._target_of(U, "", "Acme")
        acme.disconnect(C1)

        with pytest.raises(DriveNoteRefused) as info:
            await acme.service._live_drive(U, target)

        assert refusal(info) == NoteRefusal.NOT_CONNECTED

    async def test_a_drive_note_counts_in_the_lineage_cap(self, acme):
        acme.notes.count = AsyncMock(return_value=NOTES_PER_USER_MAX)

        with pytest.raises(DriveNoteRefused) as info:
            await acme.service.set(U, ACC, "", ACME_TEXT, provenance=CHAT, whole_drive_name="Acme")

        assert refusal(info) == NoteRefusal.NOTE_LIMIT
        acme.notes.count.assert_awaited_once()

    async def test_the_security_check_runs_before_the_drive_note_is_stored(self, acme):
        acme.security.validate.return_value = replace(acme.security.validate.return_value, action_taken="blocked")

        with pytest.raises(DriveNoteRefused) as info:
            await acme.service.set(U, ACC, "", ACME_TEXT, provenance=CHAT, whole_drive_name="Acme")

        assert refusal(info) == NoteRefusal.SECURITY_REJECTED and await _drive_note(acme) is None


class TestFolderNoteIsNotADriveNote:
    async def test_a_note_on_a_top_level_folder_by_item_ref_binds_the_item_not_the_drive(self, acme):
        await acme.service.set(U, ACC, ref("rd"), "R&D", provenance=CHAT)

        assert await _drive_note(acme) is None
        note = await acme.notes.find_bound(U, item_binding_key(identity("rd")))
        assert note is not None and not is_drive_note(note)


class TestAccessCheck:
    def _service(self, env: Env, access) -> DriveNoteService:
        return DriveNoteService(env.notes, env.drive, env.index, env.embeddings, env.security, env.connections,
                                access=access, embedding_space="space-a", nudge_queue=env.queue)

    async def test_the_read_check_runs_on_the_item_before_it_is_read(self):
        env = Env()
        access = AsyncMock()
        calls = []
        access.check_access.side_effect = lambda *a, **k: calls.append((a, k, env.graph_calls()))

        await self._service(env, access).set(U, ACC, ref("a"), TEXT, provenance=CHAT)

        (args, kwargs, graph_calls_then), = calls
        assert args[1].value == "read" and kwargs == {"item_id": "a"} and graph_calls_then == 0

    async def test_a_refusal_is_the_policy_line_and_nothing_is_read_or_stored(self):
        env = Env()
        access = AsyncMock()
        access.check_access.side_effect = DriveAccessRefused("That file is outside the folders you gave me.")

        with pytest.raises(DriveNoteRefused) as info:
            await self._service(env, access).set(U, ACC, ref("a"), TEXT, provenance=CHAT)

        assert refusal(info) == NoteRefusal.CANNOT_OPEN
        assert str(info.value) == "That file is outside the folders you gave me."
        assert env.graph_calls() == 0 and await env.note() is None

    async def test_a_drive_note_makes_no_access_check_it_reads_no_item(self):
        env = Env()
        access = AsyncMock()

        await self._service(env, access).set(U, ACC, "", ACME_TEXT, provenance=CHAT, whole_drive_name="Drive")

        access.check_access.assert_not_awaited()

    def test_access_is_a_required_argument(self):
        env = Env()
        with pytest.raises(TypeError):
            DriveNoteService(env.notes, env.drive, env.index, env.embeddings, env.security, env.connections,
                             embedding_space="space-a")


def _content(text: str):
    from src.domain.drive_note import note_content
    return note_content(text, "", [])


def _append(text: str):
    from src.domain.drive_note_change import NoteChange
    return NoteChange.append_text(text)


def _turn(turn_id: str):
    from src.domain.drive_note import NoteProvenance
    return NoteProvenance(origin="chat", session_id="s1", turn_id=turn_id)
