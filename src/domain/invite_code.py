from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


class InviteType(str, Enum):
    SELF_LINK = "self_link"      # Link platform to YOUR account
    TEAM_INVITE = "team_invite"  # Invite another OAuth user to YOUR account


@dataclass
class InviteCode:
    """
    Represents a secure, time-limited code for linking identities or joining teams.
    """
    code: str                  # Unique identifier (e.g. "ABC-XYZ-123")
    user_id: str               # The user who created this code (Owner)
    account_id: str            # The account this code is tied to
    type: InviteType           # SELF_LINK or TEAM_INVITE
    expires_at: datetime       # Expiration timestamp (UTC)
    
    platform: Optional[str] = None  # Target platform (only for SELF_LINK)
    role: str = "MEMBER"       # Target role (only for TEAM_INVITE)
    
    used_at: Optional[datetime] = None  # When the code was consumed
    used_by_user_id: Optional[str] = None  # Who consumed the code
    
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def is_valid(self) -> bool:
        """Check if code is unused and not expired."""
        now = datetime.now(timezone.utc)
        return self.used_at is None and now < self.expires_at

    def mark_used(self, user_id: str) -> None:
        """Mark the code as used by specific user."""
        if not self.is_valid():
            raise ValueError("Cannot consume invalid or expired code")
        
        self.used_at = datetime.now(timezone.utc)
        self.used_by_user_id = user_id
