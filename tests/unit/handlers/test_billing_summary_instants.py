"""The 09:00 billing summary at the first instant of a month (P16-D): "Yesterday" and "Month" must be the same month.

The real ``FirestoreAccountRepository`` on ``EvalDb`` keeps the accounts, so the rotation is the one production runs.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.adapters import firestore_account_repo as repo_module
from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.domain.billing import AccountUsageStats, BillingAccount
from src.domain.user import UserProfile
from src.handlers import worker_handler as worker_module
from src.handlers.worker_handler import WorkerHandler
from tests.support import clock_instants as ci
from tests.support.firestore_eval import EvalDb, eval_transactions

UTC = timezone.utc
SUMMARY_AT = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)  # the Scheduler's morning run, after the UTC month turned
OCT_31_NOON = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


class _RecordingWebhook:
    def __init__(self) -> None:
        self.posts: list[str] = []

    async def post(self, text: str) -> None:
        self.posts.append(text)


class _Unused:
    """A collaborator the billing summary never touches: any attribute access fails the test."""

    def __getattr__(self, name: str):
        raise AssertionError(f"billing summary touched an unused collaborator: {name}")


class _Users:
    async def get_user(self, user_id: str):
        return UserProfile(user_id=user_id, display_name="Clara")


def _world() -> tuple[WorkerHandler, FirestoreAccountRepository, _RecordingWebhook]:
    repo = FirestoreAccountRepository(EvalDb(), "accounts")
    webhook = _RecordingWebhook()
    worker = WorkerHandler(
        agent_worker_handler=_Unused(), email_indexing_service=_Unused(), notification_service=_Unused(),
        consolidation_service=None, coordinator=_Unused(), agent_factory=_Unused(), indexed_email_repo=None,
        user_repo=_Users(), account_repo=repo, billing_webhook=webhook,
    )
    return worker, repo, webhook


async def _seed_october(repo: FirestoreAccountRepository, account_id: str = "a1", *, daily_tokens: int = 1000) -> None:
    usage = AccountUsageStats(
        daily_tokens=daily_tokens, daily_cost=0.05 if daily_tokens else 0.0, daily_reset_at=OCT_31_NOON,
        monthly_tokens=5000, monthly_cost=0.25, monthly_reset_at=OCT_31_NOON,
        total_tokens=9000, total_cost=0.75,
    )
    await repo.create_account(BillingAccount(account_id=account_id, iam_policy={"u1": "owner"}, usage=usage))


def _month_line(text: str) -> str:
    return next(line for line in text.splitlines() if line.strip().startswith("Month:"))


async def test_month_line_shows_october_when_nothing_ran_after_utc_midnight(monkeypatch):
    worker, repo, webhook = _world()
    await _seed_october(repo)
    ci.pin_datetime(monkeypatch, worker_module, SUMMARY_AT)

    body, status = await worker._handle_billing_daily_summary()

    assert (status, body["reported"]) == (200, 1)
    assert "2026-10-31" in webhook.posts[0]
    assert "Yesterday: 1,000 tok" in webhook.posts[0]
    assert "5,000 tok / $0.2500" in _month_line(webhook.posts[0])


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="P16-D7: the monthly counter rotates at 00:00Z on the first increment of November, so the "
                          "'Month' line shows November's first tokens beside October's 'Yesterday'; owner decision")
async def test_month_line_shows_october_after_one_increment_past_utc_midnight(monkeypatch):
    worker, repo, webhook = _world()
    await _seed_october(repo)
    with eval_transactions():
        ci.pin_datetime(monkeypatch, repo_module, ci.OCT_NEXT)
        await repo.increment_account_usage("a1", tokens=7, cost=0.01)
    ci.pin_datetime(monkeypatch, worker_module, SUMMARY_AT)

    body, status = await worker._handle_billing_daily_summary()

    assert (status, body["reported"]) == (200, 1)
    assert "Yesterday: 1,000 tok" in webhook.posts[0]  # the rotation did save October 31
    assert "5,000 tok / $0.2500" in _month_line(webhook.posts[0])


async def test_account_idle_yesterday_is_not_in_the_post(monkeypatch):
    worker, repo, webhook = _world()
    await _seed_october(repo, "a1", daily_tokens=1000)
    quiet = AccountUsageStats(daily_reset_at=datetime(2026, 10, 30, 12, 0, tzinfo=UTC), daily_tokens=40,
                              daily_cost=0.01, monthly_tokens=40, monthly_cost=0.01)
    await repo.create_account(BillingAccount(account_id="a2", iam_policy={"u2": "owner"}, usage=quiet))
    ci.pin_datetime(monkeypatch, worker_module, SUMMARY_AT)

    body, status = await worker._handle_billing_daily_summary()

    assert (status, body["reported"]) == (200, 1)
    assert "1,000" in webhook.posts[0]
    assert "40 tok" not in webhook.posts[0]


async def test_nothing_is_posted_when_every_account_was_idle_yesterday(monkeypatch):
    worker, repo, webhook = _world()
    quiet = AccountUsageStats(daily_reset_at=datetime(2026, 10, 29, 12, 0, tzinfo=UTC), daily_tokens=40, daily_cost=0.01)
    await repo.create_account(BillingAccount(account_id="a2", iam_policy={"u2": "owner"}, usage=quiet))
    ci.pin_datetime(monkeypatch, worker_module, SUMMARY_AT)

    body, status = await worker._handle_billing_daily_summary()

    assert (status, body) == (200, {"reported": 0})
    assert webhook.posts == []
