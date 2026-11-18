"""
FirestoreEmailJobRepository — job journal for resume, retry, and Cabinet history.
See docs/10_rfcs/GMAIL_EMAIL_INDEXING_RFC.md §2.1.2.

Collection: {env}_email_indexing_jobs_v1
Doc ID: job_id (UUID set by caller)
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from google.api_core.exceptions import AlreadyExists, FailedPrecondition
from google.cloud import firestore
from google.cloud.firestore import FieldFilter

from ..config.environment import EnvironmentConfig
from ..domain.email import IndexingJob
from ..ports.email_indexing_job_repository import EmailIndexingJobRepository
from ..utils.logger import logger


_CONDITIONAL_UPDATE_ATTEMPTS = 3


class FirestoreEmailJobRepository(EmailIndexingJobRepository):
    """Firestore mechanics behind the port's contract.

    Collection `EnvironmentConfig.email_indexing_jobs_collection` (`{prefix}email_indexing_jobs_v1`), document id
    job_id. Composite indexes (config/firestore.indexes.json): (user_id, started_at desc),
    (user_id, provider, started_at desc) and (provider, user_id, started_at desc) for the two newest-first reads,
    (status, updated_at) for the stale query. `create_job_if_absent` is a document `create` (AlreadyExists = False).
    `update_job` is a plain document `update`; with `expected_status` it reads the job and writes under a
    `last_update_time` precondition (re-reading when another writer got in between), so a job whose status moved on is
    left alone; a datetime subclass in `updates` is rewritten as a plain datetime first. Reads rebuild every date field as a naive plain
    datetime (Firestore returns `DatetimeWithNanoseconds`).
    """

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

    async def create_job_if_absent(self, job: IndexingJob) -> bool:
        try:
            await self.collection.document(job.job_id).create(job.model_dump())
        except AlreadyExists:
            logger.info(f"📋 Job {job.job_id} already exists, not created again")
            return False
        logger.info(
            f"📋 Job created: {job.job_id} user={job.user_id[:8]} "
            f"provider={job.provider} triggered_by={job.triggered_by}"
        )
        return True

    async def update_job(
        self, job_id: str, updates: Dict[str, Any], expected_status: Optional[str] = None
    ) -> bool:
        """Partial update called after each successful chunk; with `expected_status`, only while the job has it."""
        # DatetimeWithNanoseconds is a datetime subclass returned by Firestore reads.
        # Writing it back fails in some SDK versions: '_nanosecond' attribute missing.
        # Normalize any datetime subclass to a plain datetime before the write.
        sanitized = {
            k: datetime(v.year, v.month, v.day, v.hour, v.minute, v.second, v.microsecond, v.tzinfo)
            if isinstance(v, datetime) and type(v) is not datetime else v
            for k, v in updates.items()
        }
        ref = self.collection.document(job_id)
        if expected_status is None:
            await ref.update(sanitized)
            logger.debug(f"📋 Job updated: {job_id} fields={list(updates.keys())}")
            return True
        for _ in range(_CONDITIONAL_UPDATE_ATTEMPTS):
            snap = await ref.get()
            if not snap.exists:
                await ref.update(sanitized)  # raises the backend's NotFound, as the unconditional write does
            if (snap.to_dict() or {}).get("status") != expected_status:
                logger.info(f"📋 Job {job_id} is no longer {expected_status}, update skipped")
                return False
            try:
                await ref.update(sanitized, option=self.db.write_option(last_update_time=snap.update_time))
            except FailedPrecondition:
                continue  # another writer got in between: read again and re-check the status
            logger.debug(f"📋 Job updated: {job_id} fields={list(updates.keys())}")
            return True
        logger.warning(f"📋 Job {job_id} kept changing, conditional update given up")
        return False

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
