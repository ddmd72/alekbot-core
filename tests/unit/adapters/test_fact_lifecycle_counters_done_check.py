"""Facts S3 · T14 done-check: the counters adapter's claim document and its skip of a row that cannot be read."""
from datetime import date, datetime, timedelta, timezone

import pytest

from src.adapters.firestore_fact_lifecycle_counters import FirestoreFactLifecycleCounters
from src.config.environment import EnvironmentConfig
from tests.support.firestore_eval import EvalDb

DAY = date(2026, 10, 23)


def snapshot(**over):
    values = {"run_at": datetime(2026, 10, 23, 7, tzinfo=timezone.utc), "due": 0, "newly_due": 0, "no_decision": 0, "staled": 0,
              "would_stale": 0, "deferred": 0, "stale_conflicts": 0, "breaker": "", "backlog": 0, "oldest_stale_days": None}
    values.update(over)
    return values


@pytest.fixture
def env():
    return EnvironmentConfig()


@pytest.fixture
def db():
    return EvalDb()


@pytest.fixture
def counters(db, env):
    return FirestoreFactLifecycleCounters(db, env)


async def test_the_claim_document_names_its_day_and_when_it_was_claimed(counters, db, env):
    before = datetime.now(timezone.utc)

    await counters.claim_line(DAY)

    doc = db.collection(env.fact_lifecycle_lines_collection).docs["2026-10-23"]
    assert set(doc) == {"day", "claimed_at"} and doc["day"] == "2026-10-23"
    assert before <= doc["claimed_at"] <= datetime.now(timezone.utc) + timedelta(seconds=1)
    assert doc["claimed_at"].tzinfo is not None


async def test_a_row_of_the_day_that_cannot_be_read_is_skipped_and_the_others_still_return(counters, db, env):
    await counters.record_sweep("acc-b", DAY, snapshot(due=2))
    await counters.record_sweep("acc-a", DAY, snapshot(due=1))
    db.collection(env.fact_lifecycle_daily_collection).docs["acc-z:2026-10-23"] = {
        "account_id": "acc-z", "day": "2026-10-23", "due": float("nan")}                   # int(nan) raises ValueError

    rows = await counters.for_day(DAY)

    assert [(r.account_id, r.due) for r in rows] == [("acc-a", 1), ("acc-b", 2)]
