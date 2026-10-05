"""LongTurnService session writes share the turn pair's transient retry (fix round 1)."""
from unittest.mock import AsyncMock, patch

import pytest

from src.domain.llm import MessagePart
from src.services.long_turn_service import LongTurnService


def _svc():
    registry = AsyncMock()
    store = AsyncMock()
    return LongTurnService(registry=registry, session_store=store), registry, store


async def test_mark_retries_a_transient_session_write_once():
    svc, registry, store = _svc()
    store.append_messages_batch.side_effect = [RuntimeError("RST_STREAM"), None]
    with patch("asyncio.sleep", new_callable=AsyncMock):
        await svc.mark(turn_id="slack:E1", user_id="u1", session_id="u1:D1", title="t",
                       user_parts=[MessagePart(text="q")], event_time=1.0,
                       notice_text="working", clock=svc.new_clock("u1:D1"))
    assert store.append_messages_batch.await_count == 2
    registry.start.assert_awaited_once()   # the registry write is not repeated


async def test_late_answer_retries_a_transient_session_write_once():
    svc, _, store = _svc()
    store.append_messages_batch.side_effect = [RuntimeError("503 UNAVAILABLE"), None]
    with patch("asyncio.sleep", new_callable=AsyncMock):
        await svc.save_late_answer(session_id="u1:D1", owner_id="u1", note_text="[System: late]",
                                   consolidation_texts=[], history_text="h", full_text="f")
    assert store.append_messages_batch.await_count == 2


async def test_non_transient_session_write_error_raises_without_retry():
    svc, _, store = _svc()
    store.append_messages_batch.side_effect = ValueError("schema mismatch")
    with pytest.raises(ValueError):
        await svc.save_late_answer(session_id="u1:D1", owner_id="u1", note_text="n",
                                   consolidation_texts=[], history_text="h", full_text="f")
    assert store.append_messages_batch.await_count == 1
