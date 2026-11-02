"""DriveNoteService error paths in 2b: an unreadable connection table, a failed access check (each refusal and its
line), a stale hint on a drive note, and the place name when the connection cannot be read."""
from unittest.mock import AsyncMock

import pytest

from src.domain.drive_note import DriveNoteRefused, NoteRefusal
from src.domain.user_drive import DriveItemNotFoundError, DriveNotConnectedError
from src.services.drive_note_service import DriveNoteService
from tests.unit.services.test_drive_note_service import ACC, CHAT, TEXT, U, Env, ref

UNREACHABLE = "The drive could not be reached: try again later."
ACME_TEXT = "Acme work files: R&D, Tom's plan"


def _service(env: Env, access) -> DriveNoteService:
    return DriveNoteService(env.notes, env.drive, env.index, env.embeddings, env.security, env.connections,
                            access=access, embedding_space="space-a", nudge_queue=env.queue)


async def _drive_note_ref(env: Env) -> str:
    await env.service.set(U, ACC, "", ACME_TEXT, provenance=CHAT, whole_drive_name="Drive")
    ids = await env.notes.note_ids(U)
    return f"drive_note:{ids[0]}"


class TestUnreadableConnections:
    async def test_a_whole_drive_write_with_an_unreadable_connection_is_drive_unavailable(self):
        env = Env()
        await env.service.set(U, ACC, "", ACME_TEXT, provenance=CHAT, whole_drive_name="Drive")
        env.connections.get = AsyncMock(side_effect=RuntimeError("store down"))

        with pytest.raises(DriveNoteRefused) as info:
            await env.service.set(U, ACC, "", ACME_TEXT + " more", provenance=CHAT, whole_drive_name="Drive")

        assert info.value.reason == NoteRefusal.DRIVE_UNAVAILABLE and str(info.value) == UNREACHABLE

    async def test_an_unreadable_mount_table_is_drive_unavailable(self):
        env = Env()
        env.connections.mounts = AsyncMock(side_effect=RuntimeError("store down"))

        with pytest.raises(DriveNoteRefused) as info:
            await env.service.set(U, ACC, "", ACME_TEXT, provenance=CHAT, whole_drive_name="Drive")

        assert info.value.reason == NoteRefusal.DRIVE_UNAVAILABLE and str(info.value) == UNREACHABLE


class TestAccessCheckRefusals:
    @pytest.mark.parametrize("error, reason, line", [
        (DriveNotConnectedError("conn-1"), NoteRefusal.NOT_CONNECTED,
         "The drive is not connected: connect it first (a note needs the file verified)."),
        (DriveItemNotFoundError("a"), NoteRefusal.NOT_ON_DRIVE, "The file is not on the drive."),
        (RuntimeError("boom"), NoteRefusal.DRIVE_UNAVAILABLE, UNREACHABLE),
    ])
    async def test_each_access_error_is_its_own_refusal_and_nothing_is_stored(self, error, reason, line):
        env = Env()
        access = AsyncMock()
        access.check_access.side_effect = error

        with pytest.raises(DriveNoteRefused) as info:
            await _service(env, access).set(U, ACC, ref("a"), TEXT, provenance=CHAT)

        assert info.value.reason == reason and str(info.value) == line
        assert await env.note() is None


class TestDriveNoteEditedByItsRef:
    async def test_a_noop_edit_of_a_drive_note_carries_no_status(self):
        env = Env()
        note_ref = await _drive_note_ref(env)

        again = await env.service.set(U, ACC, note_ref, ACME_TEXT, provenance=CHAT)

        assert again.noop and again.status == ""

    async def test_an_edit_shows_the_stored_drive_name_when_the_connection_cannot_be_read(self):
        env = Env()
        note_ref = await _drive_note_ref(env)
        env.connections.get = AsyncMock(side_effect=RuntimeError("store down"))

        receipt = await env.service.set(U, ACC, note_ref, ACME_TEXT + " more", provenance=CHAT)

        assert receipt.path == "Drive"

    async def test_an_item_note_edit_shows_the_stored_path_when_the_tag_lookup_fails(self):
        env = Env()
        first = await env.service.set(U, ACC, ref("a"), TEXT, provenance=CHAT)
        note = await env.note()
        env.connections.connection_of_tag = AsyncMock(side_effect=RuntimeError("store down"))

        receipt = await env.service.set(U, ACC, f"drive_note:{note.note_id}", TEXT + " more", provenance=CHAT)

        assert receipt.path == note.snapshot.path and first.path != ""


class TestNoopClearOfAnUnwrittenDriveNote:
    async def test_the_receipt_has_no_name_when_the_connection_cannot_be_read(self):
        env = Env()
        env.connections.get = AsyncMock(side_effect=RuntimeError("store down"))

        receipt = await env.service.clear(U, ACC, "", CHAT, whole_drive_name="Drive")

        assert receipt.noop and receipt.drive and receipt.path == ""

    async def test_the_receipt_names_the_drive_when_it_can(self):
        env = Env()

        receipt = await env.service.clear(U, ACC, "", CHAT, whole_drive_name="Drive")

        assert receipt.noop and receipt.drive and receipt.path == "Drive"
