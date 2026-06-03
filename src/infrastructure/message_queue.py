"""
Message Queue Infrastructure
============================

Provides abstract interface and implementations for agent message passing.

This module enables asynchronous, decoupled communication between agents
following the Actor Model pattern.

Implementations:
- InMemoryQueue: In-memory implementation for MVP (single instance)
- RedisQueue: Redis Streams implementation for production (Phase 2+)
"""

import asyncio
from abc import ABC, abstractmethod
from typing import Optional, Dict, AsyncIterator
from ..domain.agent import AgentMessage
from ..utils.logger import logger


class MessageQueue(ABC):
    """
    Abstract message queue interface.
    
    Provides a common interface for message passing between agents,
    allowing different implementations (in-memory, Redis, etc.)
    to be swapped without changing agent code.
    
    Design Principles:
    - Queue-per-agent pattern: Each agent has its own queue
    - Async-first: All operations are asynchronous
    - Timeout support: Prevents blocking on empty queues
    - Message ordering: FIFO within a single queue
    
    Usage:
        # Producer side
        await queue.enqueue("memory_search_agent", message)
        
        # Consumer side (pull)
        message = await queue.dequeue("memory_search_agent", timeout=30)
        
        # Consumer side (push/subscribe)
        async for message in queue.subscribe("memory_search_agent"):
            await process(message)
    """
    
    @abstractmethod
    async def enqueue(self, queue_name: str, message: AgentMessage) -> None:
        """
        Add a message to the specified queue.
        
        Args:
            queue_name: Name of the target queue (typically agent_id)
            message: AgentMessage to enqueue
            
        Raises:
            QueueFullError: If queue has reached capacity (implementation-specific)
        """
        pass
    
    @abstractmethod
    async def dequeue(
        self, 
        queue_name: str, 
        timeout_seconds: float = 30.0
    ) -> Optional[AgentMessage]:
        """
        Remove and return the next message from the specified queue.
        
        Args:
            queue_name: Name of the queue to read from
            timeout_seconds: Maximum time to wait for a message (0 = non-blocking)
            
        Returns:
            AgentMessage if available, None if timeout reached
            
        Note:
            - Returns immediately if message is available
            - Blocks up to timeout_seconds if queue is empty
            - Returns None if timeout reached without message
        """
        pass
    
    @abstractmethod
    async def subscribe(self, queue_name: str) -> AsyncIterator[AgentMessage]:
        """
        Create an async iterator for continuous message consumption.
        
        Args:
            queue_name: Name of the queue to subscribe to
            
        Yields:
            AgentMessage objects as they arrive
            
        Note:
            - This is a long-running operation (infinite loop)
            - Use asyncio.create_task() for background processing
            - Cancel the task to stop subscription
            
        Example:
            async def consumer():
                async for message in queue.subscribe("my_agent"):
                    await process(message)
            
            task = asyncio.create_task(consumer())
            # ... later ...
            task.cancel()
        """
        pass
    
    @abstractmethod
    async def peek(self, queue_name: str) -> Optional[AgentMessage]:
        """
        Look at the next message without removing it.
        
        Args:
            queue_name: Name of the queue to peek
            
        Returns:
            AgentMessage if available, None if queue is empty
        """
        pass
    
    @abstractmethod
    def size(self, queue_name: str) -> int:
        """
        Get current number of messages in the queue.
        
        Args:
            queue_name: Name of the queue to check
            
        Returns:
            Number of pending messages
        """
        pass
    
    @abstractmethod
    def is_empty(self, queue_name: str) -> bool:
        """
        Check if queue is empty.
        
        Args:
            queue_name: Name of the queue to check
            
        Returns:
            True if queue has no pending messages
        """
        pass
    
    @abstractmethod
    async def clear(self, queue_name: str) -> int:
        """
        Remove all messages from the specified queue.
        
        Args:
            queue_name: Name of the queue to clear
            
        Returns:
            Number of messages removed
        """
        pass
    
    @abstractmethod
    def get_stats(self) -> Dict[str, any]:
        """
        Get queue statistics for monitoring.
        
        Returns:
            Dictionary with queue names and their sizes
        """
        pass


class InMemoryQueue(MessageQueue):
    """
    In-memory message queue implementation.
    
    Uses asyncio.Queue for thread-safe, async message passing.
    Suitable for single-instance deployments (MVP phase).
    
    Features:
    - Thread-safe via asyncio.Queue
    - Configurable max capacity per queue
    - Automatic queue creation on first use
    - Statistics for monitoring
    
    Limitations:
    - Not distributed (single process only)
    - Messages lost on restart
    - No persistence
    
    For production multi-instance deployments, use RedisQueue instead.
    
    Example:
        queue = InMemoryQueue(max_size=100)
        
        # Producer
        message = AgentMessage.create(...)
        await queue.enqueue("agent_1", message)
        
        # Consumer
        message = await queue.dequeue("agent_1", timeout_seconds=10)
        if message:
            await process(message)
    """
    
    def __init__(self, max_size: int = 1000):
        """
        Initialize in-memory queue.
        
        Args:
            max_size: Maximum messages per queue (0 = unlimited)
        """
        self._max_size = max_size
        self._queues: Dict[str, asyncio.Queue] = {}
        self._lock = asyncio.Lock()
        self._total_enqueued = 0
        self._total_dequeued = 0
        
        logger.info(f"📬 InMemoryQueue initialized (max_size={max_size})")
    
    def _get_or_create_queue(self, queue_name: str) -> asyncio.Queue:
        """
        Get existing queue or create new one.
        
        Thread-safe via lock for creation only.
        """
        if queue_name not in self._queues:
            maxsize = self._max_size if self._max_size > 0 else 0
            self._queues[queue_name] = asyncio.Queue(maxsize=maxsize)
            logger.debug(f"📬 Created queue: {queue_name}")
        
        return self._queues[queue_name]
    
    async def enqueue(self, queue_name: str, message: AgentMessage) -> None:
        """
        Add a message to the specified queue.
        
        If queue is full, waits until space is available.
        """
        async with self._lock:
            queue = self._get_or_create_queue(queue_name)
        
        await queue.put(message)
        self._total_enqueued += 1
        
        logger.debug(
            f"📬 Enqueued message {message.task_id[:8]}... to {queue_name} "
            f"(queue_size={queue.qsize()})"
        )
    
    async def dequeue(
        self, 
        queue_name: str, 
        timeout_seconds: float = 30.0
    ) -> Optional[AgentMessage]:
        """
        Remove and return the next message from the specified queue.
        
        Returns None if timeout reached.
        """
        async with self._lock:
            queue = self._get_or_create_queue(queue_name)
        
        try:
            if timeout_seconds <= 0:
                # Non-blocking mode
                return queue.get_nowait()
            else:
                # Blocking with timeout
                message = await asyncio.wait_for(
                    queue.get(),
                    timeout=timeout_seconds
                )
                self._total_dequeued += 1
                
                logger.debug(
                    f"📬 Dequeued message {message.task_id[:8]}... from {queue_name} "
                    f"(queue_size={queue.qsize()})"
                )
                
                return message
                
        except asyncio.TimeoutError:
            logger.debug(f"📬 Dequeue timeout for {queue_name} ({timeout_seconds}s)")
            return None
        except asyncio.QueueEmpty:
            return None
    
    async def subscribe(self, queue_name: str) -> AsyncIterator[AgentMessage]:
        """
        Create an async iterator for continuous message consumption.
        
        This is an infinite loop - use task cancellation to stop.
        """
        async with self._lock:
            queue = self._get_or_create_queue(queue_name)
        
        logger.info(f"📬 Subscribed to queue: {queue_name}")
        
        try:
            while True:
                message = await queue.get()
                self._total_dequeued += 1
                yield message
        except asyncio.CancelledError:
            logger.info(f"📬 Subscription cancelled for queue: {queue_name}")
            raise
    
    async def peek(self, queue_name: str) -> Optional[AgentMessage]:
        """
        Look at the next message without removing it.
        
        Implementation note: asyncio.Queue doesn't support peek natively,
        so we use a workaround with _queue internal attribute.
        This is safe for read-only operations.
        """
        if queue_name not in self._queues:
            return None
        
        queue = self._queues[queue_name]
        
        if queue.empty():
            return None
        
        # Access internal deque (read-only peek)
        # This is implementation-specific but safe
        try:
            return queue._queue[0] if queue._queue else None
        except (IndexError, AttributeError):
            return None
    
    def size(self, queue_name: str) -> int:
        """Get current number of messages in the queue."""
        if queue_name not in self._queues:
            return 0
        return self._queues[queue_name].qsize()
    
    def is_empty(self, queue_name: str) -> bool:
        """Check if queue is empty."""
        if queue_name not in self._queues:
            return True
        return self._queues[queue_name].empty()
    
    async def clear(self, queue_name: str) -> int:
        """
        Remove all messages from the specified queue.
        
        Returns number of messages removed.
        """
        if queue_name not in self._queues:
            return 0
        
        queue = self._queues[queue_name]
        count = 0
        
        while not queue.empty():
            try:
                queue.get_nowait()
                count += 1
            except asyncio.QueueEmpty:
                break
        
        logger.debug(f"📬 Cleared {count} messages from queue: {queue_name}")
        return count
    
    def get_stats(self) -> Dict[str, any]:
        """Get queue statistics for monitoring."""
        queue_stats = {
            name: {
                "size": queue.qsize(),
                "max_size": queue.maxsize,
                "empty": queue.empty(),
                "full": queue.full() if queue.maxsize > 0 else False
            }
            for name, queue in self._queues.items()
        }
        
        return {
            "type": "InMemoryQueue",
            "total_queues": len(self._queues),
            "total_enqueued": self._total_enqueued,
            "total_dequeued": self._total_dequeued,
            "pending_messages": sum(q["size"] for q in queue_stats.values()),
            "max_size_per_queue": self._max_size,
            "queues": queue_stats
        }
    
    def list_queues(self) -> list[str]:
        """List all active queue names."""
        return list(self._queues.keys())
    
    async def enqueue_nowait(self, queue_name: str, message: AgentMessage) -> bool:
        """
        Try to enqueue without waiting (non-blocking).
        
        Returns:
            True if message was enqueued, False if queue is full
        """
        async with self._lock:
            queue = self._get_or_create_queue(queue_name)
        
        try:
            queue.put_nowait(message)
            self._total_enqueued += 1
            return True
        except asyncio.QueueFull:
            logger.warning(f"📬 Queue full, dropping message: {queue_name}")
            return False


class QueueFullError(Exception):
    """Raised when queue has reached maximum capacity."""
    pass


class QueueNotFoundError(Exception):
    """Raised when trying to access a non-existent queue."""
    pass
