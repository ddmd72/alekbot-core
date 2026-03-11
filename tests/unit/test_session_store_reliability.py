"""
Unit tests for FirestoreSessionStore reliability fixes (P0 + P1).

Covers:
- P0: SessionState always contains session_id (load_session edge cases)
- P1: overflow tasks tracked in _pending_tasks (no silent discard)
- P1: _on_overflow_done logs errors instead of swallowing them
- P1: soft-fail: save_session / append_message do not raise on error
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call

from src.adapters.firestore_session_store import FirestoreSessionStore
from src.domain.session import SessionState
from src.ports.llm_port import Message, MessagePart


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(overflow_callback=None, max_history_length=10, batch_size=3):
    mock_db = MagicMock()
    return FirestoreSessionStore(
        db_client=mock_db,
        max_history_length=max_history_length,
        batch_size=batch_size,
        overflow_callback=overflow_callback,
    ), mock_db


def _make_doc(exists=True, data=None):
    doc = MagicMock()
    doc.exists = exists
    doc.to_dict = lambda: data or {}
    return doc


# ---------------------------------------------------------------------------
# P0: session_id in SessionState
# ---------------------------------------------------------------------------

class TestSessionStateSessionId:
    """load_session must always return a SessionState with the correct session_id."""

    async def test_new_session_has_session_id(self):
        store, mock_db = _make_store()
        doc_ref = AsyncMock()
        doc_ref.get = AsyncMock(return_value=_make_doc(exists=False))
        mock_db.collection.return_value.document.return_value = doc_ref

        result = await store.load_session("session-abc")

        assert result.session_id == "session-abc"

    async def test_expired_session_has_session_id(self):
        store, mock_db = _make_store()
        # last_activity very old → TTL expired
        doc_ref = AsyncMock()
        old_data = {"last_activity": 0, "history": [], "created_at": 0}
        doc_ref.get = AsyncMock(return_value=_make_doc(exists=True, data=old_data))
        doc_ref.delete = AsyncMock()
        mock_db.collection.return_value.document.return_value = doc_ref

        result = await store.load_session("session-xyz")

        assert result.session_id == "session-xyz"

    async def test_error_path_has_session_id(self):
        store, mock_db = _make_store()
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(side_effect=Exception("Firestore down"))
        mock_db.collection.return_value.document.return_value = doc_ref

        result = await store.load_session("session-err")

        assert result.session_id == "session-err"


# ---------------------------------------------------------------------------
# P1: Overflow task tracking
# ---------------------------------------------------------------------------

class TestOverflowTaskTracking:
    """Overflow tasks must be tracked; errors must be logged."""

    async def test_pending_tasks_starts_empty(self):
        store, _ = _make_store()
        assert store._pending_tasks == set()

    async def test_on_overflow_done_logs_on_exception(self):
        store, _ = _make_store()

        async def failing_coro():
            raise RuntimeError("consolidation failed")

        task = asyncio.create_task(failing_coro())
        # Let the task run to completion (with exception)
        await asyncio.sleep(0)

        with patch("src.adapters.firestore_session_store.logger") as mock_logger:
            store._on_overflow_done(task)

        mock_logger.error.assert_called_once()
        logged_msg = mock_logger.error.call_args[0][0]
        assert "batch may be lost" in logged_msg

    async def test_on_overflow_done_silent_on_success(self):
        store, _ = _make_store()

        async def ok_coro():
            return "done"

        task = asyncio.create_task(ok_coro())
        await asyncio.sleep(0)

        with patch("src.adapters.firestore_session_store.logger") as mock_logger:
            store._on_overflow_done(task)

        mock_logger.error.assert_not_called()

    async def test_on_overflow_done_silent_on_cancel(self):
        store, _ = _make_store()

        async def long_coro():
            await asyncio.sleep(100)

        task = asyncio.create_task(long_coro())
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        with patch("src.adapters.firestore_session_store.logger") as mock_logger:
            store._on_overflow_done(task)

        mock_logger.error.assert_not_called()

    async def test_overflow_triggers_tracked_task(self):
        """append_messages_batch triggers a tracked task, not fire-and-forget."""
        callback_event = asyncio.Event()

        async def mock_callback(user_id, session_id, messages):
            callback_event.set()

        store, mock_db = _make_store(
            overflow_callback=mock_callback,
            max_history_length=3,
            batch_size=2,
        )

        # 3 existing messages → adding 2 → total 5 > 3 → overflow
        existing = [{"role": "user", "parts": [{"text": f"m{i}"}], "created_at": 0} for i in range(3)]
        mock_doc = _make_doc(exists=True, data={
            "owner_id": "u1",
            "history": existing,
            "created_at": 1000,
            "last_activity": 9999999999,
        })

        # doc_ref.get must be AsyncMock — the transactional fn awaits it
        mock_doc_ref = MagicMock()
        mock_doc_ref.get = AsyncMock(return_value=mock_doc)
        mock_db.collection.return_value.document.return_value = mock_doc_ref

        # transaction.set is called synchronously inside the transactional fn
        mock_transaction = MagicMock()
        mock_transaction.set = MagicMock()
        mock_db.transaction.return_value = mock_transaction

        new_msgs = [Message(role="user", parts=[MessagePart(text="n")])]

        with patch("google.cloud.firestore.async_transactional", lambda fn: fn):
            await store.append_messages_batch("sess1", new_msgs, owner_id="u1")

        # Wait for the tracked task to complete
        await asyncio.wait_for(callback_event.wait(), timeout=1.0)
        # Yield to the event loop so done-callbacks (discard from _pending_tasks) can run
        await asyncio.sleep(0)

        assert callback_event.is_set()
        # After callback completes, _pending_tasks should be empty (discard callback ran)
        assert len(store._pending_tasks) == 0


# ---------------------------------------------------------------------------
# P1: Soft-fail — save_session and append_message do not raise
# ---------------------------------------------------------------------------

class TestSoftFail:
    """Errors in session persistence must not propagate to callers."""

    async def test_save_session_does_not_raise_on_firestore_error(self):
        store, mock_db = _make_store()
        doc_ref = MagicMock()
        doc_ref.set = AsyncMock(side_effect=Exception("Firestore unavailable"))
        mock_db.collection.return_value.document.return_value = doc_ref

        state = SessionState(session_id="s1", history=[])
        # Must NOT raise
        await store.save_session("s1", state)

    async def test_append_message_does_not_raise_on_firestore_error(self):
        store, mock_db = _make_store()
        mock_db.transaction.side_effect = Exception("transaction failed")

        msg = Message(role="user", parts=[MessagePart(text="hi")])
        # Must NOT raise
        await store.append_message("s1", msg)

    async def test_save_session_logs_error_on_failure(self):
        store, mock_db = _make_store()
        doc_ref = MagicMock()
        doc_ref.set = AsyncMock(side_effect=Exception("boom"))
        mock_db.collection.return_value.document.return_value = doc_ref

        state = SessionState(session_id="s1", history=[])

        with patch("src.adapters.firestore_session_store.logger") as mock_logger:
            await store.save_session("s1", state)

        mock_logger.error.assert_called_once()
