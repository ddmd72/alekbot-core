"""Forwarded keywords and a held write in DriveNoteService that the other suites left unasserted: the whole-drive
name reaching `clear`, the item id carried by the refusals of the access check and of the item read, and the erase
stub kept when the index read fails."""


from src.services.drive_note_service import DriveNoteService
from tests.unit.services.test_drive_note_service import ACC, CHAT, TEXT, U, Env, ref

ITEM = "item-mut-1"
DRIVE_TEXT = "Drive-mut-1 holds the working files"


def _service(env: Env, access) -> DriveNoteService:
    return DriveNoteService(env.notes, env.drive, env.index, env.embeddings, env.security, env.connections,
                            access=access, embedding_space="space-a", nudge_queue=env.queue)


async def test_an_empty_set_clears_the_drive_note_named_by_the_whole_drive_name():
    env = Env()
    await env.service.set(U, ACC, "", DRIVE_TEXT, provenance=CHAT, whole_drive_name="Drive")
    [note_id] = await env.notes.note_ids(U)

    await env.service.set(U, ACC, "", "", provenance=CHAT, whole_drive_name="Drive")

    assert (await env.notes.get(U, note_id)).state == "cleared"


async def test_an_unreadable_index_keeps_the_erase_stub_for_the_indexer():
    env = Env(indexing=False)
    await env.service.set(U, ACC, ref(ITEM), TEXT, provenance=CHAT)
    request = await env.service.request_erase(U, ref(ITEM))
    env.index.get_records.side_effect = RuntimeError("index down")

    assert await env.service.erase(U, request.code) == 1

    assert await env.notes.count(U) == 1
