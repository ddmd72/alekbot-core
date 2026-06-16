"""Unit tests for FirestoreQuotaService.record_usage.

Post-#4 contract: the write is AWAITED (durable while the request holds CPU), not
detached via asyncio.create_task — a detached task is starved by Cloud Run CPU
throttling and lost on instance recycle. Still best-effort: a repo error is logged,
never raised into the caller's response path.
"""

from unittest.mock import AsyncMock

from src.adapters.firestore_quota_service import FirestoreQuotaService


class TestRecordUsage:

    async def test_awaits_increment_with_account_tokens_cost(self):
        repo = AsyncMock()
        svc = FirestoreQuotaService(repo)

        await svc.record_usage("acct-1", "claude-sonnet-4-6", tokens=150, cost=0.0012)

        repo.increment_account_usage.assert_awaited_once_with("acct-1", 150, 0.0012)

    async def test_swallows_repo_error(self):
        repo = AsyncMock()
        repo.increment_account_usage.side_effect = RuntimeError("firestore down")
        svc = FirestoreQuotaService(repo)

        # Must not raise — billing is best-effort and cannot break the response path.
        await svc.record_usage("acct-1", "gpt-5.4", tokens=10, cost=0.0)
        repo.increment_account_usage.assert_awaited_once()
