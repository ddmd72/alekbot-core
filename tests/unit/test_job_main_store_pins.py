"""The deep-research job's billing, pinned through its caller (P11 · G1-4): `job_main._record_billing` over the real
`FirestoreAccountRepository` on the evaluating Firestore fake (H1, index refusal ON), with the Slack incoming webhook
served by the shared HTTP boundary fake.

`QuotaService.record_usage` promises that the day's budget crossing is announced once; the job writes the repository
directly, so a crossing caused by the job is never announced. The job's cost is fixed (`calculate_cost` patched) so a
price-table change cannot move the precondition.
"""
from datetime import timedelta
from pathlib import Path

import pytest

import job_main
import src.adapters.firestore_account_repo as account_module
from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_quota_service import FirestoreQuotaService
from src.adapters.slack.webhook_adapter import SlackWebhookAdapter
from src.domain.billing import AccountUsageStats, BillingAccount
from tests.support import http_boundary_fake
from tests.support.clock_instants import UTC_MIDNIGHT, pin_datetime
from tests.support.firestore_eval import EvalDb
from tests.support.redelivery_world import require

INDEXES_FILE = Path(__file__).resolve().parents[2] / "config" / "firestore.indexes.json"
ACCOUNTS = "eval_domain_accounts_v2"
T0 = UTC_MIDNIGHT.utc + timedelta(hours=14)
HOOK = "https://hooks.example.com/billing"
MODEL = "gemini-flash-latest"
JOB_RESULT = {"total_tokens": 300000, "prompt_tokens": 100000, "completion_tokens": 200000}


@pytest.fixture
def world(monkeypatch):
    pin_datetime(monkeypatch, account_module, T0)
    monkeypatch.setenv("BILLING_SLACK_WEBHOOK_URL", HOOK)
    http = http_boundary_fake.install(monkeypatch)
    http.slack_webhook(HOOK)
    db = EvalDb(indexes=INDEXES_FILE, groups={ACCOUNTS: "development_domain_accounts_v2"})
    repo = FirestoreAccountRepository(db, ACCOUNTS)
    monkeypatch.setattr(job_main, "_build_account_repo", lambda: repo)
    monkeypatch.setattr(job_main, "calculate_cost", lambda **_: 1.65)
    return db, repo, http


async def seed(repo: FirestoreAccountRepository, daily_cost: float) -> None:
    usage = AccountUsageStats(daily_cost=daily_cost, daily_reset_at=T0, monthly_cost=daily_cost, monthly_reset_at=T0)
    await repo.create_account(BillingAccount(account_id="a1", usage=usage, created_at=T0, updated_at=T0))


def daily_cost(db: EvalDb) -> float:
    return db.collection(ACCOUNTS).docs["a1"]["usage"]["daily_cost"]


def budget_posts(http) -> list:
    return [p for p in http.posts if "Daily budget exceeded" in p["text"]]


async def test_a_crossing_recorded_through_the_quota_service_is_announced(world):
    db, repo, http = world
    await seed(repo, daily_cost=4.0)
    await FirestoreQuotaService(repo, SlackWebhookAdapter(HOOK)).record_usage("a1", MODEL, 300000, 1.65)
    require(4.0 < 5.0 <= daily_cost(db), "the call crossed the limit")
    assert len(budget_posts(http)) == 1


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="P11-G1-4: a deep-research job (job_main) increments usage directly and its daily-limit "
                          "crossing is never announced")
async def test_a_deep_research_job_that_crosses_the_limit_is_announced(world):
    """Flips when job_main records through `FirestoreQuotaService(repo, SlackWebhookAdapter(BILLING_SLACK_WEBHOOK_URL))`
    (or posts when its increment crossed)."""
    db, repo, http = world
    await seed(repo, daily_cost=4.0)
    await job_main._record_billing("a1", MODEL, JOB_RESULT)
    require(4.0 < 5.0 <= daily_cost(db), "the job crossed the limit")
    await FirestoreQuotaService(repo, SlackWebhookAdapter(HOOK)).record_usage("a1", MODEL, 1000, 0.01)
    assert len(budget_posts(http)) == 1
