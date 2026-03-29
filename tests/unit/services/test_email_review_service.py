"""
Unit tests for EmailReviewService.

Coverage:
  find_eligible_users()
    - user with gmail_daily_review=True and matching hour → included
    - user with gmail_daily_review=False → excluded
    - user not found in repo → skipped
    - hour mismatch in user timezone → excluded
    - multiple users → only eligible returned

  fetch_review_payload()
    - no credentials → returns None
    - token refresh fails → returns None
    - no emails in period → returns []
    - single page of emails → returns structured list
    - multi-page pagination → fetches until page_token exhausted
    - email missing from full_content → body="" and attachments=[]

  build_alert()
    - contains date string and email count
    - contains JSON dump of emails

  _refresh_if_needed()
    - token not expiring → returns creds unchanged, no refresh call
    - token expiring soon → calls refresh_token + save_credentials
    - token expiry is None → no refresh attempted
    - refresh raises exception → returns None
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.domain.email import EmailFullContent, EmailMetadata, OAuthCredentials
from src.services.email_review_service import EmailReviewService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_service():
    email_provider = MagicMock()
    email_provider.list_emails = AsyncMock()
    email_provider.batch_get_full_content = AsyncMock()
    email_provider.refresh_token = AsyncMock()

    oauth = MagicMock()
    oauth.get_credentials = AsyncMock()
    oauth.list_users_by_provider = AsyncMock()
    oauth.save_credentials = AsyncMock()

    svc = EmailReviewService(email_provider=email_provider, oauth_credentials=oauth)
    return svc, email_provider, oauth


def _make_creds(user_id="user-1", expiring_soon=False, no_expiry=False):
    if no_expiry:
        expiry = None
    elif expiring_soon:
        expiry = datetime.now(timezone.utc) + timedelta(minutes=2)
    else:
        expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    return OAuthCredentials(
        user_id=user_id,
        provider="gmail",
        access_token="tok",
        refresh_token="rtok",
        token_expiry=expiry,
        scopes=[],
        email_address="user@example.com",
    )


def _make_metadata(email_id="e1"):
    return EmailMetadata(
        email_id=email_id,
        provider="gmail",
        subject="Test Subject",
        from_address="sender@example.com",
        date=datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc),
        labels=[],
        snippet="First 200 chars...",
    )


def _make_full_content(email_id="e1", body_text="Body text", attachments=None):
    return EmailFullContent(
        email_id=email_id,
        body_text=body_text,
        body_html=None,
        attachments=attachments or [],
        attachment_binaries={},
    )


def _make_profile(*, gmail_daily_review=True, hour=10, timezone_str="UTC", account_id="acc-1"):
    cfg = MagicMock()
    cfg.gmail_daily_review = gmail_daily_review
    cfg.gmail_daily_review_hour = hour
    cfg.timezone = timezone_str
    profile = MagicMock()
    profile.config = cfg
    profile.account_id = account_id
    return profile


# ---------------------------------------------------------------------------
# find_eligible_users
# ---------------------------------------------------------------------------

class TestFindEligibleUsers:

    async def test_eligible_user_included(self):
        svc, _, oauth = _make_service()
        now_utc = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)  # 10:00 UTC
        oauth.list_users_by_provider = AsyncMock(return_value=["user-1"])
        user_repo = MagicMock()
        user_repo.get_user = AsyncMock(return_value=_make_profile(hour=10))

        result = await svc.find_eligible_users(user_repo, now_utc)

        assert ("user-1", "acc-1") in result

    async def test_gmail_daily_review_false_excluded(self):
        svc, _, oauth = _make_service()
        now_utc = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
        oauth.list_users_by_provider = AsyncMock(return_value=["user-1"])
        user_repo = MagicMock()
        user_repo.get_user = AsyncMock(
            return_value=_make_profile(gmail_daily_review=False, hour=10)
        )

        result = await svc.find_eligible_users(user_repo, now_utc)

        assert result == []

    async def test_user_not_found_skipped(self):
        svc, _, oauth = _make_service()
        now_utc = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
        oauth.list_users_by_provider = AsyncMock(return_value=["user-ghost"])
        user_repo = MagicMock()
        user_repo.get_user = AsyncMock(return_value=None)

        result = await svc.find_eligible_users(user_repo, now_utc)

        assert result == []

    async def test_hour_mismatch_excluded(self):
        svc, _, oauth = _make_service()
        now_utc = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)  # 10:00 UTC
        oauth.list_users_by_provider = AsyncMock(return_value=["user-1"])
        user_repo = MagicMock()
        user_repo.get_user = AsyncMock(
            return_value=_make_profile(hour=9)  # user wants 09:00, now is 10:00
        )

        result = await svc.find_eligible_users(user_repo, now_utc)

        assert result == []

    async def test_multiple_users_only_eligible_returned(self):
        svc, _, oauth = _make_service()
        now_utc = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
        oauth.list_users_by_provider = AsyncMock(return_value=["u1", "u2", "u3"])

        profiles = {
            "u1": _make_profile(hour=10, account_id="acc-1"),
            "u2": _make_profile(gmail_daily_review=False, hour=10, account_id="acc-2"),
            "u3": _make_profile(hour=10, account_id="acc-3"),
        }
        user_repo = MagicMock()
        user_repo.get_user = AsyncMock(side_effect=lambda uid: profiles.get(uid))

        result = await svc.find_eligible_users(user_repo, now_utc)

        assert ("u1", "acc-1") in result
        assert ("u3", "acc-3") in result
        assert len(result) == 2


# ---------------------------------------------------------------------------
# fetch_review_payload
# ---------------------------------------------------------------------------

class TestFetchReviewPayload:

    async def test_no_credentials_returns_none(self):
        svc, _, oauth = _make_service()
        oauth.get_credentials = AsyncMock(return_value=None)

        result = await svc.fetch_review_payload("user-1")

        assert result is None

    async def test_token_refresh_fails_returns_none(self):
        svc, email_provider, oauth = _make_service()
        creds = _make_creds(expiring_soon=True)
        oauth.get_credentials = AsyncMock(return_value=creds)
        email_provider.refresh_token = AsyncMock(side_effect=RuntimeError("revoked"))

        result = await svc.fetch_review_payload("user-1")

        assert result is None

    async def test_no_emails_returns_empty_list(self):
        svc, email_provider, oauth = _make_service()
        creds = _make_creds()
        oauth.get_credentials = AsyncMock(return_value=creds)
        email_provider.list_emails = AsyncMock(return_value=([], None))

        result = await svc.fetch_review_payload("user-1")

        assert result == []

    async def test_single_page_returns_structured_list(self):
        svc, email_provider, oauth = _make_service()
        creds = _make_creds()
        oauth.get_credentials = AsyncMock(return_value=creds)
        meta = _make_metadata("e1")
        email_provider.list_emails = AsyncMock(return_value=([meta], None))
        fc = _make_full_content("e1", body_text="Full body", attachments=["doc.pdf"])
        email_provider.batch_get_full_content = AsyncMock(return_value={"e1": fc})

        result = await svc.fetch_review_payload("user-1")

        assert len(result) == 1
        assert result[0]["email_id"] == "e1"
        assert result[0]["from"] == "sender@example.com"
        assert result[0]["body"] == "Full body"
        assert result[0]["attachments"] == ["doc.pdf"]

    async def test_pagination_fetches_all_pages(self):
        svc, email_provider, oauth = _make_service()
        creds = _make_creds()
        oauth.get_credentials = AsyncMock(return_value=creds)

        meta1 = _make_metadata("e1")
        meta2 = _make_metadata("e2")

        call_count = 0
        async def paginated_list(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ([meta1], "page2-token")
            return ([meta2], None)

        email_provider.list_emails = AsyncMock(side_effect=paginated_list)
        email_provider.batch_get_full_content = AsyncMock(return_value={})

        result = await svc.fetch_review_payload("user-1")

        assert call_count == 2
        assert len(result) == 2

    async def test_email_missing_from_full_content_has_empty_body(self):
        svc, email_provider, oauth = _make_service()
        creds = _make_creds()
        oauth.get_credentials = AsyncMock(return_value=creds)
        meta = _make_metadata("e1")
        email_provider.list_emails = AsyncMock(return_value=([meta], None))
        email_provider.batch_get_full_content = AsyncMock(return_value={})  # e1 absent

        result = await svc.fetch_review_payload("user-1")

        assert result[0]["body"] == ""
        assert result[0]["attachments"] == []


# ---------------------------------------------------------------------------
# build_alert
# ---------------------------------------------------------------------------

class TestBuildAlert:

    def test_contains_date_and_email_count(self):
        emails = [{"email_id": "e1", "subject": "Hello"}]
        result = EmailReviewService.build_alert("2026-01-15", emails)
        assert "2026-01-15" in result
        assert "1 emails" in result

    def test_contains_json_of_emails(self):
        emails = [{"email_id": "e42", "subject": "Test"}]
        result = EmailReviewService.build_alert("2026-01-15", emails)
        assert "e42" in result
        assert "Test" in result

    def test_contains_phase_headers(self):
        result = EmailReviewService.build_alert("2026-01-15", [])
        assert "PHASE 0" in result
        assert "PHASE 1" in result
        assert "PHASE 2" in result


# ---------------------------------------------------------------------------
# _refresh_if_needed
# ---------------------------------------------------------------------------

class TestRefreshIfNeeded:

    async def test_token_not_expiring_no_refresh(self):
        svc, email_provider, _ = _make_service()
        creds = _make_creds(expiring_soon=False)

        result = await svc._refresh_if_needed(creds)

        assert result is creds
        email_provider.refresh_token.assert_not_called()

    async def test_token_expiring_soon_refreshed_and_saved(self):
        svc, email_provider, oauth = _make_service()
        creds = _make_creds(expiring_soon=True)
        new_creds = _make_creds(user_id="user-1")
        email_provider.refresh_token = AsyncMock(return_value=new_creds)

        result = await svc._refresh_if_needed(creds)

        assert result is new_creds
        email_provider.refresh_token.assert_called_once_with(creds)
        oauth.save_credentials.assert_called_once_with(new_creds)

    async def test_no_token_expiry_no_refresh(self):
        svc, email_provider, _ = _make_service()
        creds = _make_creds(no_expiry=True)

        result = await svc._refresh_if_needed(creds)

        assert result is creds
        email_provider.refresh_token.assert_not_called()

    async def test_refresh_exception_returns_none(self):
        svc, email_provider, _ = _make_service()
        creds = _make_creds(expiring_soon=True)
        email_provider.refresh_token = AsyncMock(side_effect=Exception("network error"))

        result = await svc._refresh_if_needed(creds)

        assert result is None
