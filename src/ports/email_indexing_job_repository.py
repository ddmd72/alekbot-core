"""
EmailIndexingJobRepository — job journal for resume, retry, and Cabinet history.
See docs/10_rfcs/GMAIL_EMAIL_INDEXING_RFC.md §2.1.2.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from src.domain.email import IndexingJob


class EmailIndexingJobRepository(ABC):

    @abstractmethod
    async def create_job(self, job: IndexingJob) -> None:
        """Persist new job record at the start of an indexing run."""

    @abstractmethod
    async def update_job(self, job_id: str, updates: Dict[str, Any]) -> None:
        """
        Partial update called after each successful chunk:
          - next_page_token: current cursor (primary resume point on Cloud Tasks timeout)
          - emails_fetched, emails_stored, emails_failed, embedding_pending: running totals
          - errors: append to list (capped at 100 items)
          - status: updated on terminal transitions (completed/failed)
          - updated_at: always refreshed
        """

    @abstractmethod
    async def get_job(self, job_id: str) -> Optional[IndexingJob]:
        """Fetch a specific job by ID."""

    @abstractmethod
    async def get_latest_job(
        self, user_id: str, provider: str
    ) -> Optional[IndexingJob]:
        """
        Last job for user+provider ordered by started_at DESC.
        Cabinet uses this to show current indexing status and enable Retry.
        """

    @abstractmethod
    async def list_jobs(self, user_id: str, limit: int = 10) -> List[IndexingJob]:
        """
        Last N jobs across all providers, ordered by started_at DESC.
        Displayed in Cabinet job history panel.
        """
