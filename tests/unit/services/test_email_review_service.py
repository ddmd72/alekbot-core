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
    - token expiring soon → calls refresh_token + update_credentials
    - token expiry is None → no refresh attempted
    - refresh raises exception → returns None
"""
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.domain.daily_review_window import EligibleReviewUser
from src.domain.email import EmailFullContent, EmailMetadata
from src.domain.integration import Integration
from src.domain.oauth_credential import OAuthCredential
from src.domain.provider_account import ProviderAccount
from src.services.email_review_service import EmailReviewService
from src.composition.connector_registry import ConnectorRegistry
from tests.support.oauth_credentials_store import mirror_get


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_service():
    email_provider = MagicMock()
    email_provider.list_emails = AsyncMock()
    email_provider.batch_get_full_content = AsyncMock()
    email_provider.refresh_token = AsyncMock()

    oauth = MagicMock()
    oauth.get = AsyncMock()
    mirror_get(oauth)
    oauth.list_user_ids = AsyncMock()
    oauth.save = AsyncMock()
    oauth.update = AsyncMock(return_value=True)   # the record exists: a refresh replaces it

    svc = EmailReviewService(email_provider=email_provider, oauth_credentials=oauth, connector=ConnectorRegistry().get("gmail"))
    return svc, email_provider, oauth


def _make_creds(user_id="user-1", expiring_soon=False, no_expiry=False):
    if no_expiry:
        expiry = None
    elif expiring_soon:
        expiry = datetime.now(timezone.utc) + timedelta(minutes=2)
    else:
        expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    return OAuthCredential(
        user_id=user_id,
        account_id="acct-1",
        kind="gmail",
        integration=Integration.MAIL,
        account=ProviderAccount(realm="google", subject="", label="user@example.com"),
        access_token="tok",
        refresh_token="rtok",
        token_expiry=expiry,
        scopes=[],
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
        oauth.list_user_ids = AsyncMock(return_value=["user-1"])
        user_repo = MagicMock()
        user_repo.get_user = AsyncMock(return_value=_make_profile(hour=10))

        result = await svc.find_eligible_users(user_repo, now_utc)

        assert result == [EligibleReviewUser("user-1", "acc-1", date(2026, 1, 15))]

    async def test_gmail_daily_review_false_excluded(self):
        svc, _, oauth = _make_service()
        now_utc = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
        oauth.list_user_ids = AsyncMock(return_value=["user-1"])
        user_repo = MagicMock()
        user_repo.get_user = AsyncMock(
            return_value=_make_profile(gmail_daily_review=False, hour=10)
        )

        result = await svc.find_eligible_users(user_repo, now_utc)

        assert result == []

    async def test_user_not_found_skipped(self):
        svc, _, oauth = _make_service()
        now_utc = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
        oauth.list_user_ids = AsyncMock(return_value=["user-ghost"])
        user_repo = MagicMock()
        user_repo.get_user = AsyncMock(return_value=None)

        result = await svc.find_eligible_users(user_repo, now_utc)

        assert result == []

    @pytest.mark.parametrize(
        "now_utc",
        [
            datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),  # 3 h after 09:00: window closed
            datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc),  # 1 h before 09:00: not open yet
        ],
    )
    async def test_hour_mismatch_excluded(self, now_utc):
        svc, _, oauth = _make_service()
        oauth.list_user_ids = AsyncMock(return_value=["user-1"])
        user_repo = MagicMock()
        user_repo.get_user = AsyncMock(
            return_value=_make_profile(hour=9)  # user wants 09:00, now is outside its 3 h window
        )

        result = await svc.find_eligible_users(user_repo, now_utc)

        assert result == []

    async def test_multiple_users_only_eligible_returned(self):
        svc, _, oauth = _make_service()
        now_utc = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
        oauth.list_user_ids = AsyncMock(return_value=["u1", "u2", "u3"])

        profiles = {
            "u1": _make_profile(hour=10, account_id="acc-1"),
            "u2": _make_profile(gmail_daily_review=False, hour=10, account_id="acc-2"),
            "u3": _make_profile(hour=10, account_id="acc-3"),
        }
        user_repo = MagicMock()
        user_repo.get_user = AsyncMock(side_effect=lambda uid: profiles.get(uid))

        result = await svc.find_eligible_users(user_repo, now_utc)

        assert result == [
            EligibleReviewUser("u1", "acc-1", date(2026, 1, 15)),
            EligibleReviewUser("u3", "acc-3", date(2026, 1, 15)),
        ]


# ---------------------------------------------------------------------------
# fetch_review_payload
# ---------------------------------------------------------------------------

class TestFetchReviewPayload:

    async def test_no_credentials_returns_none(self):
        svc, _, oauth = _make_service()
        oauth.get = AsyncMock(return_value=None)
        mirror_get(oauth)

        result = await svc.fetch_review_payload("user-1")

        assert result is None

    async def test_token_refresh_fails_returns_none(self):
        svc, email_provider, oauth = _make_service()
        creds = _make_creds(expiring_soon=True)
        oauth.get = AsyncMock(return_value=creds)
        mirror_get(oauth)
        email_provider.refresh_token = AsyncMock(side_effect=RuntimeError("revoked"))

        result = await svc.fetch_review_payload("user-1")

        assert result is None

    async def test_no_emails_returns_empty_list(self):
        svc, email_provider, oauth = _make_service()
        creds = _make_creds()
        oauth.get = AsyncMock(return_value=creds)
        mirror_get(oauth)
        email_provider.list_emails = AsyncMock(return_value=([], None))

        result = await svc.fetch_review_payload("user-1")

        assert result == []

    async def test_single_page_returns_structured_list(self):
        svc, email_provider, oauth = _make_service()
        creds = _make_creds()
        oauth.get = AsyncMock(return_value=creds)
        mirror_get(oauth)
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
        oauth.get = AsyncMock(return_value=creds)
        mirror_get(oauth)

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
        oauth.get = AsyncMock(return_value=creds)
        mirror_get(oauth)
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
        result = EmailReviewService.build_alert("2026-01-15", 1)
        assert "2026-01-15" in result
        assert "1 emails" in result

    def test_contains_key_instructions(self):
        result = EmailReviewService.build_alert("2026-01-15", 5)
        assert "ACTION" in result
        assert "search_memory" in result
        assert "get_email_attachment" in result
        assert "HTML page creation" in result


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
        oauth.update.assert_called_once_with(new_creds)
        oauth.save.assert_not_called()                # a refresh never creates a record

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


class TestFindEligibleUsersWindow:
    """Task D: the review window is the first 3 hours from the configured hour."""

    async def test_returns_slot_date_inside_window(self):
        from datetime import date
        svc, _, oauth = _make_service()
        now_utc = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
        oauth.list_user_ids = AsyncMock(return_value=["user-1"])
        user_repo = MagicMock()
        user_repo.get_user = AsyncMock(return_value=_make_profile(hour=9))

        result = await svc.find_eligible_users(user_repo, now_utc)

        assert [(u.user_id, u.account_id, u.slot_date) for u in result] == [
            ("user-1", "acc-1", date(2026, 1, 15))
        ]

    async def test_excluded_outside_window(self):
        svc, _, oauth = _make_service()
        now_utc = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        oauth.list_user_ids = AsyncMock(return_value=["user-1"])
        user_repo = MagicMock()
        user_repo.get_user = AsyncMock(return_value=_make_profile(hour=9))

        assert await svc.find_eligible_users(user_repo, now_utc) == []


class TestFindEligibleUsersBadConfig:

    async def _run(self, profiles):
        svc, _, oauth = _make_service()
        oauth.list_user_ids = AsyncMock(return_value=list(profiles))
        user_repo = MagicMock()
        user_repo.get_user = AsyncMock(side_effect=lambda uid: profiles.get(uid))
        now_utc = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
        return await svc.find_eligible_users(user_repo, now_utc)

    async def test_invalid_timezone_falls_back_to_utc_and_does_not_abort(self, caplog):
        bad = _make_profile(hour=10, account_id="acc-bad")
        bad.config.timezone = "Not/AZone"
        good = _make_profile(hour=10, account_id="acc-good")
        import logging
        with caplog.at_level(logging.WARNING):
            result = await self._run({"bad": bad, "good": good})
        assert {u.user_id for u in result} == {"bad", "good"}
        assert any("invalid timezone" in r.getMessage() for r in caplog.records)

    async def test_malformed_timezone_value_error_falls_back(self):
        bad = _make_profile(hour=10, account_id="acc-bad")
        bad.config.timezone = "../etc"
        result = await self._run({"bad": bad})
        assert [u.user_id for u in result] == ["bad"]

    async def test_hour_out_of_range_skipped_with_warning(self, caplog):
        import logging
        bad = _make_profile(hour=24, account_id="acc-bad")
        good = _make_profile(hour=10, account_id="acc-good")
        with caplog.at_level(logging.WARNING):
            result = await self._run({"bad": bad, "good": good})
        assert [u.user_id for u in result] == ["good"]
        assert sum("invalid review hour" in r.getMessage() for r in caplog.records) == 1
