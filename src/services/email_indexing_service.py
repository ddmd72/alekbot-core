"""
EmailIndexingService — orchestrates the full email indexing pipeline.
See docs/10_rfcs/GMAIL_EMAIL_INDEXING_RFC.md §6, §2.2 Flow 1.

Pipeline per chunk (100 emails/page):
  list_emails → exclusion pre-filter → classify → fetch full content
  → embed (4 vectors) → save → advance cursor → update job
"""

import asyncio
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from ..domain.email import (
    EmailClassificationResult,
    EmailExclusion,
    EmailMetadata,
    EmailFullContent,
    IndexedEmail,
    IndexingJob,
    IndexingState,
    OAuthCredentials,
)
from ..ports.email_exclusions_port import EmailExclusionsPort
from ..ports.email_indexing_job_repository import EmailIndexingJobRepository
from ..ports.email_provider_port import EmailProviderPort
from ..ports.embedding_service import EmbeddingService
from ..ports.email_classifier_port import EmailClassifierPort
from ..ports.indexed_email_repository import IndexedEmailRepository
from ..utils.logger import logger

# Gmail search filter applied to every indexing job.
# Restricts fetch to Primary + Updates tabs and excludes spam — matches POC LABEL_FILTER.
# Pass gmail_query=None to override (full mailbox, for debugging only).
GMAIL_DEFAULT_QUERY = "{category:primary category:updates} -in:spam -in:trash"


class EmailIndexingService:
    """
    Orchestrates the email indexing pipeline for one provider.

    Designed for Cloud Tasks execution (long-running, resumable).
    Progress is persisted after every chunk — safe to timeout and restart.
    """

    def __init__(
        self,
        gmail: EmailProviderPort,
        email_repo: IndexedEmailRepository,
        job_repo: EmailIndexingJobRepository,
        exclusions_repo: EmailExclusionsPort,
        classifier: EmailClassifierPort,
        embedding: EmbeddingService,
    ):
        self._gmail = gmail
        self._email_repo = email_repo
        self._job_repo = job_repo
        self._exclusions_repo = exclusions_repo
        self._classifier = classifier
        self._embedding = embedding
        logger.info("📧 EmailIndexingService initialized")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_job(
        self,
        user_id: str,
        provider: str,
        triggered_by: str,
        resume_token: Optional[str] = None,
    ) -> IndexingJob:
        """Build a new IndexingJob (caller must persist via job_repo.create_job)."""
        now = datetime.utcnow()
        return IndexingJob(
            job_id=str(uuid.uuid4()),
            user_id=user_id,
            provider=provider,
            triggered_by=triggered_by,
            status="running",
            next_page_token=resume_token,
            started_at=now,
            updated_at=now,
        )

    async def start_job(
        self,
        user_id: str,
        account_id: str,
        credentials: OAuthCredentials,
        provider: str = "gmail",
        triggered_by: str = "cabinet",
        max_pages: Optional[int] = None,
        date_from: Optional[datetime] = None,
        mode: str = "incremental",
        backfill_until: Optional[datetime] = None,
    ) -> IndexingJob:
        """
        Create, persist, and run a new indexing job. Convenience wrapper for cabinet/API callers.

        mode:
          "incremental" — fetch emails newer than indexed_through (default)
          "reindex"     — clear state, re-process all emails from now back 3 years (overwrites)
          "backfill"    — fetch emails older than oldest_indexed_through, down to backfill_until
        """
        job = self.create_job(user_id=user_id, provider=provider, triggered_by=triggered_by)
        await self._job_repo.create_job(job)
        return await self.run_indexing_job(
            job=job,
            credentials=credentials,
            account_id=account_id,
            max_pages=max_pages,
            date_from=date_from,
            mode=mode,
            backfill_until=backfill_until,
        )

    async def run_indexing_job(
        self,
        job: IndexingJob,
        credentials: OAuthCredentials,
        account_id: str,
        max_pages: Optional[int] = None,
        date_from: Optional[datetime] = None,
        gmail_query: Optional[str] = GMAIL_DEFAULT_QUERY,
        mode: str = "incremental",
        backfill_until: Optional[datetime] = None,
        page_size: int = 300,
    ) -> IndexingJob:
        """
        Execute the indexing pipeline until all pages are exhausted or an error occurs.

        mode controls cursor logic:
          "incremental": fetch after indexed_through+1d; advance indexed_through forward
          "reindex":     clear state; fetch without upper bound; update both cursors
          "backfill":    fetch between backfill_until and oldest_indexed_through-1d;
                         advance oldest_indexed_through backward

        Resume: if job.next_page_token is set, resumes from that cursor.
        Idempotent: uses email_id as Firestore doc ID; duplicate writes are no-ops.
        """
        # ----------------------------------------------------------------
        # Mode-specific cursor setup
        # ----------------------------------------------------------------
        existing_state = await self._email_repo.get_indexing_state(
            job.user_id, job.provider
        )
        date_to: Optional[datetime] = None

        if mode == "reindex":
            await self._email_repo.clear_indexing_state(job.user_id, job.provider)
            existing_state = None
            if date_from is None:
                date_from = datetime.utcnow() - timedelta(days=3 * 365)
            logger.info(
                f"📧 Job {job.job_id[:8]} [reindex]: resetting cursor, "
                f"from={date_from.strftime('%Y-%m-%d')}"
            )

        elif mode == "backfill":
            if existing_state and existing_state.oldest_indexed_through:
                # Upper bound: day before what we already have
                date_to = existing_state.oldest_indexed_through - timedelta(days=1)
            if backfill_until is not None:
                date_from = backfill_until
            elif date_from is None:
                date_from = datetime.utcnow() - timedelta(days=5 * 365)
            logger.info(
                f"📧 Job {job.job_id[:8]} [backfill]: "
                f"from={date_from.strftime('%Y-%m-%d')} "
                f"to={date_to.strftime('%Y-%m-%d') if date_to else 'open'}"
            )

        else:  # incremental (default)
            if date_from is None:
                if existing_state and existing_state.indexed_through:
                    # after: is inclusive — advance by 1 day to skip already-indexed
                    date_from = existing_state.indexed_through + timedelta(days=1)
            if date_from is None:
                date_from = datetime.utcnow() - timedelta(days=3 * 365)
                logger.info(
                    f"📧 Job {job.job_id[:8]} [incremental]: no cursor, "
                    f"defaulting to 3yr ago ({date_from.strftime('%Y-%m-%d')})"
                )

        # ----------------------------------------------------------------
        # Load exclusions once per job (fast pre-filter, no LLM)
        # ----------------------------------------------------------------
        exclusions = await self._exclusions_repo.get_exclusions(job.user_id)
        logger.info(
            f"📧 Job {job.job_id[:8]} [{mode}] starting: user={job.user_id[:8]} "
            f"provider={job.provider} exclusions={len(exclusions)} "
            f"resume={bool(job.next_page_token)} query={gmail_query!r}"
        )

        page_token = job.next_page_token
        # Running max/min seen across all pages (used to advance cursors)
        job_latest_date: Optional[datetime] = None
        job_oldest_date: Optional[datetime] = None
        pages_processed = 0

        try:
            while True:
                # Refresh token if expired before each page
                if credentials.token_expiry <= datetime.utcnow():
                    credentials = await self._gmail.refresh_token(credentials)

                # 1. Fetch page of email metadata (100 emails)
                emails_page, next_page_token = await self._gmail.list_emails(
                    credentials=credentials,
                    date_from=date_from,
                    date_to=date_to,
                    page_token=page_token,
                    max_results=page_size,
                    query=gmail_query,
                )

                if not emails_page:
                    break

                # 2. Pre-filter via exclusions (no LLM cost)
                filtered = self._apply_exclusions(emails_page, exclusions)
                excluded_count = len(emails_page) - len(filtered)
                if excluded_count:
                    logger.info(
                        f"🚫 Excluded {excluded_count} emails via patterns"
                    )

                # 3. Classify remaining emails (agentic loop, Gemini Flash + tool calling)
                classifications: List[EmailClassificationResult] = []
                if filtered:
                    classifications = await self._classifier.classify_batch(
                        filtered, job.user_id, credentials=credentials
                    )
                valuable = [
                    c for c in classifications if c.valuable and c.fact
                ]

                # 4. Fetch full content for ALL valuable emails (attachment filenames).
                #    Always re-fetches regardless of what was already fetched during
                #    tool calling — storage needs authoritative data, not the truncated
                #    3000-char snippets sent to the LLM.
                email_meta_map: Dict[str, EmailMetadata] = {
                    e.email_id: e for e in filtered
                }
                full_content_map: Dict[str, EmailFullContent] = {}
                if valuable:
                    valuable_ids = [c.email_id for c in valuable]
                    try:
                        full_content_map = await self._gmail.batch_get_full_content(
                            credentials=credentials,
                            email_ids=valuable_ids,
                            deep=False,
                        )
                    except Exception as exc:
                        logger.warning(
                            f"⚠️ batch_get_full_content partial failure: {exc}"
                        )

                    # Log final candidates with attachments for inspection
                    for c in valuable:
                        meta = email_meta_map.get(c.email_id)
                        content = full_content_map.get(c.email_id)
                        attachments = content.attachments if content else []
                        logger.info(
                            f"  📧 {c.email_id[:8]} | [{c.category}] {c.fact} "
                            f"| from={meta.from_address if meta else '?'} "
                            f"| attachments={attachments if attachments else '—'}"
                        )

                # 5. Build IndexedEmail objects with embeddings (parallel)
                indexed_emails: List[IndexedEmail] = []
                if valuable:
                    indexed_emails = list(
                        await asyncio.gather(
                            *[
                                self._embed_email(
                                    ec,
                                    email_meta_map,
                                    full_content_map,
                                    job,
                                    account_id,
                                )
                                for ec in valuable
                            ]
                        )
                    )
                    # Filter out None (embed failures that exceeded retry)
                    indexed_emails = [e for e in indexed_emails if e is not None]

                # 6. Save batch (idempotent upserts by email_id)
                saved = 0
                if indexed_emails:
                    saved = await self._email_repo.save_batch(indexed_emails)

                # 7. Track max/min email dates across all pages
                chunk_dates = [e.date for e in emails_page if e.date]
                if chunk_dates:
                    chunk_max = max(chunk_dates)
                    chunk_min = min(chunk_dates)
                    if job_latest_date is None or chunk_max > job_latest_date:
                        job_latest_date = chunk_max
                    if job_oldest_date is None or chunk_min < job_oldest_date:
                        job_oldest_date = chunk_min

                # 8. Update job progress (persisted — Cloud Tasks resume point)
                job.emails_fetched += len(emails_page)
                job.emails_stored += saved
                job.next_page_token = next_page_token
                job.updated_at = datetime.utcnow()
                await self._job_repo.update_job(
                    job.job_id,
                    {
                        "emails_fetched": job.emails_fetched,
                        "emails_stored": job.emails_stored,
                        "emails_failed": job.emails_failed,
                        "embedding_pending": job.embedding_pending,
                        "next_page_token": next_page_token,
                        "updated_at": job.updated_at,
                    },
                )

                # 9. Advance indexing cursor only after successful batch write.
                #    Mode controls which cursor(s) move and in which direction.
                await self._advance_cursor(
                    job=job,
                    mode=mode,
                    existing_state=existing_state,
                    job_latest_date=job_latest_date,
                    job_oldest_date=job_oldest_date,
                )

                logger.info(
                    f"✅ Chunk done: fetched={len(emails_page)} "
                    f"valuable={len(valuable)} saved={saved} "
                    f"next={'yes' if next_page_token else 'no'}"
                )

                pages_processed += 1
                if not next_page_token:
                    break
                if max_pages is not None and pages_processed >= max_pages:
                    logger.info(f"📧 Reached max_pages={max_pages}, stopping (resume_token saved)")
                    break
                page_token = next_page_token

            # Mark job completed
            job.status = "completed"
            job.completed_at = datetime.utcnow()
            job.updated_at = job.completed_at
            await self._job_repo.update_job(
                job.job_id,
                {
                    "status": "completed",
                    "completed_at": job.completed_at,
                    "updated_at": job.updated_at,
                },
            )
            logger.info(
                f"🎉 Job {job.job_id[:8]} completed: "
                f"fetched={job.emails_fetched} stored={job.emails_stored}"
            )

        except Exception as exc:
            error_msg = str(exc).lower()
            is_auth = any(
                k in error_msg for k in ("auth", "credentials", "token", "401", "403")
            )
            job.status = "failed_auth" if is_auth else "failed"
            job.updated_at = datetime.utcnow()
            await self._job_repo.update_job(
                job.job_id,
                {"status": job.status, "updated_at": job.updated_at},
            )
            logger.error(
                f"💥 Job {job.job_id[:8]} {job.status}: {exc}"
            )
            raise

        return job

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _advance_cursor(
        self,
        job: IndexingJob,
        mode: str,
        existing_state: Optional[IndexingState],
        job_latest_date: Optional[datetime],
        job_oldest_date: Optional[datetime],
    ) -> None:
        """
        Write updated IndexingState after a successful chunk.

        Mode rules:
          incremental: indexed_through moves forward; oldest_indexed_through preserved
          reindex:     both cursors updated (full re-process from scratch)
          backfill:    oldest_indexed_through moves backward; indexed_through preserved
        """
        if mode == "backfill":
            new_indexed_through = existing_state.indexed_through if existing_state else None
            new_oldest = job_oldest_date  # moves backward as we process older pages
        elif mode == "reindex":
            new_indexed_through = job_latest_date
            new_oldest = job_oldest_date
        else:  # incremental
            new_indexed_through = job_latest_date
            new_oldest = existing_state.oldest_indexed_through if existing_state else None

        if new_indexed_through is not None or new_oldest is not None:
            await self._email_repo.update_indexing_state(
                IndexingState(
                    user_id=job.user_id,
                    provider=job.provider,
                    indexed_through=new_indexed_through,
                    oldest_indexed_through=new_oldest,
                )
            )

    @staticmethod
    def _apply_exclusions(
        emails: List[EmailMetadata],
        exclusions: List[EmailExclusion],
    ) -> List[EmailMetadata]:
        """Fast O(emails × exclusions) pre-filter — no LLM cost."""
        if not exclusions:
            return emails

        filtered = []
        for email in emails:
            from_lower = email.from_address.lower()
            subject_lower = email.subject.lower()
            excluded = False

            for ex in exclusions:
                p = ex.pattern.lower()
                if ex.pattern_type == "sender_email":
                    if p in from_lower:
                        excluded = True
                elif ex.pattern_type == "sender_domain":
                    if ("@" + p) in from_lower or from_lower.endswith(p):
                        excluded = True
                elif ex.pattern_type == "subject_pattern":
                    if p in subject_lower:
                        excluded = True
                if excluded:
                    break

            if not excluded:
                filtered.append(email)

        return filtered

    async def _embed_email(
        self,
        ec: EmailClassificationResult,
        email_meta_map: Dict[str, EmailMetadata],
        full_content_map: Dict[str, EmailFullContent],
        job: IndexingJob,
        account_id: str,
    ) -> Optional[IndexedEmail]:
        """Generate 4-vector embeddings and build IndexedEmail. Parallel per email."""
        meta = email_meta_map.get(ec.email_id)
        content = full_content_map.get(ec.email_id)
        attachments = content.attachments if content else []

        tags_text = " ".join(ec.tags) if ec.tags else (ec.fact or "")
        meta_text = " ".join(
            filter(
                None,
                [
                    meta.subject if meta else "",
                    meta.from_address if meta else "",
                    meta.date.strftime("%Y-%m") if meta else "",
                    ec.fact or "",
                ],
            )
        )

        vector: Optional[List[float]] = None
        tags_vector: Optional[List[float]] = None
        metadata_vector: Optional[List[float]] = None
        attachments_vector: Optional[List[float]] = None
        embedding_pending = False

        try:
            # 3 vectors in one batch call (efficient: ~5s vs ~15s sequential)
            batch_texts = [ec.fact or "", tags_text, meta_text]
            vectors = await self._embedding.get_embeddings_batch(
                batch_texts, "RETRIEVAL_DOCUMENT"
            )
            vector, tags_vector, metadata_vector = (
                vectors[0],
                vectors[1],
                vectors[2],
            )

            # 4th vector: attachment filenames (separate call, skipped if no attachments)
            if attachments:
                attach_text = " ".join(attachments)
                attachments_vector = await self._embedding.get_embedding(
                    attach_text, "RETRIEVAL_DOCUMENT"
                )

        except Exception as exc:
            logger.error(
                f"💥 Embedding failed for {ec.email_id}: {exc} — "
                f"will be repaired by EmailEmbeddingRepairService"
            )
            embedding_pending = True
            job.embedding_pending += 1

        return IndexedEmail(
            email_id=ec.email_id,
            user_id=job.user_id,
            account_id=account_id,
            source=job.provider,
            text=ec.fact or "",
            vector=vector,
            tags_vector=tags_vector,
            metadata_vector=metadata_vector,
            attachments_vector=attachments_vector,
            tags=ec.tags,
            category=ec.category or "personal",
            metadata={
                "subject": meta.subject if meta else "",
                "from_address": meta.from_address if meta else "",
                "snippet": meta.snippet if meta else "",
                "labels": meta.labels if meta else [],
            },
            subject=meta.subject if meta else "",
            from_address=meta.from_address if meta else "",
            email_date=meta.date if meta else datetime.utcnow(),
            attachments=attachments,
            state="current",
            indexed_at=datetime.utcnow(),
            embedding_pending=embedding_pending,
        )
