"""
Unit tests for Message Queue Infrastructure.

Tests cover:
- InMemoryQueue basic operations (enqueue, dequeue)
- Timeout behavior
- Multiple queues isolation
- Subscribe pattern
- Statistics and monitoring
- Edge cases and error handling
"""

import pytest
import asyncio
from unittest.mock import patch

from src.infrastructure.message_queue import (
    InMemoryQueue,
    MessageQueue,
    QueueFullError,
    QueueNotFoundError
)
from src.domain.agent import AgentMessage, AgentIntent


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def queue():
    """Create a fresh InMemoryQueue for each test."""
    return InMemoryQueue(max_size=100)


@pytest.fixture
def small_queue():
    """Create a queue with small capacity for testing full conditions."""
    return InMemoryQueue(max_size=3)


@pytest.fixture
def unlimited_queue():
    """Create a queue with unlimited capacity."""
    return InMemoryQueue(max_size=0)


@pytest.fixture
def sample_message():
    """Create a sample AgentMessage for testing."""
    return AgentMessage.create(
        sender="test_sender",
        recipient="test_recipient",
        intent=AgentIntent.QUERY,
        payload={"query": "test query"},
        context={"user_id": "user123", "session_id": "session456"}
    )


def create_message(sender: str = "sender", recipient: str = "recipient") -> AgentMessage:
    """Helper to create messages with custom sender/recipient."""
    return AgentMessage.create(
        sender=sender,
        recipient=recipient,
        intent=AgentIntent.QUERY,
        payload={"data": "test"},
        context={}
    )


# ============================================================================
# Basic Operations Tests
# ============================================================================

class TestInMemoryQueueBasicOperations:
    """Test basic enqueue/dequeue operations."""

    @pytest.mark.asyncio
    async def test_enqueue_creates_queue_if_not_exists(self, queue, sample_message):
        """Enqueue should auto-create queue on first use."""
        assert queue.is_empty("new_queue")
        
        await queue.enqueue("new_queue", sample_message)
        
        assert not queue.is_empty("new_queue")
        assert queue.size("new_queue") == 1

    @pytest.mark.asyncio
    async def test_dequeue_returns_message_in_fifo_order(self, queue):
        """Messages should be dequeued in FIFO order."""
        msg1 = create_message(sender="sender1")
        msg2 = create_message(sender="sender2")
        msg3 = create_message(sender="sender3")
        
        await queue.enqueue("test_q", msg1)
        await queue.enqueue("test_q", msg2)
        await queue.enqueue("test_q", msg3)
        
        result1 = await queue.dequeue("test_q", timeout_seconds=1)
        result2 = await queue.dequeue("test_q", timeout_seconds=1)
        result3 = await queue.dequeue("test_q", timeout_seconds=1)
        
        assert result1.sender == "sender1"
        assert result2.sender == "sender2"
        assert result3.sender == "sender3"

    @pytest.mark.asyncio
    async def test_dequeue_removes_message_from_queue(self, queue, sample_message):
        """Dequeue should remove the message from queue."""
        await queue.enqueue("test_q", sample_message)
        assert queue.size("test_q") == 1
        
        await queue.dequeue("test_q", timeout_seconds=1)
        
        assert queue.size("test_q") == 0
        assert queue.is_empty("test_q")

    @pytest.mark.asyncio
    async def test_enqueue_increments_size(self, queue):
        """Each enqueue should increment queue size."""
        for i in range(5):
            msg = create_message(sender=f"sender_{i}")
            await queue.enqueue("test_q", msg)
            assert queue.size("test_q") == i + 1


# ============================================================================
# Timeout Behavior Tests
# ============================================================================

class TestInMemoryQueueTimeout:
    """Test timeout behavior in dequeue operations."""

    @pytest.mark.asyncio
    async def test_dequeue_returns_none_on_timeout(self, queue):
        """Dequeue should return None when timeout is reached on empty queue."""
        result = await queue.dequeue("empty_queue", timeout_seconds=0.1)
        
        assert result is None

    @pytest.mark.asyncio
    async def test_dequeue_waits_for_message(self, queue):
        """Dequeue should wait and return message when it arrives within timeout."""
        async def delayed_enqueue():
            await asyncio.sleep(0.1)
            msg = create_message(sender="delayed_sender")
            await queue.enqueue("test_q", msg)
        
        # Start delayed enqueue
        asyncio.create_task(delayed_enqueue())
        
        # Start dequeue with longer timeout
        result = await queue.dequeue("test_q", timeout_seconds=1.0)
        
        assert result is not None
        assert result.sender == "delayed_sender"

    @pytest.mark.asyncio
    async def test_dequeue_non_blocking_mode(self, queue, sample_message):
        """Dequeue with timeout=0 should be non-blocking."""
        # Empty queue - should return immediately
        result = await queue.dequeue("test_q", timeout_seconds=0)
        assert result is None
        
        # Queue with message - should return immediately
        await queue.enqueue("test_q", sample_message)
        result = await queue.dequeue("test_q", timeout_seconds=0)
        assert result is not None

    @pytest.mark.asyncio
    async def test_dequeue_timeout_does_not_affect_other_operations(self, queue, sample_message):
        """Timeout on one dequeue should not affect queue state."""
        await queue.enqueue("test_q", sample_message)
        
        # Timeout on different queue
        await queue.dequeue("other_queue", timeout_seconds=0.1)
        
        # Original queue should be unaffected
        assert queue.size("test_q") == 1
        result = await queue.dequeue("test_q", timeout_seconds=1)
        assert result is not None


# ============================================================================
# Multiple Queues Tests
# ============================================================================

class TestInMemoryQueueIsolation:
    """Test isolation between multiple queues."""

    @pytest.mark.asyncio
    async def test_multiple_queues_are_isolated(self, queue):
        """Messages in one queue should not appear in another."""
        msg1 = create_message(sender="queue1_sender")
        msg2 = create_message(sender="queue2_sender")
        
        await queue.enqueue("queue1", msg1)
        await queue.enqueue("queue2", msg2)
        
        result1 = await queue.dequeue("queue1", timeout_seconds=1)
        result2 = await queue.dequeue("queue2", timeout_seconds=1)
        
        assert result1.sender == "queue1_sender"
        assert result2.sender == "queue2_sender"

    @pytest.mark.asyncio
    async def test_clear_only_affects_specified_queue(self, queue):
        """Clear should only remove messages from specified queue."""
        await queue.enqueue("queue1", create_message(sender="q1"))
        await queue.enqueue("queue2", create_message(sender="q2"))
        
        cleared = await queue.clear("queue1")
        
        assert cleared == 1
        assert queue.is_empty("queue1")
        assert not queue.is_empty("queue2")

    @pytest.mark.asyncio
    async def test_list_queues(self, queue):
        """list_queues should return all active queue names."""
        await queue.enqueue("alpha", create_message())
        await queue.enqueue("beta", create_message())
        await queue.enqueue("gamma", create_message())
        
        queues = queue.list_queues()
        
        assert set(queues) == {"alpha", "beta", "gamma"}


# ============================================================================
# Subscribe Pattern Tests
# ============================================================================

class TestInMemoryQueueSubscribe:
    """Test subscribe (async iterator) pattern."""

    @pytest.mark.asyncio
    async def test_subscribe_yields_messages(self, queue):
        """Subscribe should yield messages as they arrive."""
        received_messages = []
        
        async def consumer():
            async for message in queue.subscribe("test_q"):
                received_messages.append(message)
                if len(received_messages) >= 3:
                    break
        
        # Start consumer task
        task = asyncio.create_task(consumer())
        
        # Give consumer time to start
        await asyncio.sleep(0.05)
        
        # Send messages
        for i in range(3):
            await queue.enqueue("test_q", create_message(sender=f"sender_{i}"))
            await asyncio.sleep(0.01)
        
        # Wait for consumer to finish
        await asyncio.wait_for(task, timeout=2.0)
        
        assert len(received_messages) == 3
        assert received_messages[0].sender == "sender_0"
        assert received_messages[1].sender == "sender_1"
        assert received_messages[2].sender == "sender_2"

    @pytest.mark.asyncio
    async def test_subscribe_can_be_cancelled(self, queue):
        """Subscribe task should handle cancellation gracefully."""
        received = []
        
        async def consumer():
            try:
                async for message in queue.subscribe("test_q"):
                    received.append(message)
            except asyncio.CancelledError:
                # Should propagate the cancellation
                raise
        
        task = asyncio.create_task(consumer())
        await asyncio.sleep(0.05)
        
        # Send one message
        await queue.enqueue("test_q", create_message())
        await asyncio.sleep(0.05)
        
        # Cancel the task
        task.cancel()
        
        with pytest.raises(asyncio.CancelledError):
            await task


# ============================================================================
# Peek Operation Tests
# ============================================================================

class TestInMemoryQueuePeek:
    """Test peek operation (read without remove)."""

    @pytest.mark.asyncio
    async def test_peek_returns_message_without_removing(self, queue, sample_message):
        """Peek should return message without removing it from queue."""
        await queue.enqueue("test_q", sample_message)
        
        peeked = await queue.peek("test_q")
        
        assert peeked is not None
        assert peeked.task_id == sample_message.task_id
        assert queue.size("test_q") == 1  # Still there

    @pytest.mark.asyncio
    async def test_peek_returns_none_for_empty_queue(self, queue):
        """Peek should return None for empty queue."""
        result = await queue.peek("empty_queue")
        
        assert result is None

    @pytest.mark.asyncio
    async def test_peek_returns_first_message(self, queue):
        """Peek should return the first (oldest) message in queue."""
        msg1 = create_message(sender="first")
        msg2 = create_message(sender="second")
        
        await queue.enqueue("test_q", msg1)
        await queue.enqueue("test_q", msg2)
        
        peeked = await queue.peek("test_q")
        
        assert peeked.sender == "first"


# ============================================================================
# Statistics Tests
# ============================================================================

class TestInMemoryQueueStatistics:
    """Test queue statistics and monitoring."""

    @pytest.mark.asyncio
    async def test_get_stats_returns_correct_counts(self, queue):
        """get_stats should return accurate queue statistics."""
        # Enqueue some messages
        await queue.enqueue("q1", create_message())
        await queue.enqueue("q1", create_message())
        await queue.enqueue("q2", create_message())
        
        # Dequeue one
        await queue.dequeue("q1", timeout_seconds=1)
        
        stats = queue.get_stats()
        
        assert stats["type"] == "InMemoryQueue"
        assert stats["total_queues"] == 2
        assert stats["total_enqueued"] == 3
        assert stats["total_dequeued"] == 1
        assert stats["pending_messages"] == 2

    @pytest.mark.asyncio
    async def test_stats_track_individual_queues(self, queue):
        """Stats should track individual queue sizes."""
        await queue.enqueue("queue_a", create_message())
        await queue.enqueue("queue_a", create_message())
        await queue.enqueue("queue_b", create_message())
        
        stats = queue.get_stats()
        
        assert stats["queues"]["queue_a"]["size"] == 2
        assert stats["queues"]["queue_b"]["size"] == 1

    @pytest.mark.asyncio
    async def test_empty_queue_stats(self, queue):
        """Stats should work correctly for empty queues."""
        stats = queue.get_stats()
        
        assert stats["total_queues"] == 0
        assert stats["total_enqueued"] == 0
        assert stats["total_dequeued"] == 0
        assert stats["pending_messages"] == 0


# ============================================================================
# Capacity and Full Queue Tests
# ============================================================================

class TestInMemoryQueueCapacity:
    """Test queue capacity and full conditions."""

    @pytest.mark.asyncio
    async def test_enqueue_nowait_returns_false_when_full(self, small_queue):
        """enqueue_nowait should return False when queue is full."""
        # Fill the queue (max_size=3)
        for i in range(3):
            result = await small_queue.enqueue_nowait("test_q", create_message())
            assert result is True
        
        # Try to add one more
        result = await small_queue.enqueue_nowait("test_q", create_message())
        
        assert result is False
        assert small_queue.size("test_q") == 3

    @pytest.mark.asyncio
    async def test_unlimited_queue_handles_many_messages(self, unlimited_queue):
        """Unlimited queue should handle many messages."""
        for i in range(100):
            await unlimited_queue.enqueue("test_q", create_message())
        
        assert unlimited_queue.size("test_q") == 100

    @pytest.mark.asyncio
    async def test_queue_reports_full_status(self, small_queue):
        """Queue should report full status in stats."""
        for i in range(3):
            await small_queue.enqueue("test_q", create_message())
        
        stats = small_queue.get_stats()
        
        assert stats["queues"]["test_q"]["full"] is True


# ============================================================================
# Clear Operation Tests
# ============================================================================

class TestInMemoryQueueClear:
    """Test clear operation."""

    @pytest.mark.asyncio
    async def test_clear_removes_all_messages(self, queue):
        """Clear should remove all messages from queue."""
        for i in range(5):
            await queue.enqueue("test_q", create_message())
        
        cleared = await queue.clear("test_q")
        
        assert cleared == 5
        assert queue.is_empty("test_q")
        assert queue.size("test_q") == 0

    @pytest.mark.asyncio
    async def test_clear_returns_zero_for_empty_queue(self, queue):
        """Clear should return 0 for empty queue."""
        cleared = await queue.clear("empty_queue")
        
        assert cleared == 0

    @pytest.mark.asyncio
    async def test_clear_non_existent_queue_returns_zero(self, queue):
        """Clear on non-existent queue should return 0."""
        cleared = await queue.clear("does_not_exist")
        
        assert cleared == 0


# ============================================================================
# Edge Cases Tests
# ============================================================================

class TestInMemoryQueueEdgeCases:
    """Test edge cases and unusual scenarios."""

    @pytest.mark.asyncio
    async def test_size_of_non_existent_queue_is_zero(self, queue):
        """Size of non-existent queue should be 0."""
        assert queue.size("non_existent") == 0

    @pytest.mark.asyncio
    async def test_is_empty_for_non_existent_queue_is_true(self, queue):
        """is_empty for non-existent queue should be True."""
        assert queue.is_empty("non_existent") is True

    @pytest.mark.asyncio
    async def test_concurrent_enqueue_operations(self, queue):
        """Multiple concurrent enqueue operations should be safe."""
        async def enqueue_batch(batch_id: int):
            for i in range(10):
                msg = create_message(sender=f"batch_{batch_id}_msg_{i}")
                await queue.enqueue("test_q", msg)
        
        # Run 5 batches concurrently
        await asyncio.gather(
            enqueue_batch(0),
            enqueue_batch(1),
            enqueue_batch(2),
            enqueue_batch(3),
            enqueue_batch(4),
        )
        
        assert queue.size("test_q") == 50

    @pytest.mark.asyncio
    async def test_concurrent_dequeue_operations(self, queue):
        """Multiple concurrent dequeue operations should be safe."""
        # Pre-fill queue
        for i in range(50):
            await queue.enqueue("test_q", create_message(sender=f"msg_{i}"))
        
        dequeued = []
        
        async def dequeue_batch():
            for _ in range(10):
                msg = await queue.dequeue("test_q", timeout_seconds=1)
                if msg:
                    dequeued.append(msg)
        
        # Run 5 consumers concurrently
        await asyncio.gather(
            dequeue_batch(),
            dequeue_batch(),
            dequeue_batch(),
            dequeue_batch(),
            dequeue_batch(),
        )
        
        assert len(dequeued) == 50
        assert queue.is_empty("test_q")

    @pytest.mark.asyncio
    async def test_message_with_complex_payload(self, queue):
        """Queue should handle messages with complex payloads."""
        complex_message = AgentMessage.create(
            sender="complex_sender",
            recipient="complex_recipient",
            intent=AgentIntent.DELEGATE,
            payload={
                "nested": {
                    "deep": {
                        "data": [1, 2, 3],
                        "dict": {"key": "value"}
                    }
                },
                "list": [{"a": 1}, {"b": 2}],
                "unicode": "你好世界 🚀"
            },
            context={
                "trace_id": "trace-123",
                "user_id": "user-456"
            }
        )
        
        await queue.enqueue("test_q", complex_message)
        result = await queue.dequeue("test_q", timeout_seconds=1)
        
        assert result.payload["nested"]["deep"]["data"] == [1, 2, 3]
        assert result.payload["unicode"] == "你好世界 🚀"


# ============================================================================
# Message Queue ABC Tests
# ============================================================================

class TestMessageQueueABC:
    """Test that MessageQueue is a proper ABC."""

    def test_cannot_instantiate_abstract_class(self):
        """Should not be able to instantiate MessageQueue directly."""
        with pytest.raises(TypeError):
            MessageQueue()  # type: ignore

    def test_inmemory_queue_implements_interface(self, queue):
        """InMemoryQueue should implement all abstract methods."""
        assert isinstance(queue, MessageQueue)
        
        # Check all abstract methods are implemented
        assert callable(queue.enqueue)
        assert callable(queue.dequeue)
        assert callable(queue.subscribe)
        assert callable(queue.peek)
        assert callable(queue.size)
        assert callable(queue.is_empty)
        assert callable(queue.clear)
        assert callable(queue.get_stats)
