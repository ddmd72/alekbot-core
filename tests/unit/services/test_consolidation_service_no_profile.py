"""A consolidation batch whose user has no profile fails; nothing is written under the user id (owner, 2026-10-23,
FACT_LIFECYCLE_RFC §9 "S3 as built").

The agents of a user stay cached for about an hour, so `ensure_agents_for_user` can succeed after the profile is gone.
Before this rule the service then leased and wrote under `user_id`. The real lease and queue adapters run on the
evaluating fake (H1); the coordinator is the scripted LLM boundary and records the request context it was routed in.
"""
import logging
from types import SimpleNamespace
from typing import Any, List

import pytest

from src.adapters.firestore_consolidation_queue import FirestoreConsolidationQueue
from src.adapters.firestore_memory_write_lease import FirestoreMemoryWriteLease
from src.config.environment import EnvironmentConfig
from src.domain.agent import AgentResponse
from src.domain.consolidation import BatchStatus, ConsolidationBatch
from src.domain.consolidation_run import ConsolidationRun
from src.domain.request_context import get_current_account_id
from src.services.consolidation_service import ConsolidationService
from tests.support.firestore_eval import EvalDb

USER = "user-abcdef123"


class Coordinator:
    def __init__(self) -> None:
        self.accounts_seen: List[Any] = []

    async def route_message(self, message):
        self.accounts_seen.append(get_current_account_id())
        return AgentResponse.success(result={}, agent_id="c", task_id=message.payload["batch_id"])


class CachedAgents:
    """The agents are still cached: ensuring them succeeds without reading the profile."""

    async def ensure_agents_for_user(self, user_id):
        return None


class NoProfiles:
    async def get_user(self, user_id):
        return None


@pytest.fixture
def db():
    return EvalDb()


@pytest.fixture
def env():
    return EnvironmentConfig()


@pytest.fixture
def queue(db):
    return FirestoreConsolidationQueue(db, SimpleNamespace(consolidation_queue_collection="development_consolidation_queue"))


@pytest.fixture
def coordinator():
    return Coordinator()


@pytest.fixture
def service(queue, db, env, coordinator):
    return ConsolidationService(
        queue=queue, coordinator=coordinator, agent_factory=CachedAgents(), lease=FirestoreMemoryWriteLease(db, env),
        run_timeout_s=900.0, user_repo=NoProfiles(),
    )


async def _put(queue, batch_id, created_at, attempts=0):
    await queue.enqueue_batch(ConsolidationBatch(
        batch_id=batch_id, user_id=USER, session_id="s", messages=[{"role": "user"}],
        status=BatchStatus.PENDING, created_at=created_at, attempts=attempts,
    ))


async def _status(queue, batch_id):
    return (await queue.collection.document(batch_id).get()).to_dict()


async def test_a_missing_profile_fails_the_oldest_batch_and_routes_nothing(service, queue, db, env, coordinator, caplog):
    await _put(queue, "old", created_at=1.0, attempts=2)
    await _put(queue, "new", created_at=2.0)

    with caplog.at_level(logging.ERROR):
        has_more = await service.process_user_batches(USER)

    old, new = await _status(queue, "old"), await _status(queue, "new")
    assert old["status"] == BatchStatus.FAILED.value and old["last_error"] == "user has no profile"
    assert new["status"] == BatchStatus.PENDING.value
    assert coordinator.accounts_seen == []                      # no agent ran, so nothing was written
    assert db.collection(env.memory_write_leases_collection).docs == {}    # no lease, not even under the user id
    assert has_more is False
    failed = [r.getMessage() for r in caplog.records if "CONSOLIDATION_FAILED" in r.getMessage()]
    assert len(failed) == 1 and "batch_id=old" in failed[0] and "attempts=2" in failed[0]


async def test_consolidate_now_reports_the_run_as_failed(service, queue, coordinator):
    await _put(queue, "b1", created_at=1.0)

    run = await service.consolidate_now(USER)

    assert run == ConsolidationRun(failed=1)
    assert (await _status(queue, "b1"))["status"] == BatchStatus.FAILED.value
    assert coordinator.accounts_seen == []


async def test_a_missing_profile_with_no_batch_does_nothing(service, queue, coordinator):
    assert await service.process_user_batches(USER) is False
    assert coordinator.accounts_seen == []


def test_the_user_repository_is_required(queue, db, env, coordinator):
    with pytest.raises(TypeError, match="user_repo"):
        ConsolidationService(
            queue=queue, coordinator=coordinator, agent_factory=CachedAgents(),
            lease=FirestoreMemoryWriteLease(db, env), run_timeout_s=900.0,
        )
