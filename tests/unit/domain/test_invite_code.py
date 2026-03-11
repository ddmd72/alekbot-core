import pytest
from datetime import datetime, timedelta, timezone
from src.domain.invite_code import InviteCode, InviteType


class TestInviteCode:
    def test_create_invite_code(self):
        """Test creating a valid InviteCode."""
        code = "ABC-123"
        user_id = "user-1"
        account_id = "acc-1"
        expires_at = datetime.now(timezone.utc) + timedelta(days=7)
        
        invite = InviteCode(
            code=code,
            user_id=user_id,
            account_id=account_id,
            type=InviteType.SELF_LINK,
            expires_at=expires_at,
            platform="slack"
        )
        
        assert invite.code == code
        assert invite.user_id == user_id
        assert invite.type == InviteType.SELF_LINK
        assert invite.is_valid() is True
        assert invite.used_at is None

    def test_invite_code_expiration(self):
        """Test that expired codes are invalid."""
        expired_time = datetime.now(timezone.utc) - timedelta(hours=1)
        invite = InviteCode(
            code="EXPIRED",
            user_id="user-1",
            account_id="acc-1",
            type=InviteType.TEAM_INVITE,
            expires_at=expired_time
        )
        
        assert invite.is_valid() is False

    def test_mark_used(self):
        """Test marking code as used."""
        expires_at = datetime.now(timezone.utc) + timedelta(days=1)
        invite = InviteCode(
            code="VALID",
            user_id="user-1",
            account_id="acc-1",
            type=InviteType.TEAM_INVITE,
            expires_at=expires_at
        )
        
        consumer_id = "new-user-1"
        invite.mark_used(consumer_id)
        
        assert invite.used_at is not None
        assert invite.used_by_user_id == consumer_id
        assert invite.is_valid() is False

    def test_cannot_consume_expired_code(self):
        """Test that consuming an expired code raises ValueError."""
        expired_time = datetime.now(timezone.utc) - timedelta(hours=1)
        invite = InviteCode(
            code="EXPIRED",
            user_id="user-1",
            account_id="acc-1",
            type=InviteType.TEAM_INVITE,
            expires_at=expired_time
        )
        
        with pytest.raises(ValueError, match="Cannot consume invalid or expired code"):
            invite.mark_used("user-2")
            
    def test_cannot_consume_used_code(self):
        """Test that consuming an already used code raises ValueError."""
        expires_at = datetime.now(timezone.utc) + timedelta(days=1)
        invite = InviteCode(
            code="USED",
            user_id="user-1",
            account_id="acc-1",
            type=InviteType.TEAM_INVITE,
            expires_at=expires_at
        )
        
        invite.mark_used("user-2")
        
        with pytest.raises(ValueError, match="Cannot consume invalid or expired code"):
            invite.mark_used("user-3")
