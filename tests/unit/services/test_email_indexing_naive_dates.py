"""
Email indexing — tz-naive email dates vs. the job's date_from.

The Gmail adapter returns EmailMetadata.date as naive UTC (the stored convention). A user with no
cursors gets date_from = "today 00:00 UTC"; that used to be tz-aware, so the boundary check
`min(page_dates) < date_from` raised "can't compare offset-naive and offset-aware datetimes" and
the job failed every day until a cursor existed (which only a completed job writes).
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.adapters.gmail_provider_adapter import GmailProviderAdapter
from src.domain.email import EmailMetadata, IndexingJob
from src.domain.integration import Integration
from src.domain.oauth_credential import OAuthCredential
from src.domain.provider_account import ProviderAccount
from src.domain.email_dates import to_naive_utc
from src.ports.email_classifier_port import EmailClassifierPort
from src.ports.email_exclusions_port import EmailExclusionsPort
from src.ports.email_indexing_job_repository import EmailIndexingJobRepository
from src.ports.email_provider_port import EmailProviderPort
from src.ports.embedding_service import EmbeddingService
from src.ports.indexed_email_repository import IndexedEmailRepository
from src.services.email_indexing_service import EmailIndexingService
from src.composition.connector_registry import ConnectorRegistry


@pytest.fixture
def gmail():
    m = AsyncMock(spec=EmailProviderPort)
    m.batch_get_full_content.return_value = {}
    return m


@pytest.fixture
def email_repo():
    m = AsyncMock(spec=IndexedEmailRepository)
    m.get_indexing_state.return_value = None  # no cursors → "today only" path
    m.save_batch.return_value = 0
    return m


@pytest.fixture
def job_repo():
    return AsyncMock(spec=EmailIndexingJobRepository)


@pytest.fixture
def service(gmail, email_repo, job_repo):
    exclusions = AsyncMock(spec=EmailExclusionsPort)
    exclusions.get_exclusions.return_value = []
    return EmailIndexingService(
        gmail=gmail,
        email_repo=email_repo,
        job_repo=job_repo,
        exclusions_repo=exclusions,
        classifier=AsyncMock(spec=EmailClassifierPort),
        embedding=AsyncMock(spec=EmbeddingService),
        oauth=None, connector=ConnectorRegistry().get("gmail"),
    )


def _job() -> IndexingJob:
    now = datetime.now(timezone.utc)
    return IndexingJob(
        job_id="job-naive", user_id="user-abc", provider="gmail",
        triggered_by="test", status="running", started_at=now, updated_at=now,
    )


def _creds() -> OAuthCredential:
    return OAuthCredential(
        user_id="user-abc",
        account_id="acct-1",
        kind="gmail",
        integration=Integration.MAIL,
        account=ProviderAccount(realm="google", subject="", label="a@example.com"),
        access_token="t",
        refresh_token="r",
        token_expiry=datetime(2099, 1, 1, tzinfo=timezone.utc),
        scopes=["gmail.readonly"],
    )


def _naive_meta(email_id: str) -> EmailMetadata:
    """Shaped like GmailProviderAdapter._parse_metadata output: naive UTC."""
    return EmailMetadata(
        email_id=email_id, provider="gmail", subject="s", from_address="x@example.net",
        date=datetime.now(timezone.utc).replace(tzinfo=None),
        labels=["INBOX"], snippet="",
    )


class TestNoCursorIncrementalWithNaiveEmailDates:

    async def test_job_completes_instead_of_failing_on_tz_compare(
        self, service, gmail, job_repo
    ):
        gmail.list_emails.return_value = ([_naive_meta("e1")], None)
        job = _job()

        await service.run_indexing_job(job, _creds(), "account-abc")

        assert job.status == "completed"
        assert job_repo.update_job.call_args_list[-1].args[1]["status"] == "completed"

    async def test_job_completes_with_old_naive_page_hitting_boundary(
        self, service, gmail
    ):
        """Page older than date_from → terminal-page branch, which also compares the two."""
        old = EmailMetadata(
            email_id="old", provider="gmail", subject="s", from_address="x@example.net",
            date=datetime(2020, 1, 1), labels=[], snippet="",
        )
        gmail.list_emails.return_value = ([old], "next-token")
        job = _job()

        await service.run_indexing_job(job, _creds(), "account-abc")

        assert job.status == "completed"


class TestAdapterDateFallback:

    def test_unparseable_date_header_yields_naive_utc(self):
        msg = {"id": "m1", "payload": {"headers": [{"name": "Date", "value": "garbage"}]}}

        meta = GmailProviderAdapter._parse_metadata(msg)

        assert meta.date.tzinfo is None

    def test_parsed_date_header_is_naive_utc(self):
        msg = {"id": "m1", "payload": {"headers": [
            {"name": "Date", "value": "Fri, 16 Oct 2026 10:00:00 +0200"}]}}

        meta = GmailProviderAdapter._parse_metadata(msg)

        assert meta.date == datetime(2026, 10, 16, 8, 0, 0)


class TestToNaiveUtc:

    def test_aware_is_converted_to_utc_then_stripped(self):
        from datetime import timedelta
        tz = timezone(timedelta(hours=2))
        assert to_naive_utc(datetime(2026, 10, 16, 10, 0, tzinfo=tz)) == datetime(2026, 10, 16, 8, 0)

    def test_naive_is_returned_unchanged(self):
        d = datetime(2026, 10, 16, 8, 0)
        assert to_naive_utc(d) == d

    def test_none_stays_none(self):
        assert to_naive_utc(None) is None
