"""
Firestore Session Store
Persistent session storage for HTTP mode to survive container restarts
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional, List, Any, Dict, Callable, Awaitable, Coroutine, Tuple
import datetime
from datetime import datetime as dt_class, timedelta
from google.cloud import firestore

from ..domain.consolidation_window import PlannedBatch, overflow_window, plan_after_cut
from ..domain.session import SessionState
from ..ports.session_store import SessionStore
from ..ports.llm_port import Message, MessagePart, ToolCall
from ..utils.logger import logger


# Safety valve for an overflow hand-off that fails every time (a deterministic error): the batches then stay in the
# session and it keeps growing until an append fails on Firestore's 1 MiB document limit, and every later turn is lost
# from history. Past this many windows the oldest batches are cut without a hand-off (lost, logged 🚨). Two windows (140
# messages live) is about 35 turns of consecutive failed hand-offs, far past a transient outage that the next appends
# heal, while keeping the document within twice its normal size, well clear of the limit even with long replies.
OVERFLOW_CEILING_WINDOWS = 2

# The session field holding the hand-off plan: one {"size", "key"} per batch formed and not yet cut, oldest first
# (`src/domain/consolidation_window.py`).
HANDOFF_PLAN_FIELD = "handoff_plan"


class FirestoreSessionStore(SessionStore):
    """
    Manages session state persistence in Firestore.

    Firestore mechanics (the port states the observable contract):
    - Collection `EnvironmentConfig.sessions_collection` (`{prefix}sessions`), one document per session id; fields
      `history` (list of message dicts), `created_at`, `last_activity` (float epoch), `owner_id`, `updated_at`
      (SERVER_TIMESTAMP), `expires_at`. No TTL policy covers the collection: `expires_at` is written and never read;
      expiry is the lazy 90-day check in `load_session`.
    - `append_messages_batch` reads and writes the document in one transaction (`set(merge=True)`); a conflicting
      commit re-runs the body, so concurrent appends are all kept. The threshold resolver runs once, before the
      transaction. The window is applied only when the document already exists. With an overflow callback the append
      stores every message, and one tracked task after the commit hands each batch past the window to the callback,
      then cuts it with `remove_head` (a second transaction); a failed hand-off is logged and leaves the batch stored.
      Without a callback the batches are cut inside the append's transaction. With one, a history past
      `OVERFLOW_CEILING_WINDOWS` windows (hand-offs failing every time) has its oldest batches cut inside the append's
      transaction without a hand-off, logged 🚨 after the commit.
    - With a callback the append's transaction also writes `handoff_plan` (`HANDOFF_PLAN_FIELD`): the size and
      `handoff_key` of each batch it formed, so a batch that is formed again before its cut (a failed cut, a hand-off
      still in flight) keeps its boundaries when the window values changed meanwhile. A document without the field, or
      whose planned head no longer matches the history (`save_session` replaced it), forms its batches as before.
    - `remove_head` reads the document in a transaction and writes back the stored history after the head and the
      plan after it (`update`): the batches behind a cut that ends on a batch boundary, else an empty plan.
    - `save_session` is a `set(merge=True)` with no precondition.
    - `get_latest_session_id` needs the composite index (owner_id ASC, last_activity DESC).
    - `delete_session` and `cleanup_expired_sessions` are not on the port and have no caller in src/.
    """

    def __init__(
        self,
        db_client: firestore.AsyncClient,
        collection_prefix: str = "",
        max_history_length: int = 200,
        batch_size: int = 100,
        overflow_callback: Optional[Callable[[str, str, List[Message]], Coroutine[Any, Any, None]]] = None,
        threshold_resolver: Optional[Callable[[str], Awaitable[Optional[Tuple[int, int]]]]] = None,
    ):
        """
        Initialize session store.

        Args:
            db_client: Firestore AsyncClient instance
            collection_prefix: Prefix for collection names (e.g., "dev_")
            max_history_length: Maximum messages to keep in hot storage (default when
                threshold_resolver is None or opts out for a given session_id)
            batch_size: Number of messages to extract during overflow (same default rule)
            overflow_callback: Optional async callback triggered on overflow
            threshold_resolver: Optional async callable resolving (max_history_length,
                batch_size) for one session_id — e.g. CompanionWindowResolver.resolve.
                Returns None to use the constructor defaults for that session.
        """
        self.db = db_client
        # collection naming (decisions/collection_prefix_retained.md): collection_name is now passed explicitly (e.g. development_sessions)
        if collection_prefix:
            # Fallback for old calls: try to construct name or use as is
            self.collection_name = f"{collection_prefix}sessions" if not collection_prefix.endswith("sessions") else collection_prefix
        else:
             # Should be passed as full name in main.py
             self.collection_name = "sessions"

        self.ttl_hours = 2160  # Sessions expire after 90 days of inactivity
        self.max_history_length = max_history_length
        self.batch_size = batch_size
        self.overflow_callback = overflow_callback
        self.threshold_resolver = threshold_resolver
        self._pending_tasks: set = set()  # Track overflow tasks to prevent silent data loss

        logger.info(
            f"📦 FirestoreSessionStore initialized (collection: {self.collection_name}, "
            f"max_history={max_history_length}, batch_size={batch_size})"
        )

    async def load_session(self, session_id: str) -> SessionState:
        """
        Retrieve session state from Firestore.

        Args:
            session_id: Unique session identifier

        Returns:
            SessionState object (empty if not found or expired)
        """
        if not session_id:
            return SessionState(session_id=session_id or "")

        try:
            doc_ref = self.db.collection(self.collection_name).document(session_id)
            doc = await doc_ref.get()

            if not doc.exists:
                logger.debug(f"📭 Session {session_id[:8]}... not found, creating new")
                return SessionState(session_id=session_id)

            data = doc.to_dict()

            # Check TTL
            last_activity = data.get("last_activity", 0)
            if time.time() - last_activity > (self.ttl_hours * 3600):
                logger.info(f"⏰ Session {session_id[:8]}... expired, creating new")
                await self._delete_session(session_id)
                return SessionState(session_id=session_id)

            # Deserialize session
            history = self._deserialize_history(data.get("history", []))
            empty_messages = 0
            empty_parts = 0
            for msg in history:
                if not msg.parts:
                    empty_messages += 1
                    continue
                for part in msg.parts:
                    if not part.text and not part.file_data and not part.tool_call and not part.tool_response:
                        empty_parts += 1

            session = SessionState(
                session_id=session_id,
                history=history,
                created_at=data.get("created_at", time.time()),
                last_activity=last_activity,
                owner_id=data.get("owner_id"),
            )

            logger.debug(
                "📬 Session %s... loaded (%s messages, empty_messages=%s, empty_parts=%s)",
                session_id[:8],
                len(session.history),
                empty_messages,
                empty_parts
            )
            return session

        except Exception as e:
            logger.error(f"❌ Error loading session {session_id[:8]}...: {e}")
            return SessionState(session_id=session_id)

    async def save_session(self, session_id: str, state: SessionState) -> None:
        """
        Save session state to Firestore.

        Args:
            session_id: Unique session identifier
            state: SessionState object to persist
        """
        try:
            doc_ref = self.db.collection(self.collection_name).document(session_id)
            owner_id = state.owner_id or session_id

            data = {
                "history": self._serialize_history(state.history),
                "created_at": state.created_at,
                "last_activity": state.last_activity,
                "owner_id": owner_id,
                "updated_at": firestore.SERVER_TIMESTAMP,
                "expires_at": dt_class.now(datetime.UTC) + timedelta(hours=self.ttl_hours),
            }

            await doc_ref.set(data, merge=True)
            logger.debug(f"💾 Session {session_id[:8]}... saved ({len(state.history)} messages)")

        except Exception as e:
            # Soft-fail intentional: session save failure must not break the user response.
            logger.error(f"❌ Error saving session {session_id[:8]}...: {e}")

    async def append_message(self, session_id: str, message: Message, owner_id: Optional[str] = None) -> None:
        """Append a message to session history. Redirects to batch for consistency."""
        try:
            await self.append_messages_batch(session_id, [message], owner_id=owner_id)
        except Exception as e:
            logger.error(f"❌ Error appending message for {session_id[:8]}...: {e}")

    async def append_messages_batch(
        self,
        session_id: str,
        messages: List[Message],
        owner_id: Optional[str] = None,
        *,
        raise_on_error: bool = False,
    ) -> None:
        """
        Append multiple messages to session history atomically.
        Implements sliding window: hot storage + cold storage overflow.
        """
        try:
            doc_ref = self.db.collection(self.collection_name).document(session_id)
            now = time.time()
            # If owner_id is not provided, we'll try to get it from doc or fallback to session_id
            resolved_owner_id: Optional[str] = owner_id

            # Resolve the effective overflow threshold BEFORE entering the transaction:
            # a Firestore transaction retry must never re-invoke an external service call.
            effective_max_history_length = self.max_history_length
            effective_batch_size = self.batch_size
            if self.threshold_resolver:
                override = await self.threshold_resolver(session_id)
                if override:
                    effective_max_history_length, effective_batch_size = override
                    logger.debug(
                        "🎚️ Session %s... using threshold override (max=%d, batch=%d)",
                        session_id[:8], effective_max_history_length, effective_batch_size,
                    )

            ceiling = OVERFLOW_CEILING_WINDOWS * effective_max_history_length
            dropped: List[int] = []     # sizes of the batches the valve cut, logged after the commit

            @firestore.async_transactional
            async def _batch_append(transaction: firestore.AsyncTransaction) -> Optional[Tuple[str, List[List[Message]]]]:
                doc = await doc_ref.get(transaction=transaction)
                nonlocal resolved_owner_id
                extracted_batches: List[List[Message]] = []
                plan: Optional[List[PlannedBatch]] = None
                dropped.clear()

                if not doc.exists:
                    # If owner_id was not passed, fallback to session_id as owner
                    if not resolved_owner_id:
                        resolved_owner_id = session_id

                    state = SessionState(
                        history=list(messages),
                        created_at=now,
                        last_activity=now,
                        owner_id=resolved_owner_id,
                    )
                else:
                    data = doc.to_dict()
                    # Keep existing owner_id if present, otherwise use passed or session_id
                    resolved_owner_id = data.get("owner_id") or resolved_owner_id or session_id
                    history = self._deserialize_history(data.get("history", []))
                    history.extend(messages)

                    # OVERFLOW: the oldest batches past the window, the planned ones first. With a callback they stay
                    # stored until their hand-off succeeded (cut afterwards by remove_head); without one they are cut here.
                    with_callback = self.overflow_callback is not None
                    window = overflow_window(
                        session_id, history, self._read_plan(data) if with_callback else [],
                        effective_max_history_length, effective_batch_size, ceiling if with_callback else None,
                    )
                    dropped.extend(window.dropped)
                    extracted_batches = window.batches
                    remaining = len(window.history)
                    for number, batch in enumerate(extracted_batches, 1):
                        remaining -= len(batch)
                        logger.info(
                            f"❄️ Overflow batch #{number} for {session_id[:8]}... "
                            f"(extracted={len(batch)}, remaining={remaining}, max={effective_max_history_length})"
                        )
                    history = window.history if with_callback else window.rest
                    plan = window.plan if with_callback else None

                    state = SessionState(
                        history=history,
                        created_at=data.get("created_at", now),
                        last_activity=now,
                        owner_id=resolved_owner_id,
                    )

                data = {
                    "history": self._serialize_history(state.history),
                    "created_at": state.created_at,
                    "last_activity": state.last_activity,
                    "owner_id": state.owner_id,
                    "updated_at": firestore.SERVER_TIMESTAMP,
                    "expires_at": dt_class.now(datetime.UTC) + timedelta(hours=self.ttl_hours),
                }
                if plan is not None:
                    data[HANDOFF_PLAN_FIELD] = [{"size": size, "key": key} for size, key in plan]

                transaction.set(doc_ref, data, merge=True)

                if extracted_batches:
                    return (resolved_owner_id, extracted_batches)
                return None

            transaction = self.db.transaction()
            result = await _batch_append(transaction)
            for size in dropped:
                logger.error(
                    f"🚨 [SessionStore] {session_id}: the history passed {ceiling} messages "
                    f"({OVERFLOW_CEILING_WINDOWS} windows) because its overflow hand-offs keep failing; the oldest "
                    f"{size} messages were cut without a hand-off and are lost to memory"
                )

            if result and self.overflow_callback:
                owner, batches = result
                task = asyncio.create_task(self._hand_off(owner, session_id, batches))
                self._pending_tasks.add(task)
                task.add_done_callback(self._pending_tasks.discard)
                task.add_done_callback(self._on_overflow_done)

            logger.debug(
                f"💾 Session {session_id[:8]}... batch saved ({len(messages)} messages)"
            )
        except Exception as e:
            logger.error(f"❌ Error batch appending messages for {session_id[:8]}...: {e}")
            if raise_on_error:
                raise
            # Soft-fail by default: append failure must not crash the response pipeline.

    async def _hand_off(self, owner: str, session_id: str, batches: List[List[Message]]) -> None:
        """Hand each batch off, oldest first, and cut it from the session only after its hand-off succeeded (owner
        decision O2). A failed hand-off stops here: that batch and the later ones stay stored, and the next append
        that finds the session over its window hands them off again (the callback makes a repeat idempotent)."""
        callback = self.overflow_callback
        if callback is None:
            return
        for batch in batches:
            logger.info(f"🔔 Triggering overflow callback for user {owner[:8]}... ({len(batch)} messages)")
            try:
                await callback(owner, session_id, batch)
            except Exception as e:
                logger.error(
                    f"❌ [SessionStore] Overflow hand-off failed for {session_id[:16]}: {e} — the batch stays in the "
                    f"session until the next append hands it off again"
                )
                return
            try:
                cut = await self.remove_head(session_id, batch)
            except Exception:
                return  # logged by remove_head; the batch stays and is handed off again (same id)
            if not cut:
                logger.info(f"ℹ️ [SessionStore] Overflow batch of {session_id[:16]} was already cut")

    async def remove_head(self, session_id: str, messages: List[Message]) -> bool:
        """Cut `messages` from the start of the history in one transaction if it still starts with them.

        The head is compared as stored (serialized role, parts and created_at); the rest of the stored history is
        written back as it was read, so a message appended meanwhile is kept."""
        if not messages:
            return False
        doc_ref = self.db.collection(self.collection_name).document(session_id)
        expected = self._serialize_history(messages)

        @firestore.async_transactional
        async def _cut(transaction: firestore.AsyncTransaction) -> bool:
            doc = await doc_ref.get(transaction=transaction)
            if not doc.exists:
                return False
            data = doc.to_dict() or {}
            stored = data.get("history", [])
            head = self._serialize_history(self._deserialize_history(stored[: len(expected)]))
            if head != expected:
                return False
            plan = plan_after_cut(self._read_plan(data), len(expected))
            transaction.update(doc_ref, {
                "history": stored[len(expected):],
                HANDOFF_PLAN_FIELD: [{"size": size, "key": key} for size, key in plan],
                "updated_at": firestore.SERVER_TIMESTAMP,
            })
            return True

        try:
            return bool(await _cut(self.db.transaction()))
        except Exception as e:
            logger.error(f"❌ Error cutting the head of {session_id[:16]}...: {e}")
            raise

    @staticmethod
    def _read_plan(data: Dict[str, Any]) -> List[PlannedBatch]:
        """The stored hand-off plan; a missing or malformed one reads as no plan (the batches are formed as before)."""
        raw = data.get(HANDOFF_PLAN_FIELD)
        if not isinstance(raw, list):
            return []
        plan: List[PlannedBatch] = []
        for entry in raw:
            size, key = (entry.get("size"), entry.get("key")) if isinstance(entry, dict) else (None, None)
            if not isinstance(size, int) or not isinstance(key, str):
                break
            plan.append((size, key))
        return plan

    def _on_overflow_done(self, task: "asyncio.Task") -> None:
        """Log an error that escaped the overflow hand-off task. Nothing is lost: `_hand_off` cuts a batch only after
        its hand-off succeeded."""
        if not task.cancelled() and task.exception():
            logger.error(
                "❌ [SessionStore] Overflow hand-off task failed: %s — the batch stays in the session until the next "
                "append hands it off again",
                task.exception()
            )

    async def get_latest_session_id(self, owner_id: str) -> Optional[str]:
        """Find the most recently active session for a given owner."""
        try:
            query = (
                self.db.collection(self.collection_name)
                .where("owner_id", "==", owner_id)
                .order_by("last_activity", direction=firestore.Query.DESCENDING)
                .limit(1)
            )
            docs = await query.get()
            if not docs:
                return None
            return docs[0].id
        except Exception as e:
            logger.error(f"❌ Error finding latest session for {owner_id}: {e}")
            return None

    async def delete_session(self, session_id: str) -> None:
        """
        Delete a session from Firestore.

        Args:
            session_id: Unique session identifier
        """
        await self._delete_session(session_id)

    async def _delete_session(self, session_id: str) -> None:
        """Internal delete method."""
        try:
            doc_ref = self.db.collection(self.collection_name).document(session_id)
            await doc_ref.delete()
            logger.debug(f"🗑️ Session {session_id[:8]}... deleted")
        except Exception as e:
            logger.error(f"❌ Error deleting session {session_id[:8]}...: {e}")

    async def cleanup_expired_sessions(self) -> int:
        """
        Clean up expired sessions (manual trigger or scheduled job).

        Returns:
            Number of sessions deleted
        """
        try:
            cutoff_time = dt_class.now(datetime.UTC) - timedelta(hours=self.ttl_hours)

            # Query expired sessions
            query = (
                self.db.collection(self.collection_name)
                .where("last_activity", "<", cutoff_time.timestamp())
                .limit(100)
            )

            docs = await query.get()
            count = 0

            for doc in docs:
                await doc.reference.delete()
                count += 1

            if count > 0:
                logger.info(f"🧹 Cleaned up {count} expired sessions")

            return count

        except Exception as e:
            logger.error(f"❌ Error during session cleanup: {e}")
            return 0

    def _serialize_history(self, history: List[Message]) -> List[Dict[str, Any]]:
        """
        Serialize message history for Firestore storage.

        Args:
            history: List of Message objects

        Returns:
            List of dictionaries
        """
        serialized: List[Dict[str, Any]] = []
        for msg in history:
            msg_dict: Dict[str, Any] = {
                "role": msg.role,
                "parts": [],
                "created_at": msg.created_at,
            }

            for part in msg.parts:
                part_dict: Dict[str, Any] = {}
                if part.text:
                    part_dict["text"] = part.text
                if part.full_text:
                    part_dict["full_text"] = part.full_text
                if part.consolidation_text:
                    part_dict["consolidation_text"] = part.consolidation_text
                if part.file_data:
                    part_dict["file_data"] = part.file_data
                if part.tool_call:
                    # Serialize tool call
                    part_dict["tool_call"] = {
                        "name": part.tool_call.name,
                        "args": part.tool_call.args,
                    }
                if part.tool_response:
                    part_dict["tool_response"] = part.tool_response

                msg_dict["parts"].append(part_dict)

            serialized.append(msg_dict)

        return serialized

    def _deserialize_history(self, data: List[Dict[str, Any]]) -> List[Message]:
        """
        Deserialize message history from Firestore data.

        Args:
            data: List of dictionaries from Firestore

        Returns:
            List of Message objects
        """
        history: List[Message] = []

        for msg_dict in data:
            parts: List[MessagePart] = []
            for part_dict in msg_dict.get("parts", []):
                tool_call = None
                if part_dict.get("tool_call"):
                    tool_call = ToolCall(
                        name=part_dict["tool_call"].get("name", ""),
                        args=part_dict["tool_call"].get("args", {}),
                    )
                part = MessagePart(
                    text=part_dict.get("text"),
                    full_text=part_dict.get("full_text"),
                    consolidation_text=part_dict.get("consolidation_text"),
                    file_data=part_dict.get("file_data"),
                    tool_call=tool_call,
                    tool_response=part_dict.get("tool_response"),
                )
                parts.append(part)

            message = Message(
                role=msg_dict.get("role", "user"),
                parts=parts,
                raw_content=msg_dict.get("raw_content"),
                created_at=msg_dict.get("created_at", time.time()),
            )
            history.append(message)

        return history
