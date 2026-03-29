"""
Unit tests for consolidation_handler module.

Coverage:
  process_user_batches_on_overflow
    - delegates to ConsolidationService.process_user_batches
    - returns the service result (True/False)

  _execute_consolidation_background
    - SUCCESS response → logs completion without raising
    - FAILED response  → logs error without raising
    - Exception raised → logs exception without propagating
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.domain.agent import AgentResponse, AgentStatus
from src.handlers.consolidation_handler import (
    _execute_consolidation_background,
    process_user_batches_on_overflow,
)


_USER_ID = "user-abc"


# ---------------------------------------------------------------------------
# process_user_batches_on_overflow
# ---------------------------------------------------------------------------

class TestProcessUserBatchesOnOverflow:

    async def test_delegates_to_consolidation_service_returns_true(self):
        coordinator = MagicMock()
        agent_factory = MagicMock()
        queue = MagicMock()

        with patch(
            "src.handlers.consolidation_handler.ConsolidationService"
        ) as MockService:
            instance = MockService.return_value
            instance.process_user_batches = AsyncMock(return_value=True)

            result = await process_user_batches_on_overflow(
                user_id=_USER_ID,
                coordinator=coordinator,
                agent_factory=agent_factory,
                queue=queue,
            )

        assert result is True
        instance.process_user_batches.assert_called_once_with(
            user_id=_USER_ID, max_batches=None
        )

    async def test_delegates_to_consolidation_service_returns_false(self):
        coordinator = MagicMock()
        agent_factory = MagicMock()
        queue = MagicMock()

        with patch(
            "src.handlers.consolidation_handler.ConsolidationService"
        ) as MockService:
            instance = MockService.return_value
            instance.process_user_batches = AsyncMock(return_value=False)

            result = await process_user_batches_on_overflow(
                user_id=_USER_ID,
                coordinator=coordinator,
                agent_factory=agent_factory,
                queue=queue,
            )

        assert result is False

    async def test_passes_max_batches_to_service(self):
        with patch(
            "src.handlers.consolidation_handler.ConsolidationService"
        ) as MockService:
            instance = MockService.return_value
            instance.process_user_batches = AsyncMock(return_value=False)

            await process_user_batches_on_overflow(
                user_id=_USER_ID,
                coordinator=MagicMock(),
                agent_factory=MagicMock(),
                queue=MagicMock(),
                max_batches=5,
            )

        instance.process_user_batches.assert_called_once_with(
            user_id=_USER_ID, max_batches=5
        )


# ---------------------------------------------------------------------------
# _execute_consolidation_background
# ---------------------------------------------------------------------------

def _make_bg_deps(*, route_result=None, route_raises=None):
    coordinator = MagicMock()
    agent_factory = MagicMock()
    agent_factory.ensure_agents_for_user = AsyncMock()

    user_repo = MagicMock()
    profile = MagicMock()
    profile.account_id = "acc-abc"
    user_repo.get_user = AsyncMock(return_value=profile)

    if route_raises:
        coordinator.route_message = AsyncMock(side_effect=route_raises)
    else:
        coordinator.route_message = AsyncMock(return_value=route_result)

    return coordinator, agent_factory, user_repo


class TestExecuteConsolidationBackground:

    async def test_success_response_completes_without_error(self):
        success = AgentResponse.success(
            task_id="t1",
            agent_id="consolidation_agent_user-abc",
            result={"stage1_operations": 3, "email_batches": 0},
        )
        coordinator, agent_factory, user_repo = _make_bg_deps(route_result=success)

        # Should not raise
        await _execute_consolidation_background(
            coordinator=coordinator,
            agent_factory=agent_factory,
            user_id=_USER_ID,
            user_repo=user_repo,
        )

        coordinator.route_message.assert_called_once()

    async def test_failed_response_logs_error_without_raising(self):
        failed = AgentResponse(
            task_id="t1",
            agent_id="consolidation_agent_user-abc",
            status=AgentStatus.FAILED,
            result=None,
            confidence=0.0,
            error="LLM error",
        )
        coordinator, agent_factory, user_repo = _make_bg_deps(route_result=failed)

        # Should not raise
        await _execute_consolidation_background(
            coordinator=coordinator,
            agent_factory=agent_factory,
            user_id=_USER_ID,
            user_repo=user_repo,
        )

        coordinator.route_message.assert_called_once()

    async def test_exception_during_routing_does_not_propagate(self):
        coordinator, agent_factory, user_repo = _make_bg_deps(
            route_raises=RuntimeError("Firestore unavailable")
        )

        # Should not raise — exceptions are caught and logged
        await _execute_consolidation_background(
            coordinator=coordinator,
            agent_factory=agent_factory,
            user_id=_USER_ID,
            user_repo=user_repo,
        )

    async def test_no_user_repo_uses_user_id_as_account_id(self):
        success = AgentResponse.success(
            task_id="t1", agent_id="a1", result={}
        )
        coordinator, agent_factory, _ = _make_bg_deps(route_result=success)

        # user_repo=None → falls back to user_id as account_id
        await _execute_consolidation_background(
            coordinator=coordinator,
            agent_factory=agent_factory,
            user_id=_USER_ID,
            user_repo=None,
        )

        coordinator.route_message.assert_called_once()
