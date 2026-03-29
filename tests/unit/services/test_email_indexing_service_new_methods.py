"""
Unit tests for EmailIndexingService — three methods added in REQ-ARCH-25 refactor:
  - load_job_for_execution
  - start_indexing_for_eligible_users
  - mark_stale_jobs_failed
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.email import IndexingJob, OAuthCredentials
from src.ports.email_classifier_port import EmailClassifierPort
from src.ports.email_exclusions_port import EmailExclusionsPort
from src.ports.email_indexing_job_repository import EmailIndexingJobRepository
from src.ports.email_provider_port import EmailProviderPort
from src.ports.embedding_service import EmbeddingService
from src.ports.indexed_email_repository import IndexedEmailRepository
from src.ports.oauth_credentials_port import OAuthCredentialsPort
from src.services.email_indexing_service import EmailIndexingService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USER_ID = "user-abc"
_ACCOUNT_ID = "acc-abc"
_JOB_ID = "job-0001"
_NOW = datetime(2026, 3, 15, 10, 0, 0)


def _make_job(
    *,
    job_id: str = _JOB_ID,
    user_id: str = _USER_ID,
    status: str = "running",
    updated_at: datetime = None,
) -> IndexingJob:
    return IndexingJob(
        job_id=job_id,
        user_id=user_id,
        account_id=_ACCOUNT_ID,
        provider="gmail",
        triggered_by="cabinet",
        status=status,
        started_at=_NOW,
        updated_at=updated_at or _NOW,
    )


def _make_creds() -> OAuthCredentials:
    return OAuthCredentials(
        user_id=_USER_ID,
        provider="gmail",
        access_token="tok",
        refresh_token="rtok",
        token_expiry=datetime(2099, 1, 1),
        scopes=["gmail.readonly"],
        email_address="test@example.com",
    )


def _make_user_profile(*, gmail_auto_index: bool = True, hour: int = 10, timezone: str = "UTC"):
    profile = MagicMock()
    profile.account_id = _ACCOUNT_ID
    profile.config.gmail_auto_index = gmail_auto_index
    profile.config.gmail_auto_index_hour = hour
    profile.config.timezone = timezone
    return profile


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_job_repo():
    m = AsyncMock(spec=EmailIndexingJobRepository)
    m.get_job.return_value = _make_job()
    m.get_latest_job.return_value = None
    m.get_stale_running_jobs.return_value = []
    m.update_job.return_value = None
    m.create_job.return_value = None
    return m


@pytest.fixture
def mock_oauth():
    m = AsyncMock(spec=OAuthCredentialsPort)
    m.get_credentials.return_value = _make_creds()
    m.list_users_by_provider.return_value = []
    return m


@pytest.fixture
def service(mock_job_repo, mock_oauth):
    return EmailIndexingService(
        gmail=AsyncMock(spec=EmailProviderPort),
        email_repo=AsyncMock(spec=IndexedEmailRepository),
        job_repo=mock_job_repo,
        exclusions_repo=AsyncMock(spec=EmailExclusionsPort),
        classifier=AsyncMock(spec=EmailClassifierPort),
        embedding=AsyncMock(spec=EmbeddingService),
        oauth=mock_oauth,
    )


@pytest.fixture
def service_no_oauth(mock_job_repo):
    """Service without oauth injected — load_job_for_execution returns no_oauth."""
    return EmailIndexingService(
        gmail=AsyncMock(spec=EmailProviderPort),
        email_repo=AsyncMock(spec=IndexedEmailRepository),
        job_repo=mock_job_repo,
        exclusions_repo=AsyncMock(spec=EmailExclusionsPort),
        classifier=AsyncMock(spec=EmailClassifierPort),
        embedding=AsyncMock(spec=EmbeddingService),
        oauth=None,
    )


# ===========================================================================
# load_job_for_execution
# ===========================================================================

class TestLoadJobForExecution:

    async def test_returns_job_and_creds_when_all_ok(self, service, mock_job_repo, mock_oauth):
        job = _make_job(status="running")
        mock_job_repo.get_job.return_value = job
        mock_oauth.get_credentials.return_value = _make_creds()

        result_job, result_creds, skip_reason = await service.load_job_for_execution(_JOB_ID)

        assert result_job == job
        assert result_creds is not None
        assert skip_reason is None

    async def test_no_oauth_returns_no_oauth(self, service_no_oauth):
        job, creds, reason = await service_no_oauth.load_job_for_execution(_JOB_ID)

        assert job is None
        assert creds is None
        assert reason == "no_oauth"

    async def test_job_not_found_returns_not_found(self, service, mock_job_repo):
        mock_job_repo.get_job.return_value = None

        job, creds, reason = await service.load_job_for_execution(_JOB_ID)

        assert reason == "not_found"
        assert job is None

    async def test_completed_job_returns_status_string(self, service, mock_job_repo):
        mock_job_repo.get_job.return_value = _make_job(status="completed")

        job, creds, reason = await service.load_job_for_execution(_JOB_ID)

        assert reason == "completed"
        assert job is None

    async def test_failed_job_returns_failed_string(self, service, mock_job_repo):
        mock_job_repo.get_job.return_value = _make_job(status="failed")

        job, creds, reason = await service.load_job_for_execution(_JOB_ID)

        assert reason == "failed"

    async def test_missing_creds_returns_failed_auth(self, service, mock_job_repo, mock_oauth):
        mock_job_repo.get_job.return_value = _make_job(status="running")
        mock_oauth.get_credentials.return_value = None

        job, creds, reason = await service.load_job_for_execution(_JOB_ID)

        assert reason == "failed_auth"
        assert job is None

    async def test_missing_creds_updates_job_status(self, service, mock_job_repo, mock_oauth):
        mock_job_repo.get_job.return_value = _make_job(status="running")
        mock_oauth.get_credentials.return_value = None

        await service.load_job_for_execution(_JOB_ID)

        mock_job_repo.update_job.assert_called_once()
        call_args = mock_job_repo.update_job.call_args
        assert call_args[0][0] == _JOB_ID
        assert call_args[0][1]["status"] == "failed_auth"

    async def test_creds_fetched_for_correct_user_and_provider(self, service, mock_job_repo, mock_oauth):
        job = _make_job(status="running")
        mock_job_repo.get_job.return_value = job

        await service.load_job_for_execution(_JOB_ID)

        mock_oauth.get_credentials.assert_called_once_with(job.user_id, job.provider)


# ===========================================================================
# mark_stale_jobs_failed
# ===========================================================================

class TestMarkStaleJobsFailed:

    async def test_no_stale_jobs_returns_zero(self, service, mock_job_repo):
        mock_job_repo.get_stale_running_jobs.return_value = []

        count = await service.mark_stale_jobs_failed(_NOW - timedelta(hours=2))

        assert count == 0
        mock_job_repo.update_job.assert_not_called()

    async def test_marks_each_stale_job_as_failed(self, service, mock_job_repo):
        stale = [_make_job(job_id=f"job-{i}") for i in range(3)]
        mock_job_repo.get_stale_running_jobs.return_value = stale

        count = await service.mark_stale_jobs_failed(_NOW - timedelta(hours=2))

        assert count == 3
        assert mock_job_repo.update_job.call_count == 3

    async def test_updates_status_to_failed(self, service, mock_job_repo):
        stale = [_make_job(job_id="stale-001")]
        mock_job_repo.get_stale_running_jobs.return_value = stale

        await service.mark_stale_jobs_failed(_NOW - timedelta(hours=2))

        _, update_payload = mock_job_repo.update_job.call_args[0]
        assert update_payload["status"] == "failed"

    async def test_passes_threshold_to_repo(self, service, mock_job_repo):
        threshold = _NOW - timedelta(hours=2)
        mock_job_repo.get_stale_running_jobs.return_value = []

        await service.mark_stale_jobs_failed(threshold)

        mock_job_repo.get_stale_running_jobs.assert_called_once_with(threshold)


# ===========================================================================
# start_indexing_for_eligible_users
# ===========================================================================

class TestStartIndexingForEligibleUsers:

    async def test_no_oauth_returns_empty(self, service_no_oauth):
        job_ids, started, skipped = await service_no_oauth.start_indexing_for_eligible_users(
            user_repo=MagicMock(), now_utc=_NOW
        )

        assert job_ids == []
        assert started == 0
        assert skipped == 0

    async def test_no_users_returns_empty(self, service, mock_oauth):
        mock_oauth.list_users_by_provider.return_value = []
        user_repo = MagicMock()

        job_ids, started, skipped = await service.start_indexing_for_eligible_users(
            user_repo=user_repo, now_utc=_NOW
        )

        assert started == 0

    async def test_user_not_found_is_skipped(self, service, mock_oauth):
        mock_oauth.list_users_by_provider.return_value = [_USER_ID]
        user_repo = MagicMock()
        user_repo.get_user = AsyncMock(return_value=None)

        _, started, skipped = await service.start_indexing_for_eligible_users(
            user_repo=user_repo, now_utc=_NOW
        )

        assert started == 0
        assert skipped == 1

    async def test_auto_index_disabled_is_skipped(self, service, mock_oauth):
        mock_oauth.list_users_by_provider.return_value = [_USER_ID]
        user_repo = MagicMock()
        user_repo.get_user = AsyncMock(return_value=_make_user_profile(gmail_auto_index=False))

        _, started, skipped = await service.start_indexing_for_eligible_users(
            user_repo=user_repo, now_utc=_NOW
        )

        assert started == 0
        assert skipped == 1

    async def test_wrong_local_hour_is_skipped(self, service, mock_oauth):
        # now_utc=10:00 UTC, user_tz=UTC, trigger_hour=9 → mismatch
        mock_oauth.list_users_by_provider.return_value = [_USER_ID]
        user_repo = MagicMock()
        user_repo.get_user = AsyncMock(
            return_value=_make_user_profile(gmail_auto_index=True, hour=9, timezone="UTC")
        )

        _, started, skipped = await service.start_indexing_for_eligible_users(
            user_repo=user_repo, now_utc=_NOW.replace(tzinfo=timezone.utc)
        )

        assert started == 0
        assert skipped == 1

    async def test_job_already_running_is_skipped(self, service, mock_oauth, mock_job_repo):
        mock_oauth.list_users_by_provider.return_value = [_USER_ID]
        mock_job_repo.get_latest_job.return_value = _make_job(status="running")
        user_repo = MagicMock()
        user_repo.get_user = AsyncMock(
            return_value=_make_user_profile(gmail_auto_index=True, hour=10, timezone="UTC")
        )

        _, started, skipped = await service.start_indexing_for_eligible_users(
            user_repo=user_repo, now_utc=_NOW.replace(tzinfo=timezone.utc)
        )

        assert started == 0
        assert skipped == 1

    async def test_no_credentials_is_skipped(self, service, mock_oauth, mock_job_repo):
        mock_oauth.list_users_by_provider.return_value = [_USER_ID]
        mock_oauth.get_credentials.return_value = None
        mock_job_repo.get_latest_job.return_value = None
        user_repo = MagicMock()
        user_repo.get_user = AsyncMock(
            return_value=_make_user_profile(gmail_auto_index=True, hour=10, timezone="UTC")
        )

        _, started, skipped = await service.start_indexing_for_eligible_users(
            user_repo=user_repo, now_utc=_NOW.replace(tzinfo=timezone.utc)
        )

        assert started == 0
        assert skipped == 1

    async def test_eligible_user_creates_job(self, service, mock_oauth, mock_job_repo):
        mock_oauth.list_users_by_provider.return_value = [_USER_ID]
        mock_oauth.get_credentials.return_value = _make_creds()
        mock_job_repo.get_latest_job.return_value = None
        user_repo = MagicMock()
        user_repo.get_user = AsyncMock(
            return_value=_make_user_profile(gmail_auto_index=True, hour=10, timezone="UTC")
        )

        job_ids, started, skipped = await service.start_indexing_for_eligible_users(
            user_repo=user_repo, now_utc=_NOW.replace(tzinfo=timezone.utc)
        )

        assert started == 1
        assert skipped == 0
        assert len(job_ids) == 1
        mock_job_repo.create_job.assert_called_once()

    async def test_created_job_has_correct_provider_and_mode(self, service, mock_oauth, mock_job_repo):
        mock_oauth.list_users_by_provider.return_value = [_USER_ID]
        mock_oauth.get_credentials.return_value = _make_creds()
        mock_job_repo.get_latest_job.return_value = None
        user_repo = MagicMock()
        user_repo.get_user = AsyncMock(
            return_value=_make_user_profile(gmail_auto_index=True, hour=10, timezone="UTC")
        )

        await service.start_indexing_for_eligible_users(
            user_repo=user_repo, now_utc=_NOW.replace(tzinfo=timezone.utc)
        )

        created_job = mock_job_repo.create_job.call_args[0][0]
        assert created_job.provider == "gmail"
        assert created_job.mode == "incremental"
        assert created_job.triggered_by == "scheduler"
