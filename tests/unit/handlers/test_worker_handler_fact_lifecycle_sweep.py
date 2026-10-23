"""Facts S3 · T10: `/worker` task_type `fact_lifecycle_sweep` runs the janitor and answers its totals (plan §3.14, S3-D10).

The service is the real one on the evaluating fact store; what is asserted is the answer, the store and the claim.
"""
import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.cloud_task_delivery import CloudTaskDelivery
from src.handlers.worker_handler import WorkerHandler
from src.ports.task_claim_store import TaskClaimStore
from tests.support.fact_lifecycle_world import NOW, JanitorWorld

EMPTY_RUN = {"accounts": 0, "due": 0, "staled": 0, "would_stale": 0, "deferred": 0, "conflicts": 0,
             "no_decision": 0, "breaker_tripped": 0, "dry_run": True}
PAYLOAD = {"task_type": "fact_lifecycle_sweep"}


class _Claims(TaskClaimStore):
    """One process, no expiry: what the handler leaves behind is `held` (running or done) after an attempt."""

    def __init__(self) -> None:
        self.held: dict = {}

    async def try_claim(self, key, attempt):
        held = self.held.get(key)
        if held is not None and held["state"] == "done":
            return False
        self.held[key] = {"attempt": attempt, "state": "running"}
        return True

    async def mark_done(self, key, attempt):
        if key in self.held and self.held[key]["attempt"] == attempt:
            self.held[key]["state"] = "done"

    async def release(self, key, attempt):
        if key in self.held and self.held[key]["attempt"] == attempt:
            del self.held[key]


def _worker(sweep=None, claims=None) -> WorkerHandler:
    return WorkerHandler(
        agent_worker_handler=MagicMock(),
        email_indexing_service=MagicMock(),
        notification_service=AsyncMock(),
        consolidation_service=None,
        coordinator=MagicMock(),
        agent_factory=MagicMock(),
        indexed_email_repo=None,
        user_repo=MagicMock(),
        fact_lifecycle_sweep=sweep,
        task_claims=claims,
    )


async def test_an_empty_store_answers_the_zero_totals_with_deferred_and_dry_run():
    world = JanitorWorld(dry_run=True)

    assert await _worker(world.service).handle(PAYLOAD) == (EMPTY_RUN, 200)


async def test_a_log_only_run_counts_what_it_would_stale_and_writes_nothing_to_the_facts():
    world = JanitorWorld(dry_run=True)
    fact_id = await world.seed_due("due")
    before = dict(world.docs[fact_id])

    body, status = await _worker(world.service).handle(PAYLOAD)

    assert status == 200
    assert (body["would_stale"], body["staled"], body["dry_run"]) == (1, 0, True)
    assert world.docs[fact_id] == before


async def test_a_write_run_stales_the_due_fact_through_the_handler():
    world = JanitorWorld(dry_run=False)
    fact_id = await world.seed_due("due")

    body, status = await _worker(world.service).handle(PAYLOAD)

    assert status == 200 and (body["staled"], body["dry_run"]) == (1, False)
    assert world.docs[fact_id]["state"] == "stale" and world.docs[fact_id]["stale_since"] == NOW


async def test_a_failing_sweep_is_logged_with_its_type_and_answered_500(caplog):
    caplog.set_level(logging.ERROR)
    world = JanitorWorld(dry_run=False)
    fact_id = await world.seed_due("secret text of a fact")
    world.repo.fail_on = {fact_id}

    body, status = await _worker(world.service).handle(PAYLOAD)

    assert status == 500
    assert body == {"error": "sweep_failed", "error_type": "RuntimeError"}
    assert any("[Worker] fact_lifecycle_sweep failed (RuntimeError)" in r.getMessage() for r in caplog.records)
    assert not any("secret text" in r.getMessage() for r in caplog.records)
    assert world.docs[fact_id]["state"] == "current"


async def test_a_failed_delivery_releases_its_claim_so_the_retry_runs_and_a_good_run_keeps_it():
    claims = _Claims()
    world = JanitorWorld(dry_run=False)
    fact_id = await world.seed_due("due")
    world.repo.fail_on = {fact_id}
    worker = _worker(world.service, claims)
    delivery = CloudTaskDelivery(task_name="sweep-1", retry_count=0, execution_count=0)

    _, status = await worker.handle(PAYLOAD, delivery)
    assert status == 500 and claims.held == {}

    world.repo.fail_on = set()
    body, status = await worker.handle(PAYLOAD, delivery)
    assert status == 200 and body["staled"] == 1
    assert claims.held["sweep-1"]["state"] == "done"


async def test_a_cancellation_is_not_turned_into_an_answer():
    sweep = AsyncMock()
    sweep.sweep.side_effect = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await _worker(sweep).handle(PAYLOAD)


async def test_a_handler_without_the_service_answers_501():
    assert await _worker(None).handle(PAYLOAD) == ({"error": "fact lifecycle sweep not configured"}, 501)
