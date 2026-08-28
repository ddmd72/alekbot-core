import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from src.adapters.firestore_session_store import FirestoreSessionStore
from src.ports.llm_port import Message, MessagePart

@pytest.mark.asyncio
async def test_session_store_overflow_logic():
    """
    Verify that FirestoreSessionStore extracts messages and triggers callback on overflow.
    """
    mock_db = MagicMock()
    mock_doc = MagicMock()
    
    # 1. Setup mock session with 5 messages
    existing_history = [
        {"role": "user", "parts": [{"text": f"msg {i}"}]} for i in range(5)
    ]
    mock_doc.get = AsyncMock(return_value=MagicMock(
        exists=True,
        to_dict=lambda: {"owner_id": "user1", "history": existing_history, "created_at": 1000}
    ))
    mock_doc.set = AsyncMock()
    mock_db.collection.return_value.document.return_value = mock_doc
    
    # Mock transaction
    mock_transaction = AsyncMock()
    mock_transaction.get = AsyncMock(return_value=mock_doc.get.return_value)
    mock_transaction.set = MagicMock()
    mock_db.transaction.return_value = mock_transaction
    
    # Callback tracker
    callback_called = asyncio.Event()
    captured_data = {}
    
    async def mock_callback(user_id, session_id, messages):
        captured_data['user_id'] = user_id
        captured_data['session_id'] = session_id
        captured_data['messages'] = messages
        callback_called.set()

    # 2. Initialize store with threshold=5, batch=3
    store = FirestoreSessionStore(
        mock_db, 
        max_history_length=5, 
        batch_size=3,
        overflow_callback=mock_callback
    )
    
    # 3. Append 2 new messages (Total will be 5 + 2 = 7 > 5)
    new_messages = [
        Message(role="user", parts=[MessagePart(text="new 1")]),
        Message(role="model", parts=[MessagePart(text="new 2")])
    ]
    
    # We need to mock the transactional call because it's a decorator in real code
    # Actually, FirestoreSessionStore uses @firestore.async_transactional
    # In tests, we might need to patch it or ensure the mock works.
    
    with patch("google.cloud.firestore.async_transactional", lambda x: x):
        await store.append_messages_batch("sess1", new_messages)
    
    # 4. Verify overflow logic
    # Expected: 7 messages total. Threshold 5. 
    # Batch 3 extracted. 7 - 3 = 4 remaining in hot storage.
    
    # Wait for background task
    try:
        await asyncio.wait_for(callback_called.wait(), timeout=1.0)
    except asyncio.TimeoutError:
        pytest.fail("Callback was not triggered within timeout")
        
    assert callback_called.is_set()
    assert captured_data['user_id'] == "user1"
    assert len(captured_data['messages']) == 3
    assert captured_data['messages'][0].parts[0].text == "msg 0"
    
    # Verify what was saved to Firestore
    args, kwargs = mock_transaction.set.call_args
    saved_data = args[1]  # transaction.set(doc_ref, data, merge=True)
    assert len(saved_data["history"]) == 4
    assert saved_data["history"][0]["parts"][0]["text"] == "msg 3"
    assert saved_data["history"][-1]["parts"][0]["text"] == "new 2"


def test_consolidation_serializer_includes_consolidation_text_parts():
    """
    Reproduce the real session history shape observed in Firestore:

        role: "user"
        parts:
          0: {"text": "У меня бекенд отвалился..."}
          1: {"consolidation_text": "Save Toyota Corolla left mirror damage fact"}

    The consolidation serializer (overflow_callback / $consolidate path) uses:
        [{"text": p.full_text or p.consolidation_text or p.text}
         for p in msg.parts if p.full_text or p.consolidation_text or p.text]

    Both parts must appear in the output — consolidation_text is treated as the
    text of that part.
    """
    raw_firestore_message = {
        "role": "user",
        "parts": [
            {"text": "У меня бекенд отвалился и не сохранило сохрани пожалуйста еще раз"},
            {"consolidation_text": "Save Toyota Corolla left mirror damage fact"},
        ],
        "created_at": 1774481644.1386607,
    }

    mock_db = MagicMock()
    store = FirestoreSessionStore(mock_db, max_history_length=100, batch_size=10)

    # Deserialize from raw Firestore dict (same path used during overflow_callback)
    messages = store._deserialize_history([raw_firestore_message])
    assert len(messages) == 1
    msg = messages[0]
    assert len(msg.parts) == 2
    assert msg.parts[0].text == "У меня бекенд отвалился и не сохранило сохрани пожалуйста еще раз"
    assert msg.parts[0].consolidation_text is None
    assert msg.parts[1].text is None
    assert msg.parts[1].consolidation_text == "Save Toyota Corolla left mirror damage fact"

    # Apply the consolidation serializer expression (identical to main.py:248 and conversation_handler.py:759)
    serialized_parts = [
        {"text": p.full_text or p.consolidation_text or p.text}
        for p in msg.parts
        if p.full_text or p.consolidation_text or p.text
    ]

    assert len(serialized_parts) == 2
    assert serialized_parts[0] == {"text": "У меня бекенд отвалился и не сохранило сохрани пожалуйста еще раз"}
    assert serialized_parts[1] == {"text": "Save Toyota Corolla left mirror damage fact"}


@pytest.mark.asyncio
async def test_threshold_resolver_overrides_max_history_and_batch_size():
    """When threshold_resolver returns a tuple, overflow uses it instead of the constructor defaults."""
    mock_db = MagicMock()
    mock_doc = MagicMock()

    # 3 existing messages; threshold_resolver will override max_history_length to 3, so
    # appending 1 more (total 4) overflows even though the constructor default is 200.
    existing_history = [
        {"role": "user", "parts": [{"text": f"msg {i}"}]} for i in range(3)
    ]
    mock_doc.get = AsyncMock(return_value=MagicMock(
        exists=True,
        to_dict=lambda: {"owner_id": "user1", "history": existing_history, "created_at": 1000},
    ))
    mock_doc.set = AsyncMock()
    mock_db.collection.return_value.document.return_value = mock_doc

    mock_transaction = AsyncMock()
    mock_transaction.get = AsyncMock(return_value=mock_doc.get.return_value)
    mock_transaction.set = MagicMock()
    mock_db.transaction.return_value = mock_transaction

    callback_called = asyncio.Event()
    captured = {}

    async def mock_callback(user_id, session_id, messages):
        captured["messages"] = messages
        callback_called.set()

    resolver = AsyncMock(return_value=(3, 1))  # max_history_length=3, batch_size=1

    store = FirestoreSessionStore(
        mock_db, max_history_length=200, batch_size=100,
        overflow_callback=mock_callback, threshold_resolver=resolver,
    )

    with patch("google.cloud.firestore.async_transactional", lambda x: x):
        await store.append_messages_batch("companion-session", [Message(role="user", parts=[MessagePart(text="new 1")])])

    resolver.assert_called_once_with("companion-session")
    await asyncio.wait_for(callback_called.wait(), timeout=1.0)
    # batch_size=1 from the override, not the constructor default of 100
    assert len(captured["messages"]) == 1
    assert captured["messages"][0].parts[0].text == "msg 0"


@pytest.mark.asyncio
async def test_no_threshold_resolver_uses_constructor_defaults():
    """threshold_resolver=None (default) behaves exactly as before — no override call, no behavior change."""
    store = FirestoreSessionStore(MagicMock(), max_history_length=200, batch_size=100)
    assert store.threshold_resolver is None


@pytest.mark.asyncio
async def test_threshold_resolver_returning_none_falls_back_to_defaults():
    mock_db = MagicMock()
    mock_doc = MagicMock()
    # Only 2 existing messages — well under the constructor default of 5 — so no overflow
    # regardless of what the (opted-out) resolver would have returned.
    existing_history = [
        {"role": "user", "parts": [{"text": f"msg {i}"}]} for i in range(2)
    ]
    mock_doc.get = AsyncMock(return_value=MagicMock(
        exists=True,
        to_dict=lambda: {"owner_id": "user1", "history": existing_history, "created_at": 1000},
    ))
    mock_doc.set = AsyncMock()
    mock_db.collection.return_value.document.return_value = mock_doc

    mock_transaction = AsyncMock()
    mock_transaction.get = AsyncMock(return_value=mock_doc.get.return_value)
    mock_transaction.set = MagicMock()
    mock_db.transaction.return_value = mock_transaction

    callback = AsyncMock()
    resolver = AsyncMock(return_value=None)  # opts out for this session_id

    store = FirestoreSessionStore(
        mock_db, max_history_length=5, batch_size=3,
        overflow_callback=callback, threshold_resolver=resolver,
    )

    with patch("google.cloud.firestore.async_transactional", lambda x: x):
        await store.append_messages_batch("plain-session", [Message(role="user", parts=[MessagePart(text="new 1")])])

    resolver.assert_called_once_with("plain-session")
    callback.assert_not_called()  # 3 total messages, under max_history_length=5 default — no overflow
