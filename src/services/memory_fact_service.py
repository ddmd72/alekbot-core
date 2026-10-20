"""MemoryFactService — facts Smart proposes and the owner confirms (MEMORY_FACT_CONFIRMATION_RFC)."""

import secrets
from typing import Optional

from ..domain.exceptions import MemoryDraftNotFound, MemoryFactsRejected
from ..domain.llm import Message, MessagePart
from ..domain.memory_fact import MemoryFactDraft, confirmed_reply, render_confirmed_note
from ..domain.prompt_v3.security import TrustZone
from ..ports.memory_fact_draft_repository import MemoryFactDraftRepository
from ..ports.security_port import SecurityPort
from ..ports.session_store import SessionStore
from ..utils.logger import logger

_MAX_DRAFT_CODE_ATTEMPTS = 5


class MemoryFactService:

    def __init__(
        self,
        repository: MemoryFactDraftRepository,
        security_port: SecurityPort,
        session_store: SessionStore,
    ):
        self._repo = repository
        self._security = security_port
        self._sessions = session_store

    async def propose(self, user_id: str, origin_session_id: Optional[str], draft: MemoryFactDraft) -> str:
        """Store `draft` (tagged with the session it came from) under a fresh save code; return the code."""
        await self._check(user_id, draft)
        stored = draft.model_copy(update={"origin_session_id": origin_session_id})
        for _ in range(_MAX_DRAFT_CODE_ATTEMPTS):
            code = secrets.token_hex(2)
            if await self._repo.create_draft(user_id, code, stored):
                logger.info("[MemoryFacts] proposed n=%d user=%s", len(draft.facts), user_id[:8])
                return code
        logger.error("❌ [MemoryFacts] No unique draft code for user %s after %d attempts",
                     user_id[:8], _MAX_DRAFT_CODE_ATTEMPTS)
        raise RuntimeError(f"could not allocate a draft code after {_MAX_DRAFT_CODE_ATTEMPTS} attempts")

    async def confirm(self, user_id: str, code: str, fallback_session_id: str) -> int:
        """Append the confirmed facts to history as a user/model pair and return their count.

        The pair goes to the session the facts were proposed in (`fallback_session_id` when the
        draft has none). The draft is deleted only after the append succeeded; a failed append
        raises and leaves the draft for a retry. A failed delete is logged only: a retry then
        appends a duplicate pair, which consolidation deduplicates.
        """
        draft = await self._repo.get_draft(user_id, code)
        if draft is None:
            logger.info("[MemoryFacts] confirm not_found user=%s", user_id[:8])
            raise MemoryDraftNotFound(f"no pending memory draft {code!r} for user {user_id[:8]}")

        count = len(draft.facts)
        reply = confirmed_reply(count)
        try:
            await self._sessions.append_messages_batch(
                session_id=draft.origin_session_id or fallback_session_id,
                owner_id=user_id,
                messages=[
                    Message(role="user", parts=[MessagePart(text=render_confirmed_note(draft))]),
                    Message(role="model", parts=[MessagePart(text=reply, full_text=reply)]),
                ],
                raise_on_error=True,
            )
        except Exception:
            logger.error("❌ [MemoryFacts] confirm append_failed user=%s", user_id[:8], exc_info=True)
            raise

        try:
            await self._repo.delete_draft(user_id, code)
        except Exception as e:
            logger.error("❌ [MemoryFacts] draft delete failed user=%s: %s", user_id[:8], e, exc_info=True)
        logger.info("[MemoryFacts] confirmed n=%d user=%s", count, user_id[:8])
        return count

    async def _check(self, user_id: str, draft: MemoryFactDraft) -> None:
        for i, fact in enumerate(draft.facts, start=1):
            for field, text in (("text", fact.text), ("source", fact.source)):
                try:
                    result = await self._security.validate(
                        text, context=f"memory_fact_{field}_user_{user_id}", zone=TrustZone.UNTRUSTED,
                    )
                except ValueError as e:  # the regex/composite adapters raise on HIGH/CRITICAL
                    logger.warning("🛡️ [MemoryFacts] Rejected fact %d %s: blocked (%s)", i, field, e)
                    raise MemoryFactsRejected(f"fact {i} {field} blocked by the security check") from e
                if result.action_taken != "passed":
                    logger.warning("🛡️ [MemoryFacts] Rejected fact %d %s: flagged %s",
                                   i, field, result.patterns_detected)
                    raise MemoryFactsRejected(f"fact {i} {field} flagged by the security check")
