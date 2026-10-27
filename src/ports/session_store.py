"""
Session store port for persistent session history.
"""
from abc import ABC, abstractmethod
from typing import Optional

from ..domain.llm import Message
from ..domain.session import SessionState


class SessionStore(ABC):
    """Port for per-channel chat history (one record per session id `user_id:channel_id`) and its sliding window.

    Holds for every method:
    - Tenancy: the session id is the only scope; reads do not check the owner. An append stores `owner_id` on the
      first write and keeps it (a later different owner_id is ignored); overflow batches are handed off under the
      stored owner. `save_session` writes the given state's owner, defaulting to the session id.
    - Order: history is oldest first, in append order.
    - Legacy: a message without `created_at` reads with the load time; without `role` as "user"; a part's unknown
      keys are ignored; `tool_call.thought_signature` and `raw_content` are not stored. A record without
      `last_activity` or without `owner_id`: no writer produces one; behaviour pending the data audit.
    """

    @abstractmethod
    async def load_session(self, session_id: str) -> SessionState:
        """Return the session's state; an unknown id or an empty id returns an empty SessionState with that id.

        Expiry: a session idle longer than 90 days (`last_activity`) is deleted on this read and returned empty.
        Errors: never raises; a backend error or an unreadable record is logged and read as an empty session
        (callers only degrade a prompt; none writes the empty state back).
        """
        raise NotImplementedError

    @abstractmethod
    async def save_session(self, session_id: str, state: SessionState) -> None:
        """Replace the stored history, created_at, last_activity and owner (owner defaults to the session id).

        Atomicity: a whole-history replace with no precondition; an append between the caller's load and this save
        is lost (P15-S3, the `$consolidate` command).
        Errors: logged and swallowed.
        """
        raise NotImplementedError

    @abstractmethod
    async def append_message(self, session_id: str, message: Message, owner_id: Optional[str] = None) -> None:
        """Same as append_messages_batch with one message and raise_on_error=False. No caller in src/."""
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
        """Append messages at the end, all or none, then apply the sliding window.

        Window: while the history is longer than the window (the store's values, or the threshold resolver's
        (max, batch) for this session), the oldest `batch` messages are cut and each cut batch is handed to the
        overflow callback, oldest first, after the append is stored.
        Atomicity: concurrent appends to one session are all kept. The cut and the hand-off are not atomic: the cut
        is stored first, so a failed hand-off loses that batch (P11-G2-3; the memory map lists it as a known loss).
        Idempotency: none; a repeated call appends the messages again.
        Expiry: does not apply the 90-day expiry; an append to an idle session keeps its old history.
        Errors: by default a failed append is logged and swallowed (nothing stored, no exception), so it must not
        crash the response pipeline; with `raise_on_error=True` it is logged and re-raised. A caller that retries
        (retry_store_write) or must know whether the messages were stored passes raise_on_error=True (P11-G2-5:
        the chat-turn and long-turn saves retry without it, so their retry never runs).
        """
        raise NotImplementedError

    @abstractmethod
    async def get_latest_session_id(self, owner_id: str) -> Optional[str]:
        """The session id of `owner_id` with the latest last_activity, or None (also on a backend error, logged).

        Deprecated: session_id is now deterministic (user_id:channel_id); no caller in src/, kept for dev scripts
        that query existing sessions.
        """
        raise NotImplementedError
