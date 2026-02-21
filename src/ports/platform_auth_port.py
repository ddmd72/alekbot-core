"""
PlatformAuthPort — abstract interface for platform-level user authorization.

Separates platform adapters from the concrete IAMService (services layer).

Also defines IAMDecision, the value object returned by authorization checks.
Placing it here allows ports/ and domain/ layers to stay clean of service imports.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..domain.user import UserProfile


@dataclass
class IAMDecision:
    """
    Authorization decision from platform auth checks.

    action: "allow" | "reject" | "create_account"
    user: Resolved UserProfile when action == "allow".
    message: User-facing rejection message when action == "reject".
    metadata: Additional context (e.g., platform_user_id for display).
    """

    action: str
    user: Optional["UserProfile"] = None
    message: Optional[str] = None
    metadata: dict = field(default_factory=dict)


class PlatformAuthPort(ABC):
    """Abstract interface for platform-level user authorization."""

    @abstractmethod
    async def authorize(
        self,
        platform: str,
        platform_user_id: Optional[str] = None,
        email: Optional[str] = None,
    ) -> IAMDecision:
        """
        Make an authorization decision for a platform user.

        Args:
            platform: Platform identifier ("slack", "telegram", "oauth").
            platform_user_id: Platform-native user ID.
            email: Verified email (OAuth flow).

        Returns:
            IAMDecision with action and resolved UserProfile.
        """
