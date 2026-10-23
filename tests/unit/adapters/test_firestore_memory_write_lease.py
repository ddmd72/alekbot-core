"""FirestoreMemoryWriteLease on the evaluating fake: the real adapter, real transactions (FACT_LIFECYCLE_RFC D9)."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.adapters.firestore_memory_write_lease import FirestoreMemoryWriteLease
from src.config.environment import EnvironmentConfig
from src.ports.memory_write_lease import MemoryWriteLeasePort
from tests.support.firestore_eval import EvalDb, EvalDocRef, eval_transactions

ACC = "acc-1"
T0 = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
RUN = timedelta(minutes=16)


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
def lease(db, env):
    return FirestoreMemoryWriteLease(db, env)


def _docs(db, env):
    return db.collection(env.memory_write_leases_collection).docs


class TestAcquire:
    async def test_acquire_free_absent_and_expired(self, lease, db, env):
        assert await lease.acquire(ACC, "o1", T0, T0 + RUN) is True
        doc = _docs(db, env)[ACC]
        assert (doc["lease_owner"], doc["lease_until"], doc["acquired_at"]) == ("o1", T0 + RUN, T0)

        later = T0 + RUN + timedelta(seconds=1)
        assert await lease.acquire(ACC, "o2", later, later + RUN) is True
        doc = _docs(db, env)[ACC]
        assert (doc["lease_owner"], doc["lease_until"], doc["acquired_at"]) == ("o2", later + RUN, later)

    async def test_a_lease_expiring_exactly_now_is_free(self, lease):
        assert await lease.acquire(ACC, "o1", T0, T0 + RUN)
        assert await lease.acquire(ACC, "o2", T0 + RUN, T0 + 2 * RUN) is True

    async def test_acquire_held_by_another_is_false_and_writes_nothing(self, lease, db, env):
        assert await lease.acquire(ACC, "o1", T0, T0 + RUN)
        before = dict(_docs(db, env)[ACC])
        txn_version = db.collection(env.memory_write_leases_collection).txn_versions[ACC]

        assert await lease.acquire(ACC, "o2", T0 + timedelta(seconds=5), T0 + timedelta(seconds=5) + RUN) is False

        assert _docs(db, env)[ACC] == before
        assert db.collection(env.memory_write_leases_collection).txn_versions[ACC] == txn_version

    async def test_acquire_by_the_same_owner_while_held_is_false(self, lease, db, env):
        assert await lease.acquire(ACC, "o1", T0, T0 + RUN)
        before = dict(_docs(db, env)[ACC])

        assert await lease.acquire(ACC, "o1", T0 + timedelta(seconds=5), T0 + timedelta(seconds=5) + RUN) is False

        assert _docs(db, env)[ACC] == before

    async def test_accounts_do_not_share_a_lease(self, lease):
        assert await lease.acquire("acc-a", "o1", T0, T0 + RUN)
        assert await lease.acquire("acc-b", "o2", T0, T0 + RUN)

    @pytest.mark.parametrize("account_id", ["", None])
    async def test_acquire_refuses_a_falsy_account(self, lease, db, env, account_id):
        with pytest.raises(ValueError):
            await lease.acquire(account_id, "o1", T0, T0 + RUN)
        assert _docs(db, env) == {}

    async def test_two_concurrent_acquires_one_wins(self, lease, db, env, monkeypatch):
        db.collection(env.memory_write_leases_collection).yield_on_get = True
        events = []
        real_get = EvalDocRef.get
        real_commit = db._commit_writes

        async def spy_get(self, *args, **kwargs):
            events.append("read")
            return await real_get(self, *args, **kwargs)

        def spy_commit(writes, by_transaction=False):
            events.append("commit")
            return real_commit(writes, by_transaction=by_transaction)

        monkeypatch.setattr(EvalDocRef, "get", spy_get)
        monkeypatch.setattr(db, "_commit_writes", spy_commit)

        results = await asyncio.gather(
            lease.acquire(ACC, "o1", T0, T0 + RUN),
            lease.acquire(ACC, "o2", T0, T0 + RUN),
        )

        assert events[:2] == ["read", "read"], "both transactions must have read before the first commit"
        assert events.index("commit") >= 2
        assert sorted(results) == [False, True]
        winner = "o1" if results[0] else "o2"
        assert _docs(db, env)[ACC]["lease_owner"] == winner

    async def test_no_field_named_expires_at(self, lease, db, env):
        await lease.acquire(ACC, "o1", T0, T0 + RUN)
        assert set(_docs(db, env)[ACC]) == {"lease_owner", "lease_until", "acquired_at"}
        assert "expires_at" not in _docs(db, env)[ACC]


class TestRelease:
    async def test_release_only_by_the_holder(self, lease, db, env):
        await lease.acquire(ACC, "o1", T0, T0 + RUN)

        await lease.release(ACC, "o2")
        assert _docs(db, env)[ACC]["lease_owner"] == "o1"

        await lease.release(ACC, "o1")
        assert ACC not in _docs(db, env)
        assert await lease.acquire(ACC, "o3", T0 + timedelta(seconds=1), T0 + RUN) is True

    async def test_release_after_expiry_and_takeover_leaves_the_new_holder(self, lease, db, env):
        await lease.acquire(ACC, "o1", T0, T0 + RUN)
        later = T0 + RUN + timedelta(seconds=1)
        assert await lease.acquire(ACC, "o2", later, later + RUN)

        await lease.release(ACC, "o1")

        assert _docs(db, env)[ACC]["lease_owner"] == "o2"
        assert await lease.acquire(ACC, "o3", later + timedelta(seconds=1), later + RUN) is False

    async def test_a_release_racing_a_takeover_never_deletes_the_new_holder(self, lease, db, env, monkeypatch):
        await lease.acquire(ACC, "o1", T0, T0 + RUN)
        db.collection(env.memory_write_leases_collection).yield_on_get = True
        reads = []
        real_get = EvalDocRef.get

        async def spy_get(self, *args, **kwargs):
            reads.append(self.id)
            return await real_get(self, *args, **kwargs)

        monkeypatch.setattr(EvalDocRef, "get", spy_get)
        later = T0 + RUN + timedelta(seconds=1)

        taken, _ = await asyncio.gather(lease.acquire(ACC, "o2", later, later + RUN), lease.release(ACC, "o1"))

        assert taken is True
        assert _docs(db, env)[ACC]["lease_owner"] == "o2"
        assert len(reads) >= 3, "the release must have re-read after its stale read was aborted"

    async def test_release_of_an_absent_lease_is_a_no_op(self, lease, db, env):
        await lease.release(ACC, "o1")
        assert _docs(db, env) == {}


def test_the_adapter_implements_the_port(db, env):
    assert issubclass(FirestoreMemoryWriteLease, MemoryWriteLeasePort)
    assert isinstance(FirestoreMemoryWriteLease(db, env), MemoryWriteLeasePort)
    assert MemoryWriteLeasePort.__abstractmethods__ == {"acquire", "release"}
