"""
Infrastructure Layer
====================

Contains infrastructure abstractions for agent communication.

Components:
- MessageQueue: Abstract interface for agent message passing
- InMemoryQueue: In-memory implementation for MVP
"""

from .message_queue import MessageQueue, InMemoryQueue
from .agent_coordinator import AgentCoordinator

__all__ = ["MessageQueue", "InMemoryQueue", "AgentCoordinator"]
