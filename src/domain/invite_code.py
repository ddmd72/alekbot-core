from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


CODE_LENGTH = 26   # 16 random bytes (128 bits) as unpadded base32
GROUP_SIZES = (5, 5, 5, 5, 6)
HINT_LENGTH = 4


def normalise_invite_code(raw: str) -> str:
    """The form every digest is taken over: upper case, no spaces, no dashes (typed input varies in all three)."""
    return "".join(ch for ch in raw.upper() if not ch.isspace() and ch != "-")


def group_invite_code(code: str) -> str:
    """The display form, e.g. XXXXX-XXXXX-XXXXX-XXXXX-XXXXXX; a code of another length is returned normalised."""
    plain = normalise_invite_code(code)
    if len(plain) != sum(GROUP_SIZES):
        return plain
    parts, start = [], 0
    for size in GROUP_SIZES:
        parts.append(plain[start:start + size])
        start += size
    return "-".join(parts)


def invite_hint(code: str) -> str:
    """The non-secret tail of a code (its last 4 normalised characters) that the list shows."""
    return normalise_invite_code(code)[-HINT_LENGTH:]


class InviteType(str, Enum):
    SELF_LINK = "self_link"      # Link platform to YOUR account
    TEAM_INVITE = "team_invite"  # Invite another OAuth user to YOUR account


@dataclass
class InviteCode:
    """
    Represents a secure, time-limited code for linking identities or joining teams.
    """
    user_id: str               # The user who created this code (Owner)
    account_id: str            # The account this code is tied to
    type: InviteType           # SELF_LINK or TEAM_INVITE
    expires_at: datetime       # Expiration timestamp (UTC)
    # The raw code exists in memory only: at creation and on a lookup by the presented value. Never stored.
    code: Optional[str] = None
    hint: Optional[str] = None  # Last 4 characters of the normalised code; the only part a list shows
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
