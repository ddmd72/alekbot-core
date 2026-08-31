"""
OverflowRoutingService — decides, when a session's sliding window overflows,
whether the extracted batch goes to Alek's consolidation pipeline or a
companion's extraction pipeline (RFC docs/10_rfcs/COMPANION_AGENTS_RFC.md §9:
"Phase F... overflow_callback branches by binding").

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

from typing import List, Optional

from ..domain.companion_extraction import CompanionExtractionBatch
from ..domain.consolidation import ConsolidationBatch
from ..domain.consolidation_serialization import serialize_messages_for_consolidation
from ..domain.llm import Message
from ..ports.companion_extraction_queue import CompanionExtractionQueue
from ..ports.consolidation_queue import ConsolidationQueue
from ..utils.logger import logger


class OverflowRoutingService:

    def __init__(
        self,
        channel_binding_service,  # ChannelBindingService — typed loosely to avoid a services-import-services violation
        user_repo,  # UserRepository-shaped: get_user(user_id) -> profile with .account_id
        consolidation_queue: Optional[ConsolidationQueue],
        companion_extraction_queue: Optional[CompanionExtractionQueue],
        task_queue,  # TaskQueue-shaped: enqueue_consolidation_task(user_id=), enqueue_companion_consolidation_task(session_id=)
    ) -> None:
        self._channel_binding = channel_binding_service
        self._user_repo = user_repo
        self._consolidation_queue = consolidation_queue
        self._companion_extraction_queue = companion_extraction_queue
        self._task_queue = task_queue

    async def route_overflow(self, user_id: str, session_id: str, messages: List[Message]) -> None:
        try:
            binding = None
            if ":" in session_id:
                channel_id = session_id.split(":", 1)[1]
                binding = await self._channel_binding.get(channel_id)

            if binding and binding.companion_config:
                await self._route_to_companion(binding, user_id, session_id, messages)
            else:
                await self._route_to_alek(user_id, session_id, messages)
        except Exception as e:
            logger.error(f"❌ Error in overflow routing: {e}", exc_info=True)

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
        account_id = user_profile.account_id if user_profile else user_id

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
