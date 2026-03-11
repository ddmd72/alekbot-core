"""
FirestoreEmailJobRepository — job journal for resume, retry, and Cabinet history.
See docs/10_rfcs/GMAIL_EMAIL_INDEXING_RFC.md §2.1.2.

Collection: {env}_email_indexing_jobs_v1
Doc ID: job_id (UUID set by caller)
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from google.cloud import firestore
from google.cloud.firestore import FieldFilter

from ..config.environment import EnvironmentConfig
from ..domain.email import IndexingJob
from ..ports.email_indexing_job_repository import EmailIndexingJobRepository
from ..utils.logger import logger


class FirestoreEmailJobRepository(EmailIndexingJobRepository):

    def __init__(self, db_client, env_config: EnvironmentConfig):
        self.db = db_client
        collection_name = env_config.email_indexing_jobs_collection
        self.collection = self.db.collection(collection_name)
        logger.info(
            f"📂 EmailIndexingJob repository initialized. Collection: {collection_name}"
        )

    async def create_job(self, job: IndexingJob) -> None:
        data = job.model_dump()
        await self.collection.document(job.job_id).set(data)
        logger.info(
            f"📋 Job created: {job.job_id} user={job.user_id[:8]} "
            f"provider={job.provider} triggered_by={job.triggered_by}"
        )

    async def update_job(self, job_id: str, updates: Dict[str, Any]) -> None:
        """Partial update called after each successful chunk."""
        # DatetimeWithNanoseconds is a datetime subclass returned by Firestore reads.
        # Writing it back fails in some SDK versions: '_nanosecond' attribute missing.
        # Normalize any datetime subclass to a plain datetime before the write.
        sanitized = {
            k: datetime(v.year, v.month, v.day, v.hour, v.minute, v.second, v.microsecond, v.tzinfo)
            if isinstance(v, datetime) and type(v) is not datetime else v
            for k, v in updates.items()
        }
        await self.collection.document(job_id).update(sanitized)
        logger.debug(f"📋 Job updated: {job_id} fields={list(updates.keys())}")

    # Datetime fields that may carry DatetimeWithNanoseconds from Firestore.
    _DATETIME_FIELDS = (
        "last_email_date", "backfill_until",
        "max_email_date", "min_email_date",
        "started_at", "updated_at", "completed_at",
    )

    @classmethod
    def _to_job(cls, data: dict) -> IndexingJob:
        """Normalize Firestore datetime subclasses to plain naive datetimes."""
        for field in cls._DATETIME_FIELDS:
            v = data.get(field)
            if v is not None and type(v) is not datetime:
                data[field] = datetime(v.year, v.month, v.day, v.hour, v.minute, v.second, v.microsecond)
        return IndexingJob(**data)

    async def get_job(self, job_id: str) -> Optional[IndexingJob]:
        doc = await self.collection.document(job_id).get()
        if not doc.exists:
            return None
        return self._to_job(doc.to_dict())

    async def get_latest_job(
        self, user_id: str, provider: str
    ) -> Optional[IndexingJob]:
        """Last job for user+provider ordered by started_at DESC."""
        query = (
            self.collection
            .where(filter=FieldFilter("user_id", "==", user_id))
            .where(filter=FieldFilter("provider", "==", provider))
            .order_by("started_at", direction=firestore.Query.DESCENDING)
            .limit(1)
        )
        docs = await query.get()
        if not docs:
            return None
        return self._to_job(docs[0].to_dict())

    async def list_jobs(self, user_id: str, limit: int = 10) -> List[IndexingJob]:
        """Last N jobs across all providers, ordered by started_at DESC."""
        query = (
            self.collection
            .where(filter=FieldFilter("user_id", "==", user_id))
            .order_by("started_at", direction=firestore.Query.DESCENDING)
            .limit(limit)
        )
        docs = await query.get()
        return [self._to_job(doc.to_dict()) for doc in docs]

    async def get_stale_running_jobs(self, updated_before: datetime) -> List[IndexingJob]:
        """Return running jobs not updated since updated_before (zombie detection)."""
        query = (
            self.collection
            .where(filter=FieldFilter("status", "==", "running"))
            .where(filter=FieldFilter("updated_at", "<", updated_before))
        )
        docs = await query.get()
        return [self._to_job(doc.to_dict()) for doc in docs]
