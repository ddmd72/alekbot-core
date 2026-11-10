"""The profile step inside FactReviewService's daily lease (fact profile RFC §4.4, §9 row 4). The review world and rig
are the run skeleton's (imported, not edited); the step is a recorder that notes the lease document it ran under."""
import logging
from datetime import timedelta
from typing import Any, List, Optional


from src.domain.agent import AgentResponse
from src.services.fact_review_service import FactReviewService
from tests.support.fact_review_world import ReviewWorld
from tests.unit.services.test_fact_review_service_run import (
    ACC, _kwargs_of, lease_doc, rig, stale_fact, state,
)


class Step:
    def __init__(self, world: ReviewWorld, raises: Optional[Exception] = None) -> None:
        self._world = world
        self.calls: List[Any] = []
        self.lease_seen: List[Optional[dict]] = []
        self.raises = raises

    async def run(self, account_id, deadline, collect_points=False):
        self.calls.append((account_id, deadline))
        self.lease_seen.append(lease_doc(self._world))
        if self.raises is not None:
            raise self.raises
        return None


def with_step(world: ReviewWorld, raises: Optional[Exception] = None):
    r = rig(world)
    step = Step(world, raises)
    r.service._profile_step = step
    return r, step


async def test_the_step_runs_after_a_completed_review_inside_the_lease_with_its_deadline():
    world = ReviewWorld()
    r, step = with_step(world)

    assert await r.run() == {"status": "completed"}

    assert len(step.calls) == 1 and step.calls[0][0] == ACC
    seen = step.lease_seen[0]
    assert seen is not None and seen["lease_until"] == step.calls[0][1]
    assert step.calls[0][1] == world.now + timedelta(seconds=600) + timedelta(minutes=1)
    assert lease_doc(world) is None, "released after the step"


async def test_the_step_runs_after_a_failed_review():
    world = ReviewWorld()
    await world.seed_state()
    r, step = with_step(world)
    r.service.script.update(
        select=lambda now: [stale_fact()],
        route=AgentResponse.failure("run-1", "consolidation_agent_u1", "provider down"),
    )

    assert await r.run() == {"status": "failed"}

    assert len(step.calls) == 1


async def test_the_step_runs_after_a_review_deferred_at_routing():
    world = ReviewWorld()
    r, step = with_step(world)
    r.service.script.update(select=lambda now: [stale_fact()], route=None)

    assert await r.run() == {"status": "deferred"}

    assert len(step.calls) == 1


async def test_the_step_does_not_run_when_the_lease_is_busy():
    world = ReviewWorld()
    r, step = with_step(world)
    await world.lease.acquire(ACC, "consolidation-run", world.now, world.now + timedelta(minutes=30))

    assert await r.run() == {"status": "deferred"}

    assert step.calls == []


async def test_the_step_does_not_run_for_the_next_mode_or_a_non_owner():
    world = ReviewWorld()
    r, step = with_step(world)

    await r.run("next")

    assert step.calls == []


async def test_a_raising_step_leaves_the_status_unchanged_and_the_lease_released(caplog):
    world = ReviewWorld()
    await world.seed_state()
    r, step = with_step(world, raises=RuntimeError("profile step exploded"))

    with caplog.at_level(logging.ERROR):
        result = await r.run()

    assert result == {"status": "completed"} and len(step.calls) == 1
    assert lease_doc(world) is None
    assert (await state(world)).last_review_at == world.now
    assert [rec for rec in caplog.records if "[FactProfile] step failed" in rec.getMessage()]


async def test_without_a_profile_step_the_run_is_as_before():
    world = ReviewWorld()
    r = rig(world)

    assert await r.run() == {"status": "completed"}
    assert lease_doc(world) is None


def test_the_constructor_takes_the_step_as_a_keyword():
    r = rig()
    service = FactReviewService(**_kwargs_of(r), run_timeout_s=600, profile_step=object())  # type: ignore[arg-type]
    assert service._profile_step is not None
