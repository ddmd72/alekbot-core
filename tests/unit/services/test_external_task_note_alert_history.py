"""The completion alert of a task with a note (owner's decision 2026-11-24): the alert turn gives the note back to Smart as
its own instructions, and the note never enters saved history, neither the user-role line (which the consolidator reads
as the owner's words) nor the full alert history keeps.

The real stack of `test_external_task_alert_delivery.py`: the `external_task_deliver` runner over the real service, the
real WorkspaceService on the GCS fake and the real UserNotificationService; a bot stub records the routed turn and
answers. Alex Doe is `usr-1`; the worker is `research`; the task carries the shared example note.
"""
import pytest

from src.domain.external_task_alert import AlertKind, alert_summary, render_alert
from tests.support.external_task_world import NOTE
from tests.unit.services.test_external_task_alert_delivery import ANSWER, SUFFIX, World, path_of


@pytest.fixture
async def world() -> World:
    return await World().seed()


async def _delivered(world: World, outcome: str):
    task_id = await (world.done() if outcome == "done" else world.failed())
    assert await world.deliver() == ({"status": "delivered"}, 200)
    return world.stored(task_id)


@pytest.mark.parametrize("outcome,kind", [("done", AlertKind.DONE), ("failed", AlertKind.FAILED_FILE)])
async def test_the_alert_turn_carries_the_note_and_saved_history_never_does(world, outcome, kind):
    task = await _delivered(world, outcome)
    path = path_of(task.task_id)
    assert task.note == NOTE

    turn = world.turn()
    assert turn == f"[System: {render_alert(kind, task, path)}{SUFFIX}]"
    assert f"you could act on it now.\n\n{NOTE}\n\n---\n" in turn

    [(session_id, history)] = world.sessions.appended
    assert session_id == "usr-1:C-garden"
    assert history == [("user", alert_summary(kind, task, path), render_alert(kind, task, path, note=False)),
                       ("model", ANSWER, ANSWER)]
    assert NOTE not in str(history) and "Shelbyville community garden" not in str(history)
