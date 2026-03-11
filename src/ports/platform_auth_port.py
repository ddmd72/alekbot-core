"""
PlatformAuthPort — abstract interface for platform-level user authorization.

Separates platform adapters from the concrete IAMService (services layer).
IAMDecision value object lives in src/domain/auth.py (moved 2026-03-08, TD-V2).
"""
from abc import ABC, abstractmethod
from typing import Optional

from src.domain.auth import IAMDecision

__all__ = ["PlatformAuthPort", "IAMDecision"]


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
