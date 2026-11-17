"""BillingDailySummaryTask: the daily ops summary, every branch of `__call__` (composition-root refactor S5ef).

The account repository is the real Firestore one over the H1 fake; the clock is pinned, the webhook records.
"""
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.domain.billing import AccountUsageStats, BillingAccount
from src.domain.user import UserProfile
from src.services import billing_daily_summary_task as module
from src.services.billing_daily_summary_task import BillingDailySummaryTask
from tests.support import clock_instants as ci
from tests.support.firestore_eval import EvalDb

UTC = timezone.utc
RUN_AT = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
YESTERDAY_NOON = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


class Webhook:
    def __init__(self) -> None:
        self.posts: List[str] = []

    async def post(self, text: str) -> None:
        self.posts.append(text)


class Users:
    def __init__(self, names: Dict[str, str]) -> None:
        self._names = names

    async def get_user(self, user_id: str) -> Optional[UserProfile]:
        return UserProfile(user_id=user_id, display_name=self._names[user_id]) if user_id in self._names else None


def _rig(names: Dict[str, str]):
    repo = FirestoreAccountRepository(EvalDb(), "accounts")
    webhook = Webhook()
    return BillingDailySummaryTask(billing_webhook=webhook, account_repo=repo, user_repo=Users(names)), repo, webhook


async def _active(repo: FirestoreAccountRepository, account_id: str, owner: Optional[str], tokens: int = 1000) -> None:
    usage = AccountUsageStats(daily_tokens=tokens, daily_cost=0.05, daily_reset_at=YESTERDAY_NOON,
                              monthly_tokens=5000, monthly_cost=0.25, monthly_reset_at=YESTERDAY_NOON,
                              total_tokens=9000, total_cost=0.75)
    await repo.create_account(BillingAccount(account_id=account_id, iam_policy={owner: "owner"} if owner else {},
                                             usage=usage))


async def test_an_active_account_is_posted_under_its_owners_name_with_yesterday_month_and_total(monkeypatch, caplog):
    task, repo, webhook = _rig({"user-1": "Clara"})
    await _active(repo, "acct-1", "user-1")
    ci.pin_datetime(monkeypatch, module, RUN_AT)

    with caplog.at_level(logging.INFO):
        assert await task({}, {}) == ({"reported": 1}, 200)

    assert webhook.posts == [
        "📊 *Billing Summary — 2026-11-01*\n\n\n"
        "*Clara*\n"
        "  Yesterday: 1,000 tok / $0.0500\n"
        "  Month:     5,000 tok / $0.2500\n"
        "  Total:     9,000 tok / $0.7500"]
    assert "[Worker] billing_daily_summary: reported 1 accounts" in caplog.messages


async def test_an_account_without_an_owner_profile_is_named_by_the_start_of_its_id(monkeypatch):
    task, repo, webhook = _rig({})
    await _active(repo, "acct-12345678-xyz", None)
    await _active(repo, "acct-87654321-xyz", "user-gone")
    ci.pin_datetime(monkeypatch, module, RUN_AT)

    assert await task({}, {}) == ({"reported": 2}, 200)
    assert "*acct-123*" in webhook.posts[0] and "*acct-876*" in webhook.posts[0]


async def test_accounts_idle_yesterday_are_left_out_and_nothing_is_posted_when_all_are(monkeypatch, caplog):
    task, repo, webhook = _rig({"user-1": "Clara"})
    quiet = AccountUsageStats(daily_reset_at=datetime(2026, 10, 30, 12, 0, tzinfo=UTC), daily_tokens=40, daily_cost=0.01)
    await repo.create_account(BillingAccount(account_id="acct-1", iam_policy={"user-1": "owner"}, usage=quiet))
    ci.pin_datetime(monkeypatch, module, RUN_AT)

    with caplog.at_level(logging.INFO):
        assert await task({}, {}) == ({"reported": 0}, 200)

    assert webhook.posts == []
    assert "[Worker] billing_daily_summary: no accounts with activity yesterday" in caplog.messages


async def test_with_no_accounts_nothing_is_posted():
    task, _, webhook = _rig({})

    assert await task({}, {}) == ({"reported": 0}, 200)
    assert webhook.posts == []
