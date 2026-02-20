import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from src.adapters.firestore_session_store import FirestoreSessionStore
from src.ports.llm_service import Message, MessagePart

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
