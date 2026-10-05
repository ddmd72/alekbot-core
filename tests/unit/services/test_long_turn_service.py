"""LongTurnService: persistence side of a long chat turn (LONG_RUNNING_TURNS_RFC §5.3-5.7).

Covers retry classification (NEW/RUNNING/STALE/FINISHED), the atomic mark()
pair (user message + notice, with the registry record started), the turn
clock's fetch_since wiring, and the late-answer save.
"""
import time
from unittest.mock import AsyncMock

from src.domain.llm import Message, MessagePart
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


async def test_no_record_is_new():
    svc, _, _ = _svc(None)
    assert await svc.check_retry("slack:E1") is RetryVerdict.NEW


async def test_fresh_running_is_running_and_untouched():
    svc, reg, _ = _svc(_rec())
    assert await svc.check_retry("slack:E1") is RetryVerdict.RUNNING
    reg.finish.assert_not_awaited()


async def test_stale_running_is_marked_failed():
    svc, reg, _ = _svc(_rec(heartbeat_at=time.time() - 1000))
    assert await svc.check_retry("slack:E1") is RetryVerdict.STALE
    reg.finish.assert_awaited_with("slack:E1", LongTurnStatus.FAILED)


async def test_finished_is_finished():
    svc, _, _ = _svc(_rec(status=LongTurnStatus.DONE))
    assert await svc.check_retry("slack:E1") is RetryVerdict.FINISHED


async def test_mark_writes_the_pair_and_the_record():
    svc, reg, store = _svc()
    clock = svc.new_clock("u1:D1")
    await svc.mark(turn_id="slack:E1", user_id="u1", session_id="u1:D1", title="compare",
                   user_parts=[MessagePart(text="compare offers")], event_time=100.0,
                   notice_text="working on it", clock=clock)
    msgs = store.append_messages_batch.call_args.kwargs["messages"]
    assert [m.role for m in msgs] == ["user", "model"]
    assert msgs[0].created_at == 100.0
    assert msgs[1].parts[0].text == "working on it"
    assert msgs[1].created_at in clock.own_created_ats
    reg.start.assert_awaited()


async def test_fetch_since_returns_messages_after_the_time():
    svc, _, store = _svc()
    old = Message(role="user", parts=[MessagePart(text="a")], created_at=1.0)
    new = Message(role="user", parts=[MessagePart(text="b")], created_at=9.0)
    store.load_session = AsyncMock(return_value=type("S", (), {"history": [old, new]})())
    clock = svc.new_clock("u1:D1")
    assert await clock.fetch_since(5.0) == [new]


async def test_save_late_answer_carries_consolidation_text_on_the_note():
    svc, _, store = _svc()
    await svc.save_late_answer(session_id="u1:D1", owner_id="u1", note_text="[System: late answer …]",
                               consolidation_texts=["fact A"], history_text="sum", full_text="full")
    user, model = store.append_messages_batch.call_args.kwargs["messages"]
    assert user.parts[0].text.startswith("[System: late answer")
    assert any((p.consolidation_text or "").endswith("fact A") for p in user.parts)
    assert model.parts[0].text == "sum" and model.parts[0].full_text == "full"


async def test_finish_never_raises():
    svc, reg, _ = _svc()
    reg.finish.side_effect = RuntimeError("firestore down")
    await svc.finish("slack:E1", LongTurnStatus.DONE)
