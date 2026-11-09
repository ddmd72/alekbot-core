"""The profile step inside ConsolidationService's lease (fact profile RFC §4.4, §9 row 4). The rig is the lease test's:
real lease and queue adapters on one evaluating fake, a scripted coordinator, a settable clock."""
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, List, Optional

import pytest

from src.adapters.firestore_consolidation_queue import FirestoreConsolidationQueue
from tests.unit.services.test_consolidation_service_lease import (  # noqa: F401  (fixtures reused by import)
    ACC, LEASE_S, T0, USER, _TickingQueue, _doc, _hold, _lease_doc, _put, clock, coordinator, db, env,
    lease, make_service, queue, tasks, _queue_time_follows_the_clock,
)


class Step:
    """A profile step that records its call and the lease document it ran under."""

    def __init__(self, db, env, raises: Optional[Exception] = None) -> None:
        self._leases = db.collection(env.memory_write_leases_collection)
        self.calls: List[Any] = []
        self.leases_seen: List[Optional[dict]] = []
        self.raises = raises

    async def run(self, account_id, deadline):
        self.calls.append((account_id, deadline))
        doc = self._leases.docs.get(ACC)
        self.leases_seen.append(dict(doc) if doc else None)
        if self.raises is not None:
            raise self.raises
        return None


@pytest.fixture
def step(db, env):
    return Step(db, env)


async def test_the_step_runs_after_a_consolidated_batch_with_the_lease_deadline_while_the_lease_is_held(
    make_service, step, queue, db, env,
):
    await _put(queue, "b1")

    await make_service(profile_step=step).process_user_batches(USER, max_batches=1)

    until = T0 + timedelta(seconds=LEASE_S)
    assert step.calls == [(ACC, until)]
    assert step.leases_seen[0] is not None and step.leases_seen[0]["lease_until"] == until
    assert _lease_doc(db, env) is None, "released after the step"


async def test_the_step_runs_after_a_failed_batch_too(make_service, step, queue, coordinator):
    await _put(queue, "b1")
    coordinator.failing = {"b1"}

    await make_service(profile_step=step).process_user_batches(USER, max_batches=1)

    assert [account for account, _ in step.calls] == [ACC]


async def test_the_step_runs_once_per_leased_batch(make_service, step, queue):
    await _put(queue, "b1", 1.0)
    await _put(queue, "b2", 2.0)

    await make_service(profile_step=step).process_user_batches(USER)

    assert len(step.calls) == 2


async def test_the_step_is_in_the_request_context_of_the_batch(make_service, queue, db, env):
    from src.domain.request_context import get_current_account_id, get_current_user_id

    seen = []

    class ContextStep(Step):
        async def run(self, account_id, deadline):
            seen.append((get_current_user_id(), get_current_account_id()))

    await _put(queue, "b1")

    await make_service(profile_step=ContextStep(db, env)).process_user_batches(USER, max_batches=1)

    assert seen == [(USER, ACC)]


async def test_the_step_does_not_run_when_nothing_is_pending(make_service, step):
    await make_service(profile_step=step).process_user_batches(USER)

    assert step.calls == []


async def test_the_step_does_not_run_when_the_lease_margin_is_gone(make_service, step, db, clock):
    q_env = SimpleNamespace(consolidation_queue_collection="development_consolidation_queue")
    ticking = _TickingQueue(db, q_env, clock, 841, 0)
    await _put(ticking, "b1")

    await make_service(queue=ticking, profile_step=step).process_user_batches(USER, max_batches=1)

    assert step.calls == []


async def test_the_step_does_not_run_when_the_batch_step_errors(make_service, step, db):
    class Broken(FirestoreConsolidationQueue):
        async def reset_recoverable_batches(self, user_id, zombie_after_s):
            raise ConnectionError("queue down")

    q_env = SimpleNamespace(consolidation_queue_collection="development_consolidation_queue")
    broken = Broken(db, q_env)
    await _put(broken, "b1")

    await make_service(queue=broken, profile_step=step).process_user_batches(USER, max_batches=1)

    assert step.calls == []


async def test_the_step_does_not_run_when_the_lease_is_busy(make_service, step, queue, lease):
    await _hold(lease)
    await _put(queue, "b1")

    await make_service(profile_step=step).process_user_batches(USER, max_batches=1)

    assert step.calls == []


async def test_a_raising_step_leaves_the_batch_outcome_and_the_release_intact(
    make_service, queue, coordinator, db, env, caplog,
):
    import logging
    step = Step(db, env, raises=RuntimeError("profile step exploded"))
    await _put(queue, "b1", 1.0)
    await _put(queue, "b2", 2.0)

    with caplog.at_level(logging.ERROR):
        has_more = await make_service(profile_step=step).process_user_batches(USER)

    assert coordinator.routed == ["b1", "b2"], "the run went on to the next batch"
    assert len(step.calls) == 2 and has_more is False
    assert _lease_doc(db, env) is None
    assert [r for r in caplog.records if "[FactProfile] step failed" in r.getMessage()]
    assert await _doc(queue, "b1") is None, "the batch itself was consolidated"


async def test_without_a_profile_step_nothing_changes(make_service, queue, coordinator, db, env):
    await _put(queue, "b1")

    await make_service().process_user_batches(USER, max_batches=1)

    assert coordinator.routed == ["b1"] and _lease_doc(db, env) is None
