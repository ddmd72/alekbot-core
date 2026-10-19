import pytest
from unittest.mock import MagicMock, patch

from src.adapters.firestore_session_store import FirestoreSessionStore
from src.ports.llm_port import Message, MessagePart


def _store():
    mock_db = MagicMock()
    mock_db.transaction.side_effect = RuntimeError("firestore down")
    return FirestoreSessionStore(mock_db, max_history_length=5, batch_size=3)


_MESSAGES = [Message(role="user", parts=[MessagePart(text="hi")])]


@pytest.mark.asyncio
async def test_default_swallows_a_failed_append():
    with patch("google.cloud.firestore.async_transactional", lambda x: x):
        await _store().append_messages_batch("sess1", _MESSAGES, owner_id="u1")


@pytest.mark.asyncio
async def test_raise_on_error_reraises_a_failed_append():
    with patch("google.cloud.firestore.async_transactional", lambda x: x):
        with pytest.raises(RuntimeError, match="firestore down"):
            await _store().append_messages_batch("sess1", _MESSAGES, owner_id="u1", raise_on_error=True)
