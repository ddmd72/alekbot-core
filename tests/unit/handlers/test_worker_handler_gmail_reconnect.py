"""
WorkerHandler — Gmail grant revoked (refresh token rejected with invalid_grant).

Incident 2026-10-09: Google revoked the owner's Gmail grant; the daily email
review and the auto-index failed every run with only a WARNING log, and the
Cabinet kept showing "Connected". Contract guarded here:
  - both Gmail background paths (daily review, indexing) mark the stored
    credentials needs_reconnect and notify the user;
  - the user is notified once per breakage, not on every failed run;
  - the task ends 200 (a Cloud Tasks retry cannot fix a revoked grant).
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from src.domain.email import IndexingJob, OAuthCredentials
from src.domain.exceptions import OAuthGrantRevokedError
from src.domain.notification_kind import NotificationKind
from src.handlers.worker_handler import WorkerHandler
from src.ports.oauth_credentials_port import OAuthCredentialsPort
from src.services.email_indexing_service import EmailIndexingService
from src.services.email_review_service import EmailReviewService

_USER = "user-revoked"
_ACC = "acc-revoked"
_NOW = datetime(2026, 10, 10, 11, 12, tzinfo=timezone.utc)
_REVOKED = OAuthGrantRevokedError(
    "Gmail token refresh failed: invalid_grant — Token has been expired or revoked."
)


def _creds(needs_reconnect: bool = False) -> OAuthCredentials:
    return OAuthCredentials(
        user_id=_USER,
        provider="gmail",
        access_token="tok",
        refresh_token="rtok",
        token_expiry=_NOW,
        scopes=[],
        email_address="user@example.com",
        needs_reconnect=needs_reconnect,
    )


def _job() -> IndexingJob:
    return IndexingJob(
        job_id="job-0000-1111",
        user_id=_USER,
        account_id=_ACC,
        provider="gmail",
        triggered_by="scheduler",
        status="running",
        started_at=_NOW,
        updated_at=_NOW,
    )


def _worker(*, stored_creds, oauth_configured: bool = True):
    email_indexing = MagicMock(spec=EmailIndexingService)
    email_indexing.load_job_for_execution = AsyncMock(
        return_value=(_job(), _creds(), None)
    )
    email_indexing.run_indexing_job = AsyncMock(side_effect=_REVOKED)

    email_review = MagicMock(spec=EmailReviewService)
    email_review.fetch_review_payload = AsyncMock(side_effect=_REVOKED)

    oauth = AsyncMock(spec=OAuthCredentialsPort)
    oauth.get_credentials.return_value = stored_creds

    notification = AsyncMock()
    notification.notify = AsyncMock()

    worker = WorkerHandler(
        agent_worker_handler=MagicMock(),
        email_indexing_service=email_indexing,
        notification_service=notification,
        consolidation_service=None,
        coordinator=MagicMock(),
        agent_factory=MagicMock(),
        indexed_email_repo=None,
        user_repo=MagicMock(),
        task_dispatch=AsyncMock(),
        email_review=email_review,
        oauth_credentials=oauth if oauth_configured else None,
    )
    return worker, oauth, notification


class TestDailyReviewGrantRevoked:

    async def test_marks_needs_reconnect_and_notifies(self):
        worker, oauth, notification = _worker(stored_creds=_creds())

        result, status = await worker._handle_daily_email_review(
            {"user_id": _USER, "account_id": _ACC}
        )

        assert status == 200
        assert result == {"error": "gmail_reconnect_required"}
        saved = oauth.save_credentials.await_args.args[0]
        assert saved.needs_reconnect is True
        assert saved.refresh_token == "rtok"  # the rest of the record is kept
        notification.notify.assert_awaited_once()
        kw = notification.notify.await_args.kwargs
        assert kw["user_id"] == _USER
        assert kw["account_id"] == _ACC
        assert kw["kind"] == NotificationKind.DOCUMENT_DELIVERY
        assert "Reconnect" in kw["system_alert"]

    async def test_already_flagged_does_not_notify_again(self):
        worker, oauth, notification = _worker(stored_creds=_creds(needs_reconnect=True))

        result, status = await worker._handle_daily_email_review(
            {"user_id": _USER, "account_id": _ACC}
        )

        assert status == 200
        oauth.save_credentials.assert_not_awaited()
        notification.notify.assert_not_awaited()

    async def test_credentials_gone_does_nothing(self):
        worker, oauth, notification = _worker(stored_creds=None)

        _, status = await worker._handle_daily_email_review(
            {"user_id": _USER, "account_id": _ACC}
        )

        assert status == 200
        oauth.save_credentials.assert_not_awaited()
        notification.notify.assert_not_awaited()

    async def test_notify_failure_is_not_raised(self):
        worker, oauth, notification = _worker(stored_creds=_creds())
        notification.notify.side_effect = RuntimeError("channel down")

        result, status = await worker._handle_daily_email_review(
            {"user_id": _USER, "account_id": _ACC}
        )

        assert status == 200
        assert result == {"error": "gmail_reconnect_required"}

    async def test_without_oauth_port_still_returns_200(self):
        worker, _, notification = _worker(stored_creds=_creds(), oauth_configured=False)

        _, status = await worker._handle_daily_email_review(
            {"user_id": _USER, "account_id": _ACC}
        )

        assert status == 200
        notification.notify.assert_not_awaited()


class TestEmailIndexingGrantRevoked:

    async def test_marks_needs_reconnect_and_notifies(self):
        worker, oauth, notification = _worker(stored_creds=_creds())

        result, status = await worker._handle_email_indexing({"job_id": "job-0000-1111"})

        assert status == 200
        assert result == {"status": "failed", "error": "gmail_reconnect_required"}
        assert oauth.save_credentials.await_args.args[0].needs_reconnect is True
        notification.notify.assert_awaited_once()
        assert notification.notify.await_args.kwargs["account_id"] == _ACC

    async def test_already_flagged_does_not_notify_again(self):
        worker, oauth, notification = _worker(stored_creds=_creds(needs_reconnect=True))

        _, status = await worker._handle_email_indexing({"job_id": "job-0000-1111"})

        assert status == 200
        notification.notify.assert_not_awaited()

    async def test_other_refresh_errors_keep_old_path(self):
        worker, oauth, notification = _worker(stored_creds=_creds())
        worker._email_indexing.run_indexing_job.side_effect = ValueError(
            "Gmail token refresh failed: temporarily_unavailable — "
        )

        result, status = await worker._handle_email_indexing({"job_id": "job-0000-1111"})

        assert status == 200
        assert result["status"] == "failed"
        assert result["error"] != "gmail_reconnect_required"
        oauth.save_credentials.assert_not_awaited()
        notification.notify.assert_not_awaited()
