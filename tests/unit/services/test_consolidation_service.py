"""
Unit tests for ConsolidationService.

Critical invariant: the intent string sent to the coordinator must equal
"consolidate_full" — a string constant that mirrors Intent.CONSOLIDATE_FULL.
If this constant drifts, consolidation silently fails (agent not found).
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.domain.agent import AgentResponse, AgentStatus
from src.domain.consolidation import BatchStatus, ConsolidationBatch
from src.ports.consolidation_queue import ConsolidationQueue
from src.services.consolidation_service import ConsolidationService, _INTENT_CONSOLIDATE_FULL


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_USER_ID = "user-abc-123"
_BATCH_ID = "batch-001"


def _make_batch(batch_id: str = _BATCH_ID, user_id: str = _USER_ID) -> ConsolidationBatch:
    return ConsolidationBatch(
        batch_id=batch_id,
        user_id=user_id,
        session_id="sess-001",
        messages=[{"role": "user", "parts": [{"text": "hello"}]}],
    )


def _success_response() -> AgentResponse:
    return AgentResponse.success(
        result={"stage1_operations": 3, "email_batches": 1},
        agent_id="consolidation_agent",
        task_id=_BATCH_ID,
    )


def _failed_response(error: str = "LLM error") -> AgentResponse:
    return AgentResponse.failure(
        error=error,
        agent_id="consolidation_agent",
        task_id=_BATCH_ID,
    )


@pytest.fixture
def queue():
    q = AsyncMock(spec=ConsolidationQueue)
    q.reset_processing_batches.return_value = None
    q.get_pending_batches.return_value = []
    q.update_batch_status.return_value = None
    q.delete_batch.return_value = None
    q.increment_attempts.return_value = 1
    return q


@pytest.fixture
def coordinator():
    c = MagicMock()
    c.route_message = AsyncMock(return_value=_success_response())
    return c


@pytest.fixture
def agent_factory():
    f = MagicMock()
    f.ensure_agents_for_user = AsyncMock()
    return f


@pytest.fixture
def user_repo():
    r = MagicMock()
    profile = MagicMock()
    profile.account_id = "acc-abc"
    r.get_user = AsyncMock(return_value=profile)
    return r


@pytest.fixture
def service(queue, coordinator, agent_factory, user_repo):
    return ConsolidationService(
        queue=queue,
        coordinator=coordinator,
        agent_factory=agent_factory,
        user_repo=user_repo,
    )


# ---------------------------------------------------------------------------
# Intent constant guard (critical)
# ---------------------------------------------------------------------------

class TestIntentConstant:

    def test_intent_string_matches_manifest(self):
        """
        _INTENT_CONSOLIDATE_FULL must equal Intent.CONSOLIDATE_FULL from agent_manifest.
        If they drift, the coordinator cannot route the message and consolidation silently fails.
        """
        from src.infrastructure.agent_manifest import Intent
        assert _INTENT_CONSOLIDATE_FULL == Intent.CONSOLIDATE_FULL

    def test_intent_routed_in_message(self, service, queue, coordinator):
        """The AgentMessage payload carries the correct task intent string."""
        batch = _make_batch()
        queue.get_pending_batches.side_effect = [[batch], []]

        import asyncio
        asyncio.get_event_loop().run_until_complete(
            service.process_user_batches(user_id=_USER_ID)
        )

        call_args = coordinator.route_message.call_args
        message = call_args[0][0]
        assert message.payload["task"] == "consolidate_full"


# ---------------------------------------------------------------------------
# process_user_batches — no batches
# ---------------------------------------------------------------------------

class TestNoBatches:

    async def test_returns_false_when_queue_empty(self, service, queue):
        queue.get_pending_batches.return_value = []

        has_more = await service.process_user_batches(user_id=_USER_ID)

        assert has_more is False

    async def test_resets_processing_before_fetching(self, service, queue):
        queue.get_pending_batches.return_value = []
        call_order = []
        queue.reset_processing_batches.side_effect = lambda uid: call_order.append("reset")
        queue.get_pending_batches.side_effect = lambda **kw: call_order.append("fetch") or []

        await service.process_user_batches(user_id=_USER_ID)

        assert call_order[0] == "reset"


# ---------------------------------------------------------------------------
# process_user_batches — single batch success
# ---------------------------------------------------------------------------

class TestSingleBatchSuccess:

    async def test_batch_deleted_on_success(self, service, queue, coordinator):
        batch = _make_batch()
        queue.get_pending_batches.side_effect = [[batch], []]
        coordinator.route_message.return_value = _success_response()

        has_more = await service.process_user_batches(user_id=_USER_ID)

        queue.delete_batch.assert_called_once_with(_BATCH_ID)
        assert has_more is False

    async def test_status_set_to_processing_before_routing(self, service, queue, coordinator):
        batch = _make_batch()
        queue.get_pending_batches.side_effect = [[batch], []]
        call_order = []
        queue.update_batch_status.side_effect = lambda bid, status, **kw: call_order.append(("update", status))
        coordinator.route_message.side_effect = lambda m: call_order.append(("route",)) or _success_response()

        await service.process_user_batches(user_id=_USER_ID)

        assert call_order[0] == ("update", BatchStatus.PROCESSING)
        assert ("route",) in call_order

    async def test_message_addressed_to_correct_agent(self, service, queue, coordinator):
        batch = _make_batch(user_id=_USER_ID)
        queue.get_pending_batches.side_effect = [[batch], []]

        await service.process_user_batches(user_id=_USER_ID)

        message = coordinator.route_message.call_args[0][0]
        assert message.recipient == f"consolidation_agent_{_USER_ID}"
        assert message.payload["batch_id"] == _BATCH_ID
        assert message.payload["messages"] == batch.messages

    async def test_ensure_agents_called_before_processing(self, service, queue, agent_factory):
        queue.get_pending_batches.return_value = []
        call_order = []
        agent_factory.ensure_agents_for_user.side_effect = lambda uid: call_order.append("ensure")
        queue.reset_processing_batches.side_effect = lambda uid: call_order.append("reset")

        await service.process_user_batches(user_id=_USER_ID)

        assert call_order.index("ensure") < call_order.index("reset")


# ---------------------------------------------------------------------------
# process_user_batches — has_more
# ---------------------------------------------------------------------------

class TestHasMore:

    async def test_returns_true_when_batches_remain(self, service, queue, coordinator):
        batch = _make_batch()
        remaining = _make_batch(batch_id="batch-002")
        # First fetch: returns batch. After processing, remaining check returns one.
        queue.get_pending_batches.side_effect = [[batch], [remaining]]
        coordinator.route_message.return_value = _success_response()

        has_more = await service.process_user_batches(user_id=_USER_ID, max_batches=1)

        assert has_more is True

    async def test_max_batches_1_stops_after_one(self, service, queue, coordinator):
        batch1 = _make_batch(batch_id="batch-001")
        batch2 = _make_batch(batch_id="batch-002")
        queue.get_pending_batches.side_effect = [[batch1], [batch2]]
        coordinator.route_message.return_value = _success_response()

        await service.process_user_batches(user_id=_USER_ID, max_batches=1)

        assert coordinator.route_message.call_count == 1

    async def test_max_batches_none_processes_all(self, service, queue, coordinator):
        b1, b2 = _make_batch("b1"), _make_batch("b2")
        queue.get_pending_batches.side_effect = [[b1], [b2], []]
        coordinator.route_message.return_value = _success_response()

        await service.process_user_batches(user_id=_USER_ID, max_batches=None)

        assert coordinator.route_message.call_count == 2


# ---------------------------------------------------------------------------
# process_user_batches — batch failure / retry
# ---------------------------------------------------------------------------

class TestBatchFailure:

    async def test_failure_under_3_attempts_sets_retry_pending(self, service, queue, coordinator):
        batch = _make_batch()
        queue.get_pending_batches.side_effect = [[batch], []]
        queue.increment_attempts.return_value = 1
        coordinator.route_message.return_value = _failed_response()

        await service.process_user_batches(user_id=_USER_ID)

        queue.update_batch_status.assert_called_with(
            _BATCH_ID, BatchStatus.RETRY_PENDING, error="LLM error"
        )

    async def test_failure_at_3_attempts_sets_failed(self, service, queue, coordinator):
        batch = _make_batch()
        queue.get_pending_batches.side_effect = [[batch], []]
        queue.increment_attempts.return_value = 3
        coordinator.route_message.return_value = _failed_response("timeout")

        await service.process_user_batches(user_id=_USER_ID)

        queue.update_batch_status.assert_called_with(
            _BATCH_ID, BatchStatus.FAILED, error="timeout"
        )

    async def test_failure_breaks_loop(self, service, queue, coordinator):
        """After a failed batch, no further batches are processed."""
        b1, b2 = _make_batch("b1"), _make_batch("b2")
        queue.get_pending_batches.side_effect = [[b1], [b2], []]
        queue.increment_attempts.return_value = 1
        coordinator.route_message.return_value = _failed_response()

        await service.process_user_batches(user_id=_USER_ID, max_batches=None)

        assert coordinator.route_message.call_count == 1

    async def test_batch_not_deleted_on_failure(self, service, queue, coordinator):
        batch = _make_batch()
        queue.get_pending_batches.side_effect = [[batch], []]
        coordinator.route_message.return_value = _failed_response()

        await service.process_user_batches(user_id=_USER_ID)

        queue.delete_batch.assert_not_called()


# ---------------------------------------------------------------------------
# process_user_batches — exception handling
# ---------------------------------------------------------------------------

class TestExceptionHandling:

    async def test_unhandled_exception_returns_false(self, service, queue):
        queue.reset_processing_batches.side_effect = RuntimeError("Firestore down")

        result = await service.process_user_batches(user_id=_USER_ID)

        assert result is False

    async def test_no_user_repo_uses_user_id_as_account_id(self, queue, coordinator, agent_factory):
        service = ConsolidationService(
            queue=queue,
            coordinator=coordinator,
            agent_factory=agent_factory,
            user_repo=None,
        )
        queue.get_pending_batches.return_value = []

        # Should not raise — uses user_id as account_id fallback
        result = await service.process_user_batches(user_id=_USER_ID)

        assert result is False
