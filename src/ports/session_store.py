"""
Session store port for persistent session history.
"""
from abc import ABC, abstractmethod
from typing import Optional

from ..domain.llm import Message
from ..domain.session import SessionState


class SessionStore(ABC):
    """Abstract port for session persistence."""

    @abstractmethod
    async def load_session(self, session_id: str) -> SessionState:
        """Load session state by session id."""
        raise NotImplementedError

    @abstractmethod
    async def save_session(self, session_id: str, state: SessionState) -> None:
        """Persist full session state."""
        raise NotImplementedError

    @abstractmethod
    async def append_message(self, session_id: str, message: Message, owner_id: Optional[str] = None) -> None:
        """Append a message to session history."""
        raise NotImplementedError

    @abstractmethod
    async def append_messages_batch(
        self,
        session_id: str,
        messages: list[Message],
        owner_id: Optional[str] = None,
        *,
        raise_on_error: bool = False,
    ) -> None:
        """Append multiple messages atomically to session history.

        By default a failed append is logged and swallowed (it must not crash the response
        pipeline). With `raise_on_error=True` it is logged and re-raised, for callers that
        must know whether the messages were stored.
        """
        raise NotImplementedError

    @abstractmethod
    async def get_latest_session_id(self, owner_id: str) -> Optional[str]:
        """Return the latest active session id for a user, if any.

        Deprecated: session_id is now deterministic (user_id:channel_id).
        Kept for dev scripts that query existing sessions.
        """
        raise NotImplementedError
