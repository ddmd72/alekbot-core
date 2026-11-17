"""An email indexing job whose Gmail grant was revoked, on a handler composed without the reconnect flag: the task still
ends 200 and says why nothing was flagged (composition-root refactor S5ef)."""
import logging
from unittest.mock import AsyncMock, MagicMock

from src.domain.exceptions import OAuthGrantRevokedError
from src.handlers.worker_handler import WorkerHandler
from tests.unit.handlers.test_worker_handler_gmail_reconnect import _job, _creds


async def test_a_revoked_grant_without_a_reconnect_flag_warns_and_ends_200(caplog):
    indexing = MagicMock()
    indexing.load_job_for_execution = AsyncMock(return_value=(_job(), _creds(), None))
    indexing.run_indexing_job = AsyncMock(side_effect=OAuthGrantRevokedError("invalid_grant"))
    worker = WorkerHandler(
        agent_worker_handler=MagicMock(), email_indexing_service=indexing,
        consolidation_service=None, agent_factory=AsyncMock(), user_repo=MagicMock(), task_dispatch=AsyncMock())

    with caplog.at_level(logging.WARNING):
        result = await worker.handle({"task_type": "email_indexing", "job_id": "job-0000-1111"})

    assert result == ({"status": "failed", "error": "gmail_reconnect_required"}, 200)
    assert "[Worker] Gmail grant revoked but oauth_credentials not configured" in caplog.messages
