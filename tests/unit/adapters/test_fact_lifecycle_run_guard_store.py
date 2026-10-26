"""The janitor's run guard in FirestoreFactLifecycleCounters on the evaluating fake: the stored document and the transactions (S4-D10)."""
import logging
from datetime import datetime, timedelta, timezone

import pytest

from src.adapters.firestore_fact_lifecycle_counters import FirestoreFactLifecycleCounters
from src.config.environment import EnvironmentConfig
from tests.support.firestore_eval import EvalDb, eval_transactions

T0 = datetime(2026, 10, 23, 7, 30, tzinfo=timezone.utc)
SPAN = timedelta(minutes=5)


@pytest.fixture(autouse=True)
def _evaluating_transactions():
    with eval_transactions():
        yield


@pytest.fixture
def env():
    return EnvironmentConfig()


@pytest.fixture
def db():
    return EvalDb()


@pytest.fixture
def counters(db, env):
    return FirestoreFactLifecycleCounters(db, env)


def _collection(db, env):
    return db.collection(env.fact_lifecycle_run_collection)


def test_the_collection_carries_the_env_prefix(env):
    assert env.fact_lifecycle_run_collection == f"{env.firestore_collection_prefix}fact_lifecycle_run"


async def test_acquire_writes_one_document_with_holder_and_expiry(counters, db, env):
    assert await counters.acquire_run("o1", T0, T0 + SPAN) is True

    assert _collection(db, env).docs == {"sweep": {"run_owner": "o1", "run_until": T0 + SPAN, "acquired_at": T0}}


async def test_acquire_while_held_is_false_and_writes_nothing_even_for_the_same_owner(counters, db, env):
    await counters.acquire_run("o1", T0, T0 + SPAN)
    before = dict(_collection(db, env).docs["sweep"])
    version = _collection(db, env).txn_versions["sweep"]
    later = T0 + timedelta(seconds=5)

    assert await counters.acquire_run("o2", later, later + SPAN) is False
    assert await counters.acquire_run("o1", later, later + SPAN) is False

    assert _collection(db, env).docs["sweep"] == before
    assert _collection(db, env).txn_versions["sweep"] == version


async def test_an_expired_guard_is_taken_over_and_one_expiring_now_is_free(counters, db, env):
    await counters.acquire_run("o1", T0, T0 + SPAN)

    assert await counters.acquire_run("o2", T0 + SPAN, T0 + 2 * SPAN) is True
    assert _collection(db, env).docs["sweep"]["run_owner"] == "o2"

    late = T0 + 2 * SPAN + timedelta(seconds=1)
    assert await counters.acquire_run("o3", late, late + SPAN) is True
    assert _collection(db, env).docs["sweep"] == {"run_owner": "o3", "run_until": late + SPAN, "acquired_at": late}


async def test_release_deletes_the_guard_of_its_owner(counters, db, env):
    await counters.acquire_run("o1", T0, T0 + SPAN)

    await counters.release_run("o1")

    assert _collection(db, env).docs == {}
    assert await counters.acquire_run("o2", T0, T0 + SPAN) is True


async def test_release_of_an_absent_guard_is_silent(counters, caplog):
    with caplog.at_level(logging.WARNING):
        await counters.release_run("o1")

    assert caplog.records == []


async def test_a_late_release_by_a_taken_over_owner_leaves_the_new_holders_guard(counters, db, env, caplog):
    await counters.acquire_run("o1", T0, T0 + SPAN)
    await counters.acquire_run("o2", T0 + SPAN, T0 + 2 * SPAN)

    with caplog.at_level(logging.WARNING):
        await counters.release_run("o1")

    assert _collection(db, env).docs["sweep"]["run_owner"] == "o2"
    assert "another owner" in caplog.text


async def test_two_runs_racing_for_the_guard_get_one_winner(counters):
    import asyncio

    results = await asyncio.gather(*(counters.acquire_run(f"o{i}", T0, T0 + SPAN) for i in range(4)))

    assert sorted(results) == [False, False, False, True]


@pytest.mark.parametrize("owner", ["", None])
async def test_acquire_refuses_a_missing_owner(counters, db, env, owner):
    with pytest.raises(ValueError):
        await counters.acquire_run(owner, T0, T0 + SPAN)

    assert _collection(db, env).docs == {}


async def test_acquire_refuses_naive_datetimes_and_an_empty_span(counters):
    with pytest.raises(ValueError):
        await counters.acquire_run("o1", T0.replace(tzinfo=None), T0 + SPAN)
    with pytest.raises(ValueError):
        await counters.acquire_run("o1", T0, (T0 + SPAN).replace(tzinfo=None))
    with pytest.raises(ValueError):
        await counters.acquire_run("o1", T0, T0)
