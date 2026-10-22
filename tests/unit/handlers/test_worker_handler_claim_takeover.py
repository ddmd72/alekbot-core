"""WorkerHandler + FirestoreTaskClaimStore: a crashed attempt's claim is taken over, not obeyed."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from src.adapters.firestore_task_claim_store import FirestoreTaskClaimStore
from src.domain.cloud_task_delivery import CloudTaskDelivery
from src.handlers.worker_handler import WorkerHandler
from tests.support.firestore_eval import EvalDb

COL = "claims"
_AGENT = {"task_type": "agent_execution", "context": {"user_id": "u1"}}


def _delivery(name="task-1"):
    return CloudTaskDelivery(task_name=name, retry_count=0, execution_count=0)


def _handler(db, holder="proc-1"):
    store = FirestoreTaskClaimStore(db_client=db, collection_name=COL, ttl_seconds=3600)
    store.holder = holder
    return WorkerHandler(
        agent_worker_handler=MagicMock(),
        email_indexing_service=MagicMock(),
        notification_service=AsyncMock(),
        consolidation_service=None,
        coordinator=MagicMock(),
        agent_factory=AsyncMock(),
        indexed_email_repo=None,
        user_repo=MagicMock(),
        task_claims=store,
    )


def _doc(db, key="task-1"):
    return db.collection(COL).docs.get(key)


async def test_retry_after_a_crash_of_another_process_runs_the_task():
    db = EvalDb()
    dead = FirestoreTaskClaimStore(db_client=db, collection_name=COL)
    dead.holder = "proc-dead"
    await dead.try_claim("task-1", "dead-attempt", False)  # the OOM-killed attempt left this running
    assert _doc(db)["state"] == "running"

    live = _handler(db, "proc-new")
    live._agent_worker.handle_task = AsyncMock(return_value={"status": "success"})
    assert await live.handle(_AGENT, delivery=_delivery()) == ({"status": "success"}, 200)
    live._agent_worker.handle_task.assert_awaited_once()


async def test_concurrent_duplicate_in_this_process_is_skipped():
    db = EvalDb()
    h = _handler(db)
    gate = asyncio.Event()
    h._agent_worker.handle_task = AsyncMock(side_effect=_gated(gate))
    first = asyncio.create_task(h.handle(_AGENT, delivery=_delivery()))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    dup = await asyncio.wait_for(h.handle(_AGENT, delivery=_delivery()), timeout=2)
    assert dup == ({"status": "duplicate"}, 200)
    gate.set()
    assert (await first)[1] == 200
    assert h._agent_worker.handle_task.await_count == 1


def _gated(gate):
    async def run(*a, **k):
        await gate.wait()
        return {"status": "success"}
    return run


async def test_success_marks_done_and_a_redelivery_is_duplicate():
    db = EvalDb()
    h = _handler(db)
    h._agent_worker.handle_task = AsyncMock(return_value={"status": "success"})
    await h.handle(_AGENT, delivery=_delivery())
    assert _doc(db)["state"] == "done"
    assert await h.handle(_AGENT, delivery=_delivery()) == ({"status": "duplicate"}, 200)
    assert h._agent_worker.handle_task.await_count == 1


async def test_raise_releases_and_the_retry_runs():
    db = EvalDb()
    h = _handler(db)
    h._agent_worker.handle_task = AsyncMock(side_effect=[RuntimeError("boom"), {"status": "success"}])
    try:
        await h.handle(_AGENT, delivery=_delivery())
    except RuntimeError:
        pass
    assert _doc(db) is None
    assert (await h.handle(_AGENT, delivery=_delivery()))[1] == 200


async def test_cancelled_attempt_releases_its_claim():
    db = EvalDb()
    h = _handler(db)
    h._agent_worker.handle_task = AsyncMock(side_effect=_gated(asyncio.Event()))
    t = asyncio.create_task(h.handle(_AGENT, delivery=_delivery()))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        pass
    assert _doc(db) is None


async def test_chat_retry_after_a_crash_reaches_the_handler():
    db = EvalDb()
    dead = FirestoreTaskClaimStore(db_client=db, collection_name=COL)
    dead.holder = "proc-dead"
    await dead.try_claim("task-1", "dead-attempt", False)  # telegram_update crashed mid-turn
    h = _handler(db, "proc-new")
    tg = AsyncMock(return_value=({"ok": True}, 200))
    h.attach_telegram_worker(tg)
    slack = AsyncMock(return_value=({"ok": True}, 200))
    h.attach_slack_worker(slack)
    assert await h.handle({"task_type": "telegram_update"}, delivery=_delivery()) == ({"ok": True}, 200)
    tg.assert_awaited_once()
    # and the queued Slack event path, after another crash
    await dead.try_claim("task-2", "dead-2", False)
    out = await h.handle({"event": {"event": {"type": "message"}}, "session_id": "u1:C1"}, delivery=_delivery("task-2"))
    assert out == ({"ok": True}, 200)
    slack.assert_awaited_once()


class _GatedStore(FirestoreTaskClaimStore):
    """Pauses inside try_claim / mark_done so a second delivery can interleave."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.claim_gate = None
        self.done_gate = None
        self.claim_calls = 0

    async def try_claim(self, key, attempt, running_here):
        self.claim_calls += 1
        if self.claim_gate is not None:
            await self.claim_gate.wait()
        return await super().try_claim(key, attempt, running_here)

    async def mark_done(self, key, attempt):
        if self.done_gate is not None:
            await self.done_gate.wait()
        await super().mark_done(key, attempt)


def _gated_handler(db):
    h = _handler(db)
    store = _GatedStore(db_client=db, collection_name=COL)
    h._task_claims = store
    h._agent_worker.handle_task = AsyncMock(return_value={"status": "success"})
    return h, store


async def _spin():
    for _ in range(5):
        await asyncio.sleep(0)


async def test_duplicate_during_the_claim_call_is_refused_without_a_store_call():
    db = EvalDb()
    h, store = _gated_handler(db)
    store.claim_gate = asyncio.Event()
    first = asyncio.create_task(h.handle(_AGENT, delivery=_delivery()))
    await _spin()  # first is inside try_claim, key not yet claimed in the store
    dup = await asyncio.wait_for(h.handle(_AGENT, delivery=_delivery()), timeout=2)
    assert dup == ({"status": "duplicate"}, 200)
    assert store.claim_calls == 1
    store.claim_gate.set()
    assert (await first)[1] == 200
    assert h._agent_worker.handle_task.await_count == 1


async def test_redelivery_during_mark_done_does_not_take_over_and_rerun():
    db = EvalDb()
    h, store = _gated_handler(db)
    store.done_gate = asyncio.Event()
    first = asyncio.create_task(h.handle(_AGENT, delivery=_delivery()))
    await _spin()  # body finished, mark_done pending, claim still running
    assert _doc(db)["state"] == "running"
    dup = await asyncio.wait_for(h.handle(_AGENT, delivery=_delivery()), timeout=2)
    assert dup == ({"status": "duplicate"}, 200)
    store.done_gate.set()
    await first
    assert _doc(db)["state"] == "done"
    assert h._agent_worker.handle_task.await_count == 1


async def test_fail_open_duplicate_does_not_remove_the_running_key_of_the_live_attempt():
    db = EvalDb()
    h, store = _gated_handler(db)
    store.done_gate = asyncio.Event()
    first = asyncio.create_task(h.handle(_AGENT, delivery=_delivery()))
    await _spin()
    await h.handle(_AGENT, delivery=_delivery())  # refused duplicate
    assert "task-1" in h._running_claims
    store.done_gate.set()
    await first
    assert "task-1" not in h._running_claims
