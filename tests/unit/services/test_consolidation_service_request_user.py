"""ConsolidationService names the user and the account for the work it starts, so model calls made outside any agent
execution (the fact profile step) and the consolidation agent's own rows carry them."""
from unittest.mock import AsyncMock

from src.domain.agent import AgentStatus
from src.utils.telemetry import get_request_context
from tests.unit.services.test_consolidation_service import (  # noqa: F401  (fixtures)
    _USER_ID, _make_batch, account_repo, agent_factory, coordinator, lease, queue, service, user_repo,
)


async def test_the_profile_step_runs_under_the_request_user(service, queue):
    queue.get_pending_batches.side_effect = [[_make_batch()], []]
    seen = []

    async def profile(account_id, until):
        seen.append(get_request_context()["user_id"])

    service._profile_step = AsyncMock()
    service._profile_step.run = profile

    await service.process_user_batches(user_id=_USER_ID)

    assert seen == [_USER_ID]
    assert get_request_context()["user_id"] is None
