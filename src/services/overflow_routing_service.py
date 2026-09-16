"""
OverflowRoutingService — decides, when a session's sliding window overflows,
whether the extracted batch goes to Alek's consolidation pipeline or a
companion's extraction pipeline (RFC docs/10_rfcs/COMPANION_AGENTS_RFC.md §9
item 3: "Absorb the ad-hoc flags one at a time: `ChannelBinding` stateless
first, since it is the one the tutor directly contradicts" — this service is
that absorption for the overflow write-destination flag).

Extracted out of what was previously an inline, untested closure in main.py —
this branching logic is exactly the kind of thing that produced this
feature's only real bugs so far (both hid in untested main.py wiring), so it
gets a real test suite instead of staying inline.

session_id is "<prefix>:<channel_id>" for both identity models (Alek's
"user_id:channel_id", companion's "platform:channel_id") — this resolver
does not need to tell them apart any more than CompanionWindowResolver does;
it always extracts channel_id and asks ChannelBindingService.
"""
from __future__ import annotations

from typing import List, Optional, TYPE_CHECKING

from ..domain.companion_extraction import CompanionExtractionBatch
from ..domain.consolidation import ConsolidationBatch
from ..domain.consolidation_serialization import serialize_messages_for_consolidation
from ..domain.llm import Message
from ..ports.companion_extraction_queue import CompanionExtractionQueue
from ..ports.consolidation_queue import ConsolidationQueue
from ..ports.task_queue import TaskQueue
from ..ports.user_repository import UserRepository
from ..utils.logger import logger

if TYPE_CHECKING:
    from .channel_binding_service import ChannelBindingService


class OverflowRoutingService:

    def __init__(
        self,
        # ChannelBindingService is a services/ sibling — TYPE_CHECKING-guarded import
        # avoids a runtime services-import-services violation (REQ-ARCH-22), same
        # pattern as CompanionWindowResolver (companion_window_resolver.py:18-23).
        channel_binding_service: "ChannelBindingService",
        user_repo: UserRepository,
        consolidation_queue: Optional[ConsolidationQueue],
        companion_extraction_queue: Optional[CompanionExtractionQueue],
        task_queue: TaskQueue,
    ) -> None:
        self._channel_binding = channel_binding_service
        self._user_repo = user_repo
        self._consolidation_queue = consolidation_queue
        self._companion_extraction_queue = companion_extraction_queue
        self._task_queue = task_queue

    async def route_overflow(self, user_id: str, session_id: str, messages: List[Message]) -> None:
        try:
            binding = await self._resolve_binding(session_id)

            if binding and binding.companion_config:
                await self._route_to_companion(binding, user_id, session_id, messages)
            else:
                await self._route_to_alek(user_id, session_id, messages)
        except Exception as e:
            logger.error(
                f"❌ Error in overflow routing for user={user_id[:8]} "
                f"session={session_id[:16]}: {e}",
                exc_info=True,
            )

    async def _resolve_binding(self, session_id: str):
        """Look up the channel binding for this overflow's session, failing OPEN.

        A binding-lookup failure falls through to the Alek routing path (the
        safe default) instead of dropping the batch entirely — mirrors
        CompanionWindowResolver.resolve's identical lookup (Minor #8, final
        whole-branch review 2026-08-31). Only this lookup is narrowed; a
        failure in the routing dispatch itself (queue/task enqueue) is still
        caught by route_overflow's outer try/except, unchanged.
        """
        if ":" not in session_id:
            return None
        channel_id = session_id.split(":", 1)[1]
        try:
            return await self._channel_binding.get(channel_id)
        except Exception as exc:
            logger.warning(
                f"⚠️ [OverflowRouting] binding lookup failed for {channel_id[:12]}: {exc} "
                f"— falling back to Alek routing",
            )
            return None

    async def _route_to_alek(self, user_id: str, session_id: str, messages: List[Message]) -> None:
        serialized = serialize_messages_for_consolidation(messages)
        batch = ConsolidationBatch(user_id=user_id, session_id=session_id, messages=serialized)

        if self._consolidation_queue:
            batch_id = await self._consolidation_queue.enqueue_batch(batch)
            logger.info(f"📦 [Overflow] Created batch {batch_id} for user {user_id[:8]}")
            await self._task_queue.enqueue_consolidation_task(user_id=user_id)
            logger.info(f"📬 [Overflow] Consolidation task enqueued for user {user_id[:8]}")
        else:
            logger.warning("⚠️ Consolidation queue not initialized, overflow batch lost!")

    async def _route_to_companion(
        self, binding, user_id: str, session_id: str, messages: List[Message],
    ) -> None:
        if not self._companion_extraction_queue:
            logger.warning("⚠️ Companion extraction queue not initialized, overflow batch lost!")
            return

        user_profile = await self._user_repo.get_user(user_id)
        # UserProfile.account_id is Optional[str] — a profile can exist with account_id
        # still None, which the `if user_profile else user_id` shorthand didn't cover.
        # CompanionExtractionBatch.account_id is a required str, so a None there raised
        # a pydantic ValidationError, silently dropping the batch (Important #3, final
        # whole-branch review 2026-08-31).
        account_id = (user_profile.account_id if user_profile else None) or user_id

        text_mode = binding.companion_config.text_mode
        serialized = serialize_messages_for_consolidation(messages, text_mode=text_mode)
        batch = CompanionExtractionBatch(
            session_id=session_id,
            account_id=account_id,
            companion_type=binding.agent_type,
            created_by_user_id=user_id,
            messages=serialized,
        )

        batch_id = await self._companion_extraction_queue.enqueue_batch(batch)
        logger.info(f"🧑‍🏫 [Overflow] Created companion batch {batch_id} for session {session_id[:16]}")
        await self._task_queue.enqueue_companion_consolidation_task(session_id=session_id)
        logger.info(f"📬 [Overflow] Companion consolidation task enqueued for session {session_id[:16]}")
