"""Persistence side of a long chat turn (LONG_RUNNING_TURNS_RFC §5.3–5.7).

The channel side (status line, late answer) stays in ConversationHandler; this service
writes the session and the registry. Every session write is an atomic pair.
"""
import time
from typing import List

from ..domain.llm import Message, MessagePart
from ..domain.long_turn import LongTurnRecord, LongTurnStatus, RetryVerdict
from ..domain.turn_clock import HEARTBEAT_S, NOTICE_AFTER_S, STALE_AFTER_S, TurnClock
from ..ports.long_turn_registry import LongTurnRegistry
from ..ports.session_store import SessionStore
from ..utils.logger import logger


class LongTurnService:
    def __init__(self, registry: LongTurnRegistry, session_store: SessionStore) -> None:
        self._registry = registry
        self._sessions = session_store
        self.notice_after_s = NOTICE_AFTER_S
        self.heartbeat_s = HEARTBEAT_S

    @property
    def registry(self) -> LongTurnRegistry:
        return self._registry

    async def check_retry(self, turn_id: str) -> RetryVerdict:
        record = await self._registry.get(turn_id)
        if record is None:
            return RetryVerdict.NEW
        verdict = record.verdict(now=time.time(), stale_after_s=STALE_AFTER_S)
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
                   clock: TurnClock) -> None:
        notice = Message(role="model", parts=[MessagePart(text=notice_text, full_text=notice_text)])
        clock.own_created_ats.add(notice.created_at)
        await self._sessions.append_messages_batch(
            session_id=session_id, owner_id=user_id,
            messages=[Message(role="user", parts=user_parts, created_at=event_time), notice],
        )
        now = time.time()
        await self._registry.start(LongTurnRecord(
            turn_id=turn_id, user_id=user_id, session_id=session_id, title=title[:80],
            started_at=now, heartbeat_at=now,
        ))

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
        await self._sessions.append_messages_batch(
            session_id=session_id, owner_id=owner_id,
            messages=[
                Message(role="user", parts=user_parts),
                Message(role="model", parts=[MessagePart(text=history_text, full_text=full_text)]),
            ],
        )
