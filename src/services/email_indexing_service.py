"""
EmailIndexingService — orchestrates the full email indexing pipeline.
See docs/10_rfcs/GMAIL_EMAIL_INDEXING_RFC.md §6, §2.2 Flow 1.

Pipeline per chunk (100 emails/page):
  list_emails → exclusion pre-filter → classify → fetch full content
  → embed (4 vectors) → save → advance cursor → update job
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

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
from ..ports.oauth_credentials_port import OAuthCredentialsPort
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
        oauth: Optional[OAuthCredentialsPort],
    ):
        self._gmail = gmail
        self._email_repo = email_repo
        self._job_repo = job_repo
        self._exclusions_repo = exclusions_repo
        self._classifier = classifier
        self._embedding = embedding
        self._oauth = oauth
        logger.info("📧 EmailIndexingService initialized")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_job(
        self,
        user_id: str,
        provider: str,
        triggered_by: str,
        mode: str = "incremental",
        account_id: str = "",
        resume_token: Optional[str] = None,
        backfill_until: Optional[datetime] = None,
    ) -> IndexingJob:
        """Build a new IndexingJob (caller must persist via job_repo.create_job)."""
        now = datetime.now(timezone.utc)
        return IndexingJob(
            job_id=str(uuid.uuid4()),
            user_id=user_id,
            account_id=account_id,
            provider=provider,
            triggered_by=triggered_by,
            status="running",
            mode=mode,
            next_page_token=resume_token,
            backfill_until=backfill_until,
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
        job = self.create_job(
            user_id=user_id,
            provider=provider,
            triggered_by=triggered_by,
            mode=mode,
            account_id=account_id,
            backfill_until=backfill_until,
        )
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

    # ------------------------------------------------------------------
    # Worker-facing methods (called by WorkerHandler via Cloud Tasks)
    # ------------------------------------------------------------------

    async def start_indexing_for_eligible_users(
        self, user_repo: Any, now_utc: datetime
    ) -> Tuple[List[str], int, int]:
        """
        Fan-out: create and persist indexing jobs for all eligible Gmail users.

        Eligibility criteria:
          - gmail_auto_index enabled in user config
          - current local hour matches gmail_auto_index_hour
          - no indexing job already running
          - OAuth credentials present

        Returns (job_ids_to_enqueue, started, skipped). Caller enqueues the jobs.
        Requires oauth injected in constructor.
        """
        if not self._oauth:
            logger.warning("[EmailIndexing] start_indexing_for_eligible_users: oauth not configured")
            return [], 0, 0

        user_ids = await self._oauth.list_users_by_provider("gmail")
        job_ids: List[str] = []
        started = skipped = 0

        for user_id in user_ids:
            profile = await user_repo.get_user(user_id)
            if not profile:
                skipped += 1
                continue

            cfg = profile.config
            if not cfg.gmail_auto_index:
                skipped += 1
                continue

            user_tz = ZoneInfo(cfg.timezone or "UTC")
            local_hour = now_utc.astimezone(user_tz).hour
            if local_hour != cfg.gmail_auto_index_hour:
                skipped += 1
                continue

            latest_job = await self._job_repo.get_latest_job(user_id, "gmail")
            if latest_job and latest_job.status == "running":
                logger.info(
                    "[EmailIndexing] job already running for %s, skipping", user_id[:8]
                )
                skipped += 1
                continue

            creds = await self._oauth.get_credentials(user_id, "gmail")
            if not creds:
                logger.warning(
                    "[EmailIndexing] no credentials for %s", user_id[:8]
                )
                skipped += 1
                continue

            job = self.create_job(
                user_id=user_id,
                provider="gmail",
                triggered_by="scheduler",
                mode="incremental",
                account_id=profile.account_id,
            )
            await self._job_repo.create_job(job)
            job_ids.append(job.job_id)
            logger.info(
                "[EmailIndexing] created job %s for %s", job.job_id[:8], user_id[:8]
            )
            started += 1

        logger.info(
            "[EmailIndexing] start_indexing_for_eligible_users: started=%d, skipped=%d",
            started, skipped,
        )
        return job_ids, started, skipped

    async def load_job_for_execution(
        self, job_id: str
    ) -> Tuple[Optional[IndexingJob], Optional[OAuthCredentials], Optional[str]]:
        """
        Load a job and its OAuth credentials for page execution.

        Returns (job, creds, None) if ready to execute.
        Returns (None, None, skip_reason) if the job should be skipped:
          - "not_found"    — job does not exist
          - job.status     — job is not 'running' (e.g. "completed", "failed")
          - "failed_auth"  — credentials missing; job status updated to 'failed_auth'
          - "no_oauth"     — oauth port not injected

        Requires oauth injected in constructor.
        """
        if not self._oauth:
            logger.warning("[EmailIndexing] load_job_for_execution: oauth not configured")
            return None, None, "no_oauth"

        job = await self._job_repo.get_job(job_id)
        if not job:
            logger.warning("[EmailIndexing] load_job_for_execution: job %s not found", job_id[:8])
            return None, None, "not_found"
        if job.status != "running":
            logger.info(
                "[EmailIndexing] job %s is %s, skipping", job_id[:8], job.status
            )
            return None, None, job.status  # actual status: "completed", "failed", etc.

        creds = await self._oauth.get_credentials(job.user_id, job.provider)
        if not creds:
            await self._job_repo.update_job(
                job_id, {"status": "failed_auth", "updated_at": datetime.now(timezone.utc)}
            )
            return None, None, "failed_auth"

        return job, creds, None

    async def mark_stale_jobs_failed(self, stale_threshold: datetime) -> int:
        """
        Mark all 'running' jobs older than stale_threshold as 'failed'.
        Returns the count of jobs marked.
        """
        stale_jobs = await self._job_repo.get_stale_running_jobs(stale_threshold)
        marked = 0
        for stale_job in stale_jobs:
            await self._job_repo.update_job(stale_job.job_id, {
                "status": "failed",
                "updated_at": datetime.now(timezone.utc),
            })
            marked += 1
            logger.warning(
                "[EmailIndexing] Marked stale job %s as failed", stale_job.job_id[:8]
            )
        return marked

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
        page_size: int = 150,
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
            # indexed_through is owned by incremental only — never clear it.
            if date_from is None:
                date_from = datetime.now(timezone.utc) - timedelta(days=3 * 365)
            logger.info(
                f"📧 Job {job.job_id[:8]} [reindex]: from={date_from.strftime('%Y-%m-%d')}"
            )

        elif mode == "backfill":
            if backfill_until is not None:
                date_from = backfill_until
            elif date_from is None:
                date_from = datetime.now(timezone.utc) - timedelta(days=5 * 365)
            logger.info(
                f"📧 Job {job.job_id[:8]} [backfill]: from={date_from.strftime('%Y-%m-%d')}"
            )

        else:  # incremental (default)
            if date_from is None:
                if existing_state and existing_state.indexed_through:
                    date_from = existing_state.indexed_through
                    logger.info(
                        f"📧 Job {job.job_id[:8]} [incremental]: "
                        f"cursor_max={date_from.strftime('%Y-%m-%d %H:%M')}"
                    )
                else:
                    # cursor_max is null — bootstrap from max(cursor_backfill, cursor_reindex)
                    candidates = [
                        d for d in [
                            existing_state.oldest_indexed_through if existing_state else None,
                            existing_state.cursor_reindex if existing_state else None,
                        ]
                        if d is not None
                    ]
                    if candidates:
                        date_from = max(candidates)
                        logger.info(
                            f"📧 Job {job.job_id[:8]} [incremental]: no cursor_max, "
                            f"bootstrapping from confirmed cursors → date_from={date_from.strftime('%Y-%m-%d')}"
                        )
                    else:
                        now = datetime.now(timezone.utc)
                        date_from = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
                        logger.info(
                            f"📧 Job {job.job_id[:8]} [incremental]: no cursors, "
                            f"today only → date_from={date_from.strftime('%Y-%m-%d')}"
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
        pages_processed = 0

        try:
            while True:
                # Refresh token if expired before each page
                if credentials.token_expiry <= datetime.now(timezone.utc):
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

                # Boundary guard: each Cloud Task worker independently reads the lower-bound
                # cursor (indexed_through / backfill_until / reindex start); the one whose
                # page dips below date_from is terminal.  next_page_token is nulled so
                # finalization runs and re-enqueue is skipped.
                # Deduplication handles any overlap — no emails are dropped.
                if date_from is not None:
                    page_dates = [e.date for e in emails_page if e.date]
                    if page_dates and min(page_dates) < date_from:
                        logger.info(
                            f"📧 Job {job.job_id[:8]} [{mode}]: boundary reached "
                            f"(oldest_in_page={min(page_dates).strftime('%Y-%m-%d')} < "
                            f"date_from={date_from.strftime('%Y-%m-%d')}) — terminal page"
                        )
                        next_page_token = None

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

                # 7. Track max/min email dates across all Cloud Tasks invocations.
                #    Persisted to job doc — cursors in IndexingState written ONLY at completion.
                chunk_dates = [e.date for e in emails_page if e.date]
                if chunk_dates:
                    chunk_max = max(chunk_dates)
                    chunk_min = min(chunk_dates)
                    if job.max_email_date is None or chunk_max > job.max_email_date:
                        job.max_email_date = chunk_max
                    if job.min_email_date is None or chunk_min < job.min_email_date:
                        job.min_email_date = chunk_min

                # 8. Update job progress (persisted — Cloud Tasks resume point)
                job.emails_fetched += len(emails_page)
                job.emails_stored += saved
                job.next_page_token = next_page_token
                job.updated_at = datetime.now(timezone.utc)
                await self._job_repo.update_job(
                    job.job_id,
                    {
                        "emails_fetched": job.emails_fetched,
                        "emails_stored": job.emails_stored,
                        "emails_failed": job.emails_failed,
                        "embedding_pending": job.embedding_pending,
                        "next_page_token": next_page_token,
                        "updated_at": job.updated_at,
                        "max_email_date": job.max_email_date,
                        "min_email_date": job.min_email_date,
                    },
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

            if job.next_page_token:
                # More pages remain — status stays "running"; worker will re-enqueue.
                logger.info(
                    f"📧 Job {job.job_id[:8]} page done: "
                    f"fetched={job.emails_fetched} stored={job.emails_stored} "
                    f"— next_page_token set, re-enqueue pending"
                )
            else:
                # All pages consumed — finalize cursor THEN mark completed.
                await self._finalize_cursor(job=job, mode=mode, date_from=date_from)
                job.status = "completed"
                job.completed_at = datetime.now(timezone.utc)
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
            job.updated_at = datetime.now(timezone.utc)
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

    async def _finalize_cursor(
        self,
        job: IndexingJob,
        mode: str,
        date_from: Optional[datetime],
    ) -> None:
        """
        Write cursor fields only after job reaches completed status.
        Each mode writes only its own cursor; others are preserved.

        backfill:    oldest_indexed_through = date_from (lower bound queried)
        reindex:     cursor_reindex         = date_from (lower bound queried, ~now-3yr)
        incremental: indexed_through        = max(current_db, job.max_email_date)
        """
        current_state = await self._email_repo.get_indexing_state(job.user_id, job.provider)
        existing_indexed_through = current_state.indexed_through if current_state else None
        existing_oldest = current_state.oldest_indexed_through if current_state else None
        existing_reindex = current_state.cursor_reindex if current_state else None

        if mode == "backfill":
            if date_from is None:
                return
            new_state = IndexingState(
                user_id=job.user_id,
                provider=job.provider,
                indexed_through=existing_indexed_through,
                oldest_indexed_through=date_from,
                cursor_reindex=existing_reindex,
            )
            logger.info(
                f"📌 Job {job.job_id[:8]} [backfill] cursor finalized: "
                f"oldest_indexed_through={date_from.strftime('%Y-%m-%d')}"
            )

        elif mode == "reindex":
            if date_from is None:
                return
            new_state = IndexingState(
                user_id=job.user_id,
                provider=job.provider,
                indexed_through=existing_indexed_through,
                oldest_indexed_through=existing_oldest,
                cursor_reindex=date_from,
            )
            logger.info(
                f"📌 Job {job.job_id[:8]} [reindex] cursor finalized: "
                f"cursor_reindex={date_from.strftime('%Y-%m-%d')}"
            )

        else:  # incremental
            if job.max_email_date is None:
                return
            candidates = [d for d in [existing_indexed_through, job.max_email_date] if d is not None]
            new_indexed_through = max(candidates) if candidates else None
            if new_indexed_through is None:
                return
            new_state = IndexingState(
                user_id=job.user_id,
                provider=job.provider,
                indexed_through=new_indexed_through,
                oldest_indexed_through=existing_oldest,
                cursor_reindex=existing_reindex,
            )
            logger.info(
                f"📌 Job {job.job_id[:8]} [incremental] cursor finalized: "
                f"indexed_through={new_indexed_through.strftime('%Y-%m-%d')}"
            )

        await self._email_repo.update_indexing_state(new_state)

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
            email_date=meta.date if meta else datetime.now(timezone.utc),
            attachments=attachments,
            state="current",
            indexed_at=datetime.now(timezone.utc),
            embedding_pending=embedding_pending,
        )
