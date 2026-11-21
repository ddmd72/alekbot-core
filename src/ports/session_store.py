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
        is lost. To cut handed-off messages use remove_head.
        Errors: logged and swallowed.
        """
        raise NotImplementedError

    @abstractmethod
    async def remove_head(self, session_id: str, messages: list[Message]) -> bool:
        """Cut `messages` from the start of the history if it still starts with them; True when they were cut.

        Used after a batch was handed off to a queue (the overflow window, `$consolidate`). The head is compared as
        stored (role, parts, created_at), so a batch another caller cut meanwhile is not cut twice (False). The
        recorded batches behind a cut that ends on a batch boundary keep their boundaries; any other cut drops them.
        Atomicity: one atomic read-and-write; a message appended meanwhile is kept.
        Errors: logged and re-raised.
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

        Window (an append to an existing session): while the history is longer than the window (the store's
        values, or the threshold resolver's (max, batch) for this session), the oldest `batch` messages form a batch.
        The append is stored whole; then each batch is handed to the overflow callback, oldest first, and cut from the
        session only after its hand-off returned (owner decision O2): a hand-off that raises leaves that batch and the
        later ones stored, and the next append over the window hands them off again, so the callback must accept a
        repeated batch. A batch is repeated with the boundaries it was formed with: batches formed by an earlier append
        and still at the head of the history come first, as they were, even if the window values changed since (a
        session stored before batches were recorded forms them from the current values). Ceiling: a history past a
        store-defined multiple of the window (hand-offs failing every time) has its oldest batches cut with the append
        without a hand-off; they are lost to memory and logged.
        Without a callback the batches are cut with the append. The first append to a new session is stored whole.
        Atomicity: concurrent appends to one session are all kept; the cut is a separate atomic step (remove_head).
        Idempotency: none; a repeated call appends the messages again.
        Expiry: does not apply the 90-day expiry; an append to an idle session keeps its old history.
        Errors: by default a failed append is logged and swallowed (nothing stored, no exception), so it must not
        crash the response pipeline; with `raise_on_error=True` it is logged and re-raised. A caller that retries
        (retry_store_write) or must know whether the messages were stored passes raise_on_error=True (the chat-turn
        and long-turn saves do, so their retry runs).
        """
        raise NotImplementedError

    @abstractmethod
    async def get_latest_session_id(self, owner_id: str) -> Optional[str]:
        """The session id of `owner_id` with the latest last_activity, or None (also on a backend error, logged).

        Deprecated: session_id is now deterministic (user_id:channel_id); no caller in src/, kept for dev scripts
        that query existing sessions.
        """
        raise NotImplementedError
