"""`FirestoreSessionStore._on_overflow_done`: what it logs when an error escapes the overflow hand-off task.

Since the overflow fix (owner decision O2) a batch is cut from the session only after its hand-off succeeded, so an
error that escapes the task loses nothing: the batch stays stored and the next append hands it off again. The log
says that, not that the batch may be lost (an operator reading "lost" would go looking for missing memory).
"""
import asyncio
import logging

from src.adapters.firestore_session_store import FirestoreSessionStore
from tests.support.firestore_eval import EvalDb


async def test_an_escaped_error_is_logged_as_an_error_that_keeps_the_batch(caplog):
    store = FirestoreSessionStore(db_client=EvalDb())

    async def escaped():
        raise RuntimeError("hand-off broke")

    task = asyncio.create_task(escaped())
    await asyncio.sleep(0)
    with caplog.at_level(logging.ERROR):
        store._on_overflow_done(task)

    [record] = [r for r in caplog.records if r.levelno >= logging.ERROR]
    message = record.getMessage()
    assert "hand-off broke" in message
    assert "stays in the session" in message and "next append" in message
    assert "lost" not in message
