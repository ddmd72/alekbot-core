"""ExternalWorkerStore kit, wake-up methods (`set_wake`, `bump_wake_generation`; EXTERNAL_TASK_QUEUE_RFC §5.1, §5.8):
the port's docstring checked against `FirestoreExternalWorkerStore` on the evaluating Firestore fake and against the
test fake, with the backends of `test_external_worker_store_kit.py`.
"""
from datetime import timedelta

import pytest

from tests.unit.adapters.test_external_worker_store_kit import T0, WORKERS, Fake, Firestore, worker


@pytest.fixture(params=[Firestore, Fake], ids=["firestore", "fake"])
def backend(request, kit_db):
    return request.param(kit_db)


async def test_set_wake_replaces_the_target_and_both_none_clears_it(backend):
    await backend.store.create_worker(worker(), 10)
    assert await backend.store.set_wake("usr-1", "wrk-1", wake_host="hooks.example.com", wake_secret="c2VhbGVk",
                                        now=T0 + timedelta(minutes=1)) is True
    stored = await backend.store.get_worker("usr-1", "wrk-1")
    assert (stored.wake_host, stored.wake_secret, stored.has_wake, stored.wake_generation, stored.updated_at) == (
        "hooks.example.com", "c2VhbGVk", True, 0, T0 + timedelta(minutes=1))
    assert await backend.store.set_wake("usr-1", "wrk-1", wake_host=None, wake_secret=None,
                                        now=T0 + timedelta(minutes=2)) is True
    cleared = await backend.store.get_worker("usr-1", "wrk-1")
    assert (cleared.wake_host, cleared.wake_secret, cleared.has_wake) == (None, None, False)


async def test_set_wake_writes_nothing_for_a_foreign_absent_or_revoked_worker(backend):
    await backend.store.create_worker(worker(), 10)
    before = backend.written()
    assert await backend.store.set_wake("usr-2", "wrk-1", wake_host="h.example.com", wake_secret="x", now=T0) is False
    assert await backend.store.set_wake("usr-1", "wrk-9", wake_host="h.example.com", wake_secret="x", now=T0) is False
    assert await backend.store.set_wake("usr-1", "", wake_host="h.example.com", wake_secret="x", now=T0) is False
    assert backend.written() == before
    await backend.store.revoke_worker("usr-1", "wrk-1", T0)
    revoked = backend.written()
    assert await backend.store.set_wake("usr-1", "wrk-1", wake_host="h.example.com", wake_secret="x", now=T0) is False
    assert backend.written() == revoked


async def test_the_generation_moves_on_only_for_an_active_worker_with_a_target(backend):
    await backend.store.create_worker(worker(), 10)
    assert await backend.store.bump_wake_generation("usr-1", "wrk-1") is None
    await backend.store.set_wake("usr-1", "wrk-1", wake_host="hooks.example.com", wake_secret="c2VhbGVk", now=T0)
    assert await backend.store.bump_wake_generation("usr-1", "wrk-1") == 1
    assert await backend.store.bump_wake_generation("usr-1", "wrk-1") == 2
    assert (await backend.store.get_worker("usr-1", "wrk-1")).wake_generation == 2
    assert await backend.store.bump_wake_generation("usr-2", "wrk-1") is None
    assert await backend.store.bump_wake_generation("usr-1", "wrk-9") is None
    assert await backend.store.bump_wake_generation("usr-1", "") is None
    await backend.store.revoke_worker("usr-1", "wrk-1", T0)
    assert await backend.store.bump_wake_generation("usr-1", "wrk-1") is None
    assert (await backend.store.get_worker("usr-1", "wrk-1")).wake_generation == 2


async def test_the_stored_document_keeps_only_the_host_in_clear(kit_db):
    fs = Firestore(kit_db)
    await fs.store.create_worker(worker(), 10)
    await fs.store.set_wake("usr-1", "wrk-1", wake_host="hooks.example.com", wake_secret="c2VhbGVk", now=T0)
    doc = fs.db.collection(WORKERS).docs["usr-1:wrk-1"]
    assert {k: doc[k] for k in ("wake_host", "wake_secret", "has_wake", "wake_generation")} == {
        "wake_host": "hooks.example.com", "wake_secret": "c2VhbGVk", "has_wake": True, "wake_generation": 0}
