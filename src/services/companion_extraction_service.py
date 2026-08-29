"""
CompanionExtractionService
===========================

Session-keyed analog of ConsolidationService (RFC
docs/10_rfcs/COMPANION_AGENTS_RFC.md §6: "the extractor... the same
architectural slot ConsolidationAgent fills"). Depends on
CompanionExtractorPort rather than importing agents/ directly
(REQ-ARCH-22: services import domain/+ports/ only) — the concrete extractor
is constructed fresh per batch by composition/companion_extractor_runner.py.

WorkerHandler and a future sweep scheduler call process_session_batches() /
find_stuck_sessions() — mirrors how WorkerHandler calls
ConsolidationService.process_user_batches()/find_stuck_users().
"""
from __future__ import annotations

from typing import List, Optional

from ..domain.companion import CompanionRecord
from ..domain.consolidation import BatchStatus
from ..ports.companion_cache_repository import CompanionCacheRepository
from ..ports.companion_extraction_queue import CompanionExtractionQueue
from ..ports.companion_extractor_port import CompanionExtractorPort
from ..ports.companion_memory_repository import CompanionMemoryRepository
from ..ports.embedding_service import EmbeddingService
from ..utils.logger import logger


class CompanionExtractionService:

    def __init__(
        self,
        queue: CompanionExtractionQueue,
        companion_repo: CompanionMemoryRepository,
        cache_repo: CompanionCacheRepository,
        embedding_service: EmbeddingService,
        extractor: CompanionExtractorPort,
    ) -> None:
        self._queue = queue
        self._companion_repo = companion_repo
        self._cache_repo = cache_repo
        self._embedding = embedding_service
        self._extractor = extractor

    async def find_stuck_sessions(self) -> List[str]:
        return await self._queue.get_stuck_session_ids()

    async def process_session_batches(
        self,
        session_id: str,
        max_batches: Optional[int] = None,
    ) -> bool:
        logger.info(
            "🧑‍🏫 [CompanionExtraction] Starting batch processing for session %s... (max_batches=%s)",
            session_id[:12], max_batches,
        )
        try:
            await self._queue.reset_recoverable_batches(session_id)

            processed = 0
            while True:
                if max_batches is not None and processed >= max_batches:
                    break

                batches = await self._queue.get_pending_batches(session_id=session_id, limit=1)
                if not batches:
                    break

                batch = batches[0]
                await self._queue.update_batch_status(batch.batch_id, BatchStatus.PROCESSING)

                try:
                    result = await self._extractor.extract(
                        companion_type=batch.companion_type,
                        account_id=batch.account_id,
                        created_by_user_id=batch.created_by_user_id,
                        messages=batch.messages,
                    )
                    records = await self._build_records(batch, result.get("records", []))
                    if records:
                        await self._companion_repo.save_batch(records)
                    summary = result.get("summary", "")
                    if summary:
                        await self._cache_repo.save_summary(batch.session_id, batch.account_id, summary)

                    await self._queue.delete_batch(batch.batch_id)
                    logger.info(
                        "✅ [CompanionExtraction] Batch %s processed: %d records written",
                        batch.batch_id, len(records),
                    )
                    processed += 1
                except Exception as exc:
                    attempts = await self._queue.increment_attempts(batch.batch_id)
                    if attempts >= 3:
                        await self._queue.update_batch_status(batch.batch_id, BatchStatus.FAILED, error=str(exc))
                        logger.error(
                            "❌ [CompanionExtraction] EXTRACTION_FAILED batch_id=%s session_id=%s attempts=%d error=%r",
                            batch.batch_id, batch.session_id, attempts, str(exc),
                        )
                    else:
                        await self._queue.update_batch_status(batch.batch_id, BatchStatus.RETRY_PENDING, error=str(exc))
                        logger.warning(
                            "⚠️ [CompanionExtraction] Batch %s failed (attempt %d/3) session=%s: %s",
                            batch.batch_id, attempts, batch.session_id[:12], exc,
                        )
                    break

            remaining = await self._queue.get_pending_batches(session_id=session_id, limit=1)
            return len(remaining) > 0
        except Exception as exc:
            logger.error(
                "❌ [CompanionExtraction] Unhandled error for session %s: %s",
                session_id, exc, exc_info=True,
            )
            return False

    async def _build_records(self, batch, raw_records: list) -> List[CompanionRecord]:
        if not raw_records:
            return []
        texts = [r["text"] for r in raw_records]
        vectors = await self._embedding.get_embeddings_batch(texts)
        return [
            CompanionRecord(
                id=f"{batch.batch_id}-{i}",  # Deterministic ID ensures retry idempotency: Firestore set() overwrites same doc, no duplicates on re-fetch.
                session_id=batch.session_id,
                account_id=batch.account_id,
                created_by_user_id=batch.created_by_user_id,
                text=r["text"],
                vector=vector,
                tags=r.get("tags", []),
                domain=r["domain"],
            )
            for i, (r, vector) in enumerate(zip(raw_records, vectors))
        ]
