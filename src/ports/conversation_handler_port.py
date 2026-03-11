"""
ConversationHandlerPort — abstract interface for message handling.

Separates platform adapters from the concrete ConversationHandler
(handlers layer), so adapters/ can depend on ports/ only.
"""
from abc import ABC, abstractmethod

from ..domain.messaging import MessageContext, ResponseChannel


class ConversationHandlerPort(ABC):
    """Abstract interface for platform-agnostic conversation handling."""

    @abstractmethod
    async def handle_message(
        self,
        context: MessageContext,
        response_channel: ResponseChannel,
    ) -> None:
        """Process an inbound message and send reply via response_channel."""

    @abstractmethod
    async def handle_command(
        self,
        command: str,
        context: MessageContext,
        response_channel: ResponseChannel,
    ) -> None:
        """Process a bot command and send reply via response_channel."""
