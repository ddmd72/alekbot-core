"""LongTurnService — final review C1, M1, M4."""
import time
from unittest.mock import AsyncMock

from src.domain.llm import MessagePart
from src.domain.long_turn import LongTurnRecord, LongTurnStatus, RetryVerdict
from src.services.long_turn_service import LongTurnService


def _svc(record=None):
    registry = AsyncMock()
    registry.get = AsyncMock(return_value=record)
    store = AsyncMock()
    return LongTurnService(registry=registry, session_store=store), registry, store


def _rec(**kw):
    base = dict(turn_id="slack:E1", user_id="u1", session_id="u1:D1", title="t",
                started_at=0.0, heartbeat_at=time.time())
    base.update(kw)
    return LongTurnRecord(**base)


async def _mark(svc, **kw):
    await svc.mark(turn_id="slack:E1", user_id="u1", session_id="u1:D1", title="t",
                   user_parts=[MessagePart(text="q")], event_time=1.0,
                   notice_text="working", clock=svc.new_clock("u1:D1"), **kw)


# --- C1: a fresh RUNNING record not live in this process is a dead attempt ----------


async def test_fresh_running_not_live_here_is_stale_and_marked_failed():
    svc, reg, _ = _svc(_rec())
    assert await svc.check_retry("slack:E1", live_in_process=False) is RetryVerdict.STALE
    reg.finish.assert_awaited_with("slack:E1", LongTurnStatus.FAILED)


async def test_fresh_running_live_here_is_running_and_untouched():
    svc, reg, _ = _svc(_rec())
    assert await svc.check_retry("slack:E1", live_in_process=True) is RetryVerdict.RUNNING
    reg.finish.assert_not_awaited()


async def test_not_live_here_leaves_new_and_finished_unchanged():
    svc, reg, _ = _svc(None)
    assert await svc.check_retry("slack:E1", live_in_process=False) is RetryVerdict.NEW
    svc, reg, _ = _svc(_rec(status=LongTurnStatus.DONE))
    assert await svc.check_retry("slack:E1", live_in_process=False) is RetryVerdict.FINISHED
    reg.finish.assert_not_awaited()


# --- M1: registry first, session second, each on its own ---------------------------


async def test_mark_writes_the_registry_before_the_session():
    svc, reg, store = _svc()
    order = []
    reg.start.side_effect = lambda record: order.append("registry")
    store.append_messages_batch.side_effect = lambda **kw: order.append("session")
    await _mark(svc)
    assert order == ["registry", "session"]


async def test_mark_session_failure_still_leaves_the_registry_record():
    svc, reg, store = _svc()
    store.append_messages_batch.side_effect = ValueError("schema mismatch")   # not retried
    await _mark(svc)   # does not raise
    reg.start.assert_awaited_once()


async def test_mark_registry_failure_still_writes_the_session_pair():
    svc, reg, store = _svc()
    reg.start.side_effect = RuntimeError("firestore down")
    await _mark(svc)   # does not raise
    store.append_messages_batch.assert_awaited_once()


# --- M4: started_at is the turn's start, not the mark time --------------------------


async def test_mark_records_the_given_turn_start():
    svc, reg, _ = _svc()
    await _mark(svc, started_at=1234.5)
    record = reg.start.call_args.args[0]
    assert record.started_at == 1234.5
    assert record.heartbeat_at > 1234.5


async def test_mark_without_a_turn_start_falls_back_to_now():
    svc, reg, _ = _svc()
    before = time.time()
    await _mark(svc)
    assert reg.start.call_args.args[0].started_at >= before
