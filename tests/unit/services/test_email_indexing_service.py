"""
Unit tests for EmailIndexingService.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.email import (
    EmailClassificationResult,
    EmailExclusion,
    EmailFullContent,
    EmailMetadata,
    IndexingJob,
    OAuthCredentials,
)
from src.ports.email_classifier_port import EmailClassifierPort
from src.ports.email_exclusions_port import EmailExclusionsPort
from src.ports.email_indexing_job_repository import EmailIndexingJobRepository
from src.ports.email_provider_port import EmailProviderPort
from src.ports.embedding_service import EmbeddingService
from src.ports.indexed_email_repository import IndexedEmailRepository
from src.services.email_indexing_service import EmailIndexingService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_gmail():
    m = AsyncMock(spec=EmailProviderPort)
    m.list_emails.return_value = ([], None)
    m.batch_get_full_content.return_value = {}
    return m


@pytest.fixture
def mock_email_repo():
    m = AsyncMock(spec=IndexedEmailRepository)
    m.get_indexing_state.return_value = None
    m.save_batch.return_value = 0
    return m


@pytest.fixture
def mock_job_repo():
    return AsyncMock(spec=EmailIndexingJobRepository)


@pytest.fixture
def mock_exclusions_repo():
    m = AsyncMock(spec=EmailExclusionsPort)
    m.get_exclusions.return_value = []
    return m


@pytest.fixture
def mock_classifier():
    return AsyncMock(spec=EmailClassifierPort)


@pytest.fixture
def mock_embedding():
    m = AsyncMock(spec=EmbeddingService)
    m.get_embeddings_batch.return_value = [[0.1] * 768, [0.2] * 768, [0.3] * 768]
    m.get_embedding.return_value = [0.4] * 768
    return m


@pytest.fixture
def service(
    mock_gmail,
    mock_email_repo,
    mock_job_repo,
    mock_exclusions_repo,
    mock_classifier,
    mock_embedding,
):
    return EmailIndexingService(
        gmail=mock_gmail,
        email_repo=mock_email_repo,
        job_repo=mock_job_repo,
        exclusions_repo=mock_exclusions_repo,
        classifier=mock_classifier,
        embedding=mock_embedding,
        oauth=None,
    )


def _make_job(next_page_token=None):
    now = datetime.now(timezone.utc)
    return IndexingJob(
        job_id="job-123",
        user_id="user-abc",
        provider="gmail",
        triggered_by="test",
        status="running",
        next_page_token=next_page_token,
        started_at=now,
        updated_at=now,
    )


def _make_credentials():
    return OAuthCredentials(
        user_id="user-abc",
        provider="gmail",
        access_token="tok",
        refresh_token="rtok",
        token_expiry=datetime(2099, 1, 1, tzinfo=timezone.utc),  # Far future — no refresh needed
        scopes=["gmail.readonly"],
        email_address="test@example.com",
    )


def _make_meta(email_id: str) -> EmailMetadata:
    return EmailMetadata(
        email_id=email_id,
        provider="gmail",
        subject="Booking confirmed",
        from_address="noreply@ryanair.com",
        date=datetime.now(timezone.utc),
        labels=["INBOX"],
        snippet="Your flight booking is confirmed",
    )


def _make_classification(email_id: str, valuable: bool = True) -> EmailClassificationResult:
    return EmailClassificationResult(
        email_id=email_id,
        valuable=valuable,
        category="travel" if valuable else None,
        fact="User booked flight BCN-KBP" if valuable else None,
        tags=["flight", "ryanair"] if valuable else [],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestEmailIndexingServiceApplyExclusions:

    def test_no_exclusions(self, service):
        emails = [_make_meta("e1"), _make_meta("e2")]
        result = service._apply_exclusions(emails, [])
        assert result == emails

    def test_sender_email_exclusion(self, service):
        emails = [_make_meta("e1")]
        emails[0] = EmailMetadata(
            email_id="e1", provider="gmail", subject="Sale!",
            from_address="promo@marketing.com", date=datetime.now(timezone.utc),
            labels=[], snippet=""
        )
        exclusion = EmailExclusion(
            user_id="u",
            pattern_type="sender_email",
            pattern="marketing.com",
            reason="spam",
            created_at=datetime.now(timezone.utc),
        )
        result = service._apply_exclusions(emails, [exclusion])
        assert result == []

    def test_sender_domain_exclusion(self, service):
        emails = [
            EmailMetadata(
                email_id="e1", provider="gmail", subject="News",
                from_address="noreply@linkedin.com", date=datetime.now(timezone.utc),
                labels=[], snippet=""
            )
        ]
        exclusion = EmailExclusion(
            user_id="u",
            pattern_type="sender_domain",
            pattern="linkedin.com",
            reason="social",
            created_at=datetime.now(timezone.utc),
        )
        result = service._apply_exclusions(emails, [exclusion])
        assert result == []

    def test_subject_pattern_exclusion(self, service):
        emails = [
            EmailMetadata(
                email_id="e1", provider="gmail", subject="Flash Sale - 50% off!",
                from_address="shop@store.com", date=datetime.now(timezone.utc),
                labels=[], snippet=""
            )
        ]
        exclusion = EmailExclusion(
            user_id="u",
            pattern_type="subject_pattern",
            pattern="flash sale",
            reason="marketing",
            created_at=datetime.now(timezone.utc),
        )
        result = service._apply_exclusions(emails, [exclusion])
        assert result == []

    def test_non_matching_exclusion_passes(self, service):
        emails = [_make_meta("e1")]
        exclusion = EmailExclusion(
            user_id="u",
            pattern_type="sender_email",
            pattern="spam@spam.com",
            reason="spam",
            created_at=datetime.now(timezone.utc),
        )
        result = service._apply_exclusions(emails, [exclusion])
        assert len(result) == 1


class TestEmailIndexingServiceRunJob:

    async def test_empty_mailbox_completes_job(
        self, service, mock_gmail, mock_job_repo
    ):
        """No emails → job completes successfully."""
        mock_gmail.list_emails.return_value = ([], None)
        job = _make_job()
        creds = _make_credentials()

        await service.run_indexing_job(job, creds, "account-abc")

        mock_job_repo.update_job.assert_called()
        final_call = mock_job_repo.update_job.call_args_list[-1]
        assert final_call.args[1].get("status") == "completed"

    async def test_happy_path_fetches_and_stores(
        self,
        service,
        mock_gmail,
        mock_email_repo,
        mock_classifier,
        mock_job_repo,
    ):
        """2 emails → 1 valuable → 1 stored."""
        emails = [_make_meta("e1"), _make_meta("e2")]
        mock_gmail.list_emails.return_value = (emails, None)  # No next page
        mock_gmail.batch_get_full_content.return_value = {
            "e1": EmailFullContent(
                email_id="e1", body_text="Booking confirmed",
                body_html=None, attachments=[], attachment_binaries={}
            )
        }
        mock_classifier.classify_batch.return_value = [
            _make_classification("e1", valuable=True),
            _make_classification("e2", valuable=False),
        ]
        mock_email_repo.save_batch.return_value = 1

        job = _make_job()
        await service.run_indexing_job(job, _make_credentials(), "account-abc")

        assert job.emails_fetched == 2
        assert job.emails_stored == 1
        mock_email_repo.save_batch.assert_called_once()
        saved_emails = mock_email_repo.save_batch.call_args[0][0]
        assert len(saved_emails) == 1  # only the valuable email

    async def test_resumes_from_page_token(
        self, service, mock_gmail, mock_classifier, mock_job_repo
    ):
        """Job with existing next_page_token resumes from that page."""
        mock_gmail.list_emails.return_value = ([], None)
        mock_classifier.classify_batch.return_value = []

        job = _make_job(next_page_token="tok_abc")
        await service.run_indexing_job(job, _make_credentials(), "account-abc")

        call_kwargs = mock_gmail.list_emails.call_args.kwargs
        assert call_kwargs.get("page_token") == "tok_abc"

    async def test_stops_after_last_page(
        self, service, mock_gmail, mock_classifier, mock_email_repo, mock_job_repo
    ):
        """Two pages: first returns next_page_token, second doesn't → stops."""
        emails = [_make_meta("e1")]
        mock_gmail.list_emails.side_effect = [
            (emails, "page2"),
            (emails, None),
        ]
        mock_classifier.classify_batch.return_value = [
            _make_classification("e1", valuable=False)
        ]

        job = _make_job()
        await service.run_indexing_job(job, _make_credentials(), "account-abc")

        assert mock_gmail.list_emails.call_count == 2
        assert job.emails_fetched == 2

    async def test_failed_auth_status_on_auth_error(
        self, service, mock_gmail, mock_job_repo
    ):
        """Auth exception → job status = failed_auth."""
        mock_gmail.list_emails.side_effect = Exception("401 credentials invalid")

        job = _make_job()
        with pytest.raises(Exception):
            await service.run_indexing_job(job, _make_credentials(), "account-abc")

        update_calls = mock_job_repo.update_job.call_args_list
        last = update_calls[-1].args[1]
        assert last["status"] == "failed_auth"

    async def test_create_job_sets_correct_fields(self, service):
        """create_job returns an IndexingJob with expected defaults."""
        job = service.create_job("user-x", "gmail", "cabinet")
        assert job.user_id == "user-x"
        assert job.provider == "gmail"
        assert job.triggered_by == "cabinet"
        assert job.status == "running"
        assert job.emails_fetched == 0
        assert job.emails_stored == 0
