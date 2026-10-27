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

    @abstractmethod
    async def update_job(self, job_id: str, updates: Dict[str, Any]) -> None:
        """
        Partial update called after each successful chunk:
          - next_page_token: current cursor (primary resume point on Cloud Tasks timeout)
          - emails_fetched, emails_stored, emails_failed, embedding_pending: running totals
          - errors: append to list (capped at 100 items)
          - status: updated on terminal transitions (completed/failed)
          - updated_at: always refreshed

        Merges `updates` into the stored job with no read and no precondition: last writer wins per field.
        Errors: a missing job raises NotFound.
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

        Tenancy: never another user's or provider's job. Legacy: a job without started_at is not considered.
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

        Order: none. Limit: none. Covers every user. Atomicity: a read only; the caller's later update does not
        re-check the job (P11-G3-5: a job that completed in between is overwritten as failed).
        """
