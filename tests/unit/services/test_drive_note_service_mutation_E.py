"""DriveNoteService: an unreadable connection list is the DRIVE_UNAVAILABLE refusal, and a failed nudge is logged
while the write stands."""
import logging

import pytest

from src.domain.drive_note import DriveNoteRefused, NoteRefusal
from tests.unit.services.test_drive_note_service import ACC, CHAT, TEXT, U, Env, ref

ITEM = "item-mut-e1"


async def test_an_unreadable_connection_read_refuses_drive_unavailable_before_any_drive_call(caplog):
    env = Env()
    env.conn_repo.failure = RuntimeError("firestore down")
    read = env.conn_repo._read

    def read_once():
        try:
            read()
        finally:
            env.conn_repo.failure = None    # only the first read fails: a later read must not raise in its place

    env.conn_repo._read = read_once

    with caplog.at_level(logging.ERROR), pytest.raises(DriveNoteRefused) as caught:
        await env.service.set(U, ACC, ref(ITEM), TEXT, provenance=CHAT)

    assert caught.value.reason is NoteRefusal.DRIVE_UNAVAILABLE
    assert env.graph_calls() == 0
    messages = [r.getMessage() for r in caplog.records]
    assert any("connection read failed" in m and "firestore down" in m for m in messages)
    assert not any("item read failed" in m for m in messages)
    assert await env.notes.count(U) == 0


async def test_a_failing_nudge_is_logged_as_a_warning_and_the_note_is_still_written(caplog):
    env = Env()
    env.queue.enqueue_worker_task.side_effect = RuntimeError("queue down")

    with caplog.at_level(logging.WARNING):
        await env.service.set(U, ACC, ref(ITEM), TEXT, provenance=CHAT)

    assert await env.notes.count(U) == 1
    assert any("nudge failed" in r.getMessage() and "queue down" in r.getMessage() for r in caplog.records)
