"""
EmailIndexingJobRepository — job journal for resume, retry, and Cabinet history.
See docs/10_rfcs/GMAIL_EMAIL_INDEXING_RFC.md §2.1.2.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.domain.email import IndexingJob


class EmailIndexingJobRepository(ABC):
    """Job journal of the email indexing runs.

    Holds for every method: datetimes read back naive UTC; backend errors propagate; a stored job that fails
    IndexingJob validation raises (ValidationError, or AttributeError for a date field that is not a timestamp) from
    every reader, none is skipped.
    """

    @abstractmethod
    async def create_job(self, job: IndexingJob) -> None:
        """Persist new job record at the start of an indexing run, under its job_id (full replace).

        Idempotency: a repeat stores the same job.
        """

    async def create_job_if_absent(self, job: IndexingJob) -> bool:
        """Persist `job` under its job_id only when no job has that id; True when this call stored it.

        The scheduler's auto-index uses it with a deterministic id per (user, provider, local date): of two ticks that
        race, one gets True and the other False, so one day starts one job. This default is a read, then a write, and is
        NOT atomic: a backend that can create a document atomically must override it (the Firestore adapter does).
        """
        if await self.get_job(job.job_id) is not None:
            return False
        await self.create_job(job)
        return True

    @abstractmethod
    async def update_job(
        self, job_id: str, updates: Dict[str, Any], expected_status: Optional[str] = None
    ) -> bool:
        """
        Partial update called after each successful chunk:
          - next_page_token: current cursor (primary resume point on Cloud Tasks timeout)
          - emails_fetched, emails_stored, emails_failed: running totals
          - errors: append to list (capped at 100 items)
          - status: updated on terminal transitions (completed/failed)
          - updated_at: always refreshed

        Merges `updates` into the stored job: last writer wins per field. With `expected_status` the write is applied only
        while the stored job still has that status (compare and set, atomic in the backend) and returns False, writing
        nothing, when the job has moved on; without it the write is unconditional and returns True.
        Errors: a missing job raises the backend's not-found error (NotFound today).
        """

    @abstractmethod
    async def get_job(self, job_id: str) -> Optional[IndexingJob]:
        """Fetch a specific job by ID, or None. Tenancy: none; callers compare job.user_id."""

    @abstractmethod
    async def get_latest_job(
        self, user_id: str, provider: str
    ) -> Optional[IndexingJob]:
        """
        Last job for user+provider ordered by started_at DESC.
        Cabinet uses this to show current indexing status and enable Retry.

        Tenancy: never another user's or provider's job. A job without started_at: no writer produces one; behaviour pending the data audit.
        """

    @abstractmethod
    async def list_jobs(self, user_id: str, limit: int = 10) -> List[IndexingJob]:
        """
        Last N jobs across all providers, ordered by started_at DESC.
        Displayed in Cabinet job history panel. No caller in src/.

        Tenancy: only the user's jobs.
        """

    @abstractmethod
    async def get_stale_running_jobs(self, updated_before: datetime) -> List[IndexingJob]:
        """
        Return all jobs with status=running and updated_at older than updated_before.
        Used by the watchdog to detect and mark zombie jobs as failed.

        Order: none. Limit: none. Covers every user. Atomicity: a read only; a caller that fails a job
        from this list passes `expected_status="running"` to `update_job`, so a job that completed in between stays so.
        """
