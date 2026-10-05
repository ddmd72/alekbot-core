"""Persistence side of a long chat turn (LONG_RUNNING_TURNS_RFC §5.3–5.7).

The channel side (status line, late answer) stays in ConversationHandler; this service
writes the session and the registry. Every session write is an atomic pair.
"""
import time
from typing import List, Optional

from ..domain.llm import Message, MessagePart
from ..domain.long_turn import LongTurnRecord, LongTurnStatus, RetryVerdict
from ..domain.turn_clock import HEARTBEAT_S, NOTICE_AFTER_S, STALE_AFTER_S, TurnClock
from ..ports.long_turn_registry import LongTurnRegistry
from ..ports.session_store import SessionStore
from ..utils.logger import logger
from ..utils.retry import retry_store_write


class LongTurnService:
    def __init__(self, registry: LongTurnRegistry, session_store: SessionStore) -> None:
        self._registry = registry
        self._sessions = session_store
        self.notice_after_s = NOTICE_AFTER_S
        self.heartbeat_s = HEARTBEAT_S

    @property
    def registry(self) -> LongTurnRegistry:
        return self._registry

    async def check_retry(self, turn_id: str, live_in_process: bool = True) -> RetryVerdict:
        """Classify a delivery whose turn may already exist.

        `live_in_process=False` says the caller knows the turn is not running in this
        process. Cloud Tasks re-delivers only a failed attempt, and the service runs one
        instance, so a RUNNING record that is not live here belongs to a dead attempt even
        when its last heartbeat is still fresh: it is reported like a stale one.
        """
        record = await self._registry.get(turn_id)
        if record is None:
            return RetryVerdict.NEW
        verdict = record.verdict(now=time.time(), stale_after_s=STALE_AFTER_S)
        if verdict is RetryVerdict.RUNNING and not live_in_process:
            logger.warning("⚠️ [LongTurnService] %s is running but not live here — "
                           "its attempt died", turn_id)
            verdict = RetryVerdict.STALE
        if verdict is RetryVerdict.STALE:
            await self.finish(turn_id, LongTurnStatus.FAILED)
        return verdict

    def new_clock(self, session_id: str) -> TurnClock:
        async def fetch_since(since: float) -> List[Message]:
            session = await self._sessions.load_session(session_id)
            history = session.history if session else []
            return [m for m in history if m.created_at > since]

        return TurnClock.start(fetch_since=fetch_since)

    async def mark(self, *, turn_id: str, user_id: str, session_id: str, title: str,
                   user_parts: List[MessagePart], event_time: float, notice_text: str,
                   clock: TurnClock, started_at: Optional[float] = None) -> None:
        """Record the turn, then write the user message + notice pair. Never raises.

        The registry goes first and each write stands alone: a failed session write must
        not cost the record that retry idempotency and cancel depend on, and the reverse.
        `started_at` is the turn's start (wall time), so the running-jobs elapsed is right.
        """
        now = time.time()
        try:
            await self._registry.start(LongTurnRecord(
                turn_id=turn_id, user_id=user_id, session_id=session_id, title=title[:80],
                started_at=started_at if started_at is not None else now, heartbeat_at=now,
            ))
        except Exception as e:
            logger.error("❌ [LongTurnService] registry start(%s) failed: %s", turn_id, e,
                         exc_info=True)
        notice = Message(role="model", parts=[MessagePart(text=notice_text, full_text=notice_text)])
        clock.own_created_ats.add(notice.created_at)
        messages = [Message(role="user", parts=user_parts, created_at=event_time), notice]
        try:
            await retry_store_write(
                lambda: self._sessions.append_messages_batch(
                    session_id=session_id, owner_id=user_id, messages=messages,
                ),
                label="Long-turn mark save",
            )
        except Exception as e:
            logger.error("❌ [LongTurnService] mark session save(%s) failed: %s", turn_id, e,
                         exc_info=True)

    async def heartbeat(self, turn_id: str, step: str) -> bool:
        return await self._registry.heartbeat(turn_id, step)

    async def finish(self, turn_id: str, status: LongTurnStatus) -> None:
        try:
            await self._registry.finish(turn_id, status)
        except Exception as e:
            logger.error("❌ [LongTurnService] finish(%s, %s) failed: %s", turn_id, status.value, e)

    async def save_late_answer(self, *, session_id: str, owner_id: str, note_text: str,
                               consolidation_texts: List[str], history_text: str,
                               full_text: str) -> None:
        user_parts = [MessagePart(text=note_text)]
        if consolidation_texts:
            user_parts.append(MessagePart(consolidation_text="\n\n" + "\n".join(consolidation_texts)))
        messages = [
            Message(role="user", parts=user_parts),
            Message(role="model", parts=[MessagePart(text=history_text, full_text=full_text)]),
        ]
        await retry_store_write(
            lambda: self._sessions.append_messages_batch(
                session_id=session_id, owner_id=owner_id, messages=messages,
            ),
            label="Late answer save",
        )
