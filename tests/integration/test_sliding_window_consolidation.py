import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock

from src.domain.messaging import MessageContext
from src.domain.session import Session
from src.domain.consolidation import ConsolidationBatch, BatchStatus
from src.domain.agent import AgentResponse, AgentStatus
from src.handlers.conversation_handler import ConversationHandler
from src.ports.llm_service import Message, MessagePart

@pytest.mark.asyncio
async def test_sliding_window_trigger_logic():
    """
    Test that consolidation is triggered when session reaches threshold via SessionStore callback.
    """
    # 1. Setup mocks
    session_store = MagicMock()
    session_store.load_session = AsyncMock()
    session_store.save_session = AsyncMock()
    
    # Mock append_messages_batch to simulate overflow and trigger callback
    async def mock_append(session_id, messages, owner_id=None):
        if len(messages) > 0 and session_store.overflow_callback:
            # Simulate that we reached threshold
            await session_store.overflow_callback(owner_id, session_id, messages)
    
    session_store.append_messages_batch = AsyncMock(side_effect=mock_append)
    session_store.overflow_callback = None # Will be set by handler or test
    
    consolidation_queue = AsyncMock()
    consolidation_queue.enqueue_batch = AsyncMock(return_value="batch_123")
    
    agent_factory = AsyncMock()
    agent_factory.get_session_store = MagicMock(return_value=session_store)
    agent_factory.user_repo.get_user = AsyncMock(return_value=MagicMock(config=MagicMock(consolidation_threshold=200, consolidation_batch_size=100)))
    
    coordinator = AsyncMock()
    file_service = MagicMock()
    
    user_id = "test_user"
    session_id = "test_session"
    
    handler = ConversationHandler(
        coordinator=coordinator,
        agent_factory=agent_factory,
        file_service=file_service,
        consolidation_queue=consolidation_queue
    )
    
    # Set the callback manually for testing (in main.py this is done during init)
    async def overflow_callback(uid, sid, msgs):
        batch = ConsolidationBatch(user_id=uid, session_id=sid, messages=[{"text": "msg"}])
        await consolidation_queue.enqueue_batch(batch)
        
    session_store.overflow_callback = overflow_callback

    # Mock coordinator response
    coordinator.route_message.return_value = AgentResponse.success(
        task_id="t1", agent_id="router", result=AgentResponse.success(task_id="t1", agent_id="smart", result="resp")
    )

    # 2. Call handle_message
    context = MessageContext(
        session_id=session_id,
        user_id=user_id,
        text="triggering message",
        metadata={"platform": "test"}
    )
    
    response_channel = AsyncMock()
    response_channel.send_status_with_phrase.return_value = ("msg_id", "Thinking...")

    await handler.handle_message(context, response_channel)

    # 3. Verify consolidation was triggered (via our mock callback)
    assert consolidation_queue.enqueue_batch.called

@pytest.mark.asyncio
async def test_retry_mechanism_via_handler():
    """
    Test that ConsolidationHandler processes batches correctly.
    """
    from src.handlers.consolidation_handler import process_user_batches_on_overflow
    
    session_store = MagicMock()
    consolidation_queue = AsyncMock()
    agent_factory = AsyncMock()
    coordinator = AsyncMock()
    
    user_id = "test_user"
    
    # Mock pending batches
    batch = ConsolidationBatch(
        batch_id="b1",
        user_id=user_id,
        session_id="s1",
        messages=[{"role": "user", "parts": [{"text": "old msg"}]}],
        status=BatchStatus.RETRY_PENDING,
        attempts=1
    )
    
    # Return batch on first call, then empty list
    consolidation_queue.get_pending_batches.side_effect = [[batch], []]
    
    # Mock successful agent response
    coordinator.route_message.return_value = AgentResponse.success(
        task_id="b1", agent_id="consolidation_agent", result={"new_facts": 1, "new_anchors": 0}
    )

    # Execute handler logic
    await process_user_batches_on_overflow(
        user_id=user_id,
        coordinator=coordinator,
        agent_factory=agent_factory,
        queue=consolidation_queue
    )
    
    # Verify flow
    assert consolidation_queue.update_batch_status.called
    assert consolidation_queue.delete_batch.called
    assert consolidation_queue.delete_batch.call_args[0][0] == "b1"
