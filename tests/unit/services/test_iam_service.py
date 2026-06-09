"""
Unit tests for IAMService.

Tests authorization logic and message generation.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.services.iam_service import IAMService, IAMDecision
from src.domain.user import UserProfile
from src.domain.whitelist import WhitelistEntry


class TestIAMServiceMessageGeneration:
    """Test IAMService message generation (centralized)."""

    @pytest.fixture
    def iam_service(self):
        """Create IAMService with mock repositories."""
        return IAMService(
            user_repo=AsyncMock(),
            account_repo=AsyncMock(),
            whitelist_repo=AsyncMock()
        )

    def test_get_rejection_message_telegram_not_registered(self, iam_service):
        """Test Telegram rejection message includes platform ID."""
        message = iam_service.get_rejection_message(
            platform="telegram",
            platform_user_id="670659908",
            reason="not_registered"
        )

        # English onboarding (pre-auth system default)
        assert "Hi!" in message
        assert "Open" in message
        assert iam_service.CABINET_URL in message
        assert "670659908" in message  # Shows user ID
        assert "come back" in message

    def test_get_rejection_message_slack_not_registered(self, iam_service):
        """Test Slack rejection message in English."""
        message = iam_service.get_rejection_message(
            platform="slack",
            reason="not_registered"
        )

        # Should be in English
        assert "Hi" in message
        assert "Open" in message
        assert iam_service.CABINET_URL in message
        assert "come back" in message

    def test_get_rejection_message_revoked(self, iam_service):
        """Test revoked access message (same for all platforms)."""
        telegram_msg = iam_service.get_rejection_message(
            platform="telegram",
            reason="revoked"
        )
        
        slack_msg = iam_service.get_rejection_message(
            platform="slack",
            reason="revoked"
        )

        # Should be the same for both platforms
        assert telegram_msg == slack_msg
        assert "revoked" in telegram_msg.lower()
        assert "administrator" in telegram_msg.lower()

    def test_get_rejection_message_unknown_platform_fallback(self, iam_service):
        """Test fallback message for unknown platforms."""
        message = iam_service.get_rejection_message(
            platform="whatsapp",  # Not implemented
            reason="not_registered"
        )

        # Should return generic fallback
        assert "Account not found" in message
        assert iam_service.CABINET_URL in message

    def test_get_rejection_message_unknown_reason_fallback(self, iam_service):
        """Test fallback for unknown rejection reasons."""
        message = iam_service.get_rejection_message(
            platform="telegram",
            reason="unknown_reason"
        )

        # Should return generic error
        assert "Authorization failed" in message

    def test_telegram_message_without_user_id(self, iam_service):
        """Test Telegram message when platform_user_id not provided."""
        message = iam_service.get_rejection_message(
            platform="telegram",
            platform_user_id=None,
            reason="not_registered"
        )

        # Should still work, just without ID
        assert "Hi!" in message
        assert iam_service.CABINET_URL in message
        # Should NOT have ID placeholder
        assert "(ID:" not in message


class TestIAMServiceAuthorization:
    """Test IAMService authorization logic."""

    @pytest.fixture
    def mock_repos(self):
        """Create mock repositories."""
        return {
            'user_repo': AsyncMock(),
            'account_repo': AsyncMock(),
            'whitelist_repo': AsyncMock()
        }

    @pytest.fixture
    def iam_service(self, mock_repos):
        """Create IAMService with mocks."""
        return IAMService(**mock_repos)

    @pytest.mark.asyncio
    async def test_authorize_existing_platform_user(self, iam_service, mock_repos):
        """Test authorization for existing platform user."""
        # Mock user exists
        mock_user = UserProfile(
            user_id="user_123",
            email="test@example.com",
            account_id="account_456"
        )
        mock_repos['user_repo'].get_user_by_platform_id.return_value = mock_user
        
        # Mock whitelist allows
        mock_whitelist = WhitelistEntry(
            allowed_emails={"test@example.com"},
            allowed_domains=set()
        )
        mock_repos['whitelist_repo'].get_whitelist.return_value = mock_whitelist

        decision = await iam_service.authorize(
            platform="telegram",
            platform_user_id="670659908"
        )

        assert decision.action == "allow"
        assert decision.user.user_id == "user_123"
        assert decision.user.email == "test@example.com"

    @pytest.mark.asyncio
    async def test_authorize_unknown_platform_user_rejected(self, iam_service, mock_repos):
        """Test unknown platform user gets rejection with message."""
        # Mock user NOT found
        mock_repos['user_repo'].get_user_by_platform_id.return_value = None

        decision = await iam_service.authorize(
            platform="telegram",
            platform_user_id="999999999"
        )

        assert decision.action == "reject"
        assert decision.message is not None
        assert "Hi!" in decision.message  # English onboarding (pre-auth default)
        assert decision.metadata.get("platform_user_id") == "999999999"

    @pytest.mark.asyncio
    async def test_authorize_revoked_user(self, iam_service, mock_repos):
        """Test user with revoked whitelist access."""
        # Mock user exists
        mock_user = UserProfile(
            user_id="user_789",
            email="revoked@example.com",
            account_id="account_999"
        )
        mock_repos['user_repo'].get_user_by_platform_id.return_value = mock_user
        
        # Mock whitelist DOES NOT allow
        mock_whitelist = WhitelistEntry(
            allowed_emails={"other@example.com"},
            allowed_domains=set()
        )
        mock_repos['whitelist_repo'].get_whitelist.return_value = mock_whitelist

        decision = await iam_service.authorize(
            platform="telegram",
            platform_user_id="670659908"
        )

        assert decision.action == "reject"
        assert "revoked" in decision.message.lower()


class TestIAMServiceAuthorizationExtended:
    """Tests for previously uncovered authorize() branches and _create_new_user()."""

    @pytest.fixture
    def repos(self):
        return {
            "user_repo": AsyncMock(),
            "account_repo": AsyncMock(),
            "whitelist_repo": AsyncMock(),
        }

    @pytest.fixture
    def svc(self, repos):
        return IAMService(**repos)

    def _whitelist(self, *, allowed=True):
        from src.domain.whitelist import WhitelistEntry
        if allowed:
            return WhitelistEntry(allowed_emails={"user@example.com"}, allowed_domains={"example.com"})
        return WhitelistEntry(allowed_emails=set(), allowed_domains=set())

    # ------------------------------------------------------------------
    # Line 193: user found but email is empty → ValueError
    # ------------------------------------------------------------------

    async def test_platform_user_with_no_email_raises(self, svc, repos):
        corrupt_user = UserProfile(user_id="u1", email="", account_id="acc-1")
        repos["user_repo"].get_user_by_platform_id = AsyncMock(return_value=corrupt_user)

        with pytest.raises(ValueError, match="DATA CORRUPTION"):
            await svc.authorize(platform="telegram", platform_user_id="123")

    # ------------------------------------------------------------------
    # Lines 240-276: OAuth branch
    # ------------------------------------------------------------------

    async def test_oauth_existing_user_allowed(self, svc, repos):
        existing = UserProfile(user_id="u2", email="user@example.com", account_id="acc-2")
        repos["user_repo"].get_user_by_email = AsyncMock(return_value=existing)

        decision = await svc.authorize(platform="oauth", email="user@example.com")

        assert decision.action == "allow"
        assert decision.user.user_id == "u2"

    async def test_oauth_new_user_not_in_whitelist_rejected(self, svc, repos):
        repos["user_repo"].get_user_by_email = AsyncMock(return_value=None)
        repos["whitelist_repo"].get_whitelist = AsyncMock(return_value=self._whitelist(allowed=False))

        decision = await svc.authorize(platform="oauth", email="outsider@spam.com")

        assert decision.action == "reject"
        assert "authorized" in decision.message.lower() or "not" in decision.message.lower()

    async def test_oauth_new_user_in_whitelist_creates_account(self, svc, repos):
        repos["user_repo"].get_user_by_email = AsyncMock(return_value=None)
        repos["whitelist_repo"].get_whitelist = AsyncMock(return_value=self._whitelist(allowed=True))
        new_user = UserProfile(user_id="u-new", email="user@example.com", account_id="acc-new")
        repos["user_repo"].create_user = AsyncMock(return_value=new_user)
        repos["account_repo"].create_account = AsyncMock()

        decision = await svc.authorize(platform="oauth", email="user@example.com")

        assert decision.action == "create_account"
        assert decision.user.user_id == "u-new"

    # ------------------------------------------------------------------
    # Default branch: invalid parameters → reject
    # ------------------------------------------------------------------

    async def test_invalid_params_default_reject(self, svc, repos):
        decision = await svc.authorize(platform="oauth")  # no email, no platform_user_id

        assert decision.action == "reject"
        assert "Invalid parameters" in decision.message

    # ------------------------------------------------------------------
    # Lines 296-320: _create_new_user
    # ------------------------------------------------------------------

    async def test_create_new_user_creates_account_and_user(self, svc, repos):
        created = UserProfile(user_id="u-fresh", email="new@example.com", account_id="acc-fresh")
        repos["user_repo"].create_user = AsyncMock(return_value=created)
        repos["account_repo"].create_account = AsyncMock()

        result = await svc._create_new_user("new@example.com")

        repos["account_repo"].create_account.assert_called_once()
        repos["user_repo"].create_user.assert_called_once()
        assert result.user_id == "u-fresh"
