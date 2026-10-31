"""The connect activations' failure and skip branches (slice 1a): a failed To Do setup enqueue, a drive activation
without a drive reader, and a drive id that cannot be stored. Each is logged, and the connect still succeeds.
World: tests/unit/web/connect_world.py."""
import logging

import pytest

from tests.support.firestore_eval import eval_transactions
from tests.unit.web.connect_world import ALEX, ConnectWorld


@pytest.fixture
def world(monkeypatch):
    with eval_transactions():
        yield ConnectWorld(monkeypatch)


def _messages(caplog) -> str:
    return " ".join(r.getMessage() for r in caplog.records)


async def test_a_failed_todo_setup_enqueue_is_logged_and_the_connect_still_succeeds(world, caplog):
    async def down(task_type, payload):
        raise RuntimeError("queue down")

    world.queue.enqueue_worker_task = down
    with caplog.at_level(logging.ERROR):
        resp = await world.connect("microsoft_todo")
    assert resp.headers["Location"] == "/cabinet?connected=microsoft_todo"
    assert len(await world.held("microsoft_todo")) == 1
    assert world.events == [("consented", ALEX, "tasks")]
    assert world.worker_tasks() == []
    assert "Enqueueing the To Do setup failed" in _messages(caplog)

