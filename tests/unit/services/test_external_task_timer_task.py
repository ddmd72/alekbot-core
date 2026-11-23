"""The `external_task_timer` runner (EXTERNAL_TASK_QUEUE_RFC §5.8): the bodies the service enqueued, handed to the
runner as Cloud Tasks would deliver them, move the task or change nothing. The world is the service world of
`test_external_task_service.py` (stores that apply the Firestore adapter's rules, the real queue over a recording client).
"""
import pytest

from src.domain.external_task import LEASE_S, TASK_MAX_AGE_S, TIMER_MARGIN_S, ExternalTaskStatus
from src.domain.external_task_jobs import EXTERNAL_TASK_DELIVER, EXTERNAL_TASK_TIMER
from src.services.external_task_timer_task import ExternalTaskTimerTask
from tests.unit.services.test_external_task_service import World

S = ExternalTaskStatus


@pytest.fixture
async def world() -> World:
    return await World().seed()


def runner(world: World) -> ExternalTaskTimerTask:
    return ExternalTaskTimerTask(external_tasks=world.service)


async def test_a_lapsed_lease_is_requeued(world):
    claim = await world.claimed()
    body = world.jobs(EXTERNAL_TASK_TIMER)[-1]
    world.at(LEASE_S + TIMER_MARGIN_S)
    assert await runner(world)(body, {}) == ({"changed": True}, 200)
    assert world.tasks.tasks[claim.task_id].status is S.QUEUED


async def test_a_stale_generation_changes_nothing(world):
    claim = await world.claimed()
    first = world.jobs(EXTERNAL_TASK_TIMER)[-1]
    world.at(60)
    await world.service.renew(world.alex, claim.task_id, claim.lease_id)
    world.at(LEASE_S + TIMER_MARGIN_S)                     # the first lease's instant; it was renewed
    assert await runner(world)(first, {}) == ({"changed": False}, 200)
    assert world.tasks.tasks[claim.task_id].status is S.LEASED


async def test_an_expired_deadline_expires_the_task_and_enqueues_its_delivery(world):
    created = await world.create()
    deadline = world.jobs(EXTERNAL_TASK_TIMER)[0]
    world.at(TASK_MAX_AGE_S + TIMER_MARGIN_S)
    assert await runner(world)(deadline, {}) == ({"changed": True}, 200)
    assert world.tasks.tasks[created.task_id].status is S.EXPIRED
    (delivery,) = world.jobs(EXTERNAL_TASK_DELIVER)
    assert (delivery["user_id"], delivery["account_id"], delivery["task_id"], delivery["notice"]) == (
        "usr-1", "acc-1", created.task_id, "expired")


@pytest.mark.parametrize("drop,replace", [("task_id", None), ("user_id", None), ("kind", None), ("kind", "later"),
                                          ("generation", "many")])
async def test_a_body_that_is_not_a_timer_is_a_400(world, drop, replace):
    await world.claimed()
    body = dict(world.jobs(EXTERNAL_TASK_TIMER)[-1])
    body.pop(drop)
    if replace is not None:
        body[drop] = replace
    before = dict(world.tasks.tasks)
    answer, status = await runner(world)(body, {})
    assert status == 400 and "error" in answer
    assert world.tasks.tasks == before
