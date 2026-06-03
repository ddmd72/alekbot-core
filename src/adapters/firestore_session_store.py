"""
Firestore Session Store
Persistent session storage for HTTP mode to survive container restarts
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional, List, Any, Dict, Callable, Awaitable
import datetime
from datetime import datetime as dt_class, timedelta
from google.cloud import firestore

from ..domain.session import SessionState
from ..ports.session_store import SessionStore
from ..ports.llm_port import Message, MessagePart, ToolCall
from ..utils.logger import logger


class FirestoreSessionStore(SessionStore):
    """
    Manages session state persistence in Firestore.
    Provides automatic TTL cleanup and transaction support.
    """

    def __init__(
        self, 
        db_client: firestore.AsyncClient, 
        collection_prefix: str = "",
        max_history_length: int = 200,
        batch_size: int = 100,
        overflow_callback: Optional[Callable[[str, str, List[Message]], Awaitable[None]]] = None
    ):
        """
        Initialize session store.

        Args:
            db_client: Firestore AsyncClient instance
            collection_prefix: Prefix for collection names (e.g., "dev_")
            max_history_length: Maximum messages to keep in hot storage
            batch_size: Number of messages to extract during overflow
            overflow_callback: Optional async callback triggered on overflow
        """
        self.db = db_client
        # ADR-006: collection_name is now passed explicitly (e.g. development_sessions)
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

    async def append_messages_batch(self, session_id: str, messages: List[Message], owner_id: Optional[str] = None) -> None:
        """
        Append multiple messages to session history atomically.
        Implements sliding window: hot storage + cold storage overflow.
        """
        try:
            doc_ref = self.db.collection(self.collection_name).document(session_id)
            now = time.time()
            # If owner_id is not provided, we'll try to get it from doc or fallback to session_id
            resolved_owner_id: Optional[str] = owner_id

            @firestore.async_transactional
            async def _batch_append(transaction: firestore.AsyncTransaction) -> Optional[tuple[str, List[Message]]]:
                doc = await doc_ref.get(transaction=transaction)
                nonlocal resolved_owner_id
                extracted_batches = []

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

                    # OVERFLOW LOGIC: Extract all batches until within threshold
                    while len(history) > self.max_history_length:
                        batch = history[:self.batch_size]
                        history = history[self.batch_size:]
                        extracted_batches.append(batch)
                        logger.info(
                            f"❄️ Overflow batch #{len(extracted_batches)} for {session_id[:8]}... "
                            f"(extracted={len(batch)}, remaining={len(history)}, max={self.max_history_length})"
                        )

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

                transaction.set(doc_ref, data, merge=True)

                if extracted_batches:
                    return (resolved_owner_id, extracted_batches)
                return None

            transaction = self.db.transaction()
            result = await _batch_append(transaction)

            if result and self.overflow_callback:
                owner, batches = result
                for batch in batches:
                    logger.info(f"🔔 Triggering overflow callback for user {owner[:8]}... ({len(batch)} messages)")
                    task = asyncio.create_task(self.overflow_callback(owner, session_id, batch))
                    self._pending_tasks.add(task)
                    task.add_done_callback(self._pending_tasks.discard)
                    task.add_done_callback(self._on_overflow_done)

            logger.debug(
                f"💾 Session {session_id[:8]}... batch saved ({len(messages)} messages)"
            )
        except Exception as e:
            # Soft-fail intentional: append failure must not crash the response pipeline.
            logger.error(f"❌ Error batch appending messages for {session_id[:8]}...: {e}")

    def _on_overflow_done(self, task: "asyncio.Task") -> None:
        """Log errors from overflow callback tasks — data loss indicator."""
        if not task.cancelled() and task.exception():
            logger.error(
                "❌ [SessionStore] Overflow callback failed — batch may be lost: %s",
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
