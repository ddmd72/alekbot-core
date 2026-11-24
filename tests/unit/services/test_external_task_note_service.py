"""`ExternalTaskService.create` and the task's note (owner's decision 2026-11-24): required, capped, stored with the
task's entry, returned to the owner's side only.

The service world of `tests/support/external_task_world.py` (the real service over the in-memory stores, the real user
repository on `EvalDb`). Assertions are on the stored task and what each caller is handed. Alex Doe is `usr-1` with
the worker `research`.
"""
import pytest

from src.domain.external_task import NOTE_MAX_CHARS, ExternalTaskStatus
from src.domain.external_task_entry import TaskEntry
from src.domain.external_task_refusal import ExternalTaskRefused, TaskRefusal
from tests.support.external_task_world import NOTE, ORIGIN, TEXT, TITLE, USER_ID, ExternalTaskWorld

S = ExternalTaskStatus


@pytest.fixture
async def world():
    return await ExternalTaskWorld().seed()


@pytest.mark.parametrize("entry,status", [(TaskEntry.CHAT, S.HELD), (TaskEntry.REMINDER, S.QUEUED)])
async def test_the_note_is_stored_with_the_entry_and_returned_to_the_owner(world, entry, status):
    created = await world.service.create(USER_ID, "research", TITLE, TEXT, ORIGIN, entry=entry, note=f"  {NOTE}\n")
    stored = world.tasks.tasks[created.task_id]
    assert (stored.status, stored.note, stored.entry) == (status, NOTE, entry)
    assert created.note == NOTE


@pytest.mark.parametrize("note,reason", [("", TaskRefusal.INVALID_TEXT), (" \n\t", TaskRefusal.INVALID_TEXT),
                                         ("n" * (NOTE_MAX_CHARS + 1), TaskRefusal.TOO_LARGE)])
async def test_an_empty_or_overlong_note_is_refused_and_nothing_is_stored_or_scheduled(world, note, reason):
    with pytest.raises(ExternalTaskRefused) as refused:
        await world.service.create(USER_ID, "research", TITLE, TEXT, ORIGIN, entry=TaskEntry.REMINDER, note=note)
    assert refused.value.reason is reason
    assert world.tasks.tasks == {} and world.client.worker_tasks() == []


async def test_a_note_at_the_cap_is_kept_whole(world):
    created = await world.service.create(USER_ID, "research", TITLE, TEXT, ORIGIN, entry=TaskEntry.CHAT,
                                         note="n" * NOTE_MAX_CHARS)
    assert world.tasks.tasks[created.task_id].note == "n" * NOTE_MAX_CHARS


async def test_the_note_is_required():
    world = await ExternalTaskWorld().seed()
    with pytest.raises(TypeError):
        await world.service.create(USER_ID, "research", TITLE, TEXT, ORIGIN, entry=TaskEntry.CHAT)  # type: ignore
    assert world.tasks.tasks == {}


async def test_no_worker_operation_hands_the_note_over(world):
    created = await world.service.create(USER_ID, "research", TITLE, TEXT, ORIGIN, entry=TaskEntry.REMINDER,
                                         note=NOTE)
    worker = next(iter(world.workers.workers.values()))
    principal = await world.service.resolve_key((await world.service.mint_key(USER_ID, worker.worker_id)).key)
    listed = await world.service.list_for_worker(principal)
    claimed = await world.service.claim(principal)
    assert claimed is not None and claimed.task_id == created.task_id and claimed.text == TEXT
    for handed in (listed, claimed):
        assert NOTE not in repr(handed) and "note" not in repr(handed)
