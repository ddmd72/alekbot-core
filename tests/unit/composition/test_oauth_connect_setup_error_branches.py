"""The connect activations' failure and skip branches (slice 1a): a failed To Do setup enqueue, a drive activation
without a drive reader, and a drive id that cannot be stored. Each is logged, and the connect still succeeds.
World: tests/unit/web/connect_world.py."""
import logging

import pytest

from src.composition.oauth_connect_setup import build_connect_activations
from src.domain.integration import Integration
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


async def test_a_drive_activation_without_a_drive_reader_keeps_no_drive_id_and_still_clears_the_state(world):
    await world.connect("microsoft_onedrive")
    (stored,) = await world.held("microsoft_onedrive")
    world.events.clear()
    activations = build_connect_activations(
        reconnect=world.reconnect, todo_connector=world.registry.get("microsoft_todo"),
        task_queue=None, drive_activation=None)
    await activations[Integration.DRIVE](stored)
    (after,) = await world.held("microsoft_onedrive")
    assert after.drive_id == stored.drive_id == "drive-b1"
    assert world.events == [("consented", ALEX, "drive")]


async def test_a_drive_id_that_cannot_be_stored_is_logged_and_the_connect_still_succeeds(world, caplog):
    async def refused(credential):
        raise RuntimeError("store down")

    world.store.update = refused
    with caplog.at_level(logging.ERROR):
        resp = await world.connect("microsoft_onedrive")
    assert resp.headers["Location"] == "/cabinet?connected=microsoft_onedrive"
    (held,) = await world.held("microsoft_onedrive")
    assert held.drive_id == ""
    assert world.events == [("consented", ALEX, "drive")]
    assert "Storing the drive id failed" in _messages(caplog)
